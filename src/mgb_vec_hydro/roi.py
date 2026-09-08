"""Raw-provider ROI selection and normalized Stage 1 publication."""

from __future__ import annotations

import shutil
import time
from collections import defaultdict
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyogrio
from pyproj import CRS, Geod, Transformer
import shapely

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    InvalidInputSchemaError,
    OutletNotFoundError,
    PreparedDataError,
    TopologyCycleError,
)
from mgb_vec_hydro.execution.checkpoints import (
    CheckpointStore,
    execution_fingerprint,
    file_identity,
)
from mgb_vec_hydro.execution.executor import ExecutionConfig, LocalExecutor, WorkItem
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.vector import (
    VectorTable,
    VectorTableCheckpointCodec,
    inspect_vector_provider,
    vector_table_from_arrow,
    write_vector_table,
)
from mgb_vec_hydro.crs_utils import parse_crs

DEFAULT_STRAHLER_ORDER_COL = "strahler_order"
ROI_COLUMNS = [
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


@dataclass(frozen=True)
class RoiSpec:
    crs: str
    catchments: Path
    segments: Path
    outlet_ids: tuple[str, ...]
    id_col: str
    id_down_col: str
    strahler_order_col: str
    output_dir: Path
    catchments_layer: str | None = None
    segments_layer: str | None = None
    catchments_source_crs: str | None = None
    segments_source_crs: str | None = None
    workers: int = 4
    memory_limit_mb: int = 512
    io_slots: int = 2
    batch_size: int = 10_000
    checkpoint_dir: Path | None = None


@dataclass(frozen=True)
class RoiReport:
    output_dir: Path
    catchments: Path
    segments: Path
    catchment_count: int
    segment_count: int
    timings: dict[str, float]


@dataclass(frozen=True)
class _Provider:
    path: Path
    layer: str | None
    info: dict[str, Any]
    crs: CRS
    fields: dict[str, str]


@dataclass(frozen=True)
class _GeometryPacket:
    provider: _Provider
    ids: tuple[Hashable, ...]
    fids: tuple[int, ...]
    kind: str
    target_crs_wkt: str


def define_roi_dataset(spec: RoiSpec) -> RoiReport:
    """Select from raw providers and atomically publish a normalized ROI."""
    overall_started = time.perf_counter()
    _validate_spec(spec)
    target_crs = parse_crs(spec.crs)
    phase_started = time.perf_counter()
    segment_provider = _provider(
        spec.segments,
        spec.segments_layer,
        spec.segments_source_crs,
        {
            "id": spec.id_col,
            "id_down": spec.id_down_col,
            "strahler_order": spec.strahler_order_col,
        },
        "segments",
    )
    catchment_provider = _provider(
        spec.catchments,
        spec.catchments_layer,
        spec.catchments_source_crs,
        {"id": spec.id_col},
        "catchments",
    )
    topology, segment_fids = _read_topology(segment_provider, spec.batch_size)
    outlet_ids = _coerce_outlets(spec.outlet_ids, topology["id"])
    selected_ids, sub_by_id = _select_topology(topology, outlet_ids)
    selected = topology.loc[topology["id"].isin(selected_ids)].copy()
    _validate_selected_attributes(selected)
    provider_topology_seconds = time.perf_counter() - phase_started

    memory_bytes = spec.memory_limit_mb * 1024 * 1024
    output = Path(spec.output_dir)
    publisher = AtomicOutputDirectory(output)
    checkpoint = None
    with publisher as staging:
        phase_started = time.perf_counter()
        catchment_fids = _scan_fids(catchment_provider, spec.batch_size)
        missing = selected_ids - set(catchment_fids)
        if missing:
            raise InvalidInputSchemaError(
                "Selected catchment ID(s) are missing: "
                + ", ".join(map(str, sorted(missing, key=str)))
            )
        items, kinds = _geometry_work_items(
            selected_ids,
            segment_provider,
            catchment_provider,
            segment_fids,
            catchment_fids,
            target_crs,
            batch_size=spec.batch_size,
            memory_bytes=memory_bytes,
            workers=spec.workers,
        )
        checkpoint = _roi_checkpoint(spec, target_crs, items)
        packet_dir = staging / ".packets"
        packet_dir.mkdir()
        codec = VectorTableCheckpointCodec()

        def reduce_packet(result):
            codec.dump(result.value, packet_dir / f"{result.ordinal:012d}.arrow")
            return None

        execution = LocalExecutor(
            ExecutionConfig(
                workers=spec.workers,
                memory_limit_bytes=memory_bytes,
                io_slots=spec.io_slots,
            )
        ).run(items, _process_geometry_packet, reduce_packet, checkpoint=checkpoint)
        metrics: dict[Hashable, dict[str, float]] = {}
        for ordinal in range(len(items)):
            packet = codec.load(packet_dir / f"{ordinal:012d}.arrow")
            key = "unit_length" if kinds[ordinal] == "segments" else "unit_area"
            for segment_id, value in zip(
                packet.table["id"].to_pylist(),
                packet.table["unit_metric"].to_pylist(),
                strict=True,
            ):
                metrics.setdefault(segment_id, {})[key] = float(value)
        if set(metrics) != selected_ids or any(
            "unit_length" not in value or "unit_area" not in value
            for value in metrics.values()
        ):
            raise InvalidInputSchemaError(
                "Selected segment and catchment IDs do not match"
            )
        upstream_length, upstream_area = _upstream_metrics(selected, metrics)
        metric_attributes = _metric_attributes(
            selected, sub_by_id, metrics, upstream_length, upstream_area
        )
        water_course = _water_course_by_segment(metric_attributes)
        metrics_seconds = time.perf_counter() - phase_started

        phase_started = time.perf_counter()
        catchments_path = staging / "roi_catchments.fgb"
        segments_path = staging / "roi_segments.fgb"
        _write_cached_outputs(
            packet_dir,
            kinds,
            selected,
            sub_by_id,
            metrics,
            upstream_length,
            upstream_area,
            water_course,
            target_crs,
            catchments_path=catchments_path,
            segments_path=segments_path,
        )
        shutil.rmtree(packet_dir)
        _validate_roi_outputs(
            catchments_path,
            segments_path,
            target_crs=target_crs,
            feature_count=len(selected_ids),
        )
        publisher.publish((catchments_path.name, segments_path.name))
    output_publication_seconds = time.perf_counter() - phase_started

    if checkpoint is not None:
        checkpoint.cleanup()
    return RoiReport(
        output,
        output / "roi_catchments.fgb",
        output / "roi_segments.fgb",
        len(selected_ids),
        len(selected_ids),
        {
            "provider_topology": provider_topology_seconds,
            "metrics": metrics_seconds,
            "geometry_execution": execution.wall_seconds,
            "output_publication": output_publication_seconds,
            "total": time.perf_counter() - overall_started,
        },
    )


def _roi_checkpoint(
    spec: RoiSpec, target_crs: CRS, work_items: Iterable[WorkItem[Any]]
) -> CheckpointStore[Any] | None:
    if spec.checkpoint_dir is None:
        return None
    fingerprint = execution_fingerprint(
        algorithm="define-roi",
        version="2",
        input_identity={
            "catchments": file_identity(spec.catchments),
            "segments": file_identity(spec.segments),
            "target_crs": target_crs.to_wkt(version="WKT2_2019", pretty=False),
        },
        parameters={
            "outlets": list(spec.outlet_ids),
            "columns": [spec.id_col, spec.id_down_col, spec.strahler_order_col],
            "layers": [spec.catchments_layer, spec.segments_layer],
            "source_crs": [spec.catchments_source_crs, spec.segments_source_crs],
        },
        work_items=work_items,
    )
    return CheckpointStore(
        spec.checkpoint_dir, fingerprint, VectorTableCheckpointCodec()
    )


def _geometry_work_items(
    ids: set[Hashable],
    segment_provider: _Provider,
    catchment_provider: _Provider,
    segment_fids: dict[Hashable, int],
    catchment_fids: dict[Hashable, int],
    target_crs: CRS,
    *,
    batch_size: int,
    memory_bytes: int,
    workers: int,
) -> tuple[list[WorkItem[_GeometryPacket]], dict[int, str]]:
    ordered = sorted(ids, key=lambda value: (type(value).__name__, str(value)))
    packet_rows = max(1, min(batch_size, memory_bytes // max(1, workers) // 4096))
    items: list[WorkItem[_GeometryPacket]] = []
    kinds: dict[int, str] = {}
    for provider, fid_by_id, kind in (
        (segment_provider, segment_fids, "segments"),
        (catchment_provider, catchment_fids, "catchments"),
    ):
        for start in range(0, len(ordered), packet_rows):
            ordinal = len(items)
            packet_ids = tuple(ordered[start : start + packet_rows])
            fids = tuple(fid_by_id[value] for value in packet_ids)
            estimate = max(1, min(memory_bytes // max(1, workers), len(fids) * 4096))
            items.append(
                WorkItem(
                    f"{kind}-{start:012d}",
                    ordinal,
                    estimate,
                    _GeometryPacket(
                        provider, packet_ids, fids, kind, target_crs.to_wkt()
                    ),
                )
            )
            kinds[ordinal] = kind
    return items, kinds


def _process_geometry_packet(payload: _GeometryPacket, context) -> VectorTable:
    provider = payload.provider
    try:
        with context.io_bound():
            metadata, table = pyogrio.read_arrow(
                provider.path,
                layer=provider.layer,
                columns=list(dict.fromkeys(provider.fields.values())),
                fids=list(payload.fids),
            )
    except Exception as exc:
        raise InvalidInputSchemaError(
            "Cannot read selected provider geometry packet"
        ) from exc
    frame = _normalize_vector_table(
        _provider_vector(metadata, table, provider), provider
    )
    returned = frame.table["id"].to_pylist()
    if len(returned) != len(set(returned)) or set(returned) != set(payload.ids):
        raise InvalidInputSchemaError(
            "Provider geometry packet did not return the exact requested IDs"
        )
    geometries = frame.geometries()
    allowed = (
        {"LineString", "MultiLineString"}
        if payload.kind == "segments"
        else {"Polygon", "MultiPolygon"}
    )
    _validate_geometries(geometries, payload.kind, allowed)
    metrics = [_geometry_metric(value, provider, payload.kind) for value in geometries]
    target_crs = CRS.from_wkt(payload.target_crs_wkt)
    if provider.crs != target_crs:
        transformer = Transformer.from_crs(provider.crs, target_crs, always_xy=True)
        geometries = shapely.transform(
            geometries, transformer.transform, interleaved=False
        )
    result = pa.table(
        {
            "id": frame.table["id"],
            "unit_metric": pa.array(metrics, type=pa.float64()),
            "geometry": pa.array(shapely.to_wkb(geometries), type=pa.binary()),
        }
    )
    return VectorTable(result, target_crs, "geometry", frame.geometry_type)


def _write_cached_outputs(
    packet_dir: Path,
    kinds: dict[int, str],
    attrs: pd.DataFrame,
    sub_by_id,
    metrics,
    upstream_length,
    upstream_area,
    water_course,
    target_crs: CRS,
    *,
    catchments_path: Path,
    segments_path: Path,
) -> None:
    lookup = attrs.set_index("id").to_dict("index")
    id_type = pa.array(attrs["id"].tolist()).type
    codec = VectorTableCheckpointCodec()
    first = {"segments": True, "catchments": True}
    paths = {"segments": segments_path, "catchments": catchments_path}
    for ordinal in range(len(kinds)):
        kind = kinds[ordinal]
        packet = codec.load(packet_dir / f"{ordinal:012d}.arrow")
        rows = []
        for segment_id, geometry in zip(
            packet.table["id"].to_pylist(),
            packet.table["geometry"].to_pylist(),
            strict=True,
        ):
            item = lookup[segment_id]
            rows.append(
                {
                    "id": segment_id,
                    "id_down": item["id_down"],
                    "sub": sub_by_id[segment_id],
                    "strahler_order": int(item["strahler_order"]),
                    "unit_length": metrics[segment_id]["unit_length"],
                    "upstream_length": upstream_length[segment_id],
                    "unit_area": metrics[segment_id]["unit_area"],
                    "upstream_area": upstream_area[segment_id],
                    "water_course": water_course[segment_id],
                    "geometry": geometry,
                }
            )
        output = _rows_to_vector(rows, id_type, target_crs, packet.geometry_type)
        _write_fgb(output, paths[kind], append=not first[kind])
        first[kind] = False


def define_roi(spec: RoiSpec) -> RoiReport:
    """Public Stage 1 entry point."""
    return define_roi_dataset(spec)


def _validate_spec(spec: RoiSpec) -> None:
    for label, path in (("catchments", spec.catchments), ("segments", spec.segments)):
        if not Path(path).exists():
            raise PreparedDataError(f"{label} input does not exist: {path}")
    if not spec.outlet_ids:
        raise InvalidInputSchemaError("At least one outlet ID is required")
    for name, value in (
        ("workers", spec.workers),
        ("memory limit", spec.memory_limit_mb),
        ("I/O slots", spec.io_slots),
        ("batch size", spec.batch_size),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidInputSchemaError(f"{name} must be a positive integer")
    parse_crs(spec.crs)
    if spec.checkpoint_dir is not None:
        output = Path(spec.output_dir).resolve()
        checkpoint = Path(spec.checkpoint_dir).resolve()
        try:
            checkpoint.relative_to(output)
        except ValueError:
            pass
        else:
            raise InvalidInputSchemaError(
                "Checkpoint directory cannot be inside the output directory"
            )


def _provider(
    path: Path,
    layer: str | None,
    override: str | None,
    requested: dict[str, str],
    label: str,
) -> _Provider:
    source = inspect_vector_provider(path, layer=layer, source_crs=override)
    info = {
        "fields": source.fields,
        "driver": source.driver,
        "crs": source.crs,
        "geometry_type": source.geometry_type,
        "features": source.feature_count,
        "fid_column": source.fid_column,
    }
    fields = {
        name: _resolve_field(info, value, label) for name, value in requested.items()
    }
    return _Provider(Path(path), layer, info, source.crs, fields)


def _resolve_field(info: dict[str, Any], requested: str, label: str) -> str:
    fields = list(info.get("fields", ()))
    exact = [field for field in fields if field == requested]
    matches = exact or [
        field for field in fields if field.casefold() == requested.casefold()
    ]
    if len(matches) != 1:
        reason = "ambiguous" if matches else "missing"
        raise InvalidInputSchemaError(
            f"{label} provider has {reason} required field: {requested}"
        )
    return matches[0]


def _read_topology(
    provider: _Provider, batch_size: int
) -> tuple[pd.DataFrame, dict[Hashable, int]]:
    columns = list(provider.fields.values())
    batches: list[pa.RecordBatch] = []
    try:
        with pyogrio.open_arrow(
            provider.path,
            layer=provider.layer,
            columns=columns,
            read_geometry=False,
            return_fids=True,
            batch_size=batch_size,
            use_pyarrow=True,
        ) as (_, stream):
            for batch in stream:
                order = batch[provider.fields["strahler_order"]]
                if not (
                    pa.types.is_integer(order.type)
                    or pa.types.is_floating(order.type)
                    or pa.types.is_decimal(order.type)
                ):
                    raise InvalidInputSchemaError("Strahler order must be numeric")
                numeric = pc.cast(order, pa.float64())
                mask = pc.fill_null(
                    pc.and_(pc.is_finite(numeric), pc.greater_equal(numeric, 1.0)),
                    False,
                )
                filtered = batch.filter(mask)
                if filtered.num_rows:
                    batches.append(filtered)
    except InvalidInputSchemaError:
        raise
    except Exception as exc:
        raise InvalidInputSchemaError(
            "Cannot stream segment topology attributes"
        ) from exc
    if not batches:
        raise InvalidInputSchemaError("No segments remain after Strahler filtering")
    table = pa.Table.from_batches(batches).combine_chunks()
    rename = {actual: normalized for normalized, actual in provider.fields.items()}
    frame = table.to_pandas().rename(columns=rename)
    fid_name = provider.info.get("fid_column") or "fid"
    fid_column = fid_name if fid_name in frame else frame.columns[0]
    duplicated = frame.loc[frame["id"].duplicated(keep=False), "id"].tolist()
    if duplicated:
        raise DuplicateSegmentIdError(
            "Found duplicate segment ID(s): " + ", ".join(map(str, duplicated[:20]))
        )
    fids = dict(zip(frame["id"], frame[fid_column], strict=True))
    return (
        frame[["id", "id_down", "strahler_order"]].reset_index(drop=True),
        fids,
    )


def _coerce_outlets(values: Iterable[str], ids: pd.Series) -> list[Hashable]:
    dtype = ids.dtype
    result: list[Hashable] = []
    for value in values:
        try:
            if pd.api.types.is_integer_dtype(dtype):
                result.append(int(value))
            elif pd.api.types.is_float_dtype(dtype):
                result.append(float(value))
            else:
                result.append(value)
        except ValueError as exc:
            raise OutletNotFoundError(
                f"Outlet ID is incompatible with provider IDs: {value}"
            ) from exc
    return result


def _select_topology(
    frame: pd.DataFrame, outlets: list[Hashable]
) -> tuple[set[Hashable], dict[Hashable, int]]:
    ids = set(frame["id"].tolist())
    missing = [value for value in outlets if value not in ids]
    if missing:
        raise OutletNotFoundError(
            "Outlet segment ID(s) not found after Strahler filtering: "
            + ", ".join(map(str, missing))
        )
    upstream: dict[Hashable, list[Hashable]] = defaultdict(list)
    downstream = dict(frame[["id", "id_down"]].itertuples(index=False, name=None))
    for segment_id, downstream_id in downstream.items():
        if downstream_id in ids:
            upstream[downstream_id].append(segment_id)
    selected: set[Hashable] = set()
    sub_by_id: dict[Hashable, int] = {}
    count = len(outlets)
    for outlet_index, outlet in enumerate(outlets):
        stack = [outlet]
        domain: set[Hashable] = set()
        while stack:
            current = stack.pop()
            if current in domain:
                continue
            domain.add(current)
            stack.extend(upstream.get(current, ()))
        sub = count - outlet_index
        selected.update(domain)
        sub_by_id.update(dict.fromkeys(domain, sub))
    _topological_order(selected, downstream)
    outlet_set = set(outlets)
    for segment_id in selected - outlet_set:
        if downstream[segment_id] not in selected:
            raise InvalidInputSchemaError(
                f"Selected segment {segment_id} does not connect toward a selected outlet"
            )
    return selected, sub_by_id


def _topological_order(
    ids: set[Hashable], downstream: dict[Hashable, Hashable]
) -> list[Hashable]:
    upstream_count = dict.fromkeys(ids, 0)
    for downstream_id in downstream.values():
        if downstream_id in ids:
            upstream_count[downstream_id] += 1
    ready = sorted(
        (value for value, count in upstream_count.items() if count == 0),
        key=str,
        reverse=True,
    )
    order: list[Hashable] = []
    while ready:
        current = ready.pop()
        order.append(current)
        target = downstream.get(current)
        if target in upstream_count:
            upstream_count[target] -= 1
            if upstream_count[target] == 0:
                ready.append(target)
                ready.sort(key=str, reverse=True)
    if len(order) != len(ids):
        raise TopologyCycleError("Detected topology cycle in the selected ROI")
    return order


def _validate_selected_attributes(frame: pd.DataFrame) -> None:
    order = pd.to_numeric(frame["strahler_order"], errors="coerce").to_numpy(
        dtype=float
    )
    if not np.all(np.isfinite(order)) or not np.all(order == np.floor(order)):
        raise InvalidInputSchemaError(
            "Selected Strahler orders must be finite integral values"
        )


def _scan_fids(provider: _Provider, batch_size: int) -> dict[Hashable, int]:
    result: dict[Hashable, int] = {}
    try:
        with pyogrio.open_arrow(
            provider.path,
            layer=provider.layer,
            columns=[provider.fields["id"]],
            read_geometry=False,
            return_fids=True,
            batch_size=batch_size,
            use_pyarrow=True,
        ) as (metadata, batches):
            fid_name = metadata.get("fid_column") or "fid"
            for batch in batches:
                fid_index = batch.schema.get_field_index(fid_name)
                fid_index = max(fid_index, 0)
                values = zip(
                    batch[provider.fields["id"]].to_pylist(),
                    batch.column(fid_index).to_pylist(),
                    strict=True,
                )
                for segment_id, fid in values:
                    if pd.isna(segment_id):
                        continue
                    if segment_id in result:
                        raise InvalidInputSchemaError(
                            f"Provider contains duplicate ID: {segment_id}"
                        )
                    result[segment_id] = fid
    except InvalidInputSchemaError:
        raise
    except Exception as exc:
        raise InvalidInputSchemaError("Cannot scan provider IDs and FIDs") from exc
    return result


@lru_cache(maxsize=16)
def _geodetic_tools(crs_text: str) -> tuple[Transformer, Geod]:
    crs = CRS.from_wkt(crs_text)
    geodetic = crs.geodetic_crs
    if geodetic is None or crs.ellipsoid.semi_major_metre is None:
        raise InvalidInputSchemaError("Source CRS has no usable geodetic ellipsoid")
    ellipsoid = crs.ellipsoid
    if ellipsoid.inverse_flattening == 0:
        geod = Geod(a=ellipsoid.semi_major_metre, b=ellipsoid.semi_minor_metre)
    else:
        geod = Geod(a=ellipsoid.semi_major_metre, rf=ellipsoid.inverse_flattening)
    return Transformer.from_crs(crs, geodetic, always_xy=True), geod


def _geometry_metric(geometry, provider: _Provider, kind: str) -> float:
    transformer, geod = _geodetic_tools(provider.crs.to_wkt())
    geographic = shapely.transform(geometry, transformer.transform, interleaved=False)
    if kind == "segments":
        value = geod.geometry_length(geographic) / 1000.0
    else:
        value = abs(geod.geometry_area_perimeter(geographic)[0]) / 1_000_000.0
    if not np.isfinite(value) or value < 0:
        raise InvalidInputSchemaError(f"Selected {kind} produced an invalid metric")
    return float(value)


def _upstream_metrics(attrs, metrics):
    downstream = dict(attrs[["id", "id_down"]].itertuples(index=False, name=None))
    length = {key: value["unit_length"] for key, value in metrics.items()}
    area = {key: value["unit_area"] for key, value in metrics.items()}
    for segment_id in _topological_order(set(metrics), downstream):
        target = downstream.get(segment_id)
        if target in length:
            length[target] += length[segment_id]
            area[target] += area[segment_id]
    return length, area


def _metric_attributes(attrs, sub_by_id, metrics, upstream_length, upstream_area):
    result = attrs.copy()
    result["sub"] = result["id"].map(sub_by_id)
    result["unit_length"] = result["id"].map(
        lambda value: metrics[value]["unit_length"]
    )
    result["upstream_length"] = result["id"].map(upstream_length)
    result["unit_area"] = result["id"].map(lambda value: metrics[value]["unit_area"])
    result["upstream_area"] = result["id"].map(upstream_area)
    result["strahler_order"] = result["strahler_order"].astype("int64")
    return result


def _validate_geometries(geometries: np.ndarray, name: str, allowed: set[str]) -> None:
    if np.any(shapely.is_missing(geometries)) or np.any(shapely.is_empty(geometries)):
        raise InvalidInputSchemaError(f"Selected {name} contain null or empty geometry")
    type_codes = {
        "LineString": shapely.GeometryType.LINESTRING.value,
        "MultiLineString": shapely.GeometryType.MULTILINESTRING.value,
        "Polygon": shapely.GeometryType.POLYGON.value,
        "MultiPolygon": shapely.GeometryType.MULTIPOLYGON.value,
    }
    bad_types = sorted(
        set(shapely.get_type_id(geometries).tolist())
        - {type_codes[value] for value in allowed}
    )
    if bad_types:
        raise InvalidInputSchemaError(
            f"Selected {name} have invalid geometry type code(s): "
            + ", ".join(map(str, bad_types))
        )
    if not np.all(shapely.is_valid(geometries)):
        raise InvalidInputSchemaError(f"Selected {name} contain invalid geometry")


def _water_course_by_segment(segments: pd.DataFrame) -> dict[Hashable, Hashable]:
    result: dict[Hashable, Hashable] = {}
    for _, group in segments.groupby("sub", sort=False):
        ids = set(group["id"].tolist())
        downstream = dict(group[["id", "id_down"]].itertuples(index=False, name=None))
        upstream: dict[Hashable, list[Hashable]] = defaultdict(list)
        for segment_id, target in downstream.items():
            if target in ids:
                upstream[target].append(segment_id)
        attrs = group.set_index("id")
        for segment_id in reversed(_topological_order(ids, downstream)):
            result.setdefault(segment_id, segment_id)
            children = upstream.get(segment_id, ())
            if children:
                main = max(
                    children,
                    key=lambda child: (
                        attrs.at[child, "upstream_area"],
                        attrs.at[child, "unit_length"],
                        str(child),
                    ),
                )
                for child in children:
                    result[child] = result[segment_id] if child == main else child
    return result


def _write_fgb(frame: VectorTable, path: Path, *, append: bool = False) -> None:
    write_vector_table(frame, path, driver="FlatGeobuf", append=append)


def _normalize_vector_table(frame: VectorTable, provider: _Provider) -> VectorTable:
    rename = {actual: normalized for normalized, actual in provider.fields.items()}
    names = [
        rename.get(name, "geometry" if name == frame.geometry_column else name)
        for name in frame.table.column_names
    ]
    table = frame.table.rename_columns(names)
    columns = [*provider.fields.keys(), "geometry"]
    return VectorTable(
        table.select(columns), provider.crs, "geometry", frame.geometry_type
    )


def _provider_vector(
    metadata: dict[str, Any], table: pa.Table, provider: _Provider
) -> VectorTable:
    if metadata.get("crs") is None:
        metadata = {**metadata, "crs": provider.crs.to_wkt()}
    return vector_table_from_arrow(metadata, table)


def _rows_to_vector(
    rows: list[dict[str, Any]], id_type: pa.DataType, crs: CRS, geometry_type: str
) -> VectorTable:
    def nullable_id(value):
        return None if pd.isna(value) else value

    arrays = {
        "id": pa.array([row["id"] for row in rows], type=id_type),
        "id_down": pa.array(
            [nullable_id(row["id_down"]) for row in rows], type=id_type
        ),
        "sub": pa.array([row["sub"] for row in rows], type=pa.int64()),
        "strahler_order": pa.array(
            [row["strahler_order"] for row in rows], type=pa.int64()
        ),
        "unit_length": pa.array(
            [row["unit_length"] for row in rows], type=pa.float64()
        ),
        "upstream_length": pa.array(
            [row["upstream_length"] for row in rows], type=pa.float64()
        ),
        "unit_area": pa.array([row["unit_area"] for row in rows], type=pa.float64()),
        "upstream_area": pa.array(
            [row["upstream_area"] for row in rows], type=pa.float64()
        ),
        "water_course": pa.array([row["water_course"] for row in rows], type=id_type),
        "geometry": pa.array([row["geometry"] for row in rows], type=pa.binary()),
    }
    return VectorTable(pa.table(arrays), crs, "geometry", geometry_type)


def _validate_roi_outputs(
    catchments: Path,
    segments: Path,
    *,
    target_crs: CRS,
    feature_count: int,
) -> None:
    """Validate explicit normalized ROI files before publication."""

    for name, path in (("catchments", catchments), ("segments", segments)):
        if not path.is_file():
            raise PreparedDataError(f"ROI output is missing: {path}")
        try:
            info = pyogrio.read_info(path)
        except Exception as exc:
            raise PreparedDataError(f"Cannot inspect ROI output: {path}") from exc
        if info.get("driver") != "FlatGeobuf":
            raise PreparedDataError(f"ROI {name} output is not FlatGeobuf")
        if list(info.get("fields", ())) != ROI_COLUMNS[:-1]:
            raise PreparedDataError(f"ROI {name} output schema is invalid")
        if info.get("features") != feature_count:
            raise PreparedDataError(f"ROI {name} output feature count is invalid")
        if info.get("crs") is None or CRS.from_user_input(info["crs"]) != target_crs:
            raise PreparedDataError(f"ROI {name} output CRS is invalid")
