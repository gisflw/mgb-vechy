"""Raster-only staging of canonical prepared datasets."""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import rasterio
import pandas as pd
import pyarrow as pa
import pyogrio
from pyproj import CRS
from numba import njit
from rasterio.env import get_gdal_config, set_gdal_config
from rasterio.enums import MaskFlags, MergeAlg, Resampling
from rasterio.shutil import copy as copy_raster
from rasterio.transform import Affine
from rasterio.features import rasterize
from rasterio.windows import from_bounds, Window
import shapely

from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.execution.vector import geometry_column_name, read_vector_table

BLOCK_SIZE = 512
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
RESERVED_RASTER_NAMES = {"dem", "d8", "mini_ownership", "drainage"}


@dataclass(frozen=True)
class NamedRaster:
    """A named single-band raster and its scientific sampling role."""

    name: str
    path: Path
    kind: Literal["continuous", "categorical"]


@dataclass(frozen=True)
class PreparationSpec:
    """Raster inputs and normalization choices for a prepared dataset."""

    dem: Path
    output_dir: Path
    mini_catchments: Path
    mini_segments: Path
    rasters: tuple[NamedRaster, ...] = field(default_factory=tuple)
    d8: Path | None = None
    d8_encoding: Literal["canonical", "esri"] | None = None
    memory_limit_mb: int = 512
    buffer_cells: int = 1


@dataclass(frozen=True)
class PreparationReport:
    """Summary of a successfully published prepared dataset."""

    output_dir: Path
    dem: Path
    rasters: dict[str, Path]
    mini_ownership: Path
    drainage: Path
    mini_index: Path
    raster_count: int
    timings: dict[str, float]

    @property
    def d8(self) -> Path | None:
        """Return the prepared D8 path when one was requested."""

        return self.rasters.get("d8")

    @property
    def files(self) -> tuple[Path, ...]:
        """Return all published files in deterministic name order."""

        return tuple(
            [self.rasters[name] for name in sorted(self.rasters)]
            + [self.mini_ownership, self.drainage, self.mini_index]
        )


@dataclass(frozen=True)
class GridSpec:
    """Canonical north-up raster grid."""

    crs: CRS
    transform: Affine
    width: int
    height: int

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        left = self.transform.c
        top = self.transform.f
        right = left + self.width * self.transform.a
        bottom = top + self.height * self.transform.e
        return (left, bottom, right, top)

def prepare_dataset(spec: PreparationSpec) -> PreparationReport:
    """Create a prepared dataset with a bounded GDAL block cache."""
    previous = get_gdal_config("GDAL_CACHEMAX")
    cache_bytes = min(
        64 * 1024 * 1024,
        max(8 * 1024 * 1024, spec.memory_limit_mb * 1024 * 1024 // 8),
    )
    set_gdal_config("GDAL_CACHEMAX", cache_bytes)
    try:
        return _prepare_dataset(spec)
    finally:
        set_gdal_config("GDAL_CACHEMAX", previous)


def _prepare_dataset(spec: PreparationSpec) -> PreparationReport:
    """Create and atomically publish one prepared dataset."""
    overall_started = time.perf_counter()
    _validate_spec(spec)
    output = Path(spec.output_dir)
    if output.exists():
        raise PreparedDataError(f"Output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        phase_started = time.perf_counter()
        mini_catchment_vector, _mini_segment_vector = _read_mini_inputs(
            Path(spec.mini_catchments), Path(spec.mini_segments)
        )
        target_crs = mini_catchment_vector.crs
        with rasterio.open(spec.dem) as dem:
            _require_source_grid(dem, target_crs, "DEM")
            domain = _union_vector_geometries(Path(spec.mini_catchments))
            if domain.is_empty:
                raise PreparedDataError("Mini-catchment domain is empty")
            buffered_domain = domain.buffer(spec.buffer_cells * abs(dem.transform.a))
            window = (
                from_bounds(*buffered_domain.bounds, transform=dem.transform)
                .round_offsets()
                .round_lengths()
            )
            if (
                window.col_off < 0
                or window.row_off < 0
                or window.col_off + window.width > dem.width
                or window.row_off + window.height > dem.height
            ):
                raise PreparedDataError(
                    "DEM does not cover the buffered mini-catchment domain"
                )
            grid = GridSpec(
                target_crs,
                rasterio.windows.transform(window, dem.transform),
                int(window.width),
                int(window.height),
            )
            mask = rasterize(
                [(buffered_domain, 1)],
                out_shape=(grid.height, grid.width),
                transform=grid.transform,
                fill=0,
                dtype="uint8",
            ).astype(bool)
        grid_domain_seconds = time.perf_counter() - phase_started

        phase_started = time.perf_counter()
        raster_paths: dict[str, Path] = {}
        raster_kinds: dict[str, str] = {"dem": "continuous"}
        dem_path = staging / "dem.tif"
        _prepare_clipped_raster(spec.dem, dem_path, grid, window, mask, "continuous")
        raster_paths["dem"] = dem_path
        for item in sorted(spec.rasters, key=lambda value: value.name):
            target = staging / f"{item.name}.tif"
            _prepare_clipped_raster(item.path, target, grid, window, mask, item.kind)
            raster_paths[item.name] = target
            raster_kinds[item.name] = item.kind
        if spec.d8 is not None:
            target = staging / "d8.tif"
            _prepare_clipped_d8(
                spec.d8, target, grid, window, mask, spec.d8_encoding or "canonical"
            )
            raster_paths["d8"] = target
            raster_kinds["d8"] = "d8"
        raster_preparation_seconds = time.perf_counter() - phase_started

        phase_started = time.perf_counter()
        domain_assets, mini_index = _prepare_domain_rasters(
            staging,
            grid,
            Path(spec.mini_catchments),
            Path(spec.mini_segments),
            memory_limit_bytes=spec.memory_limit_mb * 1024 * 1024,
        )
        domain_rasterization_seconds = time.perf_counter() - phase_started
        phase_started = time.perf_counter()
        output_paths = raster_paths | domain_assets
        _validate_prepared_outputs(
            output_paths,
            mini_index,
            grid,
            raster_kinds=raster_kinds,
        )
        _validate_flat_staging(
            staging,
            tuple(path.name for path in output_paths.values()) + (mini_index.name,),
        )
        os.replace(staging, output)
        validation_publication_seconds = time.perf_counter() - phase_started
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return PreparationReport(
        output_dir=output,
        dem=output / "dem.tif",
        rasters={name: output / path.name for name, path in raster_paths.items()},
        mini_ownership=output / "mini_ownership.tif",
        drainage=output / "drainage.tif",
        mini_index=output / "mini_index.parquet",
        raster_count=len(raster_paths),
        timings={
            "grid_domain_setup": grid_domain_seconds,
            "raster_preparation": raster_preparation_seconds,
            "domain_rasterization": domain_rasterization_seconds,
            "validation_publication": validation_publication_seconds,
            "total": time.perf_counter() - overall_started,
        },
    )


def _validate_spec(spec: PreparationSpec) -> None:
    if not Path(spec.dem).is_file():
        raise PreparedDataError(f"DEM input is not a local file: {spec.dem}")
    if spec.memory_limit_mb <= 0:
        raise PreparedDataError("Memory limit must be positive")
    if spec.buffer_cells < 0:
        raise PreparedDataError("buffer-cells must be non-negative")
    for name, path in (
        ("mini-catchments", spec.mini_catchments),
        ("mini-segments", spec.mini_segments),
    ):
        if not Path(path).is_file():
            raise PreparedDataError(f"{name} input is not a local file: {path}")
    names: set[str] = set()
    for item in spec.rasters:
        if not NAME_RE.fullmatch(item.name) or item.name in RESERVED_RASTER_NAMES:
            raise PreparedDataError(f"Invalid or reserved raster name: {item.name}")
        if item.name in names:
            raise PreparedDataError(f"Duplicate raster name: {item.name}")
        names.add(item.name)
        if item.kind not in {"continuous", "categorical"}:
            raise PreparedDataError(f"Invalid raster kind for {item.name}: {item.kind}")
        if not Path(item.path).is_file():
            raise PreparedDataError(f"Raster input is not a local file: {item.path}")
    if (spec.d8 is None) != (spec.d8_encoding is None):
        raise PreparedDataError("--d8 and --d8-encoding must be supplied together")
    if spec.d8_encoding not in {None, "canonical", "esri"}:
        raise PreparedDataError(f"Unsupported D8 encoding: {spec.d8_encoding}")
    if spec.d8 is not None and not Path(spec.d8).is_file():
        raise PreparedDataError(f"D8 input is not a local file: {spec.d8}")


def _read_mini_inputs(catchments: Path, segments: Path):
    """Read and validate the explicit aggregated mini vector inputs."""

    try:
        catchment_vector = read_vector_table(catchments)
        segment_vector = read_vector_table(segments)
    except Exception as exc:
        raise PreparedDataError(
            "Cannot read explicit mini-catchment inputs"
        ) from exc
    expected_columns = [
        "id",
        "id_down",
        "sub",
        "strahler_order",
        "unit_length",
        "upstream_length",
        "unit_area",
        "upstream_area",
        "water_course",
        "geometry",
    ]
    for name, vector, allowed in (
        ("mini catchments", catchment_vector, {3, 6}),
        ("mini segments", segment_vector, {1, 5}),
    ):
        if list(vector.columns) != expected_columns:
            raise PreparedDataError(
                f"{name} must have the exact aggregated mini schema"
            )
        geometries = vector.geometries()
        if (
            np.any(shapely.is_missing(geometries))
            or np.any(shapely.is_empty(geometries))
            or not set(shapely.get_type_id(geometries).tolist()).issubset(allowed)
            or not np.all(shapely.is_valid(geometries))
        ):
            raise PreparedDataError(f"{name} contains invalid geometry")
        ids = vector.table["id"].to_pylist()
        if vector.table["id"].null_count or len(set(ids)) != len(ids):
            raise PreparedDataError(f"{name} IDs must be non-null and unique")
    if catchment_vector.crs != segment_vector.crs:
        raise PreparedDataError("Mini catchment and segment CRS values differ")
    if set(catchment_vector.table["id"].to_pylist()) != set(
        segment_vector.table["id"].to_pylist()
    ):
        raise PreparedDataError("Mini catchments and segments must contain matching IDs")
    return catchment_vector, segment_vector


def _require_source_grid(
    source, crs: CRS, name: str, grid: GridSpec | None = None
) -> None:
    if source.count != 1 or source.crs is None:
        raise PreparedDataError(f"{name} must be single-band and declare a CRS")
    if CRS.from_user_input(source.crs) != crs:
        raise PreparedDataError(f"{name} CRS does not match the authoritative CRS")
    transform = source.transform
    if transform.b != 0 or transform.d != 0 or transform.a <= 0 or transform.e >= 0:
        raise PreparedDataError(f"{name} must use a north-up raster grid")
    if grid is not None:
        if not (
            math.isclose(transform.a, grid.transform.a)
            and math.isclose(transform.e, grid.transform.e)
        ):
            raise PreparedDataError(f"{name} resolution does not match the DEM grid")
        col = (grid.transform.c - transform.c) / transform.a
        row = (grid.transform.f - transform.f) / transform.e
        if not (
            math.isclose(col, round(col), abs_tol=1e-7)
            and math.isclose(row, round(row), abs_tol=1e-7)
        ):
            raise PreparedDataError(f"{name} origin is not aligned to the DEM grid")
        if (
            grid.bounds[0] < source.bounds.left - 1e-7
            or grid.bounds[1] < source.bounds.bottom - 1e-7
            or grid.bounds[2] > source.bounds.right + 1e-7
            or grid.bounds[3] > source.bounds.top + 1e-7
        ):
            raise PreparedDataError(f"{name} does not cover the buffered ROI domain")


def _validate_prepared_outputs(
    paths: dict[str, Path],
    index_path: Path,
    grid: GridSpec,
    *,
    raster_kinds: dict[str, str],
) -> None:
    """Validate all direct prepared files before the atomic directory rename."""

    expected = set(raster_kinds) | {"mini_ownership", "drainage"}
    if set(paths) != expected:
        raise PreparedDataError("Prepared output file set is incomplete")
    for name, path in paths.items():
        if name == "mini_ownership":
            expected_dtype = "int32"
        elif name in {"drainage", "d8"}:
            expected_dtype = "uint8"
        elif raster_kinds[name] == "categorical":
            expected_dtype = "int32"
        else:
            expected_dtype = "float32"
        try:
            with rasterio.open(path) as source:
                if (
                    source.count != 1
                    or source.crs is None
                    or CRS.from_user_input(source.crs) != grid.crs
                    or source.transform != grid.transform
                    or source.shape != (grid.height, grid.width)
                    or source.nodata is not None
                    or source.dtypes[0] != expected_dtype
                    or source.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG"
                    or MaskFlags.per_dataset not in source.mask_flag_enums[0]
                ):
                    raise PreparedDataError(
                        f"Prepared raster {name} does not match the canonical COG grid"
                    )
        except PreparedDataError:
            raise
        except (OSError, rasterio.errors.RasterioError) as exc:
            raise PreparedDataError(f"Cannot inspect prepared raster: {name}") from exc
    _validate_mini_index(index_path, PreparedDataError)


def _validate_flat_staging(staging: Path, expected: tuple[str, ...]) -> None:
    """Reject packet, working, or nested files before prepared publication."""

    expected_paths = {staging / name for name in expected}
    actual_files = {path for path in staging.iterdir() if path.is_file()}
    nested = [path for path in staging.iterdir() if path.is_dir()]
    extras = sorted(path.name for path in actual_files - expected_paths)
    if nested or extras or actual_files != expected_paths:
        details = []
        if nested:
            details.append("nested directories: " + ", ".join(path.name for path in nested))
        if extras:
            details.append("unexpected files: " + ", ".join(extras))
        raise PreparedDataError(
            "Prepared staging is not a documented flat file set"
            + (" (" + "; ".join(details) + ")" if details else "")
        )


def _validate_mini_index(path: Path, error_type=PreparedDataError) -> pd.DataFrame:
    """Read and validate the one shared six-column mini index."""

    required = ["mini_label", "mini_id", "minx", "miny", "maxx", "maxy"]
    try:
        table = pd.read_parquet(path)
    except Exception as exc:
        raise error_type(f"Cannot read mini index: {path}") from exc
    if list(table.columns) != required or table.empty:
        raise error_type("Mini index schema is invalid")
    labels = table["mini_label"]
    if (
        labels.dtype != np.dtype("int32")
        or labels.duplicated().any()
        or table["mini_id"].isna().any()
        or table["mini_id"].duplicated().any()
        or not np.array_equal(
            labels.to_numpy(), np.arange(1, len(table) + 1, dtype="int32")
        )
    ):
        raise error_type("Mini index values are invalid")
    try:
        bounds = table[["minx", "miny", "maxx", "maxy"]].to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise error_type("Mini index bounds are invalid") from exc
    if (
        not np.isfinite(bounds).all()
        or np.any(bounds[:, 0] > bounds[:, 2])
        or np.any(bounds[:, 1] > bounds[:, 3])
    ):
        raise error_type("Mini index bounds are invalid")
    return table


def _prepare_clipped_raster(
    source: Path,
    output: Path,
    grid: GridSpec,
    window: Window,
    domain_mask: np.ndarray,
    kind: Literal["continuous", "categorical"],
) -> None:
    dtype = "float32" if kind == "continuous" else "int32"
    resampling = Resampling.bilinear if kind == "continuous" else Resampling.nearest
    intermediate = output.with_suffix(".working.tif")
    try:
        with (
            rasterio.open(source) as src,
            _working_raster(intermediate, grid, dtype) as dst,
        ):
            _require_source_grid(src, grid.crs, str(source), grid)
            source_window = (
                from_bounds(*grid.bounds, transform=src.transform)
                .round_offsets()
                .round_lengths()
            )
            values = src.read(1, window=source_window, masked=True)
            valid = ~np.ma.getmaskarray(values) & domain_mask
            raw = values.filled(0)
            if kind == "continuous":
                valid &= np.isfinite(raw)
                data = np.where(valid, raw, 0).astype(dtype)
            else:
                source_values = raw[valid]
                if source_values.size and (
                    not np.all(np.isfinite(source_values))
                    or not np.all(source_values == np.floor(source_values))
                ):
                    raise PreparedDataError(
                        f"Categorical raster {source} contains non-integral values"
                    )
                data = raw.astype(dtype)
            dst.write(data, 1)
            dst.write_mask(valid.astype("uint8") * 255)
        _to_cog(intermediate, output, resampling)
    finally:
        intermediate.unlink(missing_ok=True)


def _prepare_clipped_d8(
    source: Path,
    output: Path,
    grid: GridSpec,
    window: Window,
    domain_mask: np.ndarray,
    encoding: str,
) -> None:
    intermediate = output.with_suffix(".working.tif")
    esri = {0: 0, 1: 3, 2: 4, 4: 5, 8: 6, 16: 7, 32: 8, 64: 1, 128: 2}
    try:
        with (
            rasterio.open(source) as src,
            _working_raster(intermediate, grid, "uint8") as dst,
        ):
            _require_source_grid(src, grid.crs, "D8", grid)
            source_window = (
                from_bounds(*grid.bounds, transform=src.transform)
                .round_offsets()
                .round_lengths()
            )
            values = src.read(1, window=source_window, masked=True)
            valid = ~np.ma.getmaskarray(values) & domain_mask
            raw = values.filled(0)
            allowed = set(range(9)) if encoding == "canonical" else set(esri)
            unknown = set(np.unique(raw[valid]).tolist()) - allowed
            if unknown:
                raise PreparedDataError(
                    "D8 raster contains invalid code(s): "
                    + ", ".join(map(str, sorted(unknown)))
                )
            data = (
                raw.astype("uint8")
                if encoding == "canonical"
                else np.vectorize(lambda value: esri.get(value, 0), otypes=["uint8"])(
                    raw
                )
            )
            dst.write(data, 1)
            dst.write_mask(valid.astype("uint8") * 255)
        _to_cog(intermediate, output, Resampling.nearest)
    finally:
        intermediate.unlink(missing_ok=True)


def _prepare_domain_rasters(
    staging: Path,
    grid: GridSpec,
    catchment_path: Path,
    segment_path: Path,
    *,
    memory_limit_bytes: int,
) -> tuple[dict[str, Path], Path]:
    """Jointly rasterize minis by canonical block, then normalize connectivity."""
    from mgb_vec_hydro.execution.raster import (
        RasterAssembler,
        RasterPatch,
        RasterProductSpec,
    )

    catchment_vector = read_vector_table(catchment_path)
    segment_vector = read_vector_table(segment_path)
    catchment_ids = catchment_vector.table["id"].to_pylist()
    segment_ids = segment_vector.table["id"].to_pylist()
    catchments_by_id = dict(
        zip(catchment_ids, catchment_vector.geometries(), strict=True)
    )
    segments_by_id = dict(zip(segment_ids, segment_vector.geometries(), strict=True))
    if set(catchments_by_id) != set(segments_by_id):
        raise PreparedDataError("Mini catchments and segments do not have matching IDs")
    ordered = sorted(
        catchments_by_id, key=lambda value: (type(value).__name__, str(value))
    )
    for mini_id in ordered:
        catchment = catchments_by_id[mini_id]
        segment = segments_by_id[mini_id]
        if (
            catchment is None
            or segment is None
            or catchment.is_empty
            or segment.is_empty
        ):
            raise PreparedDataError(f"Mini {mini_id} has invalid geometry")
    catchments = np.asarray([catchments_by_id[value] for value in ordered], dtype=object)
    segments = np.asarray([segments_by_id[value] for value in ordered], dtype=object)
    dense_labels = np.arange(1, len(ordered) + 1, dtype="int32")
    catchment_index = shapely.STRtree(catchments)
    segment_index = shapely.STRtree(segments)

    raster_root = staging
    correction_root = staging / ".ownership-corrections"
    correction_root.mkdir()
    try:
        with RasterAssembler(
            raster_root,
            grid,
            (
                RasterProductSpec("mini_ownership", "int32"),
                RasterProductSpec("drainage", "uint8"),
            ),
            working_compression=None,
            compression_threads=4,
        ) as assembler:
            connectivity = _BlockConnectivity(grid.width)
            for row in range(0, grid.height, BLOCK_SIZE):
                connectivity.start_row(row)
                for col in range(0, grid.width, BLOCK_SIZE):
                    win = Window(
                        col,
                        row,
                        min(BLOCK_SIZE, grid.width - col),
                        min(BLOCK_SIZE, grid.height - row),
                    )
                    transform = rasterio.windows.transform(win, grid.transform)
                    block_bounds = rasterio.windows.bounds(win, grid.transform)
                    block_geometry = shapely.box(*block_bounds)
                    catchment_hits = np.sort(catchment_index.query(block_geometry))
                    ownership, valid = _rasterize_ownership_block(
                        catchments[catchment_hits],
                        dense_labels[catchment_hits],
                        (int(win.height), int(win.width)),
                        transform,
                    )
                    if not np.any(valid):
                        continue
                    drainage = _rasterize_drainage_block(
                        segments,
                        dense_labels,
                        np.sort(segment_index.query(block_geometry)),
                        ownership,
                        valid,
                        transform,
                    )
                    connectivity.add_block(
                        row, col, ownership, valid, drainage.astype(bool)
                    )
                    assembler.write_block(
                        RasterPatch("mini_ownership", win, ownership, valid)
                    )
                    assembler.write_block(
                        RasterPatch("drainage", win, drainage, valid)
                    )

            disconnected_labels = connectivity.disconnected_labels(dense_labels)
            correction_paths = []
            for label in disconnected_labels:
                index = int(label) - 1
                mini_id = ordered[index]
                correction = _plan_connectivity_correction(
                    assembler,
                    grid,
                    catchments[index],
                    int(label),
                    mini_id,
                    memory_limit_bytes,
                )
                if correction is not None:
                    path = correction_root / f"{index:08d}.npz"
                    np.savez(path, **correction)
                    correction_paths.append(path)

            # Corrections are fully planned and staged before any output cell is
            # changed, so mini iteration order cannot influence the decisions.
            affected_labels = {int(value) for value in disconnected_labels}
            for path in correction_paths:
                with np.load(path) as correction:
                    win = Window(*correction["window"].tolist())
                    ownership = assembler.read("mini_ownership", win, masked=True)
                    drainage = assembler.read("drainage", win, masked=True)
                    values = np.asarray(ownership.data).copy()
                    valid = ~np.ma.getmaskarray(ownership)
                    drain_values = np.asarray(drainage.data).copy()
                    flat = correction["flat"]
                    targets = correction["targets"]
                    affected_labels.update(int(value) for value in targets if value)
                    values.ravel()[flat] = targets
                    valid.ravel()[flat] = targets != 0
                    drain_values.ravel()[flat] = 0
                    assembler.replace(
                        RasterPatch("mini_ownership", win, values, valid)
                    )
                    assembler.replace(
                        RasterPatch("drainage", win, drain_values, valid)
                    )
            for label in sorted(affected_labels):
                index = label - 1
                remaining = _plan_connectivity_correction(
                    assembler,
                    grid,
                    catchments[index],
                    label,
                    ordered[index],
                    memory_limit_bytes,
                )
                if remaining is not None:
                    raise PreparedDataError(
                        f"Mini {ordered[index]} is not a single drainage-bearing "
                        "component after ownership correction"
                    )
            paths = assembler.finish()
    finally:
        shutil.rmtree(correction_root, ignore_errors=True)

    index = staging / "mini_index.parquet"
    bounds = [catchments_by_id[mini_id].bounds for mini_id in ordered]
    pd.DataFrame(
        {
            "mini_label": dense_labels,
            "mini_id": ordered,
            "minx": [value[0] for value in bounds],
            "miny": [value[1] for value in bounds],
            "maxx": [value[2] for value in bounds],
            "maxy": [value[3] for value in bounds],
        }
    ).to_parquet(index, index=False)
    return paths, index



class _BlockConnectivity:
    """Track label components while canonical blocks are assembled."""

    def __init__(self, width: int):
        self.width = width
        self.parent = [0]
        self.labels = [0]
        self.sizes = [0]
        self.drainage = [0]
        self.first = [0]
        self.previous_bottom_labels = None
        self.previous_bottom_ids = None
        self.current_bottom_labels = np.zeros(width, dtype="int32")
        self.current_bottom_ids = np.zeros(width, dtype="int32")
        self.current_row = -1
        self.left_labels = None
        self.left_ids = None

    def _find(self, value: int) -> int:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            following = self.parent[value]
            self.parent[value] = root
            value = following
        return root

    def _union(self, first: int, second: int) -> None:
        first = self._find(first)
        second = self._find(second)
        if first == second:
            return
        if first > second:
            first, second = second, first
        self.parent[second] = first
        self.sizes[first] += self.sizes[second]
        self.drainage[first] += self.drainage[second]
        self.first[first] = min(self.first[first], self.first[second])

    def start_row(self, row: int) -> None:
        if row == self.current_row:
            return
        if self.current_row >= 0:
            self.previous_bottom_labels = self.current_bottom_labels
            self.previous_bottom_ids = self.current_bottom_ids
        self.current_bottom_labels = np.zeros(self.width, dtype="int32")
        self.current_bottom_ids = np.zeros(self.width, dtype="int32")
        self.current_row = row
        self.left_labels = None
        self.left_ids = None
        self.left_col_end = None

    def add_block(self, row, col, ownership, valid, drainage) -> None:
        self.start_row(row)
        components, labels, sizes, drains, first = _label_value_components(
            ownership, valid, drainage
        )
        mapping = np.zeros(len(labels), dtype="int32")
        for local in range(1, len(labels)):
            global_id = len(self.parent)
            mapping[local] = global_id
            self.parent.append(global_id)
            self.labels.append(int(labels[local]))
            self.sizes.append(int(sizes[local]))
            self.drainage.append(int(drains[local]))
            local_first = int(first[local])
            local_row, local_col = divmod(local_first, ownership.shape[1])
            self.first.append((row + local_row) * self.width + col + local_col)
        ids = mapping[components]
        if self.previous_bottom_labels is not None:
            top_labels = ownership[0]
            top_ids = ids[0]
            for local_col, (label, component) in enumerate(
                zip(top_labels, top_ids, strict=True)
            ):
                if not valid[0, local_col]:
                    continue
                global_col = col + local_col
                for neighbor_col in range(
                    max(0, global_col - 1), min(self.width, global_col + 2)
                ):
                    if self.previous_bottom_labels[neighbor_col] == label:
                        self._union(
                            int(component), int(self.previous_bottom_ids[neighbor_col])
                        )
        if self.left_labels is not None and self.left_col_end == col:
            left_labels = ownership[:, 0]
            left_ids = ids[:, 0]
            for local_row, (label, component) in enumerate(
                zip(left_labels, left_ids, strict=True)
            ):
                if not valid[local_row, 0]:
                    continue
                for neighbor_row in range(
                    max(0, local_row - 1),
                    min(len(self.left_labels), local_row + 2),
                ):
                    if self.left_labels[neighbor_row] == label:
                        self._union(
                            int(component), int(self.left_ids[neighbor_row])
                        )
        width = ownership.shape[1]
        self.current_bottom_labels[col : col + width] = ownership[-1]
        self.current_bottom_ids[col : col + width] = ids[-1]
        self.left_labels = ownership[:, -1].copy()
        self.left_ids = ids[:, -1].copy()
        self.left_col_end = col + width

    def disconnected_labels(self, expected_labels):
        roots_by_label: dict[int, list[int]] = {}
        for component in range(1, len(self.parent)):
            root = self._find(component)
            if root != component:
                continue
            roots_by_label.setdefault(self.labels[root], []).append(root)
        disconnected = []
        for label in expected_labels:
            roots = roots_by_label.get(int(label), [])
            if not roots or not any(self.drainage[root] for root in roots):
                raise PreparedDataError(
                    f"Mini label {label} has no rasterized ownership or drainage cells"
                )
            if len(roots) > 1:
                disconnected.append(int(label))
        return disconnected


@njit
def _label_value_components(values, valid, drainage):
    rows, cols = values.shape
    components = np.zeros((rows, cols), dtype=np.int32)
    queue = np.empty(rows * cols, dtype=np.int32)
    labels_list = [np.int32(0)]
    sizes_list = [np.int64(0)]
    drain_list = [np.int64(0)]
    first_list = [np.int64(rows * cols)]
    count = 0
    for row in range(rows):
        for col in range(cols):
            if not valid[row, col] or components[row, col] != 0:
                continue
            count += 1
            value = values[row, col]
            labels_list.append(np.int32(value))
            sizes_list.append(np.int64(0))
            drain_list.append(np.int64(0))
            first_list.append(np.int64(row * cols + col))
            head = 0
            tail = 1
            queue[0] = row * cols + col
            components[row, col] = count
            while head < tail:
                cell = queue[head]
                head += 1
                current_row = cell // cols
                current_col = cell - current_row * cols
                sizes_list[count] += 1
                if drainage[current_row, current_col]:
                    drain_list[count] += 1
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue
                        nr = current_row + dr
                        nc = current_col + dc
                        if (
                            0 <= nr < rows
                            and 0 <= nc < cols
                            and valid[nr, nc]
                            and values[nr, nc] == value
                            and components[nr, nc] == 0
                        ):
                            components[nr, nc] = count
                            queue[tail] = nr * cols + nc
                            tail += 1
    return (
        components,
        np.asarray(labels_list, dtype=np.int32),
        np.asarray(sizes_list, dtype=np.int64),
        np.asarray(drain_list, dtype=np.int64),
        np.asarray(first_list, dtype=np.int64),
    )

def _rasterize_ownership_block(geometries, labels, shape, transform):
    """Rasterize a catchment union and deterministically assign every union cell."""
    if not len(geometries):
        return np.zeros(shape, dtype="int32"), np.zeros(shape, dtype=bool)
    order = np.argsort(labels)[::-1]
    shapes = [(geometries[i], int(labels[i])) for i in order]
    ownership = rasterize(
        shapes,
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype="int32",
        all_touched=False,
    )
    occupancy = rasterize(
        [(geometry, 1) for geometry in geometries],
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype="uint32",
        all_touched=False,
        merge_alg=MergeAlg.add,
    )
    # Aggregated catchments are a non-overlapping coverage. Occupancy is
    # checked independently below, so this faster union cannot hide conflicts.
    union = shapely.coverage_union_all(geometries)
    expected = rasterize(
        [(union, 1)],
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype="uint8",
        all_touched=False,
    ).astype(bool)
    ambiguous = np.argwhere((occupancy > 1) | (expected & (ownership == 0)))
    for row, col in ambiguous:
        x, y = rasterio.transform.xy(transform, int(row), int(col), offset="center")
        point = shapely.Point(x, y)
        covering = np.flatnonzero(shapely.covers(geometries, point))
        interiors = np.flatnonzero(shapely.contains(geometries, point))
        if len(interiors) > 1:
            conflict_labels = sorted(int(labels[value]) for value in interiors)
            raise PreparedDataError(
                "Mini catchments overlap at a raster cell: "
                + ", ".join(map(str, conflict_labels))
            )
        owners = interiors if len(interiors) else covering
        if len(owners):
            ownership[row, col] = min(int(labels[value]) for value in owners)
        else:
            neighbors = ownership[
                max(0, row - 1) : min(shape[0], row + 2),
                max(0, col - 1) : min(shape[1], col + 2),
            ]
            choices, counts = np.unique(neighbors[neighbors != 0], return_counts=True)
            if not len(choices):
                raise PreparedDataError(
                    "Rasterized catchment union contains a cell with no defensible owner"
                )
            best = np.max(counts)
            ownership[row, col] = int(np.min(choices[counts == best]))
    valid = expected
    if np.any(valid & (ownership == 0)):
        raise PreparedDataError("Rasterized catchment union has unlabeled cells")
    return ownership, valid


def _rasterize_drainage_block(
    segments, labels, hits, ownership, valid, transform
):
    drainage = np.zeros(ownership.shape, dtype="uint8")
    if not len(hits):
        return drainage
    # Burn all lines once. Lowest-label-last ordering makes the common case
    # deterministic; obscured matching lines are repaired only where needed.
    ordered_hits = hits[np.argsort(labels[hits])[::-1]]
    burned_labels = rasterize(
        [(segments[index], int(labels[index])) for index in ordered_hits],
        out_shape=ownership.shape,
        transform=transform,
        fill=0,
        dtype="int32",
        all_touched=True,
    )
    drainage[valid & (burned_labels == ownership) & (burned_labels != 0)] = 1
    obscured = valid & (burned_labels != 0) & (burned_labels != ownership)
    hit_labels = {int(labels[index]) for index in hits}
    for label in np.unique(ownership[obscured]):
        label = int(label)
        if label == 0 or label not in hit_labels:
            continue
        index = label - 1
        matching = rasterize(
            [(segments[index], 1)],
            out_shape=ownership.shape,
            transform=transform,
            fill=0,
            dtype="uint8",
            all_touched=True,
        ).astype(bool)
        drainage[valid & (ownership == label) & matching] = 1
    return drainage

def _geometry_window_with_halo(geometry, grid: GridSpec) -> Window:
    raw = from_bounds(*geometry.bounds, transform=grid.transform)
    col0 = max(0, math.floor(raw.col_off) - 1)
    row0 = max(0, math.floor(raw.row_off) - 1)
    col1 = min(grid.width, math.ceil(raw.col_off + raw.width) + 1)
    row1 = min(grid.height, math.ceil(raw.row_off + raw.height) + 1)
    return Window(col0, row0, col1 - col0, row1 - row0)


def _plan_connectivity_correction(
    assembler, grid, geometry, label, mini_id, memory_limit_bytes
):
    win = _geometry_window_with_halo(geometry, grid)
    cells = int(win.width * win.height)
    if cells * 32 > memory_limit_bytes:
        raise PreparedDataError(
            f"Mini {mini_id} ownership window exceeds the configured memory limit"
        )
    ownership = assembler.read("mini_ownership", win, masked=True)
    drainage = assembler.read("drainage", win, masked=True)
    valid = ~np.ma.getmaskarray(ownership)
    values = np.asarray(ownership.data)
    owned = valid & (values == label)
    matching_drainage = owned & (np.asarray(drainage.data) != 0)
    if not np.any(owned) or not np.any(matching_drainage):
        raise PreparedDataError(
            f"Mini {mini_id} has no rasterized ownership or drainage cells"
        )
    components, sizes, drain_counts, first_cells = _label_components(
        owned, matching_drainage
    )
    candidates = np.flatnonzero(drain_counts[1:] > 0) + 1
    if not len(candidates):
        raise PreparedDataError(f"Mini {mini_id} has no component containing drainage")
    selected = min(
        candidates,
        key=lambda value: (
            -int(drain_counts[value]),
            -int(sizes[value]),
            int(first_cells[value]),
        ),
    )
    removed = [value for value in range(1, len(sizes)) if value != selected]
    if not removed:
        return None
    flat_parts = []
    target_parts = []
    rows, cols = owned.shape
    for component_id in removed:
        component_cells = np.argwhere(components == component_id)
        contact: dict[int, int] = {}
        enclosed = True
        for row, col in component_cells:
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = int(row + dr), int(col + dc)
                    if not (0 <= nr < rows and 0 <= nc < cols):
                        enclosed = False
                    elif components[nr, nc] == component_id:
                        continue
                    elif not valid[nr, nc]:
                        enclosed = False
                    else:
                        other = int(values[nr, nc])
                        if other != label:
                            contact[other] = contact.get(other, 0) + 1
        target = 0
        if enclosed and contact:
            greatest = max(contact.values())
            target = min(value for value, count in contact.items() if count == greatest)
        flat = np.ravel_multi_index(component_cells.T, owned.shape).astype("int64")
        flat_parts.append(flat)
        target_parts.append(np.full(flat.shape, target, dtype="int32"))
    return {
        "window": np.asarray(
            [win.col_off, win.row_off, win.width, win.height], dtype="int64"
        ),
        "flat": np.concatenate(flat_parts),
        "targets": np.concatenate(target_parts),
    }


@njit
def _label_components(owned, drainage):
    """Label 8-connected ownership and collect deterministic component stats."""
    rows, cols = owned.shape
    labels = np.zeros((rows, cols), dtype=np.int32)
    queue = np.empty(rows * cols, dtype=np.int32)
    sizes_list = [np.int64(0)]
    drain_list = [np.int64(0)]
    first_list = [np.int64(rows * cols)]
    count = 0
    for row in range(rows):
        for col in range(cols):
            if not owned[row, col] or labels[row, col] != 0:
                continue
            count += 1
            sizes_list.append(np.int64(0))
            drain_list.append(np.int64(0))
            first_list.append(np.int64(row * cols + col))
            head = 0
            tail = 1
            queue[0] = row * cols + col
            labels[row, col] = count
            while head < tail:
                cell = queue[head]
                head += 1
                current_row = cell // cols
                current_col = cell - current_row * cols
                sizes_list[count] += 1
                if drainage[current_row, current_col]:
                    drain_list[count] += 1
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue
                        nr = current_row + dr
                        nc = current_col + dc
                        if (
                            0 <= nr < rows
                            and 0 <= nc < cols
                            and owned[nr, nc]
                            and labels[nr, nc] == 0
                        ):
                            labels[nr, nc] = count
                            queue[tail] = nr * cols + nc
                            tail += 1

    return (
        labels,
        np.asarray(sizes_list, dtype=np.int64),
        np.asarray(drain_list, dtype=np.int64),
        np.asarray(first_list, dtype=np.int64),
    )

def _union_vector_geometries(path: Path, batch_size: int = 10_000):
    """Union a provider incrementally without constructing an eager vector frame."""
    partial = None
    try:
        with pyogrio.open_arrow(
            path,
            columns=[],
            read_geometry=True,
            batch_size=batch_size,
            use_pyarrow=True,
        ) as (metadata, batches):
            for batch in batches:
                table = pa.Table.from_batches([batch])
                geometry_column = geometry_column_name(
                    table, metadata.get("geometry_name")
                )
                values = (
                    table[geometry_column]
                    .combine_chunks()
                    .to_numpy(zero_copy_only=False)
                )
                geometries = shapely.from_wkb(values, on_invalid="raise")
                batch_union = shapely.union_all(geometries)
                partial = (
                    batch_union
                    if partial is None
                    else shapely.union(partial, batch_union)
                )
    except Exception as exc:
        raise PreparedDataError(f"Cannot read mini-catchment domain geometry: {path}") from exc
    if partial is None:
        raise PreparedDataError("ROI domain is empty")
    return partial


def _working_raster(path: Path, grid: GridSpec, dtype: str):
    return rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=grid.width,
        height=grid.height,
        count=1,
        dtype=dtype,
        crs=grid.crs,
        transform=grid.transform,
        tiled=True,
        blockxsize=BLOCK_SIZE,
        blockysize=BLOCK_SIZE,
        compress="DEFLATE",
        nodata=None,
        BIGTIFF="IF_SAFER",
    )


def _to_cog(source: Path, output: Path, overview_resampling: Resampling) -> None:
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        copy_raster(
            source,
            output,
            driver="COG",
            BLOCKSIZE=BLOCK_SIZE,
            COMPRESS="DEFLATE",
            BIGTIFF="IF_SAFER",
            RESAMPLING=overview_resampling.name.upper(),
            OVERVIEW_RESAMPLING=overview_resampling.name.upper(),
        )
