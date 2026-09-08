"""Raster-only staging of canonical prepared datasets."""

from __future__ import annotations

import math
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import rasterio
import shapely
from numba import njit
from pyproj import CRS
from rasterio.enums import MaskFlags, MergeAlg, Resampling
from rasterio.env import get_gdal_config, set_gdal_config
from rasterio.features import rasterize
from rasterio.transform import Affine
from rasterio.windows import Window, from_bounds

from mgb_vec_hydro.aggregation import AGGREGATION_COLUMNS
from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.execution.executor import (
    ExecutionConfig,
    ExecutionReport,
    LocalExecutor,
    WorkerContext,
    WorkerOutput,
    WorkItem,
)
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.vector import read_vector_table

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
    workers: int = 4
    memory_limit_mb: int = 512
    io_slots: int = 2


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
    execution: ExecutionReport
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


@dataclass(frozen=True)
class _PreparationRaster:
    name: str
    path: Path
    kind: Literal["continuous", "categorical", "d8"]
    source_itemsize: int


@dataclass(frozen=True)
class _PreparationBlockPayload:
    grid: GridSpec
    window: Window
    rasters: tuple[_PreparationRaster, ...]
    catchment_wkb: tuple[bytes, ...]
    catchment_labels: tuple[int, ...]
    ownership_indices: tuple[int, ...]
    segment_wkb: tuple[bytes, ...]
    segment_labels: tuple[int, ...]
    d8_encoding: str
    gdal_cache_bytes: int


@dataclass(frozen=True)
class _PreparationBlockResult:
    window: Window
    patches: tuple[Any, ...]
    ownership: np.ndarray | None
    valid: np.ndarray | None
    drainage: np.ndarray | None
    components: tuple[np.ndarray, ...] | None


def prepare_dataset(spec: PreparationSpec) -> PreparationReport:
    """Create a prepared dataset with a bounded GDAL block cache."""
    previous = get_gdal_config("GDAL_CACHEMAX")
    cache_bytes = _gdal_cache_bytes(spec.memory_limit_mb * 1024 * 1024, 1)
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
    phase_started = time.perf_counter()
    mini_catchment_vector, mini_segment_vector = _read_mini_inputs(
        Path(spec.mini_catchments), Path(spec.mini_segments)
    )
    target_crs = mini_catchment_vector.crs
    catchment_ids = mini_catchment_vector.table["id"].to_pylist()
    segment_ids = mini_segment_vector.table["id"].to_pylist()
    catchments_by_id = dict(
        zip(catchment_ids, mini_catchment_vector.geometries(), strict=True)
    )
    segments_by_id = dict(
        zip(segment_ids, mini_segment_vector.geometries(), strict=True)
    )
    ordered = sorted(
        catchments_by_id, key=lambda value: (type(value).__name__, str(value))
    )
    catchments = np.asarray(
        [catchments_by_id[value] for value in ordered], dtype=object
    )
    segments = np.asarray([segments_by_id[value] for value in ordered], dtype=object)
    dense_labels = np.arange(1, len(ordered) + 1, dtype="int32")
    domain_bounds = shapely.total_bounds(catchments)
    if not np.all(np.isfinite(domain_bounds)):
        raise PreparedDataError("Mini-catchment domain is empty")
    with rasterio.open(spec.dem) as dem:
        _require_source_grid(dem, target_crs, "DEM")
        window = (
            from_bounds(
                float(domain_bounds[0]),
                float(domain_bounds[1]),
                float(domain_bounds[2]),
                float(domain_bounds[3]),
                transform=dem.transform,
            )
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
                "DEM does not cover the mini-catchment domain"
            )
        grid = GridSpec(
            target_crs,
            rasterio.windows.transform(window, dem.transform),
            int(window.width),
            int(window.height),
        )
    rasters = _preparation_rasters(spec, grid)
    raster_kinds = {item.name: item.kind for item in rasters}
    catchment_index = shapely.STRtree(catchments)
    segment_index = shapely.STRtree(segments)
    memory_bytes = spec.memory_limit_mb * 1024 * 1024
    items = _preparation_work_items(
        grid,
        rasters,
        catchments,
        segments,
        dense_labels,
        catchment_index,
        segment_index,
        spec.d8_encoding or "canonical",
        memory_bytes,
        spec.workers,
    )
    planning_seconds = time.perf_counter() - phase_started

    execution = ExecutionReport(0, 0, 0, 0, 0, 0, 0.0, {}, ())
    correction_seconds = compression_seconds = 0.0
    publisher = AtomicOutputDirectory(output)
    with publisher as staging:
        mini_index = _write_mini_index(staging, ordered, catchments_by_id, dense_labels)
        correction_root = staging / ".ownership-corrections"
        correction_root.mkdir()
        try:
            from mgb_vec_hydro.execution.raster import (
                RasterAssembler,
                RasterPatch,
                RasterProductSpec,
            )

            product_specs = [
                RasterProductSpec(
                    item.name,
                    _prepared_dtype(item.kind),
                    Resampling.bilinear
                    if item.kind == "continuous"
                    else Resampling.nearest,
                )
                for item in rasters
            ] + [
                RasterProductSpec("mini_ownership", "int32"),
                RasterProductSpec("drainage", "uint8"),
            ]
            with RasterAssembler(
                staging,
                grid,
                product_specs,
                working_compression=None,
                compression_threads=min(spec.workers, 4),
            ) as assembler:
                connectivity = _BlockConnectivity(grid.width)

                def reduce_block(result):
                    started = time.perf_counter()
                    value = result.value
                    connectivity.start_row(int(value.window.row_off))
                    for patch in value.patches:
                        assembler.write_block(patch)
                    if value.ownership is not None:
                        connectivity.add_block(
                            int(value.window.row_off),
                            int(value.window.col_off),
                            value.ownership,
                            value.valid,
                            value.drainage.astype(bool),
                            components=value.components,
                        )
                    return {"output_write": time.perf_counter() - started}

                execution = LocalExecutor(
                    ExecutionConfig(
                        workers=spec.workers,
                        memory_limit_bytes=memory_bytes,
                        max_in_flight=spec.workers,
                        io_slots=spec.io_slots,
                        resource_cache_size=max(8, len(rasters)),
                    )
                ).run(items, _prepare_block_worker, reduce_block)

                correction_started = time.perf_counter()
                _correct_connectivity(
                    assembler,
                    correction_root,
                    grid,
                    catchments,
                    ordered,
                    dense_labels,
                    connectivity,
                    memory_bytes,
                    RasterPatch,
                )
                correction_seconds = time.perf_counter() - correction_started
                compression_started = time.perf_counter()
                output_paths = assembler.finish()
                compression_seconds = time.perf_counter() - compression_started
        finally:
            shutil.rmtree(correction_root, ignore_errors=True)

        validation_started = time.perf_counter()
        _validate_prepared_outputs(
            output_paths,
            mini_index,
            grid,
            raster_kinds=raster_kinds,
        )
        expected_names = tuple(path.name for path in output_paths.values()) + (
            mini_index.name,
        )
        publisher.publish(expected_names)
        validation_publication_seconds = time.perf_counter() - validation_started

    return PreparationReport(
        output_dir=output,
        dem=output / "dem.tif",
        rasters={item.name: output / f"{item.name}.tif" for item in rasters},
        mini_ownership=output / "mini_ownership.tif",
        drainage=output / "drainage.tif",
        mini_index=output / "mini_index.parquet",
        raster_count=len(rasters),
        execution=execution,
        timings={
            "planning": planning_seconds
            + float(execution.timings.get("planning", 0.0)),
            "parallel_execution": execution.wall_seconds,
            "raster_reads": float(execution.timings.get("raster_reads", 0.0)),
            "mask_rasterization": float(
                execution.timings.get("mask_rasterization", 0.0)
            ),
            "domain_rasterization": float(
                execution.timings.get("domain_rasterization", 0.0)
            ),
            "connectivity": float(execution.timings.get("connectivity", 0.0)),
            "output_write": float(execution.timings.get("output_write", 0.0)),
            "coordination": float(execution.timings.get("coordination", 0.0)),
            "connectivity_correction": correction_seconds,
            "compression": compression_seconds,
            "validation_publication": validation_publication_seconds,
            "total": time.perf_counter() - overall_started,
        },
    )


def _prepared_dtype(kind: str) -> str:
    if kind == "d8":
        return "uint8"
    return "float32" if kind == "continuous" else "int32"


def _preparation_rasters(
    spec: PreparationSpec, grid: GridSpec
) -> tuple[_PreparationRaster, ...]:
    requested = [
        ("dem", Path(spec.dem), "continuous"),
        *[
            (item.name, Path(item.path), item.kind)
            for item in sorted(spec.rasters, key=lambda value: value.name)
        ],
    ]
    if spec.d8 is not None:
        requested.append(("d8", Path(spec.d8), "d8"))
    result = []
    for name, path, kind in requested:
        try:
            with rasterio.open(path) as source:
                _require_source_grid(source, grid.crs, name, grid)
                itemsize = np.dtype(source.dtypes[0]).itemsize
        except PreparedDataError:
            raise
        except (OSError, TypeError, rasterio.errors.RasterioError) as exc:
            raise PreparedDataError(f"Cannot inspect raster input: {path}") from exc
        result.append(_PreparationRaster(name, path, kind, itemsize))
    return tuple(result)


def _preparation_work_items(
    grid: GridSpec,
    rasters: tuple[_PreparationRaster, ...],
    catchments: np.ndarray,
    segments: np.ndarray,
    dense_labels: np.ndarray,
    catchment_index,
    segment_index,
    d8_encoding: str,
    memory_limit_bytes: int,
    workers: int,
):
    from mgb_vec_hydro.execution.raster import plan_raster_blocks

    output_bytes_per_cell = sum(
        np.dtype(_prepared_dtype(item.kind)).itemsize + 1 for item in rasters
    )
    max_source_itemsize = max(item.source_itemsize for item in rasters)
    gdal_cache_bytes = _gdal_cache_bytes(memory_limit_bytes, workers)

    def items():
        for ordinal, window in enumerate(plan_raster_blocks(grid)):
            bounds = rasterio.windows.bounds(window, grid.transform)
            block_geometry = shapely.box(*bounds)
            catchment_hits = np.sort(catchment_index.query(block_geometry))
            ownership_indices = tuple(range(len(catchment_hits)))
            segment_hits = np.sort(segment_index.query(block_geometry))
            catchment_wkb = tuple(
                bytes(value) for value in shapely.to_wkb(catchments[catchment_hits])
            )
            segment_wkb = tuple(
                bytes(value) for value in shapely.to_wkb(segments[segment_hits])
            )
            geometry_bytes = sum(map(len, catchment_wkb)) + sum(map(len, segment_wkb))
            cells = int(window.width * window.height)
            estimated_bytes = (
                8 * 1024 * 1024
                + geometry_bytes * 4
                + cells * (48 + 2 * max_source_itemsize + output_bytes_per_cell)
            )
            yield WorkItem(
                f"block-{int(window.row_off):010d}-{int(window.col_off):010d}",
                ordinal,
                max(1, estimated_bytes),
                _PreparationBlockPayload(
                    grid,
                    window,
                    rasters,
                    catchment_wkb,
                    tuple(int(dense_labels[index]) for index in catchment_hits),
                    ownership_indices,
                    segment_wkb,
                    tuple(int(dense_labels[index]) for index in segment_hits),
                    d8_encoding,
                    gdal_cache_bytes,
                ),
            )

    return items()


def _prepare_block_worker(
    payload: _PreparationBlockPayload, context: WorkerContext
) -> WorkerOutput[_PreparationBlockResult]:
    previous = get_gdal_config("GDAL_CACHEMAX")
    set_gdal_config("GDAL_CACHEMAX", payload.gdal_cache_bytes)
    try:
        return _prepare_block_worker_with_cache(payload, context)
    finally:
        set_gdal_config("GDAL_CACHEMAX", previous)


def _prepare_block_worker_with_cache(
    payload: _PreparationBlockPayload, context: WorkerContext
) -> WorkerOutput[_PreparationBlockResult]:
    from mgb_vec_hydro.execution.raster import CoveringRasterReader, RasterPatch

    shape = (int(payload.window.height), int(payload.window.width))
    transform = rasterio.windows.transform(payload.window, payload.grid.transform)
    catchments = shapely.from_wkb(np.asarray(payload.catchment_wkb, dtype=object))
    catchment_labels = np.asarray(payload.catchment_labels, dtype="int32")
    segments = shapely.from_wkb(np.asarray(payload.segment_wkb, dtype=object))
    segment_labels = np.asarray(payload.segment_labels, dtype="int32")
    timings = {
        "raster_reads": 0.0,
        "mask_rasterization": 0.0,
        "domain_rasterization": 0.0,
        "connectivity": 0.0,
    }

    started = time.perf_counter()
    if len(catchments):
        domain_mask = rasterize(
            [(geometry, 1) for geometry in catchments],
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype="uint8",
        ).astype(bool)
    else:
        domain_mask = np.zeros(shape, dtype=bool)
    timings["mask_rasterization"] += time.perf_counter() - started

    patches = []
    if np.any(domain_mask):
        reader = CoveringRasterReader(
            payload.grid,
            {item.name: item.path for item in payload.rasters},
            context,
        )
        for item in payload.rasters:
            started = time.perf_counter()
            values = reader.read(item.name, payload.window)
            timings["raster_reads"] += time.perf_counter() - started
            valid = ~np.ma.getmaskarray(values) & domain_mask
            raw = values.filled(0)
            if item.kind == "continuous":
                valid &= np.isfinite(raw)
                data = np.where(valid, raw, 0).astype("float32")
            elif item.kind == "categorical":
                source_values = raw[valid]
                if source_values.size and (
                    not np.all(np.isfinite(source_values))
                    or not np.all(source_values == np.floor(source_values))
                ):
                    raise PreparedDataError(
                        f"Categorical raster {item.path} contains non-integral values"
                    )
                data = raw.astype("int32")
            else:
                data = _normalize_d8(raw, valid, payload.d8_encoding, source=item.path)
            patches.append(RasterPatch(item.name, payload.window, data, valid))

    started = time.perf_counter()
    ownership_indices = np.asarray(payload.ownership_indices, dtype="int64")
    ownership, ownership_valid = _rasterize_ownership_block(
        catchments[ownership_indices],
        catchment_labels[ownership_indices],
        shape,
        transform,
    )
    if np.any(ownership_valid):
        drainage = _rasterize_drainage_block(
            segments,
            segment_labels,
            ownership,
            ownership_valid,
            transform,
        )
        patches.extend(
            (
                RasterPatch(
                    "mini_ownership", payload.window, ownership, ownership_valid
                ),
                RasterPatch("drainage", payload.window, drainage, ownership_valid),
            )
        )
    else:
        ownership = ownership_valid = drainage = None
    timings["domain_rasterization"] += time.perf_counter() - started

    components = None
    if ownership is not None:
        started = time.perf_counter()
        components = _label_value_components(
            ownership, ownership_valid, drainage.astype(bool)
        )
        timings["connectivity"] += time.perf_counter() - started

    return WorkerOutput(
        _PreparationBlockResult(
            payload.window,
            tuple(patches),
            ownership,
            ownership_valid,
            drainage,
            components,
        ),
        timings,
        {"worker_pid": os.getpid(), "block_cells": int(np.prod(shape))},
    )


def _normalize_d8(raw, valid, encoding: str, *, source: Path) -> np.ndarray:
    esri = {0: 0, 1: 3, 2: 4, 4: 5, 8: 6, 16: 7, 32: 8, 64: 1, 128: 2}
    allowed = set(range(9)) if encoding == "canonical" else set(esri)
    unknown = set(np.unique(raw[valid]).tolist()) - allowed
    if unknown:
        raise PreparedDataError(
            f"D8 raster {source} contains invalid code(s): "
            + ", ".join(map(str, sorted(unknown)))
        )
    if encoding == "canonical":
        return raw.astype("uint8")
    return np.vectorize(lambda value: esri.get(value, 0), otypes=["uint8"])(raw)


def _write_mini_index(staging, ordered, catchments_by_id, dense_labels) -> Path:
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
    return index


def _gdal_cache_bytes(memory_limit_bytes: int, workers: int) -> int:
    return min(
        64 * 1024 * 1024,
        max(8 * 1024 * 1024, memory_limit_bytes // max(8, workers * 8)),
    )


def _validate_spec(spec: PreparationSpec) -> None:
    if not Path(spec.dem).is_file():
        raise PreparedDataError(f"DEM input is not a local file: {spec.dem}")
    for name, value in (
        ("workers", spec.workers),
        ("memory limit", spec.memory_limit_mb),
        ("I/O slots", spec.io_slots),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PreparedDataError(f"{name} must be a positive integer")
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
        raise PreparedDataError("Cannot read explicit mini-catchment inputs") from exc
    for name, vector, allowed in (
        ("mini catchments", catchment_vector, {3, 6}),
        ("mini segments", segment_vector, {1, 5}),
    ):
        if list(vector.columns) != AGGREGATION_COLUMNS:
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
        raise PreparedDataError(
            "Mini catchments and segments must contain matching IDs"
        )
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
            raise PreparedDataError(f"{name} does not cover the ROI domain")


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


def _correct_connectivity(
    assembler,
    correction_root,
    grid,
    catchments,
    ordered,
    dense_labels,
    connectivity,
    memory_limit_bytes,
    raster_patch_type,
) -> None:
    disconnected_labels = connectivity.disconnected_labels(dense_labels)
    correction_paths = []
    for label in disconnected_labels:
        index = int(label) - 1
        correction = _plan_connectivity_correction(
            assembler,
            grid,
            catchments[index],
            int(label),
            ordered[index],
            memory_limit_bytes,
        )
        if correction is not None:
            path = correction_root / f"{index:08d}.npz"
            np.savez(path, **correction)
            correction_paths.append(path)

    # Plan every correction against the same initial raster so iteration order
    # cannot influence ownership decisions.
    affected_labels = {int(value) for value in disconnected_labels}
    for path in correction_paths:
        with np.load(path) as correction:
            window = Window(*correction["window"].tolist())
            ownership = assembler.read("mini_ownership", window, masked=True)
            drainage = assembler.read("drainage", window, masked=True)
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
                raster_patch_type("mini_ownership", window, values, valid)
            )
            assembler.replace(
                raster_patch_type("drainage", window, drain_values, valid)
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

    def add_block(
        self, row, col, ownership, valid, drainage, *, components=None
    ) -> None:
        self.start_row(row)
        if components is None:
            components = _label_value_components(ownership, valid, drainage)
        components, labels, sizes, drains, first = components
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
                        self._union(int(component), int(self.left_ids[neighbor_row]))
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
    """Rasterize catchments and deterministically assign every covered cell."""
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
    expected = occupancy > 0
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
                    "Rasterized catchment domain contains a cell with no defensible owner"
                )
            best = np.max(counts)
            ownership[row, col] = int(np.min(choices[counts == best]))
    valid = expected
    if np.any(valid & (ownership == 0)):
        raise PreparedDataError("Rasterized catchment domain has unlabeled cells")
    return ownership, valid


def _rasterize_drainage_block(segments, labels, ownership, valid, transform):
    drainage = np.zeros(ownership.shape, dtype="uint8")
    if not len(segments):
        return drainage
    # Burn all lines once. Lowest-label-last ordering makes the common case
    # deterministic; obscured matching lines are repaired only where needed.
    ordered_hits = np.argsort(labels)[::-1]
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
    segment_by_label = {
        int(label): segment for label, segment in zip(labels, segments, strict=True)
    }
    for label in np.unique(ownership[obscured]):
        label = int(label)
        if label == 0 or label not in segment_by_label:
            continue
        matching = rasterize(
            [(segment_by_label[label], 1)],
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
