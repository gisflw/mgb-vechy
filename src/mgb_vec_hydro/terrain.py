"""Drainage-rooted terrain routing and raster products.

The routing used here is deliberately not D8: topology is established first by
an eight-neighbour breadth-first search from the supplied drainage, and the DEM
only chooses a deterministic parent among neighbours one rank closer to it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import os
from pathlib import Path
import tempfile

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from rasterio.features import rasterize
from rasterio.windows import Window, from_bounds
from shapely.geometry.base import BaseGeometry

from mgb_vec_hydro.exceptions import TerrainProductsError


# Code, row delta, column delta. This order is also the final tie-break.
_DIRECTIONS = (
    (1, -1, 0), (2, -1, 1), (3, 0, 1), (4, 1, 1),
    (5, 1, 0), (6, 1, -1), (7, 0, -1), (8, -1, -1),
)
_DELTAS = {code: (dr, dc) for code, dr, dc in _DIRECTIONS}


@dataclass(frozen=True)
class TerrainProductPaths:
    hand: Path
    ltnd: Path
    flow_direction: Path | None


@dataclass(frozen=True)
class TerrainProductReport:
    paths: TerrainProductPaths
    owned_cells: int
    drainage_cells: int
    unreachable_components: int
    negative_hand_cells: int
    negative_hand_min: float | None
    negative_hand_max: float | None


def _pixel_sizes(transform: Affine) -> tuple[float, float]:
    if not isinstance(transform, Affine):
        transform = Affine(*transform)
    if transform.b != 0 or transform.d != 0:
        raise TerrainProductsError("A north-up, unrotated raster transform is required")
    width, height = abs(transform.a), abs(transform.e)
    if width == 0 or height == 0:
        raise TerrainProductsError("DEM pixel dimensions must be non-zero")
    return width, height


def _validate_arrays(
    elevation: np.ndarray,
    labels: np.ndarray,
    drainage: np.ndarray,
) -> None:
    if elevation.ndim != 2:
        raise TerrainProductsError("Elevation must be a two-dimensional array")
    if labels.shape != elevation.shape or drainage.shape != elevation.shape:
        raise TerrainProductsError(
            "Elevation, catchment labels, and drainage mask must have equal shapes"
        )
    if not np.issubdtype(labels.dtype, np.integer):
        raise TerrainProductsError("Catchment labels must be an integer array")


def compute_flow_directions(
    elevation: np.ndarray,
    catchment_labels: np.ndarray,
    drainage_mask: np.ndarray,
    transform: Affine,
) -> tuple[np.ndarray, np.ndarray]:
    """Return deterministic drainage-rooted parent directions and BFS ranks.

    Negative catchment labels and non-finite elevations are nodata. Rank is
    ``-1`` there; drainage cells have rank and direction zero.
    """

    elevation = np.asarray(elevation)
    labels = np.asarray(catchment_labels)
    drainage = np.asarray(drainage_mask, dtype=bool)
    _validate_arrays(elevation, labels, drainage)
    pixel_width, pixel_height = _pixel_sizes(transform)
    valid = (labels >= 0) & np.isfinite(elevation)
    if np.any(drainage & ~valid):
        raise TerrainProductsError("Drainage cells must be finite owned cells")

    rows, cols = elevation.shape
    rank = np.full((rows, cols), -1, dtype=np.int32)
    direction = np.full((rows, cols), -1, dtype=np.int8)
    queue: deque[tuple[int, int]] = deque()
    for row, col in np.argwhere(drainage & valid):
        row, col = int(row), int(col)
        rank[row, col] = 0
        direction[row, col] = 0
        queue.append((row, col))

    # Multi-source geodesic distance, constrained by the owner label.
    while queue:
        row, col = queue.popleft()
        owner = labels[row, col]
        next_rank = rank[row, col] + 1
        for _, dr, dc in _DIRECTIONS:
            nr, nc = row + dr, col + dc
            if (
                0 <= nr < rows and 0 <= nc < cols
                and valid[nr, nc] and labels[nr, nc] == owner
                and rank[nr, nc] < 0
            ):
                rank[nr, nc] = next_rank
                queue.append((nr, nc))

    unreachable = valid & (rank < 0)
    if np.any(unreachable):
        components = _count_components(unreachable, labels)
        cells = int(unreachable.sum())
        raise TerrainProductsError(
            f"{cells} owned cells in {components} raster-connected component(s) "
            "cannot connect to matching drainage"
        )

    diagonal = math.hypot(pixel_width, pixel_height)
    distances = {
        1: pixel_height, 2: diagonal, 3: pixel_width, 4: diagonal,
        5: pixel_height, 6: diagonal, 7: pixel_width, 8: diagonal,
    }
    # Each cell independently chooses only among rank r-1 neighbours.
    for row, col in np.argwhere(valid & ~drainage):
        row, col = int(row), int(col)
        candidates: list[tuple[tuple[float, ...], int]] = []
        z = float(elevation[row, col])
        for code, dr, dc in _DIRECTIONS:
            nr, nc = row + dr, col + dc
            if (
                0 <= nr < rows and 0 <= nc < cols
                and labels[nr, nc] == labels[row, col]
                and rank[nr, nc] == rank[row, col] - 1
            ):
                dz = z - float(elevation[nr, nc])
                distance = distances[code]
                if dz > 0:
                    key = (0.0, -(dz / distance), distance, float(code))
                else:
                    key = (1.0, (-dz / distance), distance, float(code))
                candidates.append((key, code))
        if not candidates:  # Defensive: BFS guarantees this cannot occur.
            raise TerrainProductsError(f"Cell ({row}, {col}) has no valid parent")
        direction[row, col] = min(candidates, key=lambda item: item[0])[1]

    return direction, rank


def compute_hand(
    elevation: np.ndarray,
    direction: np.ndarray,
    rank: np.ndarray | None = None,
) -> np.ndarray:
    """Compute signed height above terminal drainage from a direction raster."""

    elevation = np.asarray(elevation)
    direction = np.asarray(direction)
    if elevation.shape != direction.shape or elevation.ndim != 2:
        raise TerrainProductsError("Elevation and direction must be equal 2-D arrays")
    rank = _routing_rank(direction) if rank is None else np.asarray(rank)
    if rank.shape != direction.shape:
        raise TerrainProductsError("Rank and direction must have equal shapes")
    result = np.full(elevation.shape, np.nan, dtype=np.float64)
    terminal = np.full(elevation.shape, np.nan, dtype=np.float64)
    terminal[direction == 0] = elevation[direction == 0]
    for r, cells in enumerate(_rank_buckets(rank)):
        if r == 0:
            continue
        for row, col in cells:
            nr, nc = _parent(int(row), int(col), int(direction[row, col]), direction.shape)
            terminal[row, col] = terminal[nr, nc]
    valid = direction >= 0
    result[valid] = elevation[valid] - terminal[valid]
    return result


def compute_ltnd(
    direction: np.ndarray,
    transform: Affine,
    rank: np.ndarray | None = None,
) -> np.ndarray:
    """Accumulate floating-point parent-chain distance in raster CRS units."""

    direction = np.asarray(direction)
    if direction.ndim != 2:
        raise TerrainProductsError("Direction must be a two-dimensional array")
    rank = _routing_rank(direction) if rank is None else np.asarray(rank)
    if rank.shape != direction.shape:
        raise TerrainProductsError("Rank and direction must have equal shapes")
    width, height = _pixel_sizes(transform)
    diagonal = math.hypot(width, height)
    steps = {1: height, 2: diagonal, 3: width, 4: diagonal,
             5: height, 6: diagonal, 7: width, 8: diagonal}
    result = np.full(direction.shape, np.nan, dtype=np.float64)
    result[direction == 0] = 0.0
    for r, cells in enumerate(_rank_buckets(rank)):
        if r == 0:
            continue
        for row, col in cells:
            code = int(direction[row, col])
            nr, nc = _parent(int(row), int(col), code, direction.shape)
            result[row, col] = result[nr, nc] + steps[code]
    return result


def _parent(row: int, col: int, code: int, shape: tuple[int, int]) -> tuple[int, int]:
    if code not in _DELTAS:
        raise TerrainProductsError(f"Invalid flow-direction code {code}")
    dr, dc = _DELTAS[code]
    nr, nc = row + dr, col + dc
    if not (0 <= nr < shape[0] and 0 <= nc < shape[1]):
        raise TerrainProductsError("Flow direction points outside the raster")
    return nr, nc


def _routing_rank(direction: np.ndarray) -> np.ndarray:
    """Derive ranks while validating that every route terminates."""

    rank = np.full(direction.shape, -1, dtype=np.int32)
    rank[direction == 0] = 0
    visiting = np.zeros(direction.shape, dtype=bool)
    for row, col in np.argwhere(direction > 0):
        row, col = int(row), int(col)
        if rank[row, col] >= 0:
            continue
        chain: list[tuple[int, int]] = []
        current = (row, col)
        while rank[current] < 0:
            if direction[current] < 0:
                raise TerrainProductsError("A route points into nodata")
            if visiting[current]:
                raise TerrainProductsError("Flow-direction raster contains a cycle")
            visiting[current] = True
            chain.append(current)
            current = _parent(
                current[0], current[1], int(direction[current]), direction.shape
            )
        value = int(rank[current])
        while chain:
            cell = chain.pop()
            value += 1
            rank[cell] = value
            visiting[cell] = False
    return rank


def _rank_buckets(rank: np.ndarray) -> list[list[tuple[int, int]]]:
    """Group cells in linear time so propagation never repeatedly scans a raster."""

    maximum = int(rank.max(initial=0))
    buckets: list[list[tuple[int, int]]] = [[] for _ in range(maximum + 1)]
    for row, col in np.argwhere(rank >= 0):
        buckets[int(rank[row, col])].append((int(row), int(col)))
    return buckets


def _count_components(mask: np.ndarray, labels: np.ndarray) -> int:
    pending = mask.copy()
    count = 0
    rows, cols = mask.shape
    for start_row, start_col in np.argwhere(pending):
        if not pending[start_row, start_col]:
            continue
        count += 1
        owner = labels[start_row, start_col]
        pending[start_row, start_col] = False
        queue = deque([(int(start_row), int(start_col))])
        while queue:
            row, col = queue.popleft()
            for _, dr, dc in _DIRECTIONS:
                nr, nc = row + dr, col + dc
                if (0 <= nr < rows and 0 <= nc < cols and pending[nr, nc]
                        and labels[nr, nc] == owner):
                    pending[nr, nc] = False
                    queue.append((nr, nc))
    return count


def create_terrain_products(
    dem_path: str | Path,
    catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    output_dir: str | Path,
    *,
    id_col: str = "id",
    write_flow_direction: bool = False,
) -> TerrainProductReport:
    """Validate vectors, route them on the DEM grid, and publish tiled GeoTIFFs."""

    _validate_vectors(catchments, segments, id_col)
    dem_path, output_dir = Path(dem_path), Path(output_dir)
    with rasterio.open(dem_path) as src:
        _validate_dem(src)
        catchments = catchments.to_crs(src.crs)
        segments = segments.to_crs(src.crs)
        bounds = catchments.total_bounds
        db = src.bounds
        if bounds[0] < db.left or bounds[1] < db.bottom or bounds[2] > db.right or bounds[3] > db.top:
            raise TerrainProductsError("DEM does not cover the complete catchment ROI")
        raw = from_bounds(*bounds, transform=src.transform)
        col0, row0 = max(0, math.floor(raw.col_off)), max(0, math.floor(raw.row_off))
        col1 = min(src.width, math.ceil(raw.col_off + raw.width))
        row1 = min(src.height, math.ceil(raw.row_off + raw.height))
        window = Window(col0, row0, col1 - col0, row1 - row0)
        transform = src.window_transform(window)
        elevation = src.read(1, window=window, masked=True).astype(np.float64).filled(np.nan)
        shape = elevation.shape

        # Internal sequential labels avoid assumptions about the user ID dtype.
        ordered = catchments.sort_values(id_col, key=lambda s: s.astype(str))
        labels = rasterize(
            ((geom, index) for index, geom in enumerate(ordered.geometry)),
            out_shape=shape, transform=transform, fill=-1, dtype="int32",
        )
        drainage = np.zeros(shape, dtype=bool)
        segment_by_id = segments.set_index(id_col)
        for index, (_, feature) in enumerate(ordered.iterrows()):
            geometry = segment_by_id.loc[feature[id_col]].geometry
            drainage |= rasterize(
                [(geometry, 1)], out_shape=shape, transform=transform,
                fill=0, dtype="uint8",
            ).astype(bool) & (labels == index)

        direction, rank = compute_flow_directions(elevation, labels, drainage, transform)
        hand = compute_hand(elevation, direction, rank).astype("float32")
        ltnd = compute_ltnd(direction, transform, rank).astype("float32")
        profile = src.profile.copy()
        profile.update(
            width=shape[1], height=shape[0], transform=transform, count=1,
            driver="GTiff", tiled=True, blockxsize=256, blockysize=256,
            compress="deflate", BIGTIFF="IF_SAFER",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    final = TerrainProductPaths(
        output_dir / "hand.tif", output_dir / "ltnd.tif",
        output_dir / "flow_direction.tif" if write_flow_direction else None,
    )
    staged: list[tuple[Path, Path]] = []
    try:
        with tempfile.TemporaryDirectory(prefix=".terrain-", dir=output_dir) as tmp:
            tmpdir = Path(tmp)
            for name, data, dtype, nodata in (
                ("hand.tif", hand, "float32", np.nan),
                ("ltnd.tif", ltnd, "float32", np.nan),
                *((("flow_direction.tif", direction, "int8", -1),) if write_flow_direction else ()),
            ):
                path = tmpdir / name
                out_profile = profile | {"dtype": dtype, "nodata": nodata}
                with rasterio.open(path, "w", **out_profile) as dst:
                    dst.write(data, 1)
                    dst.update_tags(
                        routing="drainage-rooted geodesic rank",
                        direction_codes="-1 nodata, 0 drainage, 1-8 N NE E SE S SW W NW",
                    )
                staged.append((path, output_dir / name))
            for source, target in staged:
                os.replace(source, target)
    except Exception:
        # Files remain private in the temporary directory until all are complete.
        raise

    negatives = hand[np.isfinite(hand) & (hand < 0)]
    return TerrainProductReport(
        final, int((labels >= 0).sum()), int(drainage.sum()), 0,
        int(negatives.size),
        float(negatives.min()) if negatives.size else None,
        float(negatives.max()) if negatives.size else None,
    )


def _validate_dem(src: rasterio.io.DatasetReader) -> None:
    if src.crs is None:
        raise TerrainProductsError("DEM has no CRS")
    if src.crs.is_geographic or not src.crs.is_projected:
        raise TerrainProductsError("DEM CRS must be projected")
    unit = src.crs.linear_units.lower()
    _, factor = src.crs.linear_units_factor
    if unit not in {"metre", "meter", "metres", "meters"} or not math.isclose(factor, 1.0):
        raise TerrainProductsError("DEM CRS horizontal units must be metres")
    _pixel_sizes(src.transform)


def _validate_vectors(
    catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    id_col: str,
) -> None:
    for name, frame, allowed in (
        ("catchments", catchments, {"Polygon", "MultiPolygon"}),
        ("segments", segments, {"LineString", "MultiLineString"}),
    ):
        if id_col not in frame:
            raise TerrainProductsError(f"{name} has no '{id_col}' column")
        if frame.crs is None:
            raise TerrainProductsError(f"{name} has no CRS")
        if frame.empty or frame.geometry.is_empty.any() or frame.geometry.isna().any():
            raise TerrainProductsError(f"{name} contains missing or empty geometry")
        invalid = set(frame.geom_type) - allowed
        if invalid:
            raise TerrainProductsError(f"{name} has invalid geometry type(s): {sorted(invalid)}")
        if frame[id_col].isna().any() or frame[id_col].duplicated().any():
            raise TerrainProductsError(f"{name} IDs must be non-null and unique")
    if set(catchments[id_col]) != set(segments[id_col]):
        raise TerrainProductsError("Catchment and segment IDs do not match")
    spatial = catchments.geometry
    for i, geometry in enumerate(spatial):
        for j in catchments.sindex.query(geometry, predicate="intersects"):
            if j > i and geometry.intersection(spatial.iloc[j]).area > 0:
                raise TerrainProductsError("Catchments have positive-area overlaps")
