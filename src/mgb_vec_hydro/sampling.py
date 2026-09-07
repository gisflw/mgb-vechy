from __future__ import annotations

import json
import pickle
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import shapely
from affine import Affine
from pyarrow import ipc
from pyproj import CRS, Transformer

from mgb_vec_hydro.aggregation import (
    AGGREGATION_CONTRACT,
    AGGREGATION_CONTRACT_VERSION,
    INPUT_COLUMNS,
)
from mgb_vec_hydro.exceptions import MiniSamplingError
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
    RasterBlockPacket,
    RasterUnit,
    packet_raster_units_by_block,
    plan_raster_units,
    prepared_grid,
)
from mgb_vec_hydro.execution.vector import (
    geometry_column_name,
    inspect_vector_provider,
    iter_provider_batches,
)
from mgb_vec_hydro.preparation import BLOCK_SIZE, GridSpec, PreparedDataset
from mgb_vec_hydro.terrain import TerrainDataset

MAX_PACKET_UNITS = 8
SAMPLING_BYTES_PER_CELL = 64
TASK_FIXED_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class MiniSamplingSpec:
    minis: Path
    prepared: Path
    terrain: Path
    output_dir: Path
    hru_name: str = "hru"
    workers: int = 4
    memory_limit_mb: int = 512
    io_slots: int = 2
    batch_size: int = 10_000
    checkpoint_dir: Path | None = None


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


@dataclass(frozen=True)
class _MiniMetadata:
    label: int
    mini_id: Any
    attributes: dict[str, Any]
    longitude: float
    latitude: float
    unit_length: float


@dataclass(frozen=True)
class _SamplingPayload:
    prepared: Path
    grid: GridSpec
    terrain_assets: dict[str, Path]
    hru_name: str
    packet: RasterBlockPacket
    minis: tuple[_MiniMetadata, ...]


@dataclass(frozen=True)
class _PacketResult:
    rows: tuple[dict[str, Any], ...]
    classes: tuple[int, ...]


class _PickleCheckpointCodec:
    suffix = ".pkl"

    def dump(self, value: _PacketResult, path: Path) -> None:
        with path.open("wb") as stream:
            pickle.dump(value, stream, protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, path: Path) -> _PacketResult:
        with path.open("rb") as stream:
            return pickle.load(stream)


def sample_minibasins(spec: MiniSamplingSpec) -> MiniSamplingReport:
    """Sample canonical terrain and categorical cells with bounded block reuse."""

    overall_started = time.perf_counter()
    _validate_spec(spec)

    planning_started = time.perf_counter()
    prepared = PreparedDataset.open(spec.prepared)
    prepared.validate()
    terrain = TerrainDataset.open(spec.terrain)
    terrain.validate()
    grid = prepared_grid(spec.prepared)
    _validate_dataset_relationships(spec, prepared, terrain, grid)
    metadata, units, manifests = _plan_sampling(spec, prepared, terrain, grid)
    memory_bytes = spec.memory_limit_mb * 1024 * 1024
    packets = packet_raster_units_by_block(
        grid,
        units,
        memory_limit_bytes=memory_bytes,
        bytes_per_cell=SAMPLING_BYTES_PER_CELL,
        fixed_bytes=TASK_FIXED_BYTES,
        max_units=MAX_PACKET_UNITS,
    )
    metadata_by_key = {f"mini-{value.label:010d}": value for value in metadata.values()}
    terrain_assets = {
        name: terrain.path(name)
        for name in ("mini_ownership", "drainage", "hand", "ltnd")
    }
    items = [
        WorkItem(
            packet.key,
            ordinal,
            packet.estimated_bytes,
            _SamplingPayload(
                Path(spec.prepared),
                grid,
                terrain_assets,
                spec.hru_name,
                packet,
                tuple(metadata_by_key[unit.key] for unit in packet.units),
            ),
        )
        for ordinal, packet in enumerate(packets)
    ]
    planning_seconds = time.perf_counter() - planning_started

    checkpoint = None
    if spec.checkpoint_dir is not None:
        fingerprint = execution_fingerprint(
            algorithm="sample-minis",
            version="2",
            prepared_manifest=manifests,
            parameters={"hru_name": spec.hru_name},
            work_items=items,
        )
        checkpoint = CheckpointStore(
            spec.checkpoint_dir, fingerprint, _PickleCheckpointCodec()
        )

    config = ExecutionConfig(
        workers=spec.workers,
        memory_limit_bytes=memory_bytes,
        max_in_flight=spec.workers,
        io_slots=spec.io_slots,
    )
    publisher = AtomicOutputDirectory(spec.output_dir)
    classes: set[int] = set()
    catchment_cells = 0
    reach_cells = 0
    csv_seconds = 0.0
    publication_started = time.perf_counter()

    with publisher as staging:
        packet_root = staging / ".sampling-packets"
        packet_root.mkdir()

        def reduce_packet(result):
            nonlocal catchment_cells, reach_cells
            started = time.perf_counter()
            value = result.value
            classes.update(value.classes)
            catchment_cells += sum(int(row["_catchment_cells"]) for row in value.rows)
            reach_cells += sum(int(row["_reach_cells"]) for row in value.rows)
            table = pa.Table.from_pylist(list(value.rows))
            path = packet_root / f"{result.ordinal:012d}.arrow"
            with (
                path.open("wb") as stream,
                ipc.new_file(stream, table.schema) as writer,
            ):
                writer.write_table(table)
            return {"packet_staging": time.perf_counter() - started}

        execution = LocalExecutor(config).run(
            items,
            _sampling_worker,
            reduce_packet,
            checkpoint=checkpoint,
        )

        csv_started = time.perf_counter()
        class_ids = tuple(sorted(classes))
        output = staging / "sampled_minis.csv"
        _assemble_csv(packet_root, output, class_ids)
        csv_seconds = time.perf_counter() - csv_started
        shutil.rmtree(packet_root)
        if execution.reduced != len(items) or not output.is_file():
            raise MiniSamplingError("Sampling output is incomplete")
        publisher.publish((output.name,))

    publication_seconds = (
        time.perf_counter() - publication_started - execution.wall_seconds - csv_seconds
    )
    if checkpoint is not None:
        checkpoint.cleanup()
    if spec.checkpoint_dir is not None:
        try:
            Path(spec.checkpoint_dir).rmdir()
        except OSError:
            pass

    timings = {
        "planning": planning_seconds,
        "raster_reads": float(execution.timings.get("raster_reads", 0.0)),
        "computation": float(execution.timings.get("computation", 0.0)),
        "coordination": float(execution.timings.get("coordination", 0.0)),
        "checkpointing": float(execution.timings.get("checkpoint_write", 0.0)),
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
    )


def _validate_spec(spec: MiniSamplingSpec) -> None:
    for name, value in (
        ("workers", spec.workers),
        ("memory limit", spec.memory_limit_mb),
        ("I/O slots", spec.io_slots),
        ("batch size", spec.batch_size),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MiniSamplingError(f"{name} must be a positive integer")
    if spec.workers > 4:
        raise MiniSamplingError("workers cannot exceed four")
    for name, path in (
        ("minis", spec.minis),
        ("prepared", spec.prepared),
        ("terrain", spec.terrain),
    ):
        if not Path(path).is_dir():
            raise MiniSamplingError(f"{name} input is not a directory: {path}")
    if (
        not isinstance(spec.hru_name, str)
        or not spec.hru_name
        or spec.hru_name in {"dem", "d8"}
    ):
        raise MiniSamplingError("HRU asset name is invalid")
    output = Path(spec.output_dir)
    if output.exists():
        raise MiniSamplingError(f"Output directory already exists: {output}")
    if spec.checkpoint_dir is not None:
        checkpoint = Path(spec.checkpoint_dir).resolve()
        try:
            checkpoint.relative_to(output.resolve())
        except ValueError:
            pass
        else:
            raise MiniSamplingError(
                "Checkpoint directory cannot be inside the output directory"
            )


def _validate_dataset_relationships(
    spec: MiniSamplingSpec,
    prepared: PreparedDataset,
    terrain: TerrainDataset,
    grid: GridSpec,
) -> None:
    try:
        asset = prepared.manifest["assets"]["rasters"][spec.hru_name]
    except KeyError as exc:
        raise MiniSamplingError(
            f"Prepared dataset has no categorical raster: {spec.hru_name}"
        ) from exc
    if asset.get("role") != "categorical":
        raise MiniSamplingError(f"Prepared raster {spec.hru_name} is not categorical")
    try:
        dtype = np.dtype(asset["dtype"])
    except (KeyError, TypeError) as exc:
        raise MiniSamplingError("Prepared HRU raster dtype is invalid") from exc
    if not np.issubdtype(dtype, np.integer):
        raise MiniSamplingError("HRU raster must have an integer data type")

    try:
        value = terrain.manifest["grid"]
        terrain_grid = GridSpec(
            CRS.from_wkt(value["crs_wkt"]),
            Affine(*value["transform"]),
            int(value["width"]),
            int(value["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MiniSamplingError("Terrain canonical grid is invalid") from exc
    if terrain_grid != grid:
        raise MiniSamplingError("Prepared and terrain canonical grids do not match")


def _aggregation_manifest(root: Path) -> tuple[dict[str, Any], Path, Path]:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if (
            manifest.get("contract") != AGGREGATION_CONTRACT
            or manifest.get("version") != AGGREGATION_CONTRACT_VERSION
        ):
            raise MiniSamplingError(
                "Unsupported aggregation dataset contract or version"
            )
        assets = manifest["assets"]
        paths = []
        for name in ("catchments", "segments"):
            relative = Path(assets[name]["path"])
            candidate = (root / relative).resolve()
            candidate.relative_to(root.resolve())
            if not candidate.is_file() or assets[name].get("driver") != "FlatGeobuf":
                raise MiniSamplingError(f"Invalid aggregation asset: {name}")
            paths.append(candidate)
    except MiniSamplingError:
        raise
    except Exception as exc:
        raise MiniSamplingError("Cannot read aggregation dataset") from exc
    return manifest, paths[0], paths[1]


def _read_index(path: Path, expected_columns: list[str], name: str) -> pd.DataFrame:
    try:
        table = pd.read_parquet(path)
    except Exception as exc:
        raise MiniSamplingError(f"Cannot read {name} mini index") from exc
    if list(table.columns) != expected_columns or table.empty:
        raise MiniSamplingError(f"{name} mini index schema is invalid")
    if (
        table["mini_label"].dtype != np.dtype("int32")
        or table["mini_label"].duplicated().any()
        or table["mini_id"].isna().any()
        or table["mini_id"].duplicated().any()
        or not np.array_equal(
            table["mini_label"].to_numpy(),
            np.arange(1, len(table) + 1, dtype="int32"),
        )
    ):
        raise MiniSamplingError(f"{name} mini index values are invalid")
    return table


def _plan_sampling(
    spec: MiniSamplingSpec,
    prepared: PreparedDataset,
    terrain: TerrainDataset,
    grid: GridSpec,
) -> tuple[dict[Any, _MiniMetadata], tuple[RasterUnit, ...], dict[str, Any]]:
    aggregation_manifest, catchments_path, segments_path = _aggregation_manifest(
        Path(spec.minis)
    )
    prepared_index_path = prepared.asset_path(
        prepared.manifest["assets"]["mini_index"]["path"]
    )
    prepared_index = _read_index(
        prepared_index_path,
        ["mini_label", "mini_id", "minx", "miny", "maxx", "maxy"],
        "prepared",
    )
    terrain_index = _read_index(
        terrain.path("mini_index"), ["mini_label", "mini_id"], "terrain"
    )
    if not prepared_index[["mini_label", "mini_id"]].equals(terrain_index):
        raise MiniSamplingError("Prepared and terrain mini indexes do not match")

    catchments = _stream_vector_metadata(
        catchments_path, "catchments", grid.crs, spec.batch_size
    )
    segments = _stream_vector_metadata(
        segments_path, "segments", grid.crs, spec.batch_size
    )
    expected_ids = set(prepared_index["mini_id"].tolist())
    if set(catchments) != expected_ids or set(segments) != expected_ids:
        raise MiniSamplingError(
            "Aggregation vectors and raster mini indexes do not contain the same IDs"
        )

    metadata: dict[Any, _MiniMetadata] = {}
    unit_bounds = []
    for row in prepared_index.itertuples(index=False):
        mini_id = row.mini_id
        catchment = catchments[mini_id]
        segment = segments[mini_id]
        for column in INPUT_COLUMNS[:-1]:
            if column == "unit_length":
                continue
            if not _equal_values(
                catchment["attributes"][column], segment["attributes"][column]
            ):
                raise MiniSamplingError(
                    f"Mini {mini_id} catchment and segment attributes do not match"
                )
        label = int(row.mini_label)
        metadata[mini_id] = _MiniMetadata(
            label,
            mini_id,
            catchment["attributes"],
            float(catchment["longitude"]),
            float(catchment["latitude"]),
            float(segment["unit_length"]),
        )
        unit_bounds.append(
            (
                f"mini-{label:010d}",
                (float(row.minx), float(row.miny), float(row.maxx), float(row.maxy)),
            )
        )

    units = plan_raster_units(
        grid,
        unit_bounds,
        bytes_per_cell=SAMPLING_BYTES_PER_CELL,
        fixed_bytes=TASK_FIXED_BYTES,
        block_size=BLOCK_SIZE,
    )
    manifests = {
        "aggregation": aggregation_manifest,
        "prepared": prepared.manifest,
        "terrain": terrain.manifest,
    }
    return metadata, units, manifests


def _stream_vector_metadata(
    path: Path, name: str, expected_crs: CRS, batch_size: int
) -> dict[Any, dict[str, Any]]:
    try:
        provider = inspect_vector_provider(path)
        if provider.crs != expected_crs:
            raise MiniSamplingError(f"{name} CRS does not match the canonical grid")
        if tuple(provider.fields) != tuple(INPUT_COLUMNS[:-1]):
            raise MiniSamplingError(f"{name} must have the exact aggregation schema")
        allowed = {3, 6} if name == "catchments" else {1, 5}
        result: dict[Any, dict[str, Any]] = {}
        transformer = Transformer.from_crs(expected_crs, "EPSG:4326", always_xy=True)
        for batch in iter_provider_batches(
            provider,
            columns=INPUT_COLUMNS[:-1],
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
            if name == "catchments":
                centroids = shapely.centroid(geometries)
                lonlat = shapely.transform(
                    centroids, transformer.transform, interleaved=False
                )
            for position, row in enumerate(frame.to_dict("records")):
                row = {
                    key: (None if _is_scalar_missing(value) else value)
                    for key, value in row.items()
                }
                mini_id = row["id"]
                if pd.isna(mini_id) or mini_id in result:
                    raise MiniSamplingError(
                        f"{name} contains missing or duplicate mini IDs"
                    )
                _validate_metric_columns(row, name, mini_id)
                entry: dict[str, Any] = {"attributes": row}
                if name == "catchments":
                    entry["longitude"] = float(shapely.get_x(lonlat[position]))
                    entry["latitude"] = float(shapely.get_y(lonlat[position]))
                else:
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
    except MiniSamplingError:
        raise
    except Exception as exc:
        raise MiniSamplingError(f"Cannot stream aggregation {name}") from exc


def _validate_metric_columns(row: dict[str, Any], name: str, mini_id: Any) -> None:
    for column in (
        "sub",
        "strahler_order",
        "unit_length",
        "upstream_length",
        "unit_area",
        "upstream_area",
    ):
        value = row[column]
        if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
            raise MiniSamplingError(f"{name} has non-numeric metric column {column}")
        if not np.isfinite(float(value)):
            raise MiniSamplingError(
                f"Mini {mini_id} contains non-finite aggregation attributes"
            )


def _is_scalar_missing(value: Any) -> bool:
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(result) if isinstance(result, (bool, np.bool_)) else False


def _equal_values(left: Any, right: Any) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    return left == right


def _sampling_worker(
    payload: _SamplingPayload, context: WorkerContext
) -> WorkerOutput[_PacketResult]:
    prepared = PreparedRasterReader(payload.prepared, context)
    terrain = AlignedRasterReader(payload.grid, payload.terrain_assets, context)
    minis = {value.label: value for value in payload.minis}
    accumulators = {
        label: {
            "dem": [],
            "hand": [],
            "ltnd": [],
            "hru": np.zeros(101, dtype=np.int64),
            "catchment_cells": 0,
            "reach_cells": 0,
        }
        for label in minis
    }
    read_seconds = 0.0
    computation_seconds = 0.0
    blocks_read = 0

    for window in payload.packet.blocks:
        started = time.perf_counter()
        arrays = {
            "dem": prepared.read("dem", window, masked=True),
            "hru": prepared.read(payload.hru_name, window, masked=True),
            "ownership": terrain.read("mini_ownership", window, masked=True),
            "drainage": terrain.read("drainage", window, masked=True),
            "hand": terrain.read("hand", window, masked=True),
            "ltnd": terrain.read("ltnd", window, masked=True),
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
            _require_valid(arrays["hand"], selected, label, "HAND")
            _require_valid(arrays["ltnd"], selected, label, "LTND")
            _require_valid(arrays["hru"], selected, label, "HRU")
            _require_valid(arrays["drainage"], selected, label, "drainage")
            hand_values = np.asarray(arrays["hand"].data[selected], dtype=np.float64)
            ltnd_values = np.asarray(arrays["ltnd"].data[selected], dtype=np.float64)
            hru_values = np.asarray(arrays["hru"].data[selected])
            if (
                not np.isfinite(hand_values).all()
                or not np.isfinite(ltnd_values).all()
                or not np.issubdtype(hru_values.dtype, np.integer)
                or (
                    hru_values.size and (hru_values.min() < 1 or hru_values.max() > 100)
                )
            ):
                raise MiniSamplingError(
                    f"Mini {minis[label].mini_id} contains invalid sampled values"
                )
            drainage = np.asarray(arrays["drainage"].data) != 0
            reach = selected & drainage
            _require_valid(arrays["dem"], reach, label, "DEM")
            dem_values = np.asarray(arrays["dem"].data[reach], dtype=np.float64)
            if not np.isfinite(dem_values).all():
                raise MiniSamplingError(
                    f"Mini {minis[label].mini_id} contains non-finite DEM values"
                )
            acc["hand"].append(hand_values)
            acc["ltnd"].append(ltnd_values)
            if dem_values.size:
                acc["dem"].append(dem_values)
            acc["hru"] += np.bincount(hru_values, minlength=101)[:101]
            acc["catchment_cells"] += int(selected.sum())
            acc["reach_cells"] += int(reach.sum())
        computation_seconds += time.perf_counter() - started

    rows = []
    classes: set[int] = set()
    for value in payload.minis:
        acc = accumulators[value.label]
        if not acc["hand"]:
            raise MiniSamplingError(
                f"Mini {value.mini_id} has no sampled catchment raster cells"
            )
        if not acc["dem"]:
            raise MiniSamplingError(
                f"Mini {value.mini_id} has no sampled DEM reach cells"
            )
        hand = np.concatenate(acc["hand"])
        ltnd = np.concatenate(acc["ltnd"])
        dem = np.concatenate(acc["dem"])
        maximum_ltnd = float(np.max(ltnd))
        if maximum_ltnd <= 0:
            raise MiniSamplingError(
                f"Mini {value.mini_id} has non-positive maximum LTND"
            )
        length_km = value.unit_length
        row = {
            column: value.attributes[column]
            for column in INPUT_COLUMNS
            if column != "geometry"
        }
        row.update(
            longitude=value.longitude,
            latitude=value.latitude,
            reach_slope_m_per_km=float(
                (np.percentile(dem, 85) - np.percentile(dem, 10)) / (0.75 * length_km)
            ),
            tributary_length_km=maximum_ltnd / 1000.0,
            tributary_slope_m_per_km=float(
                np.mean(hand[np.isclose(ltnd, maximum_ltnd)]) / (maximum_ltnd / 1000.0)
            ),
            _catchment_cells=int(acc["catchment_cells"]),
            _reach_cells=int(acc["reach_cells"]),
        )
        for class_id in range(1, 101):
            count = int(acc["hru"][class_id])
            row[f"_hru_{class_id}"] = count
            if count:
                classes.add(class_id)
        _validate_row(row)
        rows.append(row)

    return WorkerOutput(
        _PacketResult(tuple(rows), tuple(sorted(classes))),
        timings={"raster_reads": read_seconds, "computation": computation_seconds},
        diagnostics={
            "blocks_read": blocks_read,
            "minis": len(rows),
            "catchment_cells": sum(row["_catchment_cells"] for row in rows),
            "reach_cells": sum(row["_reach_cells"] for row in rows),
        },
    )


def _require_valid(
    array: np.ma.MaskedArray,
    selected: np.ndarray,
    label: int,
    name: str,
) -> None:
    if np.ma.getmaskarray(array)[selected].any():
        raise MiniSamplingError(f"Mini label {label} contains {name} nodata")


def _validate_row(row: dict[str, Any]) -> None:
    for key, value in row.items():
        if key == "id_down" and pd.isna(value):
            continue
        if isinstance(value, (float, np.floating)) and not np.isfinite(float(value)):
            raise MiniSamplingError(
                "Sampled mini-basin results contain non-finite values"
            )


def _assemble_csv(packet_root: Path, output: Path, classes: tuple[int, ...]) -> None:
    if not classes:
        raise MiniSamplingError("Sampled mini domain contains no valid HRU classes")
    first = True
    percentage_columns = [f"hru_{value}_pct" for value in classes]
    output_columns = (
        [column for column in INPUT_COLUMNS if column != "geometry"]
        + [
            "longitude",
            "latitude",
            "reach_slope_m_per_km",
            "tributary_length_km",
            "tributary_slope_m_per_km",
        ]
        + percentage_columns
    )
    paths = sorted(packet_root.glob("*.arrow"))
    if not paths:
        raise MiniSamplingError("Sampling produced no packet results")
    for path in paths:
        with path.open("rb") as stream:
            frame = ipc.open_file(stream).read_all().to_pandas()
        denominators = frame["_catchment_cells"].to_numpy(dtype=float)
        if np.any(denominators <= 0):
            raise MiniSamplingError("Sampling produced an empty catchment")
        for class_id in classes:
            frame[f"hru_{class_id}_pct"] = (
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
