"""AGREE-conditioned terrain-driven basin routing and raster products.

Natural D8 drainage on the conditioned DEM is retained except on flats and
targeted shallow-breach corridors that connect trapped basins to the supplied
drainage. HAND elevations continue to use the unmodified DEM.
"""

from __future__ import annotations

import heapq
import importlib.metadata
import json
import math
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import pyogrio
import rasterio
from affine import Affine
from numba import njit
from pyproj import CRS
from rasterio.enums import MaskFlags, Resampling
from rasterio.env import get_gdal_config, set_gdal_config
from rasterio.features import rasterize
from rasterio.windows import Window

from mgb_vec_hydro.aggregation import INPUT_COLUMNS
from mgb_vec_hydro.exceptions import TerrainProductsError
from mgb_vec_hydro.execution.checkpoints import CheckpointStore, execution_fingerprint
from mgb_vec_hydro.execution.executor import (
    ExecutionConfig,
    ExecutionReport,
    LocalExecutor,
    WorkerContext,
    WorkerOutput,
    WorkItem,
)
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.raster import (
    AlignedRasterReader,
    PreparedRasterReader,
    RasterAssembler,
    RasterPacket,
    RasterPatch,
    RasterProductSpec,
    RasterUnit,
    packet_raster_units,
    plan_raster_units,
    prepared_grid,
)
from mgb_vec_hydro.execution.vector import inspect_vector_provider, scan_id_fids
from mgb_vec_hydro.preparation import BLOCK_SIZE, GridSpec, PreparedDataset

# Code, row delta, column delta. This order is also the final tie-break.
_DIRECTIONS = (
    (1, -1, 0),
    (2, -1, 1),
    (3, 0, 1),
    (4, 1, 1),
    (5, 1, 0),
    (6, 1, -1),
    (7, 0, -1),
    (8, -1, -1),
)
_DELTAS = {code: (dr, dc) for code, dr, dc in _DIRECTIONS}
_OPPOSITE = {1: 5, 2: 6, 3: 7, 4: 8, 5: 1, 6: 2, 7: 3, 8: 4}
_DR = np.array([-1, -1, 0, 1, 1, 1, 0, -1], dtype=np.int8)
_DC = np.array([0, 1, 1, 1, 0, -1, -1, -1], dtype=np.int8)


TERRAIN_CONTRACT = "mgb-terrain-dataset"
TERRAIN_CONTRACT_VERSION = 1
MAX_PACKET_UNITS = 8
DOMAIN_BYTES_PER_CELL = 16
DEM_BYTES_PER_CELL = 128
D8_BYTES_PER_CELL = 80
TASK_FIXED_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class TerrainSpec:
    prepared: Path
    minis: Path
    output_dir: Path
    direction_source: Literal["dem", "d8"] = "dem"
    write_flow_direction: bool = False
    agree_sharp: float = 80.0
    agree_smooth: float = 8.0
    agree_buffer: int = 4
    workers: int = 4
    memory_limit_mb: int = 512
    io_slots: int = 2
    batch_size: int = 10_000
    checkpoint_dir: Path | None = None


@dataclass(frozen=True)
class TerrainReport:
    output_dir: Path
    manifest: Path
    mini_count: int
    owned_cells: int
    drainage_cells: int
    negative_hand_cells: int
    negative_hand_min: float | None
    negative_hand_max: float | None
    direction_source: str
    domain_execution: ExecutionReport
    terrain_execution: ExecutionReport
    timings: dict[str, float]


class TerrainDataset:
    """Reader and validator for a published Stage 4 terrain dataset."""

    def __init__(self, root: Path, manifest: dict[str, Any]):
        self.root = root
        self.manifest = manifest

    @classmethod
    def open(cls, root: str | Path) -> TerrainDataset:
        root = Path(root)
        path = root / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TerrainProductsError(f"Cannot read terrain manifest: {path}") from exc
        return cls(root, manifest)

    def path(self, name: str) -> Path:
        try:
            relative = self.manifest["assets"][name]["path"]
        except (KeyError, TypeError) as exc:
            raise TerrainProductsError(f"Unknown terrain asset: {name}") from exc
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise TerrainProductsError(
                f"Terrain asset escapes dataset: {relative}"
            ) from exc
        return candidate

    def validate(self) -> None:
        value = self.manifest
        if (
            value.get("contract") != TERRAIN_CONTRACT
            or value.get("version") != TERRAIN_CONTRACT_VERSION
        ):
            raise TerrainProductsError(
                "Unsupported terrain dataset contract or version"
            )
        try:
            grid_value = value["grid"]
            grid = GridSpec(
                CRS.from_wkt(grid_value["crs_wkt"]),
                Affine(*grid_value["transform"]),
                int(grid_value["width"]),
                int(grid_value["height"]),
            )
            assets = value["assets"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TerrainProductsError("Terrain manifest is incomplete") from exc
        required = {"mini_index", "mini_ownership", "drainage", "hand", "ltnd"}
        if not required.issubset(assets):
            raise TerrainProductsError("Terrain manifest is missing required assets")
        index = self.path("mini_index")
        if not index.is_file():
            raise TerrainProductsError("Terrain mini index is missing")
        try:
            table = pd.read_parquet(index)
        except Exception as exc:
            raise TerrainProductsError("Cannot read terrain mini index") from exc
        if list(table.columns) != ["mini_label", "mini_id"]:
            raise TerrainProductsError("Terrain mini index schema is invalid")
        if (
            table.empty
            or table["mini_label"].dtype != np.dtype("int32")
            or table["mini_label"].duplicated().any()
            or table["mini_id"].isna().any()
            or table["mini_id"].duplicated().any()
        ):
            raise TerrainProductsError("Terrain mini index values are invalid")
        if not np.array_equal(
            table["mini_label"].to_numpy(),
            np.arange(1, len(table) + 1, dtype="int32"),
        ):
            raise TerrainProductsError("Terrain mini labels must be contiguous")
        expected_dtypes = {
            "mini_ownership": "int32",
            "drainage": "uint8",
            "hand": "float32",
            "ltnd": "float32",
            "flow_direction": "uint8",
        }
        raster_assets = (required - {"mini_index"}) | ({"flow_direction"} & set(assets))
        for name in raster_assets:
            path = self.path(name)
            if not path.is_file():
                raise TerrainProductsError(f"Terrain raster asset is missing: {name}")
            try:
                with rasterio.open(path) as source:
                    if (
                        source.count != 1
                        or source.crs is None
                        or CRS.from_user_input(source.crs) != grid.crs
                        or source.transform != grid.transform
                        or source.shape != (grid.height, grid.width)
                        or source.nodata is not None
                        or source.dtypes[0] != expected_dtypes[name]
                        or MaskFlags.per_dataset not in source.mask_flag_enums[0]
                        or source.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG"
                    ):
                        raise TerrainProductsError(
                            f"Terrain raster does not match the canonical grid: {name}"
                        )
            except rasterio.errors.RasterioError as exc:
                raise TerrainProductsError(
                    f"Cannot inspect terrain raster: {name}"
                ) from exc


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


def _validate_agree_parameters(
    sharp: float,
    smooth: float,
    buffer: int,
) -> None:
    if not np.isfinite(sharp) or sharp < 0:
        raise TerrainProductsError("AGREE sharp value must be finite and non-negative")
    if not np.isfinite(smooth) or smooth < 0:
        raise TerrainProductsError("AGREE smooth value must be finite and non-negative")
    if (
        isinstance(buffer, (bool, np.bool_))
        or not np.isscalar(buffer)
        or not np.isfinite(buffer)
        or int(buffer) != buffer
        or buffer < 0
    ):
        raise TerrainProductsError("AGREE buffer must be a non-negative integer")


def _agree_condition_dem(
    elevation: np.ndarray,
    catchment_labels: np.ndarray,
    drainage_mask: np.ndarray,
    *,
    sharp: float = 80.0,
    smooth: float = 8.0,
    buffer: int = 4,
) -> np.ndarray:
    """Return a catchment-confined AGREE-conditioned copy of a DEM."""

    elevation = np.asarray(elevation)
    labels = np.asarray(catchment_labels)
    drainage = np.asarray(drainage_mask, dtype=bool)
    _validate_arrays(elevation, labels, drainage)
    _validate_agree_parameters(sharp, smooth, buffer)
    valid = (labels >= 0) & np.isfinite(elevation)
    if np.any(drainage & ~valid):
        raise TerrainProductsError("Drainage cells must be finite owned cells")
    del valid
    return _agree_condition_kernel(
        elevation.astype(np.float64, copy=False),
        labels,
        drainage,
        float(sharp),
        float(smooth),
        int(buffer),
    )


@njit(cache=True)
def _agree_condition_kernel(elevation, labels, drainage, sharp, smooth, buffer):
    """Apply the AGREE profile using Euclidean distances in raster pixels."""
    rows, cols = elevation.shape
    distance = np.full((rows, cols), np.inf, np.float64)
    for r in range(rows):
        for c in range(cols):
            if not drainage[r, c]:
                continue
            owner = labels[r, c]
            for dr in range(-buffer, buffer + 1):
                nr = r + dr
                if nr < 0 or nr >= rows:
                    continue
                for dc in range(-buffer, buffer + 1):
                    nc = c + dc
                    if nc < 0 or nc >= cols:
                        continue
                    candidate = math.sqrt(dr * dr + dc * dc)
                    if (
                        candidate <= buffer
                        and labels[nr, nc] == owner
                        and np.isfinite(elevation[nr, nc])
                        and candidate < distance[nr, nc]
                    ):
                        distance[nr, nc] = candidate
    conditioned = elevation.copy()
    for r in range(rows):
        for c in range(cols):
            if np.isfinite(distance[r, c]):
                conditioned[r, c] += smooth * (distance[r, c] - buffer)
                if drainage[r, c]:
                    conditioned[r, c] -= sharp
    return conditioned


def compute_flow_directions(
    elevation: np.ndarray,
    catchment_labels: np.ndarray,
    drainage_mask: np.ndarray,
    transform: Affine,
) -> tuple[np.ndarray, np.ndarray]:
    """Return terrain-driven D8 directions with targeted shallow breaching.

    Negative catchment labels and non-finite elevations are nodata. Rank is
    ``-1`` there; drainage cells have rank and direction zero.

    Ordinary cells use their steepest metric downhill neighbour. Flats drain to
    their lowest natural outlet, while pits and closed flats form local basins.
    Trapped basins are connected on a basin adjacency graph by corridors that
    minimize maximum cut depth, cumulative excavation, and metric length.
    Returned ranks are traversal aids only; they never constrain ordinary flow.
    """

    elevation = np.asarray(elevation)
    labels = np.asarray(catchment_labels)
    drainage = np.asarray(drainage_mask, dtype=bool)
    _validate_arrays(elevation, labels, drainage)
    pixel_width, pixel_height = _pixel_sizes(transform)
    valid = (labels >= 0) & np.isfinite(elevation)
    if np.any(drainage & ~valid):
        raise TerrainProductsError("Drainage cells must be finite owned cells")
    del valid

    direction = _natural_d8_and_flats(
        elevation.astype(np.float64, copy=False),
        labels,
        drainage,
        pixel_width,
        pixel_height,
    )
    rank, terminal = _rank_and_terminal(direction)
    basin, unique_terminals = _label_basins(terminal)
    basin_count = unique_terminals.size
    stream_basin = np.zeros(basin_count, dtype=bool)
    stream_basin[np.unique(basin[drainage])] = True
    del unique_terminals

    order = _rank_order(rank)
    cut_max, cut_sum, corridor_length = _corridor_costs(
        elevation.astype(np.float64, copy=False),
        direction,
        terminal,
        order,
        pixel_width,
        pixel_height,
    )
    edge_data = _scan_basin_boundaries(
        basin,
        labels,
        cut_max,
        cut_sum,
        corridor_length,
        pixel_width,
        pixel_height,
    )
    selected = _basin_paths_to_stream(basin_count, stream_basin, edge_data)
    if np.any((~stream_basin) & (selected < 0)):
        trapped = (~stream_basin) & (selected < 0)
        cells = int(np.isin(basin, np.flatnonzero(trapped)).sum())
        raise TerrainProductsError(
            f"{cells} owned cells in trapped basin(s) cannot connect to matching drainage"
        )
    del basin, cut_max, cut_sum, corridor_length, order, rank, stream_basin, terminal
    direction = _reverse_selected_corridors(direction, edge_data, selected)
    del edge_data, selected
    rank = _rank_only(direction)
    return direction, rank


@njit(cache=True)
def _natural_d8_and_flats(elevation, labels, drainage, width, height):
    """Assign strict D8 descent, then resolve equal-elevation components."""
    rows, cols = elevation.shape
    size = rows * cols
    direction = np.full((rows, cols), -1, np.int8)
    unresolved = np.zeros((rows, cols), np.uint8)
    distances = np.array(
        [
            height,
            math.hypot(width, height),
            width,
            math.hypot(width, height),
            height,
            math.hypot(width, height),
            width,
            math.hypot(width, height),
        ]
    )
    for r in range(rows):
        for c in range(cols):
            if labels[r, c] < 0 or not np.isfinite(elevation[r, c]):
                continue
            if drainage[r, c]:
                direction[r, c] = 0
                continue
            best_slope = 0.0
            best_index = size
            best_code = -1
            for k in range(8):
                nr, nc = r + _DR[k], c + _DC[k]
                if (
                    0 <= nr < rows
                    and 0 <= nc < cols
                    and labels[nr, nc] == labels[r, c]
                    and np.isfinite(elevation[nr, nc])
                    and elevation[nr, nc] < elevation[r, c]
                ):
                    slope = (elevation[r, c] - elevation[nr, nc]) / distances[k]
                    index = nr * cols + nc
                    if slope > best_slope or (
                        slope == best_slope and index < best_index
                    ):
                        best_slope, best_index, best_code = slope, index, k + 1
            if best_code > 0:
                direction[r, c] = best_code
            else:
                unresolved[r, c] = 1

    # Work arrays are reused for every flat, keeping memory linear.
    seen = np.zeros((rows, cols), np.uint8)
    in_component = np.zeros((rows, cols), np.uint8)
    queue = np.empty(size, np.int64)
    component = np.empty(size, np.int64)
    for sr in range(rows):
        for sc in range(cols):
            if unresolved[sr, sc] == 0 or seen[sr, sc] != 0:
                continue
            z = elevation[sr, sc]
            owner = labels[sr, sc]
            head, tail, count = 0, 1, 0
            queue[0] = sr * cols + sc
            seen[sr, sc] = 1
            lowest_outlet = np.inf
            while head < tail:
                index = queue[head]
                head += 1
                r, c = index // cols, index % cols
                component[count] = index
                count += 1
                in_component[r, c] = 1
                if direction[r, c] > 0:
                    k = direction[r, c] - 1
                    lowest_outlet = min(
                        lowest_outlet, elevation[r + _DR[k], c + _DC[k]]
                    )
                elif direction[r, c] == 0:
                    lowest_outlet = min(lowest_outlet, z)
                for k in range(8):
                    nr, nc = r + _DR[k], c + _DC[k]
                    if (
                        0 <= nr < rows
                        and 0 <= nc < cols
                        and seen[nr, nc] == 0
                        and labels[nr, nc] == owner
                        and np.isfinite(elevation[nr, nc])
                        and elevation[nr, nc] == z
                    ):
                        seen[nr, nc] = 1
                        queue[tail] = nr * cols + nc
                        tail += 1

            # Seed a breadth-first routing from natural outlets at the lowest
            # downslope elevation. Closed flats use their row-major first cell.
            head, tail = 0, 0
            for i in range(count):
                index = component[i]
                r, c = index // cols, index % cols
                seed = direction[r, c] == 0
                if direction[r, c] > 0:
                    k = direction[r, c] - 1
                    seed = elevation[r + _DR[k], c + _DC[k]] == lowest_outlet
                if seed:
                    queue[tail] = index
                    tail += 1
            if tail == 0:
                root = component[0]
                direction[root // cols, root % cols] = 0
                unresolved[root // cols, root % cols] = 0
                queue[0] = root
                tail = 1
            while head < tail:
                index = queue[head]
                head += 1
                r, c = index // cols, index % cols
                for k in range(8):
                    nr, nc = r + _DR[k], c + _DC[k]
                    if (
                        0 <= nr < rows
                        and 0 <= nc < cols
                        and in_component[nr, nc] != 0
                        and unresolved[nr, nc] != 0
                    ):
                        # Point toward the already routed flat cell.
                        direction[nr, nc] = ((k + 4) % 8) + 1
                        unresolved[nr, nc] = 0
                        queue[tail] = nr * cols + nc
                        tail += 1
            # Cells with their own strict downhill direction are deliberately
            # not rewritten. Such cells can partition the unresolved part of
            # a plateau, so use every natural outlet as a fallback seed for
            # any portion the globally lowest outlet could not reach.
            head, tail = 0, 0
            for i in range(count):
                index = component[i]
                r, c = index // cols, index % cols
                if direction[r, c] >= 0:
                    queue[tail] = index
                    tail += 1
            while head < tail:
                index = queue[head]
                head += 1
                r, c = index // cols, index % cols
                for k in range(8):
                    nr, nc = r + _DR[k], c + _DC[k]
                    if (
                        0 <= nr < rows
                        and 0 <= nc < cols
                        and in_component[nr, nc] != 0
                        and unresolved[nr, nc] != 0
                    ):
                        direction[nr, nc] = ((k + 4) % 8) + 1
                        unresolved[nr, nc] = 0
                        queue[tail] = nr * cols + nc
                        tail += 1
            for i in range(count):
                index = component[i]
                in_component[index // cols, index % cols] = 0
    return direction


@njit(cache=True)
def _rank_and_terminal(direction):
    """Validate routes and return numeric traversal ranks and terminal cells."""
    rows, cols = direction.shape
    size = rows * cols
    rank = np.full(size, -1, np.int32)
    terminal = np.full(size, -1, np.int64)
    state = np.zeros(size, np.uint8)
    path = np.empty(size, np.int64)
    flat = direction.ravel()
    for start in range(size):
        if flat[start] == 0:
            rank[start], terminal[start], state[start] = 0, start, 2
        elif flat[start] < 0:
            state[start] = 2
    for start in range(size):
        if flat[start] <= 0 or state[start] == 2:
            continue
        current, count = start, 0
        while state[current] != 2:
            if state[current] == 1:
                raise ValueError("Flow-direction raster contains a cycle")
            state[current] = 1
            path[count] = current
            count += 1
            code = flat[current]
            if code < 1 or code > 8:
                raise ValueError("Invalid flow-direction code")
            r, c = current // cols, current % cols
            nr, nc = r + _DR[code - 1], c + _DC[code - 1]
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                raise ValueError("Flow direction points outside the raster")
            current = nr * cols + nc
            if flat[current] < 0:
                raise ValueError("A route points into nodata")
        value, root = rank[current], terminal[current]
        for i in range(count - 1, -1, -1):
            cell = path[i]
            value += 1
            rank[cell], terminal[cell], state[cell] = value, root, 2
    return rank.reshape((rows, cols)), terminal.reshape((rows, cols))


@njit(cache=True)
def _rank_only(direction):
    """Validate routes and return ranks without retaining terminal indices."""
    rows, cols = direction.shape
    size = rows * cols
    rank = np.full(size, -1, np.int32)
    state = np.zeros(size, np.uint8)
    path = np.empty(size, np.int64)
    flat = direction.ravel()
    for start in range(size):
        if flat[start] == 0:
            rank[start], state[start] = 0, 2
        elif flat[start] < 0:
            state[start] = 2
    for start in range(size):
        if flat[start] <= 0 or state[start] == 2:
            continue
        current, count = start, 0
        while state[current] != 2:
            if state[current] == 1:
                raise ValueError("Flow-direction raster contains a cycle")
            state[current] = 1
            path[count] = current
            count += 1
            code = flat[current]
            if code < 1 or code > 8:
                raise ValueError("Invalid flow-direction code")
            r, c = current // cols, current % cols
            nr, nc = r + _DR[code - 1], c + _DC[code - 1]
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                raise ValueError("Flow direction points outside the raster")
            current = nr * cols + nc
            if flat[current] < 0:
                raise ValueError("A route points into nodata")
        value = rank[current]
        for i in range(count - 1, -1, -1):
            cell = path[i]
            value += 1
            rank[cell], state[cell] = value, 2
    return rank.reshape((rows, cols))


@njit(cache=True)
def _label_basins(terminal):
    """Compact terminal cell indices into dense basin labels in linear time."""
    rows, cols = terminal.shape
    size = rows * cols
    root_to_basin = np.full(size, -1, np.int64)
    basin = np.full(size, -1, np.int64)
    root_count = 0
    flat_terminal = terminal.ravel()
    for cell in range(size):
        root = flat_terminal[cell]
        if root >= 0 and root_to_basin[root] < 0:
            root_to_basin[root] = root_count
            root_count += 1
    unique = np.empty(root_count, np.int64)
    for cell in range(size):
        root = flat_terminal[cell]
        if root >= 0:
            basin[cell] = root_to_basin[root]
            if cell == root:
                unique[root_to_basin[root]] = root
    return basin.reshape((rows, cols)), unique


@njit(cache=True)
def _rank_order(rank):
    """Counting-sort valid cells by traversal rank in linear time."""
    flat = rank.ravel()
    maximum = 0
    valid_count = 0
    for i in range(flat.size):
        if flat[i] >= 0:
            valid_count += 1
            maximum = max(maximum, flat[i])
    counts = np.zeros(maximum + 1, np.int64)
    for i in range(flat.size):
        if flat[i] >= 0:
            counts[flat[i]] += 1
    offsets = np.empty(maximum + 1, np.int64)
    position = 0
    for value in range(maximum + 1):
        offsets[value] = position
        position += counts[value]
    order = np.empty(valid_count, np.int64)
    for i in range(flat.size):
        value = flat[i]
        if value >= 0:
            position = offsets[value]
            order[position] = i
            offsets[value] += 1
    return order


@njit(cache=True)
def _corridor_costs(elevation, direction, terminal, order, width, height):
    size = direction.size
    cols = direction.shape[1]
    maximum = np.zeros(size, np.float64)
    cumulative = np.zeros(size, np.float64)
    length = np.zeros(size, np.float64)
    elev = elevation.ravel()
    dirs = direction.ravel()
    roots = terminal.ravel()
    diagonal = math.hypot(width, height)
    steps = np.array(
        [height, diagonal, width, diagonal, height, diagonal, width, diagonal]
    )
    for oi in range(order.size):
        cell = order[oi]
        code = dirs[cell]
        if code <= 0:
            continue
        r, c = cell // cols, cell % cols
        parent = (r + _DR[code - 1]) * cols + c + _DC[code - 1]
        depth = max(elev[cell] - elev[roots[cell]], 0.0)
        maximum[cell] = max(maximum[parent], depth)
        cumulative[cell] = cumulative[parent] + depth
        length[cell] = length[parent] + steps[code - 1]
    return (
        maximum.reshape(direction.shape),
        cumulative.reshape(direction.shape),
        length.reshape(direction.shape),
    )


@njit(cache=True)
def _scan_basin_boundaries(basin, labels, cut_max, cut_sum, path_length, width, height):
    """Return the best numeric corridor for every directed adjacent basin pair."""
    rows, cols = basin.shape
    occurrences = 0
    for r in range(rows):
        for c in range(cols):
            if basin[r, c] < 0:
                continue
            for k in (2, 3, 4, 5):  # E, SE, S, SW: each pair exactly once.
                nr, nc = r + _DR[k], c + _DC[k]
                if (
                    0 <= nr < rows
                    and 0 <= nc < cols
                    and basin[nr, nc] >= 0
                    and labels[nr, nc] == labels[r, c]
                    and basin[nr, nc] != basin[r, c]
                ):
                    occurrences += 2
    capacity = 1
    while capacity < max(4, occurrences * 2):
        capacity *= 2
    keys = np.full(capacity, -1, np.int64)
    origins = np.full(capacity, -1, np.int64)
    destinations = np.full(capacity, -1, np.int64)
    maxima = np.full(capacity, np.inf)
    sums = np.full(capacity, np.inf)
    lengths = np.full(capacity, np.inf)
    diagonal = math.hypot(width, height)
    steps = np.array(
        [height, diagonal, width, diagonal, height, diagonal, width, diagonal]
    )
    mask = capacity - 1
    for r in range(rows):
        for c in range(cols):
            a = basin[r, c]
            if a < 0:
                continue
            for k in (2, 3, 4, 5):
                nr, nc = r + _DR[k], c + _DC[k]
                if (
                    nr < 0
                    or nr >= rows
                    or nc < 0
                    or nc >= cols
                    or labels[nr, nc] != labels[r, c]
                ):
                    continue
                b = basin[nr, nc]
                if b < 0 or a == b:
                    continue
                for reverse in range(2):
                    source = a if reverse == 0 else b
                    target = b if reverse == 0 else a
                    origin = r * cols + c if reverse == 0 else nr * cols + nc
                    destination = nr * cols + nc if reverse == 0 else r * cols + c
                    rr, cc = origin // cols, origin % cols
                    cm, cs = cut_max[rr, cc], cut_sum[rr, cc]
                    pl = path_length[rr, cc] + steps[k]
                    key = (source << 32) | target
                    slot = (key * 1140071481932319845) & mask
                    while keys[slot] != -1 and keys[slot] != key:
                        slot = (slot + 1) & mask
                    better = (
                        cm < maxima[slot]
                        or (cm == maxima[slot] and cs < sums[slot])
                        or (
                            cm == maxima[slot]
                            and cs == sums[slot]
                            and pl < lengths[slot]
                        )
                        or (
                            cm == maxima[slot]
                            and cs == sums[slot]
                            and pl == lengths[slot]
                            and origin < origins[slot]
                        )
                    )
                    if keys[slot] == -1 or better:
                        keys[slot], origins[slot], destinations[slot] = (
                            key,
                            origin,
                            destination,
                        )
                        maxima[slot], sums[slot], lengths[slot] = cm, cs, pl
    count = np.sum(keys != -1)
    result = np.empty((count, 8), np.float64)
    out = 0
    for slot in range(capacity):
        if keys[slot] != -1:
            result[out, 0] = keys[slot] >> 32
            result[out, 1] = keys[slot] & 0xFFFFFFFF
            result[out, 2] = maxima[slot]
            result[out, 3] = sums[slot]
            result[out, 4] = lengths[slot]
            result[out, 5] = origins[slot]
            result[out, 6] = destinations[slot]
            result[out, 7] = origins[slot]
            out += 1
    return result


def _basin_paths_to_stream(
    count: int, stream: np.ndarray, edges: np.ndarray
) -> np.ndarray:
    """Run a lexicographic multi-source shortest path on the basin graph."""
    incoming: list[list[int]] = [[] for _ in range(count)]
    for edge_index, edge in enumerate(edges):
        incoming[int(edge[1])].append(edge_index)
    costs: list[tuple[float, float, float, int, int] | None] = [None] * count
    selected = np.full(count, -1, dtype=np.int64)
    settled = np.zeros(count, dtype=bool)
    queue: list[tuple[float, float, float, int, int, int]] = []
    for basin_id in np.flatnonzero(stream):
        costs[int(basin_id)] = (0.0, 0.0, 0.0, -1, -1)
        heapq.heappush(queue, (0.0, 0.0, 0.0, -1, -1, int(basin_id)))
    while queue:
        *raw, basin_id = heapq.heappop(queue)
        if costs[basin_id] != tuple(raw) or settled[basin_id]:
            continue
        settled[basin_id] = True
        for edge_index in incoming[basin_id]:
            edge = edges[edge_index]
            source = int(edge[0])
            if settled[source]:
                continue
            candidate = (
                max(float(edge[2]), raw[0]),
                float(edge[3]) + raw[1],
                float(edge[4]) + raw[2],
                int(edge[7]),
                basin_id,
            )
            if costs[source] is None or candidate < costs[source]:
                costs[source] = candidate
                selected[source] = edge_index
                heapq.heappush(queue, (*candidate, source))
    return selected


@njit(cache=True)
def _reverse_selected_corridors(direction, edges, selected):
    original = direction
    result = direction.copy()
    cols = direction.shape[1]
    for basin_id in range(selected.size):
        edge_index = selected[basin_id]
        if edge_index < 0:
            continue
        origin = int(edges[edge_index, 5])
        destination = int(edges[edge_index, 6])
        previous = destination
        current = origin
        while True:
            cr, cc = current // cols, current % cols
            pr, pc = previous // cols, previous % cols
            dr, dc = pr - cr, pc - cc
            for k in range(8):
                if _DR[k] == dr and _DC[k] == dc:
                    result[cr, cc] = k + 1
                    break
            code = original[cr, cc]
            if code == 0:
                break
            previous = current
            current = (cr + _DR[code - 1]) * cols + cc + _DC[code - 1]
    return result


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
    order = _rank_order(rank)
    return _hand_kernel(elevation.astype(np.float64, copy=False), direction, order)


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
    order = _rank_order(rank)
    return _ltnd_kernel(direction, order, width, height)


@njit(cache=True)
def _hand_kernel(elevation, direction, order):
    result = np.full(direction.size, np.nan)
    terminal_z = np.full(direction.size, np.nan)
    dirs = direction.ravel()
    elev = elevation.ravel()
    cols = direction.shape[1]
    for oi in range(order.size):
        cell = order[oi]
        code = dirs[cell]
        if code < 0:
            continue
        if code == 0:
            terminal_z[cell] = elev[cell]
        else:
            r, c = cell // cols, cell % cols
            parent = (r + _DR[code - 1]) * cols + c + _DC[code - 1]
            terminal_z[cell] = terminal_z[parent]
        result[cell] = elev[cell] - terminal_z[cell]
    return result.reshape(direction.shape)


@njit(cache=True)
def _ltnd_kernel(direction, order, width, height):
    result = np.full(direction.size, np.nan)
    dirs = direction.ravel()
    cols = direction.shape[1]
    diagonal = math.hypot(width, height)
    steps = np.array(
        [height, diagonal, width, diagonal, height, diagonal, width, diagonal]
    )
    for oi in range(order.size):
        cell = order[oi]
        code = dirs[cell]
        if code < 0:
            continue
        if code == 0:
            result[cell] = 0.0
        else:
            r, c = cell // cols, cell % cols
            parent = (r + _DR[code - 1]) * cols + c + _DC[code - 1]
            result[cell] = result[parent] + steps[code - 1]
    return result.reshape(direction.shape)


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
    try:
        return _rank_and_terminal(np.asarray(direction))[0]
    except ValueError as exc:
        raise TerrainProductsError(str(exc)) from exc


def _warm_routing_kernels() -> None:
    """Load/compile cached kernels separately from measured production routing."""
    if _ltnd_kernel.signatures and _agree_condition_kernel.signatures:
        return
    elevation = np.array([[1.0, 0.0]], dtype=np.float64)
    labels = np.zeros((1, 2), dtype=np.int64)
    drainage = np.array([[False, True]])
    _agree_condition_dem(elevation, labels, drainage)
    direction, rank = compute_flow_directions(
        elevation, labels, drainage, Affine(1, 0, 0, 0, -1, 0)
    )
    compute_hand(elevation, direction, rank)
    compute_ltnd(direction, Affine(1, 0, 0, 0, -1, 0), rank)


@dataclass(frozen=True)
class _MiniUnit:
    raster: RasterUnit
    mini_label: int
    mini_id: Any
    catchment_fid: int
    segment_fid: int


@dataclass(frozen=True)
class _DomainPayload:
    grid: GridSpec
    catchments: Path
    segments: Path
    units: tuple[_MiniUnit, ...]


@dataclass(frozen=True)
class _TerrainPayload:
    prepared: Path
    grid: GridSpec
    ownership: Path
    drainage: Path
    units: tuple[_MiniUnit, ...]
    direction_source: str
    write_flow_direction: bool
    agree_sharp: float
    agree_smooth: float
    agree_buffer: int
    gdal_cache_bytes: int


@dataclass(frozen=True)
class _DomainPatch:
    window: Window
    valid: np.ndarray
    ownership: np.ndarray
    drainage: np.ndarray


@dataclass(frozen=True)
class _TerrainPatch:
    window: Window
    valid: np.ndarray
    hand: np.ndarray
    ltnd: np.ndarray
    direction: np.ndarray | None


@dataclass(frozen=True)
class _PacketValue:
    patches: tuple[Any, ...]


class _PickleCheckpointCodec:
    suffix = ".pkl"

    def dump(self, value: Any, path: Path) -> None:
        with path.open("wb") as stream:
            pickle.dump(value, stream, protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, path: Path) -> Any:
        with path.open("rb") as stream:
            return pickle.load(stream)


def create_terrain_dataset(spec: TerrainSpec) -> TerrainReport:
    """Build terrain products with a bounded coordinator GDAL block cache."""
    previous = get_gdal_config("GDAL_CACHEMAX")
    set_gdal_config(
        "GDAL_CACHEMAX",
        _gdal_cache_bytes(spec.memory_limit_mb * 1024 * 1024, 1),
    )
    try:
        return _create_terrain_dataset(spec)
    finally:
        set_gdal_config("GDAL_CACHEMAX", previous)


def _create_terrain_dataset(spec: TerrainSpec) -> TerrainReport:
    """Build and atomically publish bounded mini-based terrain products."""

    overall_started = time.perf_counter()
    _validate_terrain_spec(spec)
    prepared = PreparedDataset.open(spec.prepared)
    prepared.validate()
    grid = prepared_grid(spec.prepared)
    planning_started = time.perf_counter()
    mini_units, mini_index, mini_identity = _plan_minis(spec, grid)
    memory_bytes = spec.memory_limit_mb * 1024 * 1024
    domain_items = _domain_work_items(mini_units, spec, grid, memory_bytes)
    planning_seconds = time.perf_counter() - planning_started
    config = ExecutionConfig(
        workers=spec.workers,
        memory_limit_bytes=memory_bytes,
        max_in_flight=spec.workers,
        io_slots=spec.io_slots,
    )
    checkpoint_root = Path(spec.checkpoint_dir) if spec.checkpoint_dir else None
    domain_checkpoint = _checkpoint(
        checkpoint_root / "domain" if checkpoint_root else None,
        "terrain-domain",
        prepared.manifest,
        {"minis": mini_identity},
        domain_items,
    )

    publisher = AtomicOutputDirectory(spec.output_dir)
    compression_seconds = 0.0
    with publisher as staging:
        raster_root = staging / "rasters"
        raster_root.mkdir()
        mini_index_path = staging / "mini_index.parquet"
        mini_index.to_parquet(mini_index_path, index=False)

        domain_specs = (
            RasterProductSpec(
                "mini_ownership",
                "int32",
                tags={
                    "role": "mini-catchment ownership",
                    "labels": "mini_index.parquet",
                },
            ),
            RasterProductSpec(
                "drainage",
                "uint8",
                tags={
                    "role": "matching mini drainage",
                    "values": "0 non-drainage, 1 drainage",
                },
            ),
        )
        with RasterAssembler(
            raster_root,
            grid,
            domain_specs,
            compression_threads=min(spec.workers, 4),
        ) as domain_assembler:

            def reduce_domain(result):
                started = time.perf_counter()
                for patch in result.value.patches:
                    domain_assembler.write(
                        RasterPatch(
                            "mini_ownership",
                            patch.window,
                            patch.ownership,
                            patch.valid,
                        )
                    )
                    domain_assembler.write(
                        RasterPatch(
                            "drainage", patch.window, patch.drainage, patch.valid
                        )
                    )
                return {"output_write": time.perf_counter() - started}

            domain_report = LocalExecutor(config).run(
                domain_items,
                _domain_worker,
                reduce_domain,
                checkpoint=domain_checkpoint,
            )
            started = time.perf_counter()
            domain_paths = domain_assembler.finish()
            compression_seconds += time.perf_counter() - started

        terrain_items = _terrain_work_items(
            mini_units,
            spec,
            grid,
            domain_paths["mini_ownership"],
            domain_paths["drainage"],
            memory_bytes,
        )
        terrain_checkpoint = _checkpoint(
            checkpoint_root / "terrain" if checkpoint_root else None,
            "terrain-products",
            prepared.manifest,
            {
                "minis": mini_identity,
                "direction_source": spec.direction_source,
                "write_flow_direction": spec.write_flow_direction,
                "agree_sharp": spec.agree_sharp,
                "agree_smooth": spec.agree_smooth,
                "agree_buffer": spec.agree_buffer,
            },
            terrain_items,
        )
        terrain_specs = [
            RasterProductSpec(
                "hand",
                "float32",
                Resampling.average,
                _terrain_tags(spec, "height above matching drainage"),
            ),
            RasterProductSpec(
                "ltnd",
                "float32",
                Resampling.average,
                _terrain_tags(spec, "along-route distance to matching drainage"),
            ),
        ]
        if spec.write_flow_direction:
            terrain_specs.append(
                RasterProductSpec(
                    "flow_direction",
                    "uint8",
                    Resampling.nearest,
                    _terrain_tags(spec, "canonical clockwise D8 direction")
                    | {"direction_codes": ("0 drainage, 1-8 N NE E SE S SW W NW")},
                )
            )
        with RasterAssembler(
            raster_root,
            grid,
            terrain_specs,
            compression_threads=min(spec.workers, 4),
        ) as terrain_assembler:

            def reduce_terrain(result):
                started = time.perf_counter()
                for patch in result.value.patches:
                    terrain_assembler.write(
                        RasterPatch("hand", patch.window, patch.hand, patch.valid)
                    )
                    terrain_assembler.write(
                        RasterPatch("ltnd", patch.window, patch.ltnd, patch.valid)
                    )
                    if patch.direction is not None:
                        terrain_assembler.write(
                            RasterPatch(
                                "flow_direction",
                                patch.window,
                                patch.direction,
                                patch.valid,
                            )
                        )
                return {"output_write": time.perf_counter() - started}

            terrain_report = LocalExecutor(config).run(
                terrain_items,
                _terrain_worker,
                reduce_terrain,
                checkpoint=terrain_checkpoint,
            )
            started = time.perf_counter()
            terrain_paths = terrain_assembler.finish()
            compression_seconds += time.perf_counter() - started

        paths = domain_paths | terrain_paths
        manifest = _terrain_manifest(spec, grid, mini_identity, mini_index_path, paths)
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        TerrainDataset(staging, manifest).validate()
        expected = ["manifest.json", "mini_index.parquet"] + [
            str(path.relative_to(staging)) for path in paths.values()
        ]
        publisher.publish(tuple(expected))

    if domain_checkpoint is not None:
        domain_checkpoint.cleanup()
    if terrain_checkpoint is not None:
        terrain_checkpoint.cleanup()
    if checkpoint_root is not None:
        try:
            checkpoint_root.rmdir()
        except OSError:
            pass

    diagnostics = tuple(terrain_report.worker_diagnostics)
    owned_cells = sum(int(value.get("owned_cells", 0)) for value in diagnostics)
    drainage_cells = sum(int(value.get("drainage_cells", 0)) for value in diagnostics)
    negative_cells = sum(
        int(value.get("negative_hand_cells", 0)) for value in diagnostics
    )
    negative_mins = [
        float(value["negative_hand_min"])
        for value in diagnostics
        if value.get("negative_hand_min") is not None
    ]
    negative_maxs = [
        float(value["negative_hand_max"])
        for value in diagnostics
        if value.get("negative_hand_max") is not None
    ]
    timings = _terrain_timings(
        planning_seconds,
        compression_seconds,
        domain_report,
        terrain_report,
        overall_started,
    )
    return TerrainReport(
        Path(spec.output_dir),
        Path(spec.output_dir) / "manifest.json",
        len(mini_units),
        owned_cells,
        drainage_cells,
        negative_cells,
        min(negative_mins) if negative_mins else None,
        max(negative_maxs) if negative_maxs else None,
        spec.direction_source,
        domain_report,
        terrain_report,
        timings,
    )


def _validate_terrain_spec(spec: TerrainSpec) -> None:
    for name, path in (("prepared", spec.prepared), ("minis", spec.minis)):
        if not Path(path).is_dir():
            raise TerrainProductsError(f"{name} input is not a directory: {path}")
    if spec.direction_source not in {"dem", "d8"}:
        raise TerrainProductsError("direction source must be 'dem' or 'd8'")
    for name, value in (
        ("workers", spec.workers),
        ("memory limit", spec.memory_limit_mb),
        ("I/O slots", spec.io_slots),
        ("batch size", spec.batch_size),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise TerrainProductsError(f"{name} must be a positive integer")
    if spec.workers > 4:
        raise TerrainProductsError("workers cannot exceed four")
    output = Path(spec.output_dir)
    if output.exists():
        raise TerrainProductsError(f"Output directory already exists: {output}")
    if spec.checkpoint_dir is not None:
        checkpoint = Path(spec.checkpoint_dir).resolve()
        try:
            checkpoint.relative_to(output.resolve())
        except ValueError:
            pass
        else:
            raise TerrainProductsError(
                "Checkpoint directory cannot be inside the output directory"
            )
    _validate_agree_parameters(spec.agree_sharp, spec.agree_smooth, spec.agree_buffer)
    if spec.direction_source == "d8":
        prepared = PreparedDataset.open(spec.prepared)
        prepared.validate()
        asset = prepared.manifest.get("assets", {}).get("rasters", {}).get("d8")
        if not isinstance(asset, dict):
            raise TerrainProductsError("Prepared dataset has no D8 raster")
        if asset.get("encoding") != "canonical-clockwise":
            raise TerrainProductsError("Prepared D8 raster has an unsupported encoding")


def _plan_minis(
    spec: TerrainSpec, grid: GridSpec
) -> tuple[tuple[_MiniUnit, ...], pd.DataFrame, dict[str, Any]]:
    root = Path(spec.minis)
    catchment_path = root / "mini_catchments.fgb"
    segment_path = root / "mini_segments.fgb"
    providers = {
        "catchments": inspect_vector_provider(catchment_path),
        "segments": inspect_vector_provider(segment_path),
    }
    for name, provider in providers.items():
        if provider.driver != "FlatGeobuf":
            raise TerrainProductsError(f"Mini {name} must be FlatGeobuf")
        if list(provider.fields) != INPUT_COLUMNS[:-1]:
            raise TerrainProductsError(f"Mini {name} schema is invalid")
        if provider.crs != grid.crs:
            raise TerrainProductsError(f"Mini {name} does not use the canonical CRS")
    if providers["catchments"].geometry_type not in {"Polygon", "MultiPolygon"}:
        raise TerrainProductsError("Mini catchments must contain polygon geometry")
    if providers["segments"].geometry_type not in {
        "LineString",
        "MultiLineString",
    }:
        raise TerrainProductsError("Mini segments must contain line geometry")
    catchment_fids = scan_id_fids(
        providers["catchments"], "id", batch_size=spec.batch_size
    )
    segment_fids = scan_id_fids(providers["segments"], "id", batch_size=spec.batch_size)
    if not catchment_fids or any(_is_missing_id(value) for value in catchment_fids):
        raise TerrainProductsError("Mini catchment IDs must be non-null")
    if any(_is_missing_id(value) for value in segment_fids):
        raise TerrainProductsError("Mini segment IDs must be non-null")
    if set(catchment_fids) != set(segment_fids):
        raise TerrainProductsError("Mini catchment and segment IDs do not match")
    if len(catchment_fids) > np.iinfo(np.int32).max:
        raise TerrainProductsError("Too many minis for int32 ownership labels")
    try:
        bound_fids, raw_bounds = pyogrio.read_bounds(catchment_path)
    except Exception as exc:
        raise TerrainProductsError("Cannot read indexed mini bounds") from exc
    bounds_by_fid = {
        int(fid): tuple(float(raw_bounds[row, index]) for row in range(4))
        for index, fid in enumerate(bound_fids)
    }
    ordered_ids = sorted(catchment_fids, key=_mini_id_sort_key)
    labels_by_key = {
        f"mini-{label:010d}": (label, mini_id)
        for label, mini_id in enumerate(ordered_ids, start=1)
    }
    try:
        unit_bounds = [
            (
                key,
                bounds_by_fid[catchment_fids[mini_id]],
            )
            for key, (_, mini_id) in labels_by_key.items()
        ]
    except KeyError as exc:
        raise TerrainProductsError("Mini bounds do not match provider FIDs") from exc
    planned = plan_raster_units(
        grid,
        unit_bounds,
        bytes_per_cell=DOMAIN_BYTES_PER_CELL,
        fixed_bytes=TASK_FIXED_BYTES,
    )
    result = tuple(
        _MiniUnit(
            raster,
            labels_by_key[raster.key][0],
            labels_by_key[raster.key][1],
            catchment_fids[labels_by_key[raster.key][1]],
            segment_fids[labels_by_key[raster.key][1]],
        )
        for raster in planned
    )
    mini_index = pd.DataFrame(
        {
            "mini_label": np.arange(1, len(ordered_ids) + 1, dtype="int32"),
            "mini_id": ordered_ids,
        }
    )
    identity = {
        "catchments": _file_identity(catchment_path),
        "segments": _file_identity(segment_path),
    }
    return result, mini_index, identity


def _reestimated_units(
    units: tuple[_MiniUnit, ...], bytes_per_cell: int
) -> tuple[RasterUnit, ...]:
    return tuple(
        RasterUnit(
            unit.raster.key,
            unit.raster.bounds,
            unit.raster.window,
            int(unit.raster.window.width * unit.raster.window.height) * bytes_per_cell
            + TASK_FIXED_BYTES,
            unit.raster.spatial_key,
        )
        for unit in units
    )


def _packet_units(
    units: tuple[_MiniUnit, ...],
    bytes_per_cell: int,
    memory_limit_bytes: int,
) -> tuple[tuple[RasterPacket, tuple[_MiniUnit, ...]], ...]:
    packets = packet_raster_units(
        _reestimated_units(units, bytes_per_cell),
        memory_limit_bytes=memory_limit_bytes,
        max_units=MAX_PACKET_UNITS,
    )
    by_key = {unit.raster.key: unit for unit in units}
    return tuple(
        (packet, tuple(by_key[value.key] for value in packet.units))
        for packet in packets
    )


def _domain_work_items(
    units: tuple[_MiniUnit, ...],
    spec: TerrainSpec,
    grid: GridSpec,
    memory_limit_bytes: int,
) -> tuple[WorkItem[_DomainPayload], ...]:
    result = []
    for ordinal, (packet, packet_units) in enumerate(
        _packet_units(units, DOMAIN_BYTES_PER_CELL, memory_limit_bytes)
    ):
        result.append(
            WorkItem(
                packet.key,
                ordinal,
                packet.estimated_bytes,
                _DomainPayload(
                    grid,
                    Path(spec.minis) / "mini_catchments.fgb",
                    Path(spec.minis) / "mini_segments.fgb",
                    packet_units,
                ),
            )
        )
    return tuple(result)


def _terrain_work_items(
    units: tuple[_MiniUnit, ...],
    spec: TerrainSpec,
    grid: GridSpec,
    ownership: Path,
    drainage: Path,
    memory_limit_bytes: int,
) -> tuple[WorkItem[_TerrainPayload], ...]:
    bytes_per_cell = (
        DEM_BYTES_PER_CELL if spec.direction_source == "dem" else D8_BYTES_PER_CELL
    )
    result = []
    for ordinal, (packet, packet_units) in enumerate(
        _packet_units(units, bytes_per_cell, memory_limit_bytes)
    ):
        result.append(
            WorkItem(
                packet.key,
                ordinal,
                packet.estimated_bytes,
                _TerrainPayload(
                    Path(spec.prepared),
                    grid,
                    ownership,
                    drainage,
                    packet_units,
                    spec.direction_source,
                    spec.write_flow_direction,
                    spec.agree_sharp,
                    spec.agree_smooth,
                    spec.agree_buffer,
                    _gdal_cache_bytes(memory_limit_bytes, spec.workers),
                ),
            )
        )
    return tuple(result)


def _domain_worker(
    payload: _DomainPayload, context: WorkerContext
) -> WorkerOutput[_PacketValue]:
    started = time.perf_counter()
    with context.io_bound():
        catchments = pyogrio.read_dataframe(
            payload.catchments,
            columns=["id"],
            fids=[unit.catchment_fid for unit in payload.units],
            use_arrow=True,
        )
    with context.io_bound():
        segments = pyogrio.read_dataframe(
            payload.segments,
            columns=["id"],
            fids=[unit.segment_fid for unit in payload.units],
            use_arrow=True,
        )
    vector_seconds = time.perf_counter() - started
    catchment_by_id = dict(zip(catchments["id"], catchments.geometry, strict=True))
    segment_by_id = dict(zip(segments["id"], segments.geometry, strict=True))
    patches = []
    raster_started = time.perf_counter()
    owned_count = drainage_count = 0
    for unit in payload.units:
        try:
            catchment = catchment_by_id[unit.mini_id]
            segment = segment_by_id[unit.mini_id]
        except KeyError as exc:
            raise TerrainProductsError(
                f"Cannot read geometry for mini {unit.mini_id}"
            ) from exc
        if (
            catchment is None
            or segment is None
            or catchment.is_empty
            or segment.is_empty
            or not catchment.is_valid
            or not segment.is_valid
        ):
            raise TerrainProductsError(f"Mini {unit.mini_id} has invalid geometry")
        window = unit.raster.window
        shape = (int(window.height), int(window.width))
        transform = rasterio.windows.transform(window, payload.grid.transform)
        valid = rasterize(
            [(catchment, 1)],
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype="uint8",
            all_touched=False,
        ).astype(bool)
        if not np.any(valid):
            raise TerrainProductsError(
                f"Mini {unit.mini_id} owns no canonical-grid cells"
            )
        drainage = (
            rasterize(
                [(segment, 1)],
                out_shape=shape,
                transform=transform,
                fill=0,
                dtype="uint8",
                all_touched=True,
            ).astype(bool)
            & valid
        )
        if not np.any(drainage):
            raise TerrainProductsError(
                f"Mini {unit.mini_id} has no matching drainage cells"
            )
        patches.append(
            _DomainPatch(
                window,
                valid,
                np.full(shape, unit.mini_label, dtype="int32"),
                drainage.astype("uint8", copy=False),
            )
        )
        owned_count += int(valid.sum())
        drainage_count += int(drainage.sum())
    return WorkerOutput(
        _PacketValue(tuple(patches)),
        {
            "vector_read": vector_seconds,
            "rasterization": time.perf_counter() - raster_started,
        },
        {"owned_cells": owned_count, "drainage_cells": drainage_count},
    )


def _terrain_worker(
    payload: _TerrainPayload, context: WorkerContext
) -> WorkerOutput[_PacketValue]:
    previous = get_gdal_config("GDAL_CACHEMAX")
    set_gdal_config("GDAL_CACHEMAX", payload.gdal_cache_bytes)
    try:
        return _terrain_worker_with_cache(payload, context)
    finally:
        set_gdal_config("GDAL_CACHEMAX", previous)


def _terrain_worker_with_cache(
    payload: _TerrainPayload, context: WorkerContext
) -> WorkerOutput[_PacketValue]:
    jit_started = time.perf_counter()
    context.resources.get("terrain-numba-kernels-v1", _warm_worker_kernels)
    jit_seconds = time.perf_counter() - jit_started
    prepared_reader = context.resources.get(
        f"terrain-prepared:{Path(payload.prepared).resolve()}",
        lambda: PreparedRasterReader(payload.prepared, context),
    )
    aligned_reader = context.resources.get(
        f"terrain-domain:{Path(payload.ownership).resolve()}",
        lambda: AlignedRasterReader(
            payload.grid,
            {"ownership": payload.ownership, "drainage": payload.drainage},
            context,
        ),
    )
    timings = {
        "jit_initialization": jit_seconds,
        "raster_read": 0.0,
        "conditioning": 0.0,
        "d8_validation": 0.0,
        "routing": 0.0,
        "products": 0.0,
    }
    patches = []
    owned_count = drainage_count = negative_count = 0
    negative_min = negative_max = None
    for unit in payload.units:
        window = unit.raster.window
        read_started = time.perf_counter()
        ownership = aligned_reader.read("ownership", window)
        drainage_values = aligned_reader.read("drainage", window)
        dem = prepared_reader.read("dem", window)
        d8 = (
            prepared_reader.read("d8", window)
            if payload.direction_source == "d8"
            else None
        )
        timings["raster_read"] += time.perf_counter() - read_started
        owned = (~np.ma.getmaskarray(ownership)) & (
            np.asarray(ownership.data) == unit.mini_label
        )
        if not np.any(owned):
            raise TerrainProductsError(
                f"Rasterized mini {unit.mini_id} has no owned cells"
            )
        if np.any(np.ma.getmaskarray(drainage_values)[owned]):
            raise TerrainProductsError("Drainage mask does not cover mini ownership")
        drainage = owned & (np.asarray(drainage_values.data) != 0)
        if not np.any(drainage):
            raise TerrainProductsError(
                f"Rasterized mini {unit.mini_id} has no drainage"
            )
        elevation = np.asarray(dem.filled(np.nan), dtype=np.float64)
        del ownership, drainage_values, dem
        if np.any(~np.isfinite(elevation[owned])):
            raise TerrainProductsError(
                f"DEM contains nodata within mini {unit.mini_id}"
            )
        labels = np.full(owned.shape, -1, dtype="int32")
        labels[owned] = 0
        transform = rasterio.windows.transform(window, payload.grid.transform)
        if payload.direction_source == "dem":
            conditioning_started = time.perf_counter()
            routing_elevation = _agree_condition_dem(
                elevation,
                labels,
                drainage,
                sharp=payload.agree_sharp,
                smooth=payload.agree_smooth,
                buffer=payload.agree_buffer,
            )
            timings["conditioning"] += time.perf_counter() - conditioning_started
            routing_started = time.perf_counter()
            direction, rank = compute_flow_directions(
                routing_elevation, labels, drainage, transform
            )
            timings["routing"] += time.perf_counter() - routing_started
            del routing_elevation
        else:
            validation_started = time.perf_counter()
            direction, rank = _validated_d8(d8, owned, drainage, unit.mini_id)
            del d8
            timings["d8_validation"] += time.perf_counter() - validation_started
        product_started = time.perf_counter()
        hand, ltnd = _terrain_products_float32(elevation, direction, rank, transform)
        del elevation, labels, rank
        timings["products"] += time.perf_counter() - product_started
        negatives = hand[owned & (hand < 0)]
        if negatives.size:
            value_min, value_max = float(negatives.min()), float(negatives.max())
            negative_min = (
                value_min if negative_min is None else min(negative_min, value_min)
            )
            negative_max = (
                value_max if negative_max is None else max(negative_max, value_max)
            )
        direction_output = None
        if payload.write_flow_direction:
            direction_output = np.where(owned, direction, 0).astype("uint8")
        patches.append(_TerrainPatch(window, owned, hand, ltnd, direction_output))
        owned_count += int(owned.sum())
        drainage_count += int(drainage.sum())
        negative_count += int(negatives.size)
    return WorkerOutput(
        _PacketValue(tuple(patches)),
        timings,
        {
            "owned_cells": owned_count,
            "drainage_cells": drainage_count,
            "negative_hand_cells": negative_count,
            "negative_hand_min": negative_min,
            "negative_hand_max": negative_max,
        },
    )


def _warm_worker_kernels() -> bool:
    _warm_routing_kernels()
    return True


def _validated_d8(
    values: np.ma.MaskedArray | None,
    owned: np.ndarray,
    drainage: np.ndarray,
    mini_id: Any,
) -> tuple[np.ndarray, np.ndarray]:
    if values is None:
        raise TerrainProductsError("Prepared D8 values were not loaded")
    if np.any(np.ma.getmaskarray(values)[owned]):
        raise TerrainProductsError(f"D8 contains nodata within mini {mini_id}")
    raw = np.asarray(values.data)
    if np.any((raw[owned] > 8) | (raw[owned] < 0)):
        raise TerrainProductsError(f"D8 contains invalid codes within mini {mini_id}")
    if np.any(owned & ~drainage & (raw == 0)):
        raise TerrainProductsError(
            f"D8 has non-drainage terminals within mini {mini_id}"
        )
    direction = np.full(owned.shape, -1, dtype="int8")
    direction[owned] = raw[owned].astype("int8", copy=False)
    direction[drainage] = 0
    try:
        rank, _ = _rank_and_terminal(direction)
    except ValueError as exc:
        raise TerrainProductsError(
            f"Invalid D8 routing in mini {mini_id}: {exc}"
        ) from exc
    if np.any(rank[owned] < 0):
        raise TerrainProductsError(f"D8 does not terminate within mini {mini_id}")
    return direction, rank


def _terrain_products_float32(
    elevation: np.ndarray,
    direction: np.ndarray,
    rank: np.ndarray,
    transform: Affine,
) -> tuple[np.ndarray, np.ndarray]:
    order = _rank_order(rank)
    hand = _hand_kernel(elevation, direction, order).astype("float32")
    width, height = _pixel_sizes(transform)
    ltnd = _ltnd_kernel(direction, order, width, height).astype("float32")
    return hand, ltnd


def _checkpoint(
    root: Path | None,
    algorithm: str,
    prepared_manifest: dict[str, Any],
    parameters: dict[str, Any],
    items: tuple[WorkItem[Any], ...],
) -> CheckpointStore[Any] | None:
    if root is None:
        return None
    fingerprint = execution_fingerprint(
        algorithm=algorithm,
        version="1",
        prepared_manifest=prepared_manifest,
        parameters=parameters,
        work_items=items,
    )
    return CheckpointStore(root, fingerprint, _PickleCheckpointCodec())


def _terrain_tags(spec: TerrainSpec, role: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": role,
        "routing_source": spec.direction_source,
        "ownership": "strict aggregated mini catchments; no buffer",
    }
    if spec.direction_source == "dem":
        result.update(
            agree_sharp=spec.agree_sharp,
            agree_smooth=spec.agree_smooth,
            agree_buffer_pixels=spec.agree_buffer,
        )
    return result


def _terrain_manifest(
    spec: TerrainSpec,
    grid: GridSpec,
    mini_identity: dict[str, Any],
    mini_index: Path,
    rasters: dict[str, Path],
) -> dict[str, Any]:
    assets: dict[str, Any] = {
        "mini_index": {
            "path": mini_index.name,
            "driver": "Parquet",
            "role": "dense mini-label index",
        }
    }
    roles = {
        "mini_ownership": "mini-catchment ownership",
        "drainage": "matching mini drainage",
        "hand": "height above matching drainage",
        "ltnd": "along-route distance to matching drainage",
        "flow_direction": "canonical clockwise D8 direction",
    }
    output_root = mini_index.parent
    for name, path in rasters.items():
        with rasterio.open(path) as source:
            asset = {
                "path": path.relative_to(output_root).as_posix(),
                "driver": "COG",
                "role": roles[name],
                "dtype": source.dtypes[0],
                "nodata": "internal-mask",
                "block_size": BLOCK_SIZE,
                "overviews": source.overviews(1),
            }
        if name == "flow_direction":
            asset["encoding"] = "canonical-clockwise"
        assets[name] = asset
    return {
        "contract": TERRAIN_CONTRACT,
        "version": TERRAIN_CONTRACT_VERSION,
        "producer": _producer_version(),
        "grid": grid.to_manifest(),
        "inputs": {
            "prepared": str(Path(spec.prepared).resolve()),
            "minis": str(Path(spec.minis).resolve()),
            "mini_assets": mini_identity,
        },
        "routing": {
            "source": spec.direction_source,
            "agree": (
                {
                    "sharp": spec.agree_sharp,
                    "smooth": spec.agree_smooth,
                    "buffer_pixels": spec.agree_buffer,
                }
                if spec.direction_source == "dem"
                else None
            ),
            "ownership_buffer_cells": 0,
        },
        "assets": assets,
    }


def _terrain_timings(
    planning_seconds: float,
    compression_seconds: float,
    domain_report: ExecutionReport,
    terrain_report: ExecutionReport,
    overall_started: float,
) -> dict[str, float]:
    return {
        "planning": planning_seconds,
        "domain_execution": domain_report.wall_seconds,
        "terrain_execution": terrain_report.wall_seconds,
        "vector_read": domain_report.timings.get("vector_read", 0.0),
        "rasterization": domain_report.timings.get("rasterization", 0.0),
        "raster_read": terrain_report.timings.get("raster_read", 0.0),
        "jit_initialization": terrain_report.timings.get("jit_initialization", 0.0),
        "conditioning": terrain_report.timings.get("conditioning", 0.0),
        "d8_validation": terrain_report.timings.get("d8_validation", 0.0),
        "routing": terrain_report.timings.get("routing", 0.0),
        "products": terrain_report.timings.get("products", 0.0),
        "coordination": domain_report.timings.get("coordination", 0.0)
        + terrain_report.timings.get("coordination", 0.0),
        "checkpoint": domain_report.timings.get("checkpoint_write", 0.0)
        + terrain_report.timings.get("checkpoint_write", 0.0),
        "output_write": domain_report.timings.get("output_write", 0.0)
        + terrain_report.timings.get("output_write", 0.0),
        "compression": compression_seconds,
        "total": time.perf_counter() - overall_started,
    }


def _gdal_cache_bytes(memory_limit_bytes: int, workers: int) -> int:
    """Reserve a small bounded block cache within the task-memory envelope."""
    return min(
        64 * 1024 * 1024,
        max(8 * 1024 * 1024, memory_limit_bytes // max(8, workers * 8)),
    )


def _file_identity(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise TerrainProductsError(f"Cannot inspect mini asset: {path}") from exc
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _is_missing_id(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _mini_id_sort_key(value: Any) -> tuple[str, Any]:
    """Provide deterministic natural ordering for provider scalar ID types."""
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return ("integer", int(value))
    if isinstance(value, (float, np.floating)):
        return ("real", float(value))
    if isinstance(value, str):
        return ("text", value)
    if isinstance(value, bytes):
        return ("bytes", value)
    kind = f"{type(value).__module__}.{type(value).__qualname__}"
    return (kind, repr(value))


def _producer_version() -> str:
    try:
        return importlib.metadata.version("mgb-vec-hydro")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"
