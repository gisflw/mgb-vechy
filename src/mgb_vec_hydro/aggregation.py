from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import CRS

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    InvalidInputSchemaError,
    TopologyCycleError,
)
from mgb_vec_hydro.execution.checkpoints import (
    CheckpointStore,
    JsonCheckpointCodec,
    execution_fingerprint,
)
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
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


@dataclass(frozen=True)
class AggregationResult:
    """Stage 2 aggregated mini catchments, reaches, and source mapping."""

    catchments: gpd.GeoDataFrame
    segments: gpd.GeoDataFrame
    mapping: pd.DataFrame


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
    catchment_count: int
    segment_count: int
    mapping_count: int


def aggregate_minibasins(
    roi_catchments: gpd.GeoDataFrame,
    roi_segments: gpd.GeoDataFrame,
    *,
    uparea_min: float,
    lmin: float,
) -> AggregationResult:
    """Aggregate normalized ROI products into mini-basins."""

    _validate_input_schema(roi_catchments, "roi_catchments")
    _validate_input_schema(roi_segments, "roi_segments")
    _validate_unique_ids(roi_segments, "roi_segments")
    _validate_unique_ids(roi_catchments, "roi_catchments")

    segments = roi_segments.reset_index(drop=True).copy()
    catchments = roi_catchments.reset_index(drop=True).copy()
    downstream_by_id = dict(
        segments[["id", "id_down"]].itertuples(index=False, name=None)
    )
    upstream_by_downstream = _build_reverse_adjacency(segments)
    _downstream_to_upstream_order(segments, downstream_by_id)

    eligible_segments = segments.loc[segments["upstream_area"] >= uparea_min].copy()
    excluded_segments = segments.loc[segments["upstream_area"] < uparea_min].copy()
    eligible_ids = set(eligible_segments["id"].tolist())
    eligible_downstream_by_id = {
        segment_id: downstream_id if downstream_id in eligible_ids else None
        for segment_id, downstream_id in eligible_segments[
            ["id", "id_down"]
        ].itertuples(index=False, name=None)
    }
    domain_by_id = _aggregation_domain_ids(segments)
    sub_domain_by_id = _sub_domain_ids(segments)
    mini_by_segment = _initial_candidate_groups(eligible_segments)
    mini_by_segment = _merge_short_groups(
        eligible_segments,
        mini_by_segment,
        domain_by_id,
        lmin,
    )
    mini_by_segment, short_mini_by_segment = _filter_unmergeable_short_groups(
        eligible_segments,
        segments,
        downstream_by_id,
        upstream_by_downstream,
        mini_by_segment,
        domain_by_id,
        sub_domain_by_id,
        lmin,
    )
    excluded_mini_by_segment = _assign_excluded_segments(
        excluded_segments,
        segments,
        downstream_by_id,
        upstream_by_downstream,
        mini_by_segment,
        domain_by_id,
        sub_domain_by_id,
    )
    catchment_mini_by_segment = {
        **mini_by_segment,
        **short_mini_by_segment,
        **excluded_mini_by_segment,
    }

    groups = _groups_from_assignment(mini_by_segment)
    catchment_groups = _groups_from_assignment(catchment_mini_by_segment)
    attributes = _mini_attributes(
        eligible_segments,
        groups,
        eligible_downstream_by_id,
        mini_by_segment,
    )
    catchment_mini = catchments["id"].map(catchment_mini_by_segment)
    if catchment_mini.isna().any():
        missing = catchments.loc[catchment_mini.isna(), "id"].tolist()
        raise InvalidInputSchemaError(
            "Catchment ID(s) have no eligible aggregation target in the same sub: "
            + ", ".join(str(value) for value in missing)
        )

    segment_mini = eligible_segments["id"].map(mini_by_segment)
    segment_geometries = _dissolved_geometries(
        eligible_segments,
        segment_mini,
        "mini_id",
    )
    catchment_geometries = _dissolved_geometries(catchments, catchment_mini, "mini_id")

    segment_rows = []
    catchment_rows = []
    for mini_id in sorted(groups, key=_stable_key):
        attrs = attributes[mini_id]
        row = {**attrs, "geometry": segment_geometries.loc[mini_id]}
        segment_rows.append(row)
        catchment_group = catchments.loc[
            catchments["id"].isin(catchment_groups[mini_id])
        ]
        catchment_attrs = {
            **attrs,
            "unit_area": float(catchment_group["unit_area"].sum()),
            "geometry": catchment_geometries.loc[mini_id],
        }
        catchment_rows.append(catchment_attrs)

    aggregated_segments = gpd.GeoDataFrame(
        segment_rows,
        columns=INPUT_COLUMNS,
        geometry="geometry",
        crs=segments.crs,
    )
    aggregated_catchments = gpd.GeoDataFrame(
        catchment_rows,
        columns=INPUT_COLUMNS,
        geometry="geometry",
        crs=catchments.crs,
    )
    # Centroids are computed in the canonical projected CRS, then transformed.
    centroid = gpd.GeoSeries(catchments.geometry.centroid, crs=catchments.crs).to_crs(
        "EPSG:4326"
    )
    mapping = pd.DataFrame(
        {
            "id": catchments["id"].to_numpy(),
            "mini_id": catchment_mini.to_numpy(),
            "sub": catchments["sub"].to_numpy(),
            "longitude": centroid.x.to_numpy(),
            "latitude": centroid.y.to_numpy(),
        }
    )

    return AggregationResult(
        catchments=aggregated_catchments.reset_index(drop=True),
        segments=aggregated_segments.reset_index(drop=True),
        mapping=mapping.reset_index(drop=True),
    )


def aggregate_roi_dataset(spec: AggregationSpec) -> AggregationReport:
    """Aggregate one versioned ROI and atomically publish fixed Stage 2 assets."""
    if spec.workers <= 0 or spec.workers > 4:
        raise InvalidInputSchemaError("workers must be between one and four")
    if spec.memory_limit_mb <= 0 or spec.io_slots <= 0 or spec.batch_size <= 0:
        raise InvalidInputSchemaError("execution limits must be positive")
    if spec.uparea_min < 0 or spec.lmin < 0:
        raise InvalidInputSchemaError("uparea-min and lmin must be non-negative")
    dataset = RoiDataset.open(spec.roi)
    dataset.validate()
    checkpoint = None
    if spec.checkpoint_dir is not None:
        fingerprint = execution_fingerprint(
            algorithm="aggregate",
            version="1",
            prepared_manifest=dataset.manifest,
            parameters={"uparea_min": spec.uparea_min, "lmin": spec.lmin},
            work_items=(),
        )
        checkpoint = CheckpointStore(
            spec.checkpoint_dir, fingerprint, JsonCheckpointCodec()
        )
    catchments = gpd.read_file(dataset.path("catchments"))
    segments = gpd.read_file(dataset.path("segments"))
    expected_crs = CRS.from_wkt(dataset.manifest["crs_wkt"])
    if (
        catchments.crs is None
        or segments.crs is None
        or CRS.from_user_input(catchments.crs) != expected_crs
        or CRS.from_user_input(segments.crs) != expected_crs
    ):
        raise InvalidInputSchemaError("ROI assets do not use the manifest CRS")
    result = aggregate_minibasins(
        catchments, segments, uparea_min=spec.uparea_min, lmin=spec.lmin
    )
    output = Path(spec.output_dir)
    publisher = AtomicOutputDirectory(output)
    with publisher as staging:
        catchment_path = staging / "mini_catchments.fgb"
        segment_path = staging / "mini_segments.fgb"
        mapping_path = staging / "source_to_mini.csv"
        result.catchments.to_file(
            catchment_path, driver="FlatGeobuf", index=False, SPATIAL_INDEX="YES"
        )
        result.segments.to_file(
            segment_path, driver="FlatGeobuf", index=False, SPATIAL_INDEX="YES"
        )
        mapping = result.mapping.sort_values(
            "id", key=lambda values: values.astype(str), kind="stable"
        ).reset_index(drop=True)
        mapping.to_csv(mapping_path, index=False)
        _validate_aggregation_outputs(
            catchment_path, segment_path, mapping_path,
            expected_crs=expected_crs, source_ids=set(catchments["id"]),
        )
        publisher.publish(
            ("mini_catchments.fgb", "mini_segments.fgb", "source_to_mini.csv")
        )
    if checkpoint is not None:
        checkpoint.cleanup()
    return AggregationReport(
        output, len(result.catchments), len(result.segments), len(result.mapping)
    )


def _validate_aggregation_outputs(
    catchments: Path,
    segments: Path,
    mapping: Path,
    *,
    expected_crs: CRS,
    source_ids: set[Hashable],
) -> None:
    for name, path in (("mini_catchments", catchments), ("mini_segments", segments)):
        frame = gpd.read_file(path)
        if list(frame.columns) != ROI_COLUMNS:
            raise InvalidInputSchemaError(f"{name} output schema is invalid")
        if frame.crs is None or CRS.from_user_input(frame.crs) != expected_crs:
            raise InvalidInputSchemaError(f"{name} output CRS is invalid")
    table = pd.read_csv(mapping)
    if list(table.columns) != ["id", "mini_id", "sub", "longitude", "latitude"]:
        raise InvalidInputSchemaError("source_to_mini.csv schema is invalid")
    if len(table) != len(source_ids) or table["id"].duplicated().any():
        raise InvalidInputSchemaError("source_to_mini.csv does not contain each source once")
    if {str(value) for value in table["id"]} != {str(value) for value in source_ids}:
        raise InvalidInputSchemaError("source_to_mini.csv source IDs do not match the ROI")


def _validate_input_schema(gdf: gpd.GeoDataFrame, name: str) -> None:
    if not isinstance(gdf, gpd.GeoDataFrame):
        raise InvalidInputSchemaError(f"{name} must be a GeoDataFrame")
    actual = list(gdf.columns)
    if actual != INPUT_COLUMNS:
        raise InvalidInputSchemaError(
            f"{name} must have exact input columns in order: "
            + ", ".join(INPUT_COLUMNS)
        )
    if gdf.geometry.name != "geometry":
        raise InvalidInputSchemaError(f"{name} must have an active geometry column")
    numeric_columns = [
        "sub",
        "strahler_order",
        "unit_length",
        "upstream_length",
        "unit_area",
        "upstream_area",
    ]
    non_numeric = [
        column
        for column in numeric_columns
        if not pd.api.types.is_numeric_dtype(gdf[column])
    ]
    if non_numeric:
        raise InvalidInputSchemaError(
            f"{name} has non-numeric metric column(s): " + ", ".join(non_numeric)
        )


def _validate_unique_ids(gdf: gpd.GeoDataFrame, name: str) -> None:
    duplicated = gdf.loc[gdf["id"].duplicated(), "id"].tolist()
    if duplicated:
        values = ", ".join(str(value) for value in duplicated)
        raise DuplicateSegmentIdError(f"Found duplicate ID(s) in {name}: {values}")


def _build_reverse_adjacency(segments: gpd.GeoDataFrame) -> dict[Hashable, list[Hashable]]:
    upstream_by_downstream: dict[Hashable, list[Hashable]] = defaultdict(list)
    ids = set(segments["id"].tolist())
    for segment_id, downstream_id in segments[["id", "id_down"]].itertuples(
        index=False,
        name=None,
    ):
        if not _is_sink_value(downstream_id) and downstream_id in ids:
            upstream_by_downstream[downstream_id].append(segment_id)
    return dict(upstream_by_downstream)


def _downstream_to_upstream_order(
    segments: gpd.GeoDataFrame,
    downstream_by_id: dict[Hashable, Hashable],
) -> list[Hashable]:
    ids = set(segments["id"].tolist())
    upstream_count_by_id = dict.fromkeys(ids, 0)
    for downstream_id in downstream_by_id.values():
        if downstream_id in ids:
            upstream_count_by_id[downstream_id] += 1

    ready = [
        segment_id
        for segment_id, upstream_count in upstream_count_by_id.items()
        if upstream_count == 0
    ]
    upstream_to_downstream: list[Hashable] = []
    while ready:
        segment_id = ready.pop()
        upstream_to_downstream.append(segment_id)
        downstream_id = downstream_by_id.get(segment_id)
        if downstream_id in upstream_count_by_id:
            upstream_count_by_id[downstream_id] -= 1
            if upstream_count_by_id[downstream_id] == 0:
                ready.append(downstream_id)

    if len(upstream_to_downstream) != len(ids):
        raise TopologyCycleError("Detected topology cycle while aggregating")

    return list(reversed(upstream_to_downstream))


def _aggregation_domain_ids(
    segments: gpd.GeoDataFrame,
) -> dict[Hashable, tuple[Hashable, Hashable]]:
    return {
        segment_id: (sub, water_course)
        for segment_id, sub, water_course in segments[
            ["id", "sub", "water_course"]
        ].itertuples(index=False, name=None)
    }


def _sub_domain_ids(segments: gpd.GeoDataFrame) -> dict[Hashable, Hashable]:
    return dict(segments[["id", "sub"]].itertuples(index=False, name=None))


def _initial_candidate_groups(
    segments: gpd.GeoDataFrame,
) -> dict[Hashable, Hashable]:
    mini_by_segment: dict[Hashable, Hashable] = {}
    for segment_id in segments["id"]:
        mini_by_segment[segment_id] = segment_id
    return mini_by_segment


def _merge_short_groups(
    segments: gpd.GeoDataFrame,
    mini_by_segment: dict[Hashable, Hashable],
    domain_by_id: dict[Hashable, tuple[Hashable, Hashable]],
    lmin: float,
) -> dict[Hashable, Hashable]:
    if not mini_by_segment:
        return mini_by_segment

    downstream_by_id = dict(
        segments[["id", "id_down"]].itertuples(index=False, name=None)
    )
    upstream_by_downstream = _build_reverse_adjacency(segments)
    unit_length = segments.set_index("id")["unit_length"]

    changed = True
    while changed:
        changed = False
        groups = _groups_from_assignment(mini_by_segment)
        lengths = {
            mini_id: float(unit_length.loc[list(member_ids)].sum())
            for mini_id, member_ids in groups.items()
        }
        short_ids = sorted(
            [mini_id for mini_id, length in lengths.items() if length < lmin],
            key=_stable_key,
        )
        for mini_id in short_ids:
            groups = _groups_from_assignment(mini_by_segment)
            if mini_id not in groups:
                continue
            target = _best_adjacent_group(
                groups[mini_id],
                mini_id,
                mini_by_segment,
                downstream_by_id,
                upstream_by_downstream,
                domain_by_id,
                lengths,
            )
            if target is None:
                continue
            for segment_id in groups[mini_id]:
                mini_by_segment[segment_id] = target
            changed = True
            break

    return mini_by_segment


def _assign_excluded_segments(
    excluded_segments: gpd.GeoDataFrame,
    all_segments: gpd.GeoDataFrame,
    downstream_by_id: dict[Hashable, Hashable],
    upstream_by_downstream: dict[Hashable, list[Hashable]],
    mini_by_segment: dict[Hashable, Hashable],
    domain_by_id: dict[Hashable, tuple[Hashable, Hashable]],
    fallback_domain_by_id: dict[Hashable, Hashable],
) -> dict[Hashable, Hashable]:
    if excluded_segments.empty or not mini_by_segment:
        return {}

    unit_length = all_segments.set_index("id")["unit_length"]
    groups = _groups_from_assignment(mini_by_segment)
    lengths = {
        mini_id: float(unit_length.loc[list(member_ids)].sum())
        for mini_id, member_ids in groups.items()
    }
    excluded_mini_by_segment: dict[Hashable, Hashable] = {}
    for segment_id in sorted(excluded_segments["id"], key=_stable_key):
        target = _best_adjacent_group(
            {segment_id},
            None,
            mini_by_segment,
            downstream_by_id,
            upstream_by_downstream,
            domain_by_id,
            lengths,
        )
        if target is None:
            target = _best_adjacent_group(
                {segment_id},
                None,
                mini_by_segment,
                downstream_by_id,
                upstream_by_downstream,
                fallback_domain_by_id,
                lengths,
            )
        if target is not None:
            excluded_mini_by_segment[segment_id] = target

    return excluded_mini_by_segment


def _filter_unmergeable_short_groups(
    eligible_segments: gpd.GeoDataFrame,
    all_segments: gpd.GeoDataFrame,
    downstream_by_id: dict[Hashable, Hashable],
    upstream_by_downstream: dict[Hashable, list[Hashable]],
    mini_by_segment: dict[Hashable, Hashable],
    domain_by_id: dict[Hashable, tuple[Hashable, Hashable]],
    fallback_domain_by_id: dict[Hashable, Hashable],
    lmin: float,
) -> tuple[dict[Hashable, Hashable], dict[Hashable, Hashable]]:
    if not mini_by_segment:
        return mini_by_segment, {}

    unit_length = eligible_segments.set_index("id")["unit_length"]
    groups = _groups_from_assignment(mini_by_segment)
    short_mini_ids = {
        mini_id
        for mini_id, member_ids in groups.items()
        if float(unit_length.loc[list(member_ids)].sum()) < lmin
    }
    if not short_mini_ids:
        return mini_by_segment, {}

    surviving_mini_by_segment = {
        segment_id: mini_id
        for segment_id, mini_id in mini_by_segment.items()
        if mini_id not in short_mini_ids
    }
    short_segments = all_segments.loc[
        all_segments["id"].isin(
            segment_id
            for segment_id, mini_id in mini_by_segment.items()
            if mini_id in short_mini_ids
        )
    ]
    short_mini_by_segment = _assign_excluded_segments(
        short_segments,
        all_segments,
        downstream_by_id,
        upstream_by_downstream,
        surviving_mini_by_segment,
        domain_by_id,
        fallback_domain_by_id,
    )
    return surviving_mini_by_segment, short_mini_by_segment


def _best_adjacent_group(
    members: set[Hashable],
    mini_id: Hashable | None,
    mini_by_segment: dict[Hashable, Hashable],
    downstream_by_id: dict[Hashable, Hashable],
    upstream_by_downstream: dict[Hashable, list[Hashable]],
    domain_by_id: dict[Hashable, Hashable],
    lengths: dict[Hashable, float],
) -> Hashable | None:
    candidates: set[Hashable] = set()
    all_ids = set(downstream_by_id)
    member_domain = domain_by_id[next(iter(members))]
    for segment_id in members:
        downstream_id = downstream_by_id.get(segment_id)
        while downstream_id in all_ids:
            if domain_by_id[downstream_id] != member_domain:
                break
            downstream_mini = mini_by_segment.get(downstream_id)
            if downstream_mini is not None and downstream_mini != mini_id:
                candidates.add(downstream_mini)
                break
            downstream_id = downstream_by_id.get(downstream_id)

        stack = list(upstream_by_downstream.get(segment_id, []))
        seen: set[Hashable] = set()
        while stack:
            upstream_id = stack.pop()
            if upstream_id in seen:
                continue
            seen.add(upstream_id)
            if domain_by_id[upstream_id] != member_domain:
                continue
            upstream_mini = mini_by_segment.get(upstream_id)
            if upstream_mini is not None and upstream_mini != mini_id:
                candidates.add(upstream_mini)
                continue
            stack.extend(upstream_by_downstream.get(upstream_id, []))

    if not candidates:
        return None
    return min(
        candidates,
        key=lambda candidate: (lengths[candidate], str(candidate)),
    )


def _groups_from_assignment(
    mini_by_segment: dict[Hashable, Hashable],
) -> dict[Hashable, set[Hashable]]:
    groups: dict[Hashable, set[Hashable]] = defaultdict(set)
    for segment_id, mini_id in mini_by_segment.items():
        groups[mini_id].add(segment_id)
    return dict(groups)


def _mini_attributes(
    segments: gpd.GeoDataFrame,
    groups: dict[Hashable, set[Hashable]],
    downstream_by_id: dict[Hashable, Hashable],
    mini_by_segment: dict[Hashable, Hashable],
) -> dict[Hashable, dict[str, object]]:
    segment_by_id = segments.set_index("id")
    attrs: dict[Hashable, dict[str, object]] = {}
    for mini_id, member_ids in groups.items():
        group = segments.loc[segments["id"].isin(member_ids)]
        representative_id = _representative_id(group)
        representative = segment_by_id.loc[representative_id]
        downstream_mini = _downstream_mini(
            representative_id,
            downstream_by_id,
            mini_by_segment,
        )
        attrs[mini_id] = {
            "id": representative_id,
            "id_down": downstream_mini,
            "sub": representative["sub"],
            "strahler_order": representative["strahler_order"],
            "unit_length": float(group["unit_length"].sum()),
            "upstream_length": representative["upstream_length"],
            "unit_area": float(group["unit_area"].sum()),
            "upstream_area": representative["upstream_area"],
            "water_course": representative["water_course"],
        }
    return attrs


def _downstream_mini(
    representative_id: Hashable,
    downstream_by_id: dict[Hashable, Hashable],
    mini_by_segment: dict[Hashable, Hashable],
) -> Hashable | None:
    current_mini = mini_by_segment[representative_id]
    downstream_id = downstream_by_id.get(representative_id)
    seen = {representative_id}
    all_ids = set(downstream_by_id)
    while downstream_id in all_ids and downstream_id not in seen:
        downstream_mini = mini_by_segment.get(downstream_id)
        if downstream_mini is None:
            seen.add(downstream_id)
            downstream_id = downstream_by_id.get(downstream_id)
            continue
        if downstream_mini != current_mini:
            return downstream_mini
        seen.add(downstream_id)
        downstream_id = downstream_by_id.get(downstream_id)
    return None


def _dissolved_geometries(
    gdf: gpd.GeoDataFrame,
    mini_ids: pd.Series,
    column_name: str,
) -> gpd.GeoSeries:
    table = gdf[[gdf.geometry.name]].copy()
    table[column_name] = mini_ids.to_numpy()
    dissolved = table.dissolve(by=column_name, sort=False)
    return dissolved.geometry


def _representative_id(group: gpd.GeoDataFrame) -> Hashable:
    row = max(
        group.itertuples(index=False),
        key=lambda item: (item.upstream_area, item.unit_length, str(item.id)),
    )
    return row.id


def _stable_key(value: Hashable) -> str:
    return str(value)
