from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import time
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyogrio
from pyproj import CRS, Transformer
import shapely

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    InvalidInputSchemaError,
    TopologyCycleError,
)
from mgb_vec_hydro.execution.checkpoints import (
    CheckpointStore,
    execution_fingerprint,
)
from mgb_vec_hydro.execution.executor import ExecutionConfig, LocalExecutor, WorkItem
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.vector import (
    VectorTable,
    VectorTableCheckpointCodec,
    read_vector_table,
    write_vector_table,
)
from mgb_vec_hydro.roi import ROI_COLUMNS, RoiDataset
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
AGGREGATION_CONTRACT = "mgb-aggregation-dataset"
AGGREGATION_CONTRACT_VERSION = 2


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
    roi: Path
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
    manifest: Path
    catchment_count: int
    segment_count: int
    mapping_count: int
    timings: dict[str, float]


@dataclass(frozen=True)
class _AggregationPacket:
    vector: VectorTable
    assignment: dict[Hashable, Hashable]


def aggregate_minibasins(
    roi_catchments: VectorTable,
    roi_segments: VectorTable,
    *,
    uparea_min: float,
    lmin: float,
) -> AggregationResult:
    """Aggregate normalized ROI products using chain-first topology reduction."""
    return _aggregate_minibasins(
        roi_catchments, roi_segments, uparea_min=uparea_min, lmin=lmin, dissolve=True
    )


def _aggregate_minibasins(
    roi_catchments: VectorTable,
    roi_segments: VectorTable,
    *,
    uparea_min: float,
    lmin: float,
    dissolve: bool,
) -> AggregationResult:
    _validate_input_schema(roi_catchments, "roi_catchments")
    _validate_input_schema(roi_segments, "roi_segments")
    if roi_catchments.crs != roi_segments.crs:
        raise InvalidInputSchemaError("ROI catchment and segment CRS values differ")
    if uparea_min < 0 or lmin < 0:
        raise InvalidInputSchemaError("uparea-min and lmin must be non-negative")

    catchment_geometry = roi_catchments.geometries()
    segment_geometry = roi_segments.geometries()
    _validate_geometries(catchment_geometry, "roi_catchments", {3, 6})
    _validate_geometries(segment_geometry, "roi_segments", {1, 5})
    catchments = _attributes(roi_catchments)
    segments = _attributes(roi_segments)
    _validate_unique_ids(catchments, "roi_catchments")
    _validate_unique_ids(segments, "roi_segments")
    if set(catchments["id"]) != set(segments["id"]):
        raise InvalidInputSchemaError("ROI catchment and segment IDs do not match")

    state = _build_aggregation_state(segments, uparea_min, lmin)
    source_assignment = state["catchment_assignment"]
    reach_assignment = state["reach_assignment"]
    groups = _groups_from_assignment(reach_assignment)

    catchment_ids = catchments["id"].tolist()
    catchment_mini = [source_assignment[value] for value in catchment_ids]
    catchment_geometry_by_id = dict(zip(catchment_ids, catchment_geometry, strict=True))
    segment_geometry_by_id = dict(zip(segments["id"], segment_geometry, strict=True))
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
    attrs = {
        mini_id: {**segment_attributes[mini_id], **catchment_metrics[mini_id]}
        for mini_id in groups
    }

    segment_rows: list[dict[str, Any]] = []
    catchment_rows: list[dict[str, Any]] = []
    for mini_id in sorted(groups, key=_stable_key):
        members = groups[mini_id]
        segment_rows.append(
            {
                **attrs[mini_id],
                "geometry": (
                    shapely.union_all(
                        [segment_geometry_by_id[value] for value in members]
                    )
                    if dissolve
                    else segment_geometry_by_id[next(iter(members))]
                ),
            }
        )
        source_members = catchment_groups[mini_id]
        catchment_rows.append(
            {
                **attrs[mini_id],
                "geometry": (
                    shapely.union_all(
                        [catchment_geometry_by_id[value] for value in source_members]
                    )
                    if dissolve
                    else catchment_geometry_by_id[next(iter(source_members))]
                ),
            }
        )

    id_type = roi_segments.table["id"].type
    water_course_type = roi_segments.table["water_course"].type
    aggregated_segments = _rows_to_vector(
        segment_rows, id_type, roi_segments.crs, "Unknown", water_course_type
    )
    aggregated_catchments = _rows_to_vector(
        catchment_rows, id_type, roi_catchments.crs, "Unknown", water_course_type
    )
    centroids = shapely.centroid(catchment_geometry)
    transformer = Transformer.from_crs(roi_catchments.crs, "EPSG:4326", always_xy=True)
    lonlat = shapely.transform(centroids, transformer.transform, interleaved=False)
    mapping = pd.DataFrame(
        {
            "id": catchment_ids,
            "mini_id": catchment_mini,
            "sub": catchments["sub"].to_numpy(),
            "longitude": shapely.get_x(lonlat),
            "latitude": shapely.get_y(lonlat),
        }
    )
    return AggregationResult(
        aggregated_catchments,
        aggregated_segments,
        mapping,
        dict(source_assignment),
        dict(reach_assignment),
    )


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
            "strahler_order": representative["strahler_order"],
            "unit_length": float(row_by_id.loc[list(members), "unit_length"].sum()),
            "upstream_length": float(representative["upstream_length"]),
            "water_course": representative["water_course"],
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


def aggregate_roi_dataset(spec: AggregationSpec) -> AggregationReport:
    overall_started = time.perf_counter()
    _validate_spec(spec)
    phase_started = time.perf_counter()
    dataset = RoiDataset.open(spec.roi)
    dataset.validate()
    catchments = read_vector_table(dataset.path("catchments", validate=False))
    segments = read_vector_table(dataset.path("segments", validate=False))
    expected_crs = CRS.from_wkt(dataset.manifest["crs_wkt"])
    if catchments.crs != expected_crs or segments.crs != expected_crs:
        raise InvalidInputSchemaError("ROI assets do not use the manifest CRS")
    roi_input_seconds = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    result = _aggregate_minibasins(
        catchments,
        segments,
        uparea_min=spec.uparea_min,
        lmin=spec.lmin,
        dissolve=False,
    )
    aggregation_seconds = time.perf_counter() - phase_started
    items, kinds = _aggregation_work_items(
        catchments,
        segments,
        result,
        batch_size=spec.batch_size,
    )
    checkpoint = None
    if spec.checkpoint_dir is not None:
        fingerprint = execution_fingerprint(
            algorithm="aggregate",
            version="2",
            prepared_manifest=dataset.manifest,
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
            return None

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
            result,
            catchment_path=catchment_path,
            segment_path=segment_path,
        )
        shutil.rmtree(packet_dir)
        mapping = result.mapping.sort_values(
            "id", key=lambda values: values.astype(str), kind="stable"
        ).reset_index(drop=True)
        mapping.to_csv(mapping_path, index=False)
        _validate_aggregation_outputs(
            catchment_path,
            segment_path,
            mapping_path,
            expected_crs=expected_crs,
            source_ids=set(catchments.table["id"].to_pylist()),
        )
        manifest = {
            "contract": AGGREGATION_CONTRACT,
            "version": AGGREGATION_CONTRACT_VERSION,
            "roi": str(Path(spec.roi).resolve()),
            "crs_wkt": expected_crs.to_wkt(version="WKT2_2019", pretty=False),
            "assets": {
                "catchments": {
                    "path": catchment_path.name,
                    "driver": "FlatGeobuf",
                    "feature_count": len(result.catchments),
                },
                "segments": {
                    "path": segment_path.name,
                    "driver": "FlatGeobuf",
                    "feature_count": len(result.segments),
                },
                "mapping": {
                    "path": mapping_path.name,
                    "driver": "CSV",
                    "feature_count": len(mapping),
                },
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        publisher.publish(
            ("manifest.json", catchment_path.name, segment_path.name, mapping_path.name)
        )
    output_publication_seconds = time.perf_counter() - phase_started
    if checkpoint is not None:
        checkpoint.cleanup()
    return AggregationReport(
        output,
        output / "manifest.json",
        len(result.catchments),
        len(result.segments),
        len(result.mapping),
        {
            "roi_input": roi_input_seconds,
            "aggregation": aggregation_seconds,
            "geometry_execution": execution.wall_seconds,
            "output_publication": output_publication_seconds,
            "total": time.perf_counter() - overall_started,
        },
    )


def _aggregation_work_items(
    catchments: VectorTable,
    segments: VectorTable,
    result: AggregationResult,
    *,
    batch_size: int,
) -> tuple[list[WorkItem[_AggregationPacket]], dict[int, str]]:
    """Plan deterministic packets without splitting a complete water course."""
    domains: dict[tuple[Any, Any], list[Hashable]] = defaultdict(list)
    for segment_id, sub, water_course in _attributes(segments)[
        ["id", "sub", "water_course"]
    ].itertuples(index=False, name=None):
        domains[(sub, water_course)].append(segment_id)
    packets: list[list[Hashable]] = []
    current: list[Hashable] = []
    for domain in sorted(domains, key=lambda value: (str(value[0]), str(value[1]))):
        values = sorted(domains[domain], key=_stable_key)
        if current and len(current) + len(values) > batch_size:
            packets.append(current)
            current = []
        current.extend(values)
    if current:
        packets.append(current)

    items: list[WorkItem[_AggregationPacket]] = []
    kinds: dict[int, str] = {}
    for kind, vector, assignment in (
        ("catchments", catchments, result._catchment_assignment),
        ("segments", segments, result._reach_assignment),
    ):
        if assignment is None:
            raise InvalidInputSchemaError("Aggregation assignment metadata is missing")
        source_ids = vector.table["id"].to_pylist()
        position_by_id = {value: index for index, value in enumerate(source_ids)}
        for packet_index, ids in enumerate(packets):
            selected_ids = [value for value in ids if value in assignment]
            if not selected_ids:
                continue
            positions = pa.array(
                [position_by_id[value] for value in selected_ids], type=pa.int64()
            )
            table = vector.table.take(positions)
            packet = VectorTable(
                table, vector.crs, vector.geometry_column, vector.geometry_type
            )
            ordinal = len(items)
            items.append(
                WorkItem(
                    f"{kind}-{packet_index:012d}",
                    ordinal,
                    max(1, table.nbytes * 4),
                    _AggregationPacket(
                        packet,
                        {value: assignment[value] for value in selected_ids},
                    ),
                )
            )
            kinds[ordinal] = kind
    return items, kinds


def _prepare_aggregation_packet(payload: _AggregationPacket, _context) -> VectorTable:
    ids = payload.vector.table["id"].to_pylist()
    table = pa.table(
        {
            "mini_id": pa.array(
                [payload.assignment[value] for value in ids],
                type=payload.vector.table["id"].type,
            ),
            "geometry": payload.vector.table[
                payload.vector.geometry_column
            ].combine_chunks(),
        }
    )
    return VectorTable(
        table, payload.vector.crs, "geometry", payload.vector.geometry_type
    )


def _gdal_dissolve_outputs(
    staging: Path,
    packet_dir: Path,
    kinds: dict[int, str],
    result: AggregationResult,
    *,
    catchment_path: Path,
    segment_path: Path,
) -> None:
    """Dissolve assigned WKB with GDAL's SQLite engine and stream to FGB."""
    workspace = staging / ".aggregation.gpkg"
    layers = (
        ("catchment_sources", "catchment_attrs", result.catchments, catchment_path),
        ("segment_sources", "segment_attrs", result.segments, segment_path),
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
        for _, attrs_layer, attributes, _ in layers:
            attrs = attributes.table.drop([attributes.geometry_column])
            pyogrio.write_arrow(attrs, workspace, layer=attrs_layer, driver="GPKG")

        for source_layer, attrs_layer, attributes, output in layers:
            geometry_name = pyogrio.read_info(workspace, layer=source_layer)[
                "geometry_name"
            ]
            attribute_names = [
                name
                for name in attributes.table.column_names
                if name != attributes.geometry_column
            ]
            select = ", ".join(f'a."{name}"' for name in attribute_names)
            group_by = ", ".join(f'a."{name}"' for name in attribute_names)
            sql = (
                f'SELECT {select}, ST_Union(s."{geometry_name}") AS geometry '
                f'FROM "{source_layer}" s JOIN "{attrs_layer}" a '
                f'ON s."mini_id" = a."id" GROUP BY {group_by} '
                'ORDER BY CAST(a."id" AS TEXT)'
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
                    crs=attributes.crs.to_wkt(version="WKT2_2019", pretty=False),
                    layer_options={"SPATIAL_INDEX": "YES"},
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
    if spec.workers <= 0 or spec.workers > 4:
        raise InvalidInputSchemaError("workers must be between one and four")
    if spec.memory_limit_mb <= 0 or spec.io_slots <= 0 or spec.batch_size <= 0:
        raise InvalidInputSchemaError("execution limits must be positive")
    if spec.uparea_min < 0 or spec.lmin < 0:
        raise InvalidInputSchemaError("uparea-min and lmin must be non-negative")


def _validate_aggregation_outputs(
    catchments, segments, mapping, *, expected_crs, source_ids
):
    for name, path in (("mini_catchments", catchments), ("mini_segments", segments)):
        info = pyogrio.read_info(path)
        if [*info["fields"], "geometry"] != ROI_COLUMNS:
            raise InvalidInputSchemaError(f"{name} output schema is invalid")
        if info.get("crs") is None or CRS.from_user_input(info["crs"]) != expected_crs:
            raise InvalidInputSchemaError(f"{name} output CRS is invalid")
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
    for column in (
        "sub",
        "strahler_order",
        "unit_length",
        "upstream_length",
        "unit_area",
        "upstream_area",
    ):
        value = vector.table[column].type
        if not (
            pa.types.is_integer(value)
            or pa.types.is_floating(value)
            or pa.types.is_decimal(value)
        ):
            raise InvalidInputSchemaError(
                f"{name} has non-numeric metric column(s): {column}"
            )


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
    ready = sorted(
        (value for value, count in indegree.items() if count == 0),
        key=_stable_key,
        reverse=True,
    )
    order = []
    while ready:
        value = ready.pop()
        order.append(value)
        target = downstream.get(value)
        if target in indegree:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=_stable_key, reverse=True)
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


def _rows_to_vector(rows, id_type, crs, geometry_type, water_course_type):
    def nullable(value):
        return None if pd.isna(value) else value

    columns = {
        "id": pa.array([row["id"] for row in rows], type=id_type),
        "id_down": pa.array([nullable(row["id_down"]) for row in rows], type=id_type),
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
        "water_course": pa.array(
            [row["water_course"] for row in rows], type=water_course_type
        ),
        "geometry": pa.array(
            shapely.to_wkb([row["geometry"] for row in rows]), type=pa.binary()
        ),
    }
    return VectorTable(pa.table(columns), crs, "geometry", geometry_type)


def _stable_key(value: Hashable) -> str:
    return str(value)
