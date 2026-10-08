"""Raster-only staging of canonical prepared datasets."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import rasterio
import shapely
from pyproj import CRS
from rasterio.enums import MaskFlags, MergeAlg, Resampling
from rasterio.features import rasterize
from rasterio.transform import Affine
from rasterio.windows import Window, from_bounds

from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.execution.executor import (
    ExecutionConfig,
    ExecutionReport,
    LocalExecutor,
    WorkerContext,
    WorkerOutput,
    WorkItem,
)
from mgb_vec_hydro.execution.manifest import write_manifest
from mgb_vec_hydro.execution.memory import MemorySizing, raster_cache
from mgb_vec_hydro.execution.progress import ProgressCallback, StageReporter
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.vector import read_vector_table

BLOCK_SIZE = 512
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
RESERVED_RASTER_NAMES = {"dem", "d8", "grid_catchments", "grid_segments", "cells", "drainage"}


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
    memory_limit_mb: int = 4096
    io_slots: int = 2
    dem_scale: float = 1.0
    overwrite: bool = False


@dataclass(frozen=True)
class PreparationReport:
    """Summary of a successfully published prepared dataset."""

    output_dir: Path
    dem: Path
    rasters: dict[str, Path]
    grid_catchments: Path
    grid_segments: Path
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
            + [self.grid_catchments, self.grid_segments]
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
    segment_wkb: tuple[bytes, ...]
    segment_labels: tuple[int, ...]
    downstream: dict[int, int | None]
    d8_encoding: str
    gdal_cache_bytes: int
    dem_scale: float = 1.0


@dataclass(frozen=True)
class _PreparationBlockResult:
    patches: tuple[Any, ...]


def prepare_dataset(
    spec: PreparationSpec, *, progress: ProgressCallback | None = None
) -> PreparationReport:
    """Create a prepared dataset with a bounded GDAL block cache."""
    reporter = StageReporter(progress)
    sizing = MemorySizing(spec.memory_limit_mb * 1024**2, spec.workers)
    with raster_cache(sizing.coordinator_cache_bytes):
        report = _prepare_dataset(spec, reporter)
    reporter.finish(report.timings)
    return report


def _prepare_dataset(spec: PreparationSpec, reporter: StageReporter) -> PreparationReport:
    """Create and atomically publish one prepared dataset."""
    overall_started = time.perf_counter()
    _validate_spec(spec)
    output = Path(spec.output_dir)
    phase_started = time.perf_counter()
    reporter.operation("Reading mini inputs")
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
    ordered = sorted(catchments_by_id)
    catchments = np.asarray(
        [catchments_by_id[value] for value in ordered], dtype=object
    )
    segments = np.asarray([segments_by_id[value] for value in ordered], dtype=object)
    mini_ids = np.asarray(ordered, dtype="int32")
    downstream = _segment_graph(mini_segment_vector)
    domain_bounds = shapely.total_bounds(np.concatenate((catchments, segments)))
    if not np.all(np.isfinite(domain_bounds)):
        raise PreparedDataError("Mini-catchment domain is empty")
    with rasterio.open(spec.dem) as dem:
        _require_source_grid(dem, target_crs, "DEM")
        raw = from_bounds(*domain_bounds, transform=dem.transform)
        # Geographic affine inversion can put an exact edge a few ulps outside.
        edges = [raw.col_off, raw.row_off,
                 raw.col_off + raw.width, raw.row_off + raw.height]
        edges = [round(value) if math.isclose(value, round(value), rel_tol=0, abs_tol=1e-7)
                 else value for value in edges]
        col0, row0 = math.floor(edges[0]), math.floor(edges[1])
        col1, row1 = math.ceil(edges[2]), math.ceil(edges[3])
        if col0 < 0 or row0 < 0 or col1 > dem.width or row1 > dem.height:
            raise PreparedDataError("DEM does not cover the mini polygon/segment domain")
        col0, row0 = max(0, col0 - 1), max(0, row0 - 1)
        col1, row1 = min(dem.width, col1 + 1), min(dem.height, row1 + 1)
        window = Window(col0, row0, col1 - col0, row1 - row0)
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
    sizing = MemorySizing(spec.memory_limit_mb * 1024**2, spec.workers)
    memory_bytes = sizing.limit_bytes
    config = ExecutionConfig(
        workers=spec.workers,
        memory_limit_bytes=memory_bytes,
        max_in_flight=2 * spec.workers,
        io_slots=spec.io_slots,
        resource_cache_size=max(8, len(rasters)),
    )
    reporter.operation("Planning raster blocks")
    items = _preparation_work_items(
        grid,
        rasters,
        catchments,
        segments,
        mini_ids,
        catchment_index,
        segment_index,
        downstream,
        spec.d8_encoding or "canonical",
        memory_bytes,
        spec.workers,
        dem_scale=spec.dem_scale,
        gdal_cache_bytes=sizing.worker_cache_bytes,
    )
    planning_seconds = time.perf_counter() - phase_started

    block_count = (
        ((grid.width + BLOCK_SIZE - 1) // BLOCK_SIZE)
        * ((grid.height + BLOCK_SIZE - 1) // BLOCK_SIZE)
    )
    reporter.enter(
        "processing", block_count, operation="Processing raster blocks",
        unit="blocks",
    )
    execution = ExecutionReport(0, 0, 0, 0, 0, 0.0, {}, ())
    compression_seconds = 0.0
    publisher = AtomicOutputDirectory(output, overwrite=spec.overwrite)
    with publisher as staging:
        from mgb_vec_hydro.execution.raster import (
            RasterAssembler,
            RasterProductSpec,
        )

        product_specs = [
            RasterProductSpec("grid_catchments", "int32"),
            RasterProductSpec("grid_segments", "int32"),
        ] + [
            RasterProductSpec(
                item.name,
                _prepared_dtype(item.kind),
                Resampling.bilinear
                if item.kind == "continuous"
                else Resampling.nearest,
                {"units": "m", "dem_scale": spec.dem_scale}
                if item.name == "dem" else {},
            )
            for item in sorted(rasters, key=lambda item: item.name)
        ]
        with RasterAssembler(
            staging,
            grid,
            product_specs,
            scratch_memory_bytes=sizing.limit_bytes,
            working_compression=None,
            compression_threads=min(spec.workers, 4),
        ) as assembler:
            def reduce_block(result):
                started = time.perf_counter()
                for patch in result.value.patches:
                    assembler.write_block(patch)
                return {"output_write": time.perf_counter() - started}

            execution = LocalExecutor(config).run(
                items, _prepare_block_worker, reduce_block,
                progress=reporter.execution_progress,
            )

            reporter.enter("finalizing")
            reporter.operation(
                "Building mini ownership index", total=block_count, unit="blocks"
            )
            assembler.specs["grid_catchments"].tags["mini_index"] = json.dumps(
                _ownership_index(
                    assembler, grid, len(ordered), progress=reporter.advance
                ),
                separators=(",", ":"),
            )
            compression_started = time.perf_counter()
            reporter.operation(
                "Compressing raster outputs", total=len(product_specs), unit="rasters"
            )
            output_paths = assembler.finish(
                progress=lambda name, completed, total: reporter.operation(
                    f"Compressing {name}.tif", completed=completed,
                    total=total, unit="rasters",
                )
            )
            compression_seconds = time.perf_counter() - compression_started

        validation_started = time.perf_counter()
        reporter.operation("Validating staged rasters")
        _validate_prepared_outputs(
            output_paths,
            grid,
            raster_kinds=raster_kinds,
        )
        expected_names = tuple(path.name for path in output_paths.values())
        reporter.operation("Writing output manifest")
        expected_names += (write_manifest(staging, "prepare", spec),)
        reporter.operation("Publishing outputs", total=1, unit="steps")
        publisher.publish(expected_names, remove=("cells.tif", "drainage.tif"))
        reporter.advance(1)
        validation_publication_seconds = time.perf_counter() - validation_started

    return PreparationReport(
        output_dir=output,
        dem=output / "dem.tif",
        rasters={item.name: output / f"{item.name}.tif" for item in rasters},
        grid_catchments=output / "grid_catchments.tif",
        grid_segments=output / "grid_segments.tif",
        raster_count=len(rasters),
        execution=execution,
        timings={
            "planning": planning_seconds
            + float(execution.timings.get("planning", 0.0)),
            "parallel_execution": execution.wall_seconds,
            "raster_reads": float(execution.timings.get("raster_reads", 0.0)),
            "domain_rasterization": float(
                execution.timings.get("domain_rasterization", 0.0)
            ),
            "output_write": float(execution.timings.get("output_write", 0.0)),
            "coordination": float(execution.timings.get("coordination", 0.0)),
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
        with rasterio.open(path) as source:
            _require_source_grid(source, grid.crs, name, grid)
            itemsize = np.dtype(source.dtypes[0]).itemsize
        result.append(_PreparationRaster(name, path, kind, itemsize))
    return tuple(result)


def _preparation_work_items(
    grid: GridSpec,
    rasters: tuple[_PreparationRaster, ...],
    catchments: np.ndarray,
    segments: np.ndarray,
    mini_ids: np.ndarray,
    catchment_index,
    segment_index,
    downstream,
    d8_encoding: str,
    memory_limit_bytes: int,
    workers: int,
    *,
    dem_scale: float = 1.0,
    gdal_cache_bytes: int | None = None,
):
    from mgb_vec_hydro.execution.raster import plan_raster_blocks

    output_bytes_per_cell = sum(
        np.dtype(_prepared_dtype(item.kind)).itemsize + 1 for item in rasters
    )
    max_source_itemsize = max(item.source_itemsize for item in rasters)
    if gdal_cache_bytes is None:
        gdal_cache_bytes = MemorySizing(memory_limit_bytes, workers).worker_cache_bytes

    def items():
        for ordinal, window in enumerate(plan_raster_blocks(grid)):
            bounds = rasterio.windows.bounds(window, grid.transform)
            block_geometry = shapely.box(*bounds)
            catchment_hits = np.sort(catchment_index.query(block_geometry))
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
                + len(downstream) * 80
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
                    tuple(int(mini_ids[index]) for index in catchment_hits),
                    segment_wkb,
                    tuple(int(mini_ids[index]) for index in segment_hits),
                    downstream,
                    d8_encoding,
                    gdal_cache_bytes,
                    dem_scale,
                ),
            )

    return items()


def _prepare_block_worker(
    payload: _PreparationBlockPayload, context: WorkerContext
) -> WorkerOutput[_PreparationBlockResult]:
    with raster_cache(payload.gdal_cache_bytes):
        return _prepare_block_worker_with_cache(payload, context)


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
        "domain_rasterization": 0.0,
    }

    started = time.perf_counter()
    ownership, ownership_valid = _rasterize_ownership_block(
        catchments, catchment_labels, shape, transform
    )
    segment_grid = _rasterize_segments_block(
        segments, segment_labels, shape, transform, payload.downstream
    )
    stream = segment_grid > 0
    ownership[stream] = segment_grid[stream]
    ownership_valid |= stream
    domain_mask = ownership_valid
    timings["domain_rasterization"] += time.perf_counter() - started

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
                if item.name == "dem" and payload.dem_scale != 1.0:
                    # Scale before converting wide source values to float32.
                    # Keep the common float32 path in-place; use float64 when
                    # the source or conversion factor requires that range.
                    limits = np.finfo(np.float32)
                    dtype = np.result_type(raw.dtype, np.float32)
                    if not limits.tiny <= payload.dem_scale <= limits.max:
                        dtype = np.dtype("float64")
                    data = np.where(valid, raw, 0).astype(dtype, copy=False)
                    with np.errstate(over="ignore", invalid="ignore"):
                        np.multiply(data, payload.dem_scale, out=data)
                        data = data.astype("float32", copy=False)
                    if not np.isfinite(data[valid]).all():
                        raise PreparedDataError("Scaled DEM exceeds finite float32 values")
                else:
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

    patches.extend((
        RasterPatch("grid_catchments", payload.window, ownership, ownership_valid),
        RasterPatch("grid_segments", payload.window, segment_grid, ownership_valid),
    ))
    return WorkerOutput(
        _PreparationBlockResult(tuple(patches)),
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


def read_mini_index(cells: Path) -> list[list[int | float]]:
    """Read mini IDs and pixel-edge bounds embedded in the ownership raster."""
    with rasterio.open(cells) as source:
        return json.loads(source.tags()["mini_index"])


def _ownership_index(
    assembler,
    grid: GridSpec,
    mini_count: int,
    *,
    progress: Callable[[int], None] | None = None,
):
    """Accumulate tight final ownership bounds using one bounded block scan."""
    from mgb_vec_hydro.execution.raster import plan_raster_blocks

    min_rows = np.full(mini_count + 1, grid.height, dtype="int64")
    min_cols = np.full(mini_count + 1, grid.width, dtype="int64")
    max_rows = np.full(mini_count + 1, -1, dtype="int64")
    max_cols = np.full(mini_count + 1, -1, dtype="int64")
    for completed, window in enumerate(plan_raster_blocks(grid), start=1):
        cells = assembler.read("grid_catchments", window)
        rows, cols = np.nonzero(~np.ma.getmaskarray(cells))
        ids = cells.data[rows, cols]
        rows += int(window.row_off)
        cols += int(window.col_off)
        np.minimum.at(min_rows, ids, rows)
        np.minimum.at(min_cols, ids, cols)
        np.maximum.at(max_rows, ids, rows)
        np.maximum.at(max_cols, ids, cols)
        if progress is not None:
            progress(completed)
    records = []
    for mini_id in range(1, mini_count + 1):
        if max_rows[mini_id] < 0:
            raise PreparedDataError(f"Mini {mini_id} has no rasterized ownership cells")
        window = Window(
            int(min_cols[mini_id]), int(min_rows[mini_id]),
            int(max_cols[mini_id] - min_cols[mini_id] + 1),
            int(max_rows[mini_id] - min_rows[mini_id] + 1),
        )
        records.append([mini_id, *rasterio.windows.bounds(window, grid.transform)])
    return records


def _validate_spec(spec: PreparationSpec) -> None:
    names = [item.name for item in spec.rasters]
    if any(
        not isinstance(name, str) or not NAME_RE.fullmatch(name)
        or name in RESERVED_RASTER_NAMES for name in names
    ) or len(names) != len(set(names)):
        raise PreparedDataError("Named raster names must be unique, valid and non-reserved")
    if (
        isinstance(spec.dem_scale, bool)
        or not isinstance(spec.dem_scale, (int, float, np.integer, np.floating))
        or not math.isfinite(spec.dem_scale)
        or spec.dem_scale <= 0
    ):
        raise PreparedDataError("DEM scale must be a finite positive number")
    if (spec.d8 is None) != (spec.d8_encoding is None):
        raise PreparedDataError("--d8 and --d8-encoding must be supplied together")
    if spec.d8_encoding not in {None, "canonical", "esri"}:
        raise PreparedDataError(f"Unsupported D8 encoding: {spec.d8_encoding}")


def _read_mini_inputs(catchments: Path, segments: Path):
    """Read and validate the explicit aggregated mini vector inputs."""

    catchment_vector = read_vector_table(catchments)
    segment_vector = read_vector_table(segments)
    for name, vector, allowed in (
        ("mini catchments", catchment_vector, {3, 6}),
        ("mini segments", segment_vector, {1, 5}),
    ):
        geometries = vector.geometries()
        if (
            np.any(shapely.is_missing(geometries))
            or np.any(shapely.is_empty(geometries))
            or not set(shapely.get_type_id(geometries).tolist()).issubset(allowed)
            or not np.all(shapely.is_valid(geometries))
        ):
            raise PreparedDataError(f"{name} contains invalid geometry")
        if "id" not in vector.table.column_names:
            raise PreparedDataError(f"{name} requires id")
        ids = vector.table["id"].to_pylist()
        if (
            not ids
            or any(isinstance(value, bool) or not isinstance(value, int) for value in ids)
            or sorted(ids) != list(range(1, len(ids) + 1))
            or len(ids) > np.iinfo(np.int32).max
        ):
            raise PreparedDataError(f"{name} IDs must be dense integers 1..N")
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
    grid: GridSpec,
    *,
    raster_kinds: dict[str, str],
) -> None:
    """Validate all direct prepared files before the atomic directory rename."""

    expected = set(raster_kinds) | {"grid_catchments", "grid_segments"}
    if set(paths) != expected:
        raise PreparedDataError("Prepared output file set is incomplete")
    for name, path in paths.items():
        if name in {"grid_catchments", "grid_segments"}:
            expected_dtype = "int32"
        elif name == "d8":
            expected_dtype = "uint8"
        elif raster_kinds[name] == "categorical":
            expected_dtype = "int32"
        else:
            expected_dtype = "float32"
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


def _segment_graph(vector):
    """Validate the downstream graph once, without relying on input order."""
    if "id_down" not in vector.table.column_names:
        raise PreparedDataError("Mini segments require id_down")
    graph = dict(zip(vector.table["id"].to_pylist(),
                     vector.table["id_down"].to_pylist(), strict=True))
    for mini_id, target in graph.items():
        if target is None or target == -1:
            graph[mini_id] = None
        elif isinstance(target, bool) or not isinstance(target, int) or target not in graph:
            raise PreparedDataError(f"Mini {mini_id} has missing downstream target {target}")
    done = set()
    for mini_id in graph:
        path = set()
        current = mini_id
        while current is not None and current not in done:
            if current in path:
                raise PreparedDataError(f"Mini segment graph contains a cycle at {current}")
            path.add(current)
            current = graph[current]
        done.update(path)
    return graph


def _rasterize_segments_block(segments, labels, shape, transform, downstream):
    """Burn IDs, resolving only collisions by ancestry and then lowest ID."""
    if not len(segments):
        return np.zeros(shape, dtype="int32")
    order = np.argsort(labels)[::-1]
    burned = rasterize([(segments[i], int(labels[i])) for i in order],
                       out_shape=shape, transform=transform, fill=0,
                       dtype="int32", all_touched=True)
    occupancy = rasterize([(geometry, 1) for geometry in segments],
                          out_shape=shape, transform=transform, fill=0,
                          dtype="uint32", all_touched=True, merge_alg=MergeAlg.add)
    rows, cols = np.nonzero(occupancy > 1)
    if not len(rows):
        return burned
    contenders = [[] for _ in rows]
    for geometry, label in zip(segments, labels, strict=True):
        mask = rasterize([(geometry, 1)], out_shape=shape, transform=transform,
                         fill=0, dtype="uint8", all_touched=True)
        for index in np.flatnonzero(mask[rows, cols]):
            contenders[index].append(int(label))
    winners = {}
    for row, col, values in zip(rows, cols, contenders, strict=True):
        key = tuple(sorted(set(values)))
        if key not in winners:
            remaining = set(key)
            for value in key:
                current = downstream[value]
                while current is not None:
                    if current in key:
                        remaining.discard(value)
                        break
                    current = downstream[current]
            winners[key] = min(remaining)
        burned[row, col] = winners[key]
    return burned


def validate_segment_ownership(assets, grid, error_type):
    """Check segment IDs over the complete grid, including unowned blocks."""
    from mgb_vec_hydro.execution.raster import plan_raster_blocks

    with rasterio.open(assets["grid_catchments"]) as catchments, rasterio.open(
        assets["grid_segments"]
    ) as segments:
        for window in plan_raster_blocks(grid):
            ownership = catchments.read(1, window=window, masked=True)
            stream = segments.read(1, window=window, masked=True)
            valid = ~np.ma.getmaskarray(stream)
            positive = valid & (stream.data > 0)
            if np.any(valid & (stream.data < 0)) or np.any(
                positive & (np.ma.getmaskarray(ownership) | (stream.data != ownership.data))
            ):
                raise error_type("Positive segment IDs must match overlaid ownership")
