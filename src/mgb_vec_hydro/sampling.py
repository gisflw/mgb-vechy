from __future__ import annotations

import csv
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import rasterio
import shapely
from pyproj import CRS, Transformer

from mgb_vec_hydro.aggregation import AGGREGATION_COLUMNS
from mgb_vec_hydro.crs_utils import require_metre_units
from mgb_vec_hydro.exceptions import MiniSamplingError
from mgb_vec_hydro.execution.executor import (
    ExecutionConfig,
    ExecutionReport,
    LocalExecutor,
    WorkerContext,
    WorkerOutput,
    WorkItem,
)
from mgb_vec_hydro.execution.manifest import write_manifest
from mgb_vec_hydro.execution.memory import ArrowPacketStore, MemorySizing, raster_cache
from mgb_vec_hydro.execution.progress import ProgressCallback, StageReporter
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.raster import (
    AlignedRasterReader,
    RasterBlockPacket,
    RasterUnit,
    _require_grid,
    grid_from_dem,
    packet_raster_units_by_block,
    plan_raster_units,
)
from mgb_vec_hydro.execution.vector import (
    geometry_column_name,
    inspect_vector_provider,
    iter_provider_batches,
)
from mgb_vec_hydro.preparation import BLOCK_SIZE, GridSpec, read_mini_index

SAMPLING_BYTES_PER_CELL = 96
TASK_FIXED_BYTES = 8 * 1024 * 1024
NODATA_DATASETS = ("dem", "drainage", "hand", "hru", "ltnd")
NODATA_REPORT_FILENAMES = tuple(f"nodata_{name}.csv" for name in NODATA_DATASETS)


class _SamplingNodataWarning(RuntimeWarning):
    pass


@dataclass(frozen=True)
class MiniSamplingSpec:
    mini_catchments: Path
    mini_segments: Path
    dem: Path
    mini_ownership: Path
    drainage: Path
    hand: Path
    ltnd: Path
    hru: Path
    output_dir: Path
    workers: int = 4
    memory_limit_mb: int = 4096
    io_slots: int = 2
    batch_size: int = 10_000
    overwrite: bool = False


@dataclass(frozen=True)
class MiniSamplingReport:
    output_dir: Path
    sampled_minis: Path
    mini_count: int
    catchment_cells: int
    reach_cells: int
    hru_class_ids: tuple[int, ...]
    execution: ExecutionReport
    timings: dict[str, float]
    nodata_reports: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _MiniMetadata:
    mini_id: int
    attributes: dict[str, Any]
    longitude: float
    latitude: float
    unit_length: float


@dataclass(frozen=True)
class _SamplingPayload:
    grid: GridSpec
    raster_assets: dict[str, Path]
    packet: RasterBlockPacket
    minis: tuple[_MiniMetadata, ...]
    gdal_cache_bytes: int


@dataclass(frozen=True)
class _PacketResult:
    rows: tuple[dict[str, Any], ...]
    classes: tuple[int, ...]
    nodata: tuple[tuple[int, str, int, int], ...]


def sample_minibasins(
    spec: MiniSamplingSpec, *, progress: ProgressCallback | None = None
) -> MiniSamplingReport:
    """Sample canonical terrain and categorical cells with bounded block reuse."""

    reporter = StageReporter(progress)
    sizing = MemorySizing(spec.memory_limit_mb * 1024**2, spec.workers)
    with raster_cache(sizing.coordinator_cache_bytes):
        report = _sample_minibasins(spec, reporter)
    reporter.finish(report.timings)
    return report


def _sample_minibasins(spec: MiniSamplingSpec, reporter: StageReporter) -> MiniSamplingReport:
    overall_started = time.perf_counter()

    planning_started = time.perf_counter()
    grid = grid_from_dem(spec.dem)
    raster_assets = {
        "dem": Path(spec.dem),
        "hru": Path(spec.hru),
        "cells": Path(spec.mini_ownership),
        "drainage": Path(spec.drainage),
        "hand": Path(spec.hand),
        "ltnd": Path(spec.ltnd),
    }
    _validate_sampling_rasters(raster_assets, grid)
    metadata, units = _plan_sampling(spec, grid)
    sizing = MemorySizing(spec.memory_limit_mb * 1024**2, spec.workers)
    memory_bytes = sizing.limit_bytes
    packets = packet_raster_units_by_block(
        grid,
        units,
        memory_limit_bytes=memory_bytes,
        bytes_per_cell=SAMPLING_BYTES_PER_CELL,
        fixed_bytes=TASK_FIXED_BYTES,
        target_bytes=sizing.packet_bytes,
    )
    metadata_by_key = {f"mini-{value.mini_id:010d}": value for value in metadata.values()}
    items = [
        WorkItem(
            packet.key,
            ordinal,
            packet.estimated_bytes,
            _SamplingPayload(
                grid,
                raster_assets,
                packet,
                tuple(metadata_by_key[unit.key] for unit in packet.units),
                sizing.worker_cache_bytes,
            ),
        )
        for ordinal, packet in enumerate(packets)
    ]
    planning_seconds = time.perf_counter() - planning_started


    reporter.enter("processing", len(items))
    config = ExecutionConfig(
        workers=spec.workers,
        memory_limit_bytes=memory_bytes,
        max_in_flight=2 * spec.workers,
        io_slots=spec.io_slots,
    )
    publisher = AtomicOutputDirectory(spec.output_dir, overwrite=spec.overwrite)
    classes: set[int] = set()
    catchment_cells = 0
    reach_cells = 0
    nodata_findings = []
    csv_seconds = 0.0
    publication_started = time.perf_counter()

    with (
        publisher as staging,
        ArrowPacketStore(staging / ".sampling-packets", sizing.limit_bytes) as store,
    ):

        def reduce_packet(result):
            nonlocal catchment_cells, reach_cells
            started = time.perf_counter()
            value = result.value
            classes.update(value.classes)
            catchment_cells += sum(int(row["_catchment_cells"]) for row in value.rows)
            reach_cells += sum(int(row["_reach_cells"]) for row in value.rows)
            nodata_findings.extend(value.nodata)
            if value.rows:
                store.put(result.ordinal, pa.Table.from_pylist(list(value.rows)))
            return {"packet_staging": time.perf_counter() - started}

        execution = LocalExecutor(config).run(
            items,
            _sampling_worker,
            reduce_packet,
            progress=reporter.execution_progress,
        )

        reporter.enter("finalizing")
        failures = [
            message
            for diagnostics in execution.worker_diagnostics
            for message in diagnostics.get("sampling_failures", ())
        ]
        nodata_filenames = _write_nodata_reports(staging, nodata_findings)
        _warn_nodata(nodata_findings)
        nodata_reports = tuple(Path(spec.output_dir) / name for name in nodata_filenames)
        if failures:
            store.close()
            if nodata_filenames:
                publisher.publish(
                    nodata_filenames,
                    remove=tuple(
                        name
                        for name in NODATA_REPORT_FILENAMES
                        if name not in nodata_filenames
                    ),
                )
            message = "; ".join(failures)
            if nodata_filenames:
                message += f". Nodata reports saved in {spec.output_dir}"
            raise MiniSamplingError(message)

        csv_started = time.perf_counter()
        class_ids = tuple(sorted(classes))
        output = staging / "sampled_minis.csv"
        _assemble_csv(store, output, class_ids)
        csv_seconds = time.perf_counter() - csv_started
        store.close()
        if execution.reduced != len(items) or not output.is_file():
            raise MiniSamplingError("Sampling output is incomplete")
        manifest = write_manifest(staging, "sample-minis", spec)
        published = (output.name, manifest, *nodata_filenames)
        publisher.publish(
            published,
            remove=tuple(
                name for name in NODATA_REPORT_FILENAMES if name not in nodata_filenames
            ),
        )

    publication_seconds = (
        time.perf_counter() - publication_started - execution.wall_seconds - csv_seconds
    )

    timings = {
        "planning": planning_seconds,
        "raster_reads": float(execution.timings.get("raster_reads", 0.0)),
        "computation": float(execution.timings.get("computation", 0.0)),
        "coordination": float(execution.timings.get("coordination", 0.0)),
        "packet_staging": float(execution.timings.get("packet_staging", 0.0)),
        "csv_assembly": csv_seconds,
        "publication": max(0.0, publication_seconds),
        "total": time.perf_counter() - overall_started,
    }
    output_dir = Path(spec.output_dir)
    return MiniSamplingReport(
        output_dir,
        output_dir / "sampled_minis.csv",
        len(metadata),
        catchment_cells,
        reach_cells,
        tuple(sorted(classes)),
        execution,
        timings,
        nodata_reports,
    )


def _validate_sampling_rasters(
    assets: dict[str, Path], grid: GridSpec
) -> None:
    expected_dtypes = {
        "dem": None,
        "hru": None,
        "cells": "int32",
        "drainage": "uint8",
        "hand": "float32",
        "ltnd": "float32",
    }
    for name, path in assets.items():
        with rasterio.open(path) as source:
            _require_grid(source, grid, name)
            if name in {"dem", "hand", "ltnd"}:
                require_metre_units(source, name.upper())
            if name == "hru" and not np.issubdtype(
                np.dtype(source.dtypes[0]), np.integer
            ):
                raise MiniSamplingError("HRU raster must have an integer data type")
            expected = expected_dtypes[name]
            if expected is not None and source.dtypes[0] != expected:
                raise MiniSamplingError(
                    f"{name.upper()} raster has dtype {source.dtypes[0]}, "
                    f"expected {expected}"
                )


def _plan_sampling(
    spec: MiniSamplingSpec,
    grid: GridSpec,
) -> tuple[dict[int, _MiniMetadata], tuple[RasterUnit, ...]]:
    catchments_path = Path(spec.mini_catchments)
    segments_path = Path(spec.mini_segments)
    prepared_index = read_mini_index(spec.mini_ownership)
    catchments = _stream_vector_metadata(
        catchments_path, "catchments", grid.crs, spec.batch_size
    )
    segments = _stream_vector_metadata(
        segments_path, "segments", grid.crs, spec.batch_size
    )
    expected_ids = {mini_id for mini_id, *_ in prepared_index}
    if set(catchments) != expected_ids or set(segments) != expected_ids:
        raise MiniSamplingError(
            "Aggregation vectors and raster mini indexes do not contain the same IDs"
        )

    metadata: dict[int, _MiniMetadata] = {}
    unit_bounds = []
    for mini_id, *bounds in prepared_index:
        catchment = catchments[mini_id]
        segment = segments[mini_id]
        for column in AGGREGATION_COLUMNS[:-1]:
            if column == "unit_length":
                continue
            if not _equal_values(
                catchment["attributes"][column], segment["attributes"][column]
            ):
                raise MiniSamplingError(
                    f"Mini {mini_id} catchment and segment attributes do not match"
                )
        metadata[mini_id] = _MiniMetadata(
            mini_id,
            catchment["attributes"],
            float(segment["longitude"]),
            float(segment["latitude"]),
            float(segment["unit_length"]),
        )
        unit_bounds.append(
            (
                f"mini-{mini_id:010d}",
                tuple(bounds),
            )
        )

    units = plan_raster_units(
        grid,
        unit_bounds,
        bytes_per_cell=SAMPLING_BYTES_PER_CELL,
        fixed_bytes=TASK_FIXED_BYTES,
        block_size=BLOCK_SIZE,
    )
    return metadata, units


def _stream_vector_metadata(
    path: Path, name: str, expected_crs: CRS, batch_size: int
) -> dict[int, dict[str, Any]]:
    provider = inspect_vector_provider(path)
    if provider.crs != expected_crs:
        raise MiniSamplingError(f"{name} CRS does not match the canonical grid")
    allowed = {3, 6} if name == "catchments" else {1, 5}
    result: dict[int, dict[str, Any]] = {}
    transformer = Transformer.from_crs(expected_crs, "EPSG:4326", always_xy=True)
    for batch in iter_provider_batches(
        provider,
        columns=AGGREGATION_COLUMNS[:-1],
        batch_size=batch_size,
        read_geometry=True,
    ):
        table = pa.Table.from_batches([batch])
        geometry_column = geometry_column_name(table)
        geometries = shapely.from_wkb(
            table[geometry_column].combine_chunks().to_numpy(zero_copy_only=False),
            on_invalid="raise",
        )
        if (
            np.any(shapely.is_empty(geometries))
            or np.any(shapely.is_missing(geometries))
            or not set(shapely.get_type_id(geometries).tolist()).issubset(allowed)
        ):
            raise MiniSamplingError(f"{name} contains invalid geometry")
        frame = table.drop([geometry_column]).to_pandas()
        if name == "segments":
            points = shapely.point_on_surface(geometries)
            lonlat = shapely.transform(
                points, transformer.transform, interleaved=False
            )
        for position, row in enumerate(frame.to_dict("records")):
            row = {
                key: (None if pd.isna(value) else value)
                for key, value in row.items()
            }
            mini_id = row["id"]
            if pd.isna(mini_id) or mini_id in result:
                raise MiniSamplingError(
                    f"{name} contains missing or duplicate mini IDs"
                )
            _validate_metric_columns(row, name, mini_id)
            entry: dict[str, Any] = {"attributes": row}
            if name == "segments":
                entry["longitude"] = float(shapely.get_x(lonlat[position]))
                entry["latitude"] = float(shapely.get_y(lonlat[position]))
                length = float(row["unit_length"])
                if length <= 0 or float(shapely.length(geometries[position])) <= 0:
                    raise MiniSamplingError(
                        f"Mini {mini_id} has a zero or invalid reach length"
                    )
                entry["unit_length"] = length
            result[mini_id] = entry
    if not result:
        raise MiniSamplingError(f"{name} is empty")
    return result


def _validate_metric_columns(row: dict[str, Any], name: str, mini_id: Any) -> None:
    for column in (
        "sub",
        "p_order",
        "unit_length",
        "upstream_length",
        "unit_area",
        "upstream_area",
    ):
        value = row[column]
        if not np.isfinite(float(value)):
            raise MiniSamplingError(
                f"Mini {mini_id} contains non-finite aggregation attributes"
            )


def _equal_values(left: Any, right: Any) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    return left == right


def _sampling_worker(
    payload: _SamplingPayload, context: WorkerContext
) -> WorkerOutput[_PacketResult]:
    with raster_cache(payload.gdal_cache_bytes):
        return _sampling_worker_with_cache(payload, context)


def _sampling_worker_with_cache(
    payload: _SamplingPayload, context: WorkerContext
) -> WorkerOutput[_PacketResult]:
    rasters = context.resources.get(
        "sampling-aligned-inputs:"
        + ":".join(
            f"{name}={Path(path).resolve()}"
            for name, path in sorted(payload.raster_assets.items())
        ),
        lambda: AlignedRasterReader(payload.grid, payload.raster_assets, context),
    )
    minis = {value.mini_id: value for value in payload.minis}
    accumulators = {
        label: {
            "dem": [],
            "hand": [],
            "ltnd": [],
            "flooded_area": np.zeros(101, dtype=np.float64),
            "hru": np.zeros(101, dtype=np.int64),
            "catchment_cells": 0,
            "reach_cells": 0,
            "hru_valid_cells": 0,
            "nodata": {
                name: 0 for name in payload.raster_assets if name != "cells"
            },
        }
        for label in minis
    }
    read_seconds = 0.0
    computation_seconds = 0.0
    blocks_read = 0

    for window in payload.packet.blocks:
        started = time.perf_counter()
        arrays = {
            "dem": rasters.read("dem", window, masked=True),
            "hru": rasters.read("hru", window, masked=True),
            "ownership": rasters.read("cells", window, masked=True),
            "drainage": rasters.read("drainage", window, masked=True),
            "hand": rasters.read("hand", window, masked=True),
            "ltnd": rasters.read("ltnd", window, masked=True),
        }
        read_seconds += time.perf_counter() - started
        blocks_read += len(arrays)

        started = time.perf_counter()
        ownership = np.asarray(arrays["ownership"].data)
        owner_valid = ~np.ma.getmaskarray(arrays["ownership"])
        labels = np.unique(ownership[owner_valid])
        for raw_label in labels:
            label = int(raw_label)
            if label not in minis:
                continue
            selected = owner_valid & (ownership == label)
            acc = accumulators[label]
            acc["catchment_cells"] += int(selected.sum())
            valid = {
                name: _valid_cells(arrays[name], selected, label, name.upper())
                for name in ("dem", "hand", "ltnd", "hru", "drainage")
            }
            for name, valid_cells in valid.items():
                acc["nodata"][name] += int((selected & ~valid_cells).sum())

            hru_values = np.asarray(arrays["hru"].data[valid["hru"]])
            if not np.issubdtype(hru_values.dtype, np.integer) or (
                hru_values.size and (hru_values.min() < 1 or hru_values.max() > 100)
            ):
                raise MiniSamplingError(
                    f"Mini {minis[label].mini_id} contains invalid sampled values"
                )
            hand_values = np.asarray(
                arrays["hand"].data[valid["hand"]], dtype=np.float64
            )
            bins = np.searchsorted(np.arange(1, 101), hand_values, side="left")
            acc["flooded_area"] += np.bincount(bins, minlength=101)

            paired = valid["hand"] & valid["ltnd"]
            acc["hand"].append(
                np.asarray(arrays["hand"].data[paired], dtype=np.float64)
            )
            acc["ltnd"].append(
                np.asarray(arrays["ltnd"].data[paired], dtype=np.float64)
            )
            reach = (
                valid["drainage"]
                & (np.asarray(arrays["drainage"].data) != 0)
                & valid["dem"]
            )
            dem_values = np.asarray(arrays["dem"].data[reach], dtype=np.float64)
            if dem_values.size:
                acc["dem"].append(dem_values)
            if hru_values.size:
                acc["hru"] += np.bincount(hru_values, minlength=101)[:101]
                acc["hru_valid_cells"] += int(hru_values.size)
            acc["reach_cells"] += int(reach.sum())
        computation_seconds += time.perf_counter() - started

    rows = []
    classes: set[int] = set()
    nodata = []
    failures = []
    for value in payload.minis:
        acc = accumulators[value.mini_id]
        for name, count in acc["nodata"].items():
            if count:
                nodata.append((value.mini_id, name, count, acc["catchment_cells"]))
        missing_statistics = [
            statistic
            for has_values, statistic in (
                (acc["hru_valid_cells"], "HRU percentages"),
                (acc["flooded_area"].sum(), "flooded area (HAND)"),
                (sum(values.size for values in acc["dem"]), "DEM reach statistics"),
                (
                    sum(values.size for values in acc["hand"]),
                    "paired HAND/LTND statistics",
                ),
            )
            if not has_values
        ]
        if missing_statistics:
            failures.extend(
                f"Mini {value.mini_id} has no valid data for {statistic}"
                for statistic in missing_statistics
            )
            continue
        hand = np.concatenate(acc["hand"])
        ltnd = np.concatenate(acc["ltnd"])
        dem = np.concatenate(acc["dem"])
        maximum_ltnd = float(np.max(ltnd))
        if maximum_ltnd <= 0:
            failures.append(f"Mini {value.mini_id} has non-positive maximum LTND")
            continue
        length_km = value.unit_length
        row = {
            column: value.attributes[column]
            for column in AGGREGATION_COLUMNS
            if column != "geometry"
        }
        reach_percentiles = np.percentile(dem, (10, 50, 85))
        row.update(
            longitude=value.longitude,
            latitude=value.latitude,
            reach_elevation=float(reach_percentiles[1]),
            reach_slope=float(
                (reach_percentiles[2] - reach_percentiles[0]) / (0.75 * length_km)
            ),
            tributary_length=maximum_ltnd / 1000.0,
            tributary_slope=float(
                np.mean(hand[np.isclose(ltnd, maximum_ltnd)]) / (maximum_ltnd / 1000.0)
            ),
            _catchment_cells=int(acc["catchment_cells"]),
            _hru_valid_cells=int(acc["hru_valid_cells"]),
            _reach_cells=int(acc["reach_cells"]),
        )
        row.update(
            {
                f"flooded_area_{stage}": float(area)
                for stage, area in enumerate(
                    _flooded_areas(acc["flooded_area"], value.attributes["unit_area"]),
                    start=1,
                )
            }
        )
        for class_id in range(1, 101):
            count = int(acc["hru"][class_id])
            row[f"_hru_{class_id}"] = count
            if count:
                classes.add(class_id)
        _validate_row(row)
        rows.append(row)

    return WorkerOutput(
        _PacketResult(tuple(rows), tuple(sorted(classes)), tuple(nodata)),
        timings={"raster_reads": read_seconds, "computation": computation_seconds},
        diagnostics={
            "blocks_read": blocks_read,
            "minis": len(rows),
            "catchment_cells": sum(row["_catchment_cells"] for row in rows),
            "reach_cells": sum(row["_reach_cells"] for row in rows),
            **({"sampling_failures": tuple(failures)} if failures else {}),
        },
    )


def _valid_cells(
    array: np.ma.MaskedArray,
    selected: np.ndarray,
    label: int,
    name: str,
) -> np.ndarray:
    data = np.asarray(array.data)
    valid = selected & ~np.ma.getmaskarray(array)
    if np.issubdtype(data.dtype, np.floating):
        values = data[valid]
        if np.isinf(values).any():
            raise MiniSamplingError(f"Mini {label} contains infinite {name} values")
        nan = np.isnan(values)
        if nan.any():
            positions = np.flatnonzero(valid)
            valid.flat[positions[nan]] = False
    return valid


def _flooded_areas(counts: np.ndarray, unit_area: float) -> np.ndarray:
    total = int(counts.sum())
    cumulative = np.cumsum(counts)[:100]
    areas = unit_area * cumulative / total
    areas[cumulative == total] = unit_area
    return areas


def _write_nodata_reports(
    directory: Path,
    findings: list[tuple[int, str, int, int]],
) -> tuple[str, ...]:
    by_dataset: dict[str, list[tuple[int, int, int]]] = {}
    for mini_id, name, count, total in findings:
        by_dataset.setdefault(name, []).append((mini_id, count, total))
    filenames = []
    for name in NODATA_DATASETS:
        values = by_dataset.get(name)
        if not values:
            continue
        filename = f"nodata_{name}.csv"
        with (directory / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(("mini_id", "nodata_cells", "total_cells", "percentage_nodata"))
            writer.writerows(
                (mini_id, count, total, 100 * count / total)
                for mini_id, count, total in sorted(values)
            )
        filenames.append(filename)
    return tuple(filenames)


def _warn_nodata(findings: list[tuple[int, str, int, int]]) -> None:
    if not findings:
        return
    flags = sorted({f"{name}" for _, name, _, _ in findings})
    warnings.warn(
        "Nodata cells were found within the domain for raster(s): "
        + ", ".join(flags)
        + ". Statistics exclude these cells; substantial missing coverage can produce "
        "unrealistic results. Please verify whether the affected results are suitable.",
        _SamplingNodataWarning,
        stacklevel=2,
    )


def _validate_row(row: dict[str, Any]) -> None:
    for key, value in row.items():
        if key == "id_down" and pd.isna(value):
            continue
        if isinstance(value, (float, np.floating)) and not np.isfinite(float(value)):
            raise MiniSamplingError(
                "Sampled mini-basin results contain non-finite values"
            )


def _assemble_csv(store: ArrowPacketStore, output: Path, classes: tuple[int, ...]) -> None:
    if not classes:
        raise MiniSamplingError("Sampled mini domain contains no valid HRU classes")
    first = True
    percentage_columns = [f"hru_{value}" for value in classes]
    output_columns = (
        [column for column in AGGREGATION_COLUMNS if column != "geometry"]
        + [
            "longitude",
            "latitude",
            "reach_slope",
            "reach_elevation",
            "tributary_length",
            "tributary_slope",
        ]
        + percentage_columns
        + [f"flooded_area_{stage}" for stage in range(1, 101)]
    )
    keys = sorted(store.keys)
    if not keys:
        raise MiniSamplingError("Sampling produced no packet results")
    for key in keys:
        frame = store.pop(key).to_pandas()
        denominators = frame["_hru_valid_cells"].to_numpy(dtype=float)
        if np.any(denominators <= 0):
            raise MiniSamplingError("Sampling produced no valid HRU cells")
        for class_id in classes:
            frame[f"hru_{class_id}"] = (
                100.0 * frame[f"_hru_{class_id}"].to_numpy(dtype=float) / denominators
            )
        if not np.allclose(frame[percentage_columns].sum(axis=1), 100.0):
            raise MiniSamplingError("HRU percentages do not sum to 100%")
        frame[output_columns].to_csv(
            output,
            mode="w" if first else "a",
            header=first,
            index=False,
            lineterminator="\n",
        )
        first = False
