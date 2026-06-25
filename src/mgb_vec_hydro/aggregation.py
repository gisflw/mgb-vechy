from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Hashable

import geopandas as gpd
import pandas as pd

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    InvalidInputSchemaError,
    TopologyCycleError,
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


@dataclass(frozen=True)
class AggregationResult:
    """Stage 2 aggregated mini catchments, reaches, and source mapping."""

    catchments: gpd.GeoDataFrame
    segments: gpd.GeoDataFrame
    mapping: gpd.GeoDataFrame


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
    ids = set(segments["id"].tolist())
    downstream_by_id = dict(
        segments[["id", "id_down"]].itertuples(index=False, name=None)
    )
    upstream_by_downstream = _build_reverse_adjacency(segments)
    _downstream_to_upstream_order(segments, downstream_by_id)
    domain_by_id = _aggregation_domain_ids(segments)
    mini_by_segment = _initial_candidate_groups(segments, uparea_min)
    mini_by_segment = _assign_remaining_segments(
        segments,
        downstream_by_id,
        upstream_by_downstream,
        mini_by_segment,
        domain_by_id,
    )
    mini_by_segment = _merge_short_groups(segments, mini_by_segment, domain_by_id, lmin)
    mini_by_segment = _assign_remaining_segments(
        segments,
        downstream_by_id,
        upstream_by_downstream,
        mini_by_segment,
        domain_by_id,
    )

    groups = _groups_from_assignment(mini_by_segment)
    attributes = _mini_attributes(segments, groups, downstream_by_id, mini_by_segment)
    catchment_mini = catchments["id"].map(mini_by_segment)
    if catchment_mini.isna().any():
        missing = catchments.loc[catchment_mini.isna(), "id"].tolist()
        raise InvalidInputSchemaError(
            "Catchment ID(s) missing from segment topology: "
            + ", ".join(str(value) for value in missing)
        )

    segment_mini = segments["id"].map(mini_by_segment)
    segment_geometries = _dissolved_geometries(segments, segment_mini, "mini_id")
    catchment_geometries = _dissolved_geometries(catchments, catchment_mini, "mini_id")

    segment_rows = []
    catchment_rows = []
    for mini_id in sorted(groups, key=_stable_key):
        attrs = attributes[mini_id]
        row = {**attrs, "geometry": segment_geometries.loc[mini_id]}
        segment_rows.append(row)
        catchment_rows.append({**attrs, "geometry": catchment_geometries.loc[mini_id]})

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
    mapping = gpd.GeoDataFrame(
        {
            "id": catchments["id"].to_numpy(),
            "mini_id": catchment_mini.to_numpy(),
            "sub": catchments["sub"].to_numpy(),
        },
        geometry=catchments.geometry.to_numpy(),
        crs=catchments.crs,
    )

    return AggregationResult(
        catchments=aggregated_catchments.reset_index(drop=True),
        segments=aggregated_segments.reset_index(drop=True),
        mapping=mapping.reset_index(drop=True),
    )


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


def _initial_candidate_groups(
    segments: gpd.GeoDataFrame,
    uparea_min: float,
) -> dict[Hashable, Hashable]:
    candidate_segments = segments.loc[segments["upstream_area"] >= uparea_min]
    mini_by_segment: dict[Hashable, Hashable] = {}
    for segment_id in candidate_segments["id"]:
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


def _assign_remaining_segments(
    segments: gpd.GeoDataFrame,
    downstream_by_id: dict[Hashable, Hashable],
    upstream_by_downstream: dict[Hashable, list[Hashable]],
    mini_by_segment: dict[Hashable, Hashable],
    domain_by_id: dict[Hashable, tuple[Hashable, Hashable]],
) -> dict[Hashable, Hashable]:
    ids = set(segments["id"].tolist())
    if not mini_by_segment:
        for _, group in segments.assign(domain=segments["id"].map(domain_by_id)).groupby(
            ["sub", "domain"],
            sort=False,
        ):
            representative_id = _representative_id(group)
            for segment_id in group["id"]:
                mini_by_segment[segment_id] = representative_id
        return mini_by_segment

    pending = ids - set(mini_by_segment)
    unit_length = segments.set_index("id")["unit_length"]
    while pending:
        progressed = False
        groups = _groups_from_assignment(mini_by_segment)
        lengths = {
            mini_id: float(unit_length.loc[list(member_ids)].sum())
            for mini_id, member_ids in groups.items()
        }
        for segment_id in sorted(pending, key=_stable_key):
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
                continue
            mini_by_segment[segment_id] = target
            pending.remove(segment_id)
            progressed = True
            break
        if not progressed:
            fallback_id = sorted(pending, key=_stable_key)[0]
            component = _unassigned_component(fallback_id, pending, downstream_by_id)
            group = segments.loc[segments["id"].isin(component)]
            representative_id = _representative_id(group)
            for segment_id in component:
                mini_by_segment[segment_id] = representative_id
                pending.remove(segment_id)

    return mini_by_segment


def _best_adjacent_group(
    members: set[Hashable],
    mini_id: Hashable | None,
    mini_by_segment: dict[Hashable, Hashable],
    downstream_by_id: dict[Hashable, Hashable],
    upstream_by_downstream: dict[Hashable, list[Hashable]],
    domain_by_id: dict[Hashable, tuple[Hashable, Hashable]],
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


def _unassigned_component(
    start_id: Hashable,
    pending: set[Hashable],
    downstream_by_id: dict[Hashable, Hashable],
) -> set[Hashable]:
    component = {start_id}
    changed = True
    while changed:
        changed = False
        for segment_id in list(pending):
            downstream_id = downstream_by_id.get(segment_id)
            if downstream_id in component or (
                segment_id in component and downstream_id in pending
            ):
                before = len(component)
                component.add(segment_id)
                if downstream_id in pending:
                    component.add(downstream_id)
                changed = changed or len(component) != before
    return component


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
        downstream_mini = mini_by_segment[downstream_id]
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
