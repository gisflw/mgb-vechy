from __future__ import annotations

import heapq
import shutil
import time
from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyogrio
import shapely
from pyproj import CRS, Transformer

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    InvalidInputSchemaError,
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
    VectorProvider,
    VectorTable,
    VectorTableCheckpointCodec,
    conservative_geometry_packet_rows,
    geometry_column_name,
    inspect_vector_provider,
    iter_provider_batches,
    write_vector_table,
)
from mgb_vec_hydro.topology import _is_sink_value

INPUT_COLUMNS = [
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

AGGREGATION_COLUMNS = [
    "id",
    "id_down",
    "sub",
    "p_order",
    "unit_length",
    "upstream_length",
    "unit_area",
    "upstream_area",
    "geometry",
]


@dataclass(frozen=True)
class AggregationResult:
    catchments: VectorTable
    segments: VectorTable
    mapping: pd.DataFrame
    _catchment_assignment: dict[Hashable, Hashable] | None = field(
        default=None, repr=False, compare=False
    )
    _reach_assignment: dict[Hashable, Hashable] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class AggregationSpec:
    roi_catchments: Path
    roi_segments: Path
    uparea_min: float
    lmin: float
    output_dir: Path
    workers: int = 4
    memory_limit_mb: int = 512
    io_slots: int = 2
    batch_size: int = 10_000
    checkpoint_dir: Path | None = None


@dataclass(frozen=True)
class AggregationReport:
    output_dir: Path
    mini_catchments: Path
    mini_segments: Path
    source_to_mini: Path
    catchment_count: int
    segment_count: int
    mapping_count: int
    timings: dict[str, float]


@dataclass(frozen=True)
class _AggregationPacket:
    provider: VectorProvider
    ids: tuple[Hashable, ...]
    fids: tuple[int, ...]
    assignment: dict[Hashable, Hashable]
    kind: str


@dataclass(frozen=True)
class _AggregationPlan:
    attributes: pd.DataFrame
    catchment_assignment: dict[Hashable, int]
    reach_assignment: dict[Hashable, int]


def aggregate_minibasins(
    roi_catchments: VectorTable,
    roi_segments: VectorTable,
    *,
    uparea_min: float,
    lmin: float,
) -> AggregationResult:
    """Aggregate normalized ROI products using chain-first topology reduction."""
    _validate_input_schema(roi_catchments, "roi_catchments")
    _validate_input_schema(roi_segments, "roi_segments")
    if roi_catchments.crs != roi_segments.crs:
        raise InvalidInputSchemaError("ROI catchment and segment CRS values differ")
    if uparea_min < 0 or lmin < 0:
        raise InvalidInputSchemaError("uparea-min and lmin must be non-negative")

    catchments = _attributes(roi_catchments)
    segments = _attributes(roi_segments)
    plan = _plan_aggregation(catchments, segments, uparea_min=uparea_min, lmin=lmin)

    # Geometry is deliberately decoded only after topology, ordering, and the
    # dense ID remap are complete.
    catchment_geometry = roi_catchments.geometries()
    segment_geometry = roi_segments.geometries()
    _validate_geometries(catchment_geometry, "roi_catchments", {3, 6})
    _validate_geometries(segment_geometry, "roi_segments", {1, 5})
    catchment_ids = catchments["id"].tolist()
    catchment_geometry_by_id = dict(zip(catchment_ids, catchment_geometry, strict=True))
    segment_geometry_by_id = dict(zip(segments["id"], segment_geometry, strict=True))
    catchment_groups = _groups_from_assignment(plan.catchment_assignment)
    segment_groups = _groups_from_assignment(plan.reach_assignment)
    attrs = plan.attributes.set_index("id", drop=False).to_dict("index")

    segment_rows: list[dict[str, Any]] = []
    catchment_rows: list[dict[str, Any]] = []
    for mini_id in plan.attributes["id"]:
        segment_rows.append(
            {
                **attrs[mini_id],
                "geometry": shapely.union_all(
                    [segment_geometry_by_id[value] for value in segment_groups[mini_id]]
                ),
            }
        )
        catchment_rows.append(
            {
                **attrs[mini_id],
                "geometry": shapely.union_all(
                    [
                        catchment_geometry_by_id[value]
                        for value in catchment_groups[mini_id]
                    ]
                ),
            }
        )

    aggregated_segments = _rows_to_vector(segment_rows, roi_segments.crs, "Unknown")
    aggregated_catchments = _rows_to_vector(
        catchment_rows, roi_catchments.crs, "Unknown"
    )
    centroids = shapely.centroid(catchment_geometry)
    transformer = Transformer.from_crs(roi_catchments.crs, "EPSG:4326", always_xy=True)
    lonlat = shapely.transform(centroids, transformer.transform, interleaved=False)
    mapping = pd.DataFrame(
        {
            "id": catchment_ids,
            "mini_id": [plan.catchment_assignment[value] for value in catchment_ids],
            "sub": catchments["sub"].to_numpy(),
            "longitude": shapely.get_x(lonlat),
            "latitude": shapely.get_y(lonlat),
        }
    )
    return AggregationResult(
        aggregated_catchments,
        aggregated_segments,
        mapping,
        dict(plan.catchment_assignment),
        dict(plan.reach_assignment),
    )


def _plan_aggregation(
    catchments: pd.DataFrame,
    segments: pd.DataFrame,
    *,
    uparea_min: float,
    lmin: float,
) -> _AggregationPlan:
    _validate_unique_ids(catchments, "roi_catchments")
    _validate_unique_ids(segments, "roi_segments")
    if set(catchments["id"]) != set(segments["id"]):
        raise InvalidInputSchemaError("ROI catchment and segment IDs do not match")

    state = _build_aggregation_state(segments, uparea_min, lmin)
    source_assignment = state["catchment_assignment"]
    reach_assignment = state["reach_assignment"]
    groups = _groups_from_assignment(reach_assignment)
    catchment_groups = _groups_from_assignment(source_assignment)
    segment_attributes = _mini_attributes(
        segments, groups, reach_assignment, state["downstream"]
    )
    catchment_metrics = _mini_metric_attributes(
        catchments,
        catchment_groups,
        unit_column="unit_area",
        upstream_column="upstream_area",
    )
    attributes = {
        mini_id: {**segment_attributes[mini_id], **catchment_metrics[mini_id]}
        for mini_id in groups
    }
    ordered, dense_id = _order_and_reindex(attributes)
    return _AggregationPlan(
        pd.DataFrame(ordered, columns=AGGREGATION_COLUMNS[:-1]),
        {source: dense_id[mini] for source, mini in source_assignment.items()},
        {source: dense_id[mini] for source, mini in reach_assignment.items()},
    )


def _order_and_reindex(attributes):
    ids = set(attributes)
    downstream = {
        mini_id: (row["id_down"] if row["id_down"] in ids else None)
        for mini_id, row in attributes.items()
    }
    p_order = dict.fromkeys(ids, 1)
    for mini_id in _topological_order(ids, downstream):
        target = downstream[mini_id]
        if target is not None:
            p_order[target] = max(p_order[target], p_order[mini_id] + 1)
    old_ids = sorted(
        ids,
        key=lambda value: (
            attributes[value]["sub"],
            p_order[value],
            attributes[value]["upstream_area"],
            _stable_key(value),
        ),
    )
    dense_id = {old_id: new_id for new_id, old_id in enumerate(old_ids, start=1)}
    rows = []
    for old_id in old_ids:
        row = attributes[old_id]
        target = downstream[old_id]
        rows.append(
            {
                "id": dense_id[old_id],
                "id_down": -1 if target is None else dense_id[target],
                "sub": row["sub"],
                "p_order": p_order[old_id],
                "unit_length": row["unit_length"],
                "upstream_length": row["upstream_length"],
                "unit_area": row["unit_area"],
                "upstream_area": row["upstream_area"],
            }
        )
    return rows, dense_id


def _build_aggregation_state(
    segments: pd.DataFrame, uparea_min: float, lmin: float
) -> dict[str, Any]:
    ids = segments["id"].tolist()
    id_set = set(ids)
    downstream = {
        segment_id: (None if _is_sink_value(target) or target not in id_set else target)
        for segment_id, target in segments[["id", "id_down"]].itertuples(
            index=False, name=None
        )
    }
    _topological_order(id_set, downstream)
    upstream = _reverse_adjacency(id_set, downstream)
    row_by_id = segments.set_index("id", drop=False)
    domain = {
        value: (row_by_id.at[value, "sub"], row_by_id.at[value, "water_course"])
        for value in ids
    }
    sub = {value: row_by_id.at[value, "sub"] for value in ids}
    eligible = {
        value
        for value in ids
        if float(row_by_id.at[value, "upstream_area"]) >= uparea_min
    }
    if not eligible:
        raise InvalidInputSchemaError("No segments satisfy uparea-min")

    reduced_downstream: dict[Hashable, Hashable | None] = {}
    for value in eligible:
        target = downstream[value]
        while target is not None and target not in eligible:
            target = downstream[target]
        reduced_downstream[value] = target
    reduced_upstream = _reverse_adjacency(eligible, reduced_downstream)

    parent = {value: value for value in eligible}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for upstream_id, downstream_id in reduced_downstream.items():
        if (
            downstream_id is not None
            and len(reduced_upstream.get(downstream_id, ())) == 1
            and domain[upstream_id] == domain[downstream_id]
        ):
            union(upstream_id, downstream_id)

    components: dict[Hashable, set[Hashable]] = defaultdict(set)
    for value in eligible:
        components[find(value)].add(value)
    members = {
        _representative_id(values, row_by_id): values for values in components.values()
    }
    assignment = {
        value: mini_id for mini_id, values in members.items() for value in values
    }
    lengths = {
        mini_id: float(row_by_id.loc[list(values), "unit_length"].sum())
        for mini_id, values in members.items()
    }
    group_domain = {mini_id: domain[mini_id] for mini_id in members}
    neighbors = _group_neighbors(assignment, reduced_downstream, group_domain)

    while True:
        merge_pair = None
        for mini_id in sorted(
            (value for value in members if lengths[value] < lmin), key=_stable_key
        ):
            candidates = neighbors.get(mini_id, set())
            if candidates:
                merge_pair = (
                    mini_id,
                    min(candidates, key=lambda value: (lengths[value], str(value))),
                )
                break
        if merge_pair is None:
            break
        source, target = merge_pair
        combined = members[source] | members[target]
        new_id = _representative_id({source, target}, row_by_id)
        combined_length = lengths[source] + lengths[target]
        adjacent_groups = (
            neighbors.get(source, set()) | neighbors.get(target, set())
        ) - {source, target}
        for old in {source, target}:
            members.pop(old)
            lengths.pop(old)
            group_domain.pop(old)
            neighbors.pop(old, None)
        members[new_id] = combined
        lengths[new_id] = combined_length
        group_domain[new_id] = domain[new_id]
        neighbors[new_id] = set(adjacent_groups)
        for adjacent in adjacent_groups:
            values = neighbors[adjacent]
            values.discard(source)
            values.discard(target)
            if adjacent != new_id:
                values.add(new_id)
        for value in combined:
            assignment[value] = new_id

    short_ids = {value for value in members if lengths[value] < lmin}
    for mini_id in short_ids:
        for value in members.pop(mini_id):
            assignment.pop(value, None)
        lengths.pop(mini_id)
    if not assignment:
        raise InvalidInputSchemaError(
            "Catchment ID(s) have no eligible aggregation target in the same sub"
        )

    unassigned = id_set - set(assignment)
    catchment_assignment = dict(assignment)
    catchment_assignment.update(
        _assign_components(
            unassigned, assignment, downstream, upstream, domain, lengths
        )
    )
    remaining = id_set - set(catchment_assignment)
    if remaining:
        catchment_assignment.update(
            _assign_components(
                remaining, assignment, downstream, upstream, sub, lengths
            )
        )
    missing = id_set - set(catchment_assignment)
    if missing:
        raise InvalidInputSchemaError(
            "Catchment ID(s) have no eligible aggregation target in the same sub: "
            + ", ".join(map(str, sorted(missing, key=_stable_key)))
        )
    return {
        "reach_assignment": assignment,
        "catchment_assignment": catchment_assignment,
        "downstream": downstream,
    }


def _assign_components(requested, surviving, downstream, upstream, domain, lengths):
    transparent = set(downstream) - set(surviving)
    seen: set[Hashable] = set()
    result: dict[Hashable, Hashable] = {}
    for start in sorted(requested, key=_stable_key):
        if start in seen:
            continue
        member_domain = domain[start]
        stack = [start]
        component: set[Hashable] = set()
        candidates: set[Hashable] = set()
        while stack:
            value = stack.pop()
            if value in component or domain[value] != member_domain:
                continue
            component.add(value)
            for other in [*upstream.get(value, ()), downstream.get(value)]:
                if other is None or domain[other] != member_domain:
                    continue
                target = surviving.get(other)
                if target is not None:
                    candidates.add(target)
                elif other in transparent and other not in component:
                    stack.append(other)
        seen.update(component)
        if candidates:
            target = min(candidates, key=lambda value: (lengths[value], str(value)))
            for value in component & set(requested):
                result[value] = target
    return result


def _group_neighbors(assignment, reduced_downstream, group_domain):
    result = {value: set() for value in group_domain}
    for upstream_id, downstream_id in reduced_downstream.items():
        if downstream_id is None:
            continue
        left, right = assignment[upstream_id], assignment[downstream_id]
        if left != right and group_domain[left] == group_domain[right]:
            result[left].add(right)
            result[right].add(left)
    return result


def _mini_attributes(segments, groups, assignment, downstream):
    row_by_id = segments.set_index("id", drop=False)
    result = {}
    for mini_id, members in groups.items():
        representative = row_by_id.loc[mini_id]
        target = downstream.get(mini_id)
        seen = set(members)
        downstream_mini = None
        while target is not None and target not in seen:
            candidate = assignment.get(target)
            if candidate is not None and candidate != mini_id:
                downstream_mini = candidate
                break
            seen.add(target)
            target = downstream.get(target)
        result[mini_id] = {
            "id": mini_id,
            "id_down": downstream_mini,
            "sub": representative["sub"],
            "unit_length": float(row_by_id.loc[list(members), "unit_length"].sum()),
            "upstream_length": float(representative["upstream_length"]),
        }
    return result


def _mini_metric_attributes(source, groups, *, unit_column, upstream_column):
    source_by_id = source.set_index("id", drop=False)
    return {
        mini_id: {
            unit_column: float(source_by_id.loc[list(members), unit_column].sum()),
            upstream_column: float(source_by_id.at[mini_id, upstream_column]),
        }
        for mini_id, members in groups.items()
    }


def _read_aggregation_attributes(provider, name, batch_size):
    expected = tuple(INPUT_COLUMNS[:-1])
    if tuple(provider.fields) != expected:
        raise InvalidInputSchemaError(
            f"{name} must have exact input columns in order: " + ", ".join(expected)
        )
    batches = []
    fids = {}
    for batch in iter_provider_batches(
        provider,
        columns=expected,
        batch_size=batch_size,
        read_geometry=False,
        return_fids=True,
    ):
        extra = [column for column in batch.schema.names if column not in expected]
        if len(extra) != 1:
            raise InvalidInputSchemaError("Cannot identify vector feature IDs")
        ids = batch["id"].to_pylist()
        fid_values = batch[extra[0]].to_pylist()
        for value, fid in zip(ids, fid_values, strict=True):
            if value in fids:
                raise DuplicateSegmentIdError(f"Found duplicate ID in {name}: {value}")
            fids[value] = int(fid)
        batches.append(batch.select(expected))
    if not batches:
        raise InvalidInputSchemaError(f"{name} contains no features")
    table = pa.Table.from_batches(batches).combine_chunks()
    _validate_numeric_columns(table, name)
    if table["id"].null_count:
        raise InvalidInputSchemaError(f"{name} contains null IDs")
    return table.to_pandas().reset_index(drop=True), fids


def _validate_numeric_columns(table, name):
    for column in (
        "sub",
        "strahler_order",
        "unit_length",
        "upstream_length",
        "unit_area",
        "upstream_area",
    ):
        value = table[column].type
        if not (
            pa.types.is_integer(value)
            or pa.types.is_floating(value)
            or pa.types.is_decimal(value)
        ):
            raise InvalidInputSchemaError(
                f"{name} has non-numeric metric column(s): {column}"
            )


def _geometry_packet_bytes(provider, row_count):
    source_bytes = max(1, provider.path.stat().st_size)
    per_feature = max(4096, int(np.ceil(source_bytes / provider.feature_count)) * 4)
    return max(1, per_feature * row_count)


def _mapping_from_packets(packet_dir, kinds, catchments):
    codec = VectorTableCheckpointCodec()
    frames = []
    for ordinal, kind in kinds.items():
        if kind != "catchments":
            continue
        packet = codec.load(packet_dir / f"{ordinal:012d}.arrow")
        frames.append(
            packet.table.select(
                ["source_id", "mini_id", "longitude", "latitude"]
            ).to_pandas()
        )
    if not frames:
        raise InvalidInputSchemaError("Aggregation produced no catchment mapping")
    mapping = pd.concat(frames, ignore_index=True).rename(columns={"source_id": "id"})
    sub_by_id = catchments.set_index("id")["sub"]
    mapping.insert(2, "sub", mapping["id"].map(sub_by_id))
    return mapping.sort_values(
        "id", key=lambda values: values.astype(str), kind="stable"
    ).reset_index(drop=True)


def aggregate_roi_dataset(spec: AggregationSpec) -> AggregationReport:
    overall_started = time.perf_counter()
    _validate_spec(spec)
    phase_started = time.perf_counter()
    catchment_provider = inspect_vector_provider(spec.roi_catchments)
    segment_provider = inspect_vector_provider(spec.roi_segments)
    expected_crs = catchment_provider.crs
    if segment_provider.crs != expected_crs:
        raise InvalidInputSchemaError("ROI catchment and segment CRS values differ")
    catchments, catchment_fids = _read_aggregation_attributes(
        catchment_provider, "roi_catchments", spec.batch_size
    )
    segments, segment_fids = _read_aggregation_attributes(
        segment_provider, "roi_segments", spec.batch_size
    )
    roi_input_seconds = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    plan = _plan_aggregation(
        catchments,
        segments,
        uparea_min=spec.uparea_min,
        lmin=spec.lmin,
    )
    aggregation_seconds = time.perf_counter() - phase_started
    items, kinds = _aggregation_work_items(
        catchment_provider,
        segment_provider,
        catchment_fids,
        segment_fids,
        plan,
        batch_size=spec.batch_size,
        memory_limit_bytes=spec.memory_limit_mb * 1024 * 1024,
    )
    checkpoint = None
    if spec.checkpoint_dir is not None:
        fingerprint = execution_fingerprint(
            algorithm="aggregate",
            version="3",
            input_identity={
                "roi_catchments": file_identity(spec.roi_catchments),
                "roi_segments": file_identity(spec.roi_segments),
                "crs": expected_crs.to_wkt(version="WKT2_2019", pretty=False),
            },
            parameters={"uparea_min": spec.uparea_min, "lmin": spec.lmin},
            work_items=items,
        )
        checkpoint = CheckpointStore(
            spec.checkpoint_dir, fingerprint, VectorTableCheckpointCodec()
        )
    output = Path(spec.output_dir)
    publisher = AtomicOutputDirectory(output)
    phase_started = time.perf_counter()
    with publisher as staging:
        packet_dir = staging / ".packets"
        packet_dir.mkdir()
        codec = VectorTableCheckpointCodec()

        def reduce_packet(work_result):
            codec.dump(
                work_result.value,
                packet_dir / f"{work_result.ordinal:012d}.arrow",
            )

        execution = LocalExecutor(
            ExecutionConfig(
                workers=spec.workers,
                memory_limit_bytes=spec.memory_limit_mb * 1024 * 1024,
                io_slots=spec.io_slots,
            )
        ).run(
            items,
            _prepare_aggregation_packet,
            reduce_packet,
            checkpoint=checkpoint,
        )
        catchment_path = staging / "mini_catchments.fgb"
        segment_path = staging / "mini_segments.fgb"
        mapping_path = staging / "source_to_mini.csv"
        _gdal_dissolve_outputs(
            staging,
            packet_dir,
            kinds,
            plan,
            expected_crs,
            catchment_path=catchment_path,
            segment_path=segment_path,
        )
        mapping = _mapping_from_packets(packet_dir, kinds, catchments)
        mapping.to_csv(mapping_path, index=False)
        shutil.rmtree(packet_dir)
        _validate_aggregation_outputs(
            catchment_path,
            segment_path,
            mapping_path,
            expected_crs=expected_crs,
            source_ids=set(catchments["id"]),
        )
        publisher.publish((catchment_path.name, segment_path.name, mapping_path.name))
    output_publication_seconds = time.perf_counter() - phase_started
    if checkpoint is not None:
        checkpoint.cleanup()
    return AggregationReport(
        output,
        output / "mini_catchments.fgb",
        output / "mini_segments.fgb",
        output / "source_to_mini.csv",
        len(plan.attributes),
        len(plan.attributes),
        len(mapping),
        {
            "roi_input": roi_input_seconds,
            "aggregation": aggregation_seconds,
            "geometry_execution": execution.wall_seconds,
            "output_publication": output_publication_seconds,
            "total": time.perf_counter() - overall_started,
        },
    )


def _aggregation_work_items(
    catchments,
    segments,
    catchment_fids,
    segment_fids,
    plan: _AggregationPlan,
    *,
    batch_size: int,
    memory_limit_bytes: int,
) -> tuple[list[WorkItem[_AggregationPacket]], dict[int, str]]:
    """Plan bounded geometry reads after the attribute-only aggregation pass."""
    items: list[WorkItem[_AggregationPacket]] = []
    kinds: dict[int, str] = {}
    for kind, provider, fids, assignment in (
        ("catchments", catchments, catchment_fids, plan.catchment_assignment),
        ("segments", segments, segment_fids, plan.reach_assignment),
    ):
        rows = conservative_geometry_packet_rows(
            provider,
            memory_limit_bytes=memory_limit_bytes,
            requested_rows=batch_size,
        )
        source_ids = sorted(assignment, key=_stable_key)
        for packet_index, offset in enumerate(range(0, len(source_ids), rows)):
            selected_ids = source_ids[offset : offset + rows]
            ordinal = len(items)
            items.append(
                WorkItem(
                    f"{kind}-{packet_index:012d}",
                    ordinal,
                    _geometry_packet_bytes(provider, len(selected_ids)),
                    _AggregationPacket(
                        provider,
                        tuple(selected_ids),
                        tuple(fids[value] for value in selected_ids),
                        {value: assignment[value] for value in selected_ids},
                        kind,
                    ),
                )
            )
            kinds[ordinal] = kind
    return items, kinds


def _prepare_aggregation_packet(payload: _AggregationPacket, context) -> VectorTable:
    batches = list(
        iter_provider_batches(
            payload.provider,
            columns=("id",),
            batch_size=len(payload.ids),
            read_geometry=True,
            fids=payload.fids,
            context=context,
        )
    )
    if not batches:
        raise InvalidInputSchemaError("Aggregation geometry packet is empty")
    source = pa.Table.from_batches(batches).combine_chunks()
    geometry_column = geometry_column_name(source)
    ids = source["id"].to_pylist()
    if len(ids) != len(set(ids)) or set(ids) != set(payload.ids):
        raise InvalidInputSchemaError("Aggregation geometry packet IDs do not match")
    geometries = shapely.from_wkb(
        source[geometry_column].to_numpy(zero_copy_only=False), on_invalid="raise"
    )
    allowed = {3, 6} if payload.kind == "catchments" else {1, 5}
    _validate_geometries(geometries, f"roi_{payload.kind}", allowed)
    columns = {
        "mini_id": pa.array(
            [payload.assignment[value] for value in ids], type=pa.int64()
        )
    }
    if payload.kind == "catchments":
        transformer = Transformer.from_crs(
            payload.provider.crs, "EPSG:4326", always_xy=True
        )
        lonlat = shapely.transform(
            shapely.centroid(geometries), transformer.transform, interleaved=False
        )
        columns.update(
            source_id=source["id"],
            longitude=pa.array(shapely.get_x(lonlat), type=pa.float64()),
            latitude=pa.array(shapely.get_y(lonlat), type=pa.float64()),
        )
    columns["geometry"] = source[geometry_column]
    return VectorTable(
        pa.table(columns),
        payload.provider.crs,
        "geometry",
        payload.provider.geometry_type,
    )


def _gdal_dissolve_outputs(
    staging: Path,
    packet_dir: Path,
    kinds: dict[int, str],
    plan: _AggregationPlan,
    crs: CRS,
    *,
    catchment_path: Path,
    segment_path: Path,
) -> None:
    """Dissolve assigned WKB with GDAL's SQLite engine and stream to FGB."""
    workspace = staging / ".aggregation.gpkg"
    layers = (
        ("catchment_sources", "catchment_attrs", catchment_path),
        ("segment_sources", "segment_attrs", segment_path),
    )
    try:
        codec = VectorTableCheckpointCodec()
        first = {"catchments": True, "segments": True}
        source_layers = {
            "catchments": "catchment_sources",
            "segments": "segment_sources",
        }
        for ordinal in range(len(kinds)):
            kind = kinds[ordinal]
            packet = codec.load(packet_dir / f"{ordinal:012d}.arrow")
            write_vector_table(
                packet,
                workspace,
                driver="GPKG",
                layer=source_layers[kind],
                append=not first[kind],
                spatial_index=False,
            )
            first[kind] = False
        attrs = pa.Table.from_pandas(plan.attributes, preserve_index=False)
        for _, attrs_layer, _ in layers:
            pyogrio.write_arrow(attrs, workspace, layer=attrs_layer, driver="GPKG")

        for source_layer, attrs_layer, output in layers:
            geometry_name = pyogrio.read_info(workspace, layer=source_layer)[
                "geometry_name"
            ]
            attribute_names = list(plan.attributes.columns)
            select = ", ".join(f'a."{name}"' for name in attribute_names)
            group_by = ", ".join(f'a."{name}"' for name in attribute_names)
            sql = (
                f'SELECT {select}, ST_Union(s."{geometry_name}") AS geometry '
                f'FROM "{source_layer}" s JOIN "{attrs_layer}" a '
                f'ON s."mini_id" = a."id" GROUP BY {group_by} '
                'ORDER BY a."id"'
            )
            with pyogrio.open_arrow(
                workspace, sql=sql, sql_dialect="SQLITE", use_pyarrow=True
            ) as (metadata, batches):
                pyogrio.write_arrow(
                    batches,
                    output,
                    driver="FlatGeobuf",
                    geometry_name=metadata.get("geometry_name") or "geometry",
                    geometry_type="Unknown",
                    crs=crs.to_wkt(version="WKT2_2019", pretty=False),
                    layer_options={"SPATIAL_INDEX": "NO"},
                )
    except InvalidInputSchemaError:
        raise
    except Exception as exc:
        raise InvalidInputSchemaError(
            "GDAL could not dissolve aggregation geometry"
        ) from exc
    finally:
        workspace.unlink(missing_ok=True)


def _validate_spec(spec):
    for name, path in (
        ("roi catchments", spec.roi_catchments),
        ("roi segments", spec.roi_segments),
    ):
        if not Path(path).is_file():
            raise InvalidInputSchemaError(f"{name} input is not a local file: {path}")
    if spec.workers <= 0:
        raise InvalidInputSchemaError("workers must be a positive integer")
    if spec.memory_limit_mb <= 0 or spec.io_slots <= 0 or spec.batch_size <= 0:
        raise InvalidInputSchemaError("execution limits must be positive")
    if spec.uparea_min < 0 or spec.lmin < 0:
        raise InvalidInputSchemaError("uparea-min and lmin must be non-negative")
    output = Path(spec.output_dir)
    if output.exists():
        raise InvalidInputSchemaError(f"Output directory already exists: {output}")
    if spec.checkpoint_dir is not None:
        checkpoint = Path(spec.checkpoint_dir).resolve()
        try:
            checkpoint.relative_to(output.resolve())
        except ValueError:
            pass
        else:
            raise InvalidInputSchemaError(
                "Checkpoint directory cannot be inside the output directory"
            )


def _validate_aggregation_outputs(
    catchments, segments, mapping, *, expected_crs, source_ids
):
    output_frames = []
    for name, path in (("mini_catchments", catchments), ("mini_segments", segments)):
        info = pyogrio.read_info(path)
        if [*info["fields"], "geometry"] != AGGREGATION_COLUMNS:
            raise InvalidInputSchemaError(f"{name} output schema is invalid")
        if info.get("crs") is None or CRS.from_user_input(info["crs"]) != expected_crs:
            raise InvalidInputSchemaError(f"{name} output CRS is invalid")
        _, attributes = pyogrio.read_arrow(
            path, columns=AGGREGATION_COLUMNS[:-1], read_geometry=False
        )
        frame = attributes.to_pandas()
        if frame["id"].tolist() != list(range(1, len(frame) + 1)):
            raise InvalidInputSchemaError(f"{name} output order or IDs are invalid")
        domain = set(frame["id"])
        if not set(frame["id_down"]).issubset(domain | {-1}):
            raise InvalidInputSchemaError(f"{name} downstream IDs are invalid")
        ordered = frame.sort_values(
            ["sub", "p_order", "upstream_area"], kind="stable"
        ).index.tolist()
        if ordered != frame.index.tolist():
            raise InvalidInputSchemaError(f"{name} output feature order is invalid")
        expected_p_order = dict.fromkeys(domain, 1)
        for downstream_id, group in frame.loc[frame["id_down"] != -1].groupby(
            "id_down"
        ):
            expected_p_order[downstream_id] = int(group["p_order"].max()) + 1
        if frame["p_order"].tolist() != [
            expected_p_order[value] for value in frame["id"]
        ]:
            raise InvalidInputSchemaError(f"{name} processing order is invalid")
        output_frames.append(frame)
    if not output_frames[0].equals(output_frames[1]):
        raise InvalidInputSchemaError("Aggregation vector attributes differ")
    table = pd.read_csv(mapping)
    if list(table.columns) != ["id", "mini_id", "sub", "longitude", "latitude"]:
        raise InvalidInputSchemaError("source_to_mini.csv schema is invalid")
    if len(table) != len(source_ids) or table["id"].duplicated().any():
        raise InvalidInputSchemaError(
            "source_to_mini.csv does not contain each source once"
        )
    if {str(value) for value in table["id"]} != {str(value) for value in source_ids}:
        raise InvalidInputSchemaError(
            "source_to_mini.csv source IDs do not match the ROI"
        )
    if not set(table["mini_id"]).issubset(set(output_frames[0]["id"])):
        raise InvalidInputSchemaError("source_to_mini.csv mini IDs are invalid")


def _attributes(vector):
    return (
        vector.table.drop([vector.geometry_column]).to_pandas().reset_index(drop=True)
    )


def _validate_input_schema(vector, name):
    if not isinstance(vector, VectorTable):
        raise InvalidInputSchemaError(f"{name} must be a VectorTable")
    if list(vector.columns) != INPUT_COLUMNS:
        raise InvalidInputSchemaError(
            f"{name} must have exact input columns in order: "
            + ", ".join(INPUT_COLUMNS)
        )
    _validate_numeric_columns(vector.table, name)


def _validate_unique_ids(frame, name):
    duplicated = frame.loc[frame["id"].duplicated(), "id"].tolist()
    if duplicated:
        raise DuplicateSegmentIdError(
            f"Found duplicate ID(s) in {name}: " + ", ".join(map(str, duplicated))
        )


def _validate_geometries(geometries, name, allowed_codes):
    if np.any(shapely.is_missing(geometries)) or np.any(shapely.is_empty(geometries)):
        raise InvalidInputSchemaError(f"{name} contains missing or empty geometry")
    if not set(shapely.get_type_id(geometries).tolist()).issubset(allowed_codes):
        raise InvalidInputSchemaError(f"{name} has invalid geometry types")
    if not np.all(shapely.is_valid(geometries)):
        raise InvalidInputSchemaError(f"{name} contains invalid geometry")


def _reverse_adjacency(ids, downstream):
    result = defaultdict(list)
    id_set = set(ids)
    for value in id_set:
        target = downstream.get(value)
        if target in id_set:
            result[target].append(value)
    for values in result.values():
        values.sort(key=_stable_key)
    return dict(result)


def _topological_order(ids, downstream):
    indegree = dict.fromkeys(ids, 0)
    for target in downstream.values():
        if target in indegree:
            indegree[target] += 1
    serial = 0
    ready = []
    for value in ids:
        if indegree[value] == 0:
            heapq.heappush(
                ready, (_stable_key(value), type(value).__name__, serial, value)
            )
            serial += 1
    order = []
    while ready:
        _, _, _, value = heapq.heappop(ready)
        order.append(value)
        target = downstream.get(value)
        if target in indegree:
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(
                    ready, (_stable_key(target), type(target).__name__, serial, target)
                )
                serial += 1
    if len(order) != len(ids):
        raise TopologyCycleError("Detected topology cycle while aggregating")
    return order


def _groups_from_assignment(assignment):
    result = defaultdict(set)
    for value, mini_id in assignment.items():
        result[mini_id].add(value)
    return dict(result)


def _representative_id(values, row_by_id):
    return max(
        values,
        key=lambda value: (
            row_by_id.at[value, "upstream_area"],
            row_by_id.at[value, "unit_length"],
            str(value),
        ),
    )


def _rows_to_vector(rows, crs, geometry_type):
    columns = {
        "id": pa.array([row["id"] for row in rows], type=pa.int64()),
        "id_down": pa.array([row["id_down"] for row in rows], type=pa.int64()),
        "sub": pa.array([row["sub"] for row in rows], type=pa.int64()),
        "p_order": pa.array([row["p_order"] for row in rows], type=pa.int64()),
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
        "geometry": pa.array(
            shapely.to_wkb([row["geometry"] for row in rows]), type=pa.binary()
        ),
    }
    return VectorTable(pa.table(columns), crs, "geometry", geometry_type)


def _stable_key(value: Hashable) -> str:
    return str(value)
