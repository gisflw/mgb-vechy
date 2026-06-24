from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Hashable

import geopandas as gpd
import pandas as pd
from mgb_vec_hydro.exceptions import MissingCrsError, TopologyCycleError
from mgb_vec_hydro.topology import (
    DEFAULT_ID_COL,
    DEFAULT_ID_DOWN_COL,
    find_upstream_selection,
    resolve_column_name,
)


DEFAULT_STRAHLER_ORDER_COL = "strahler_order"


@dataclass(frozen=True)
class RoiResult:
    """ROI catchments and segments produced by Stage 1."""

    catchments: gpd.GeoDataFrame
    segments: gpd.GeoDataFrame


def define_roi(
    catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    *,
    outlet_ids: Iterable[Hashable],
    destine_crs: str,
    source_crs: str | None = None,
    id_col: str = DEFAULT_ID_COL,
    id_down_col: str = DEFAULT_ID_DOWN_COL,
    strahler_order_col: str = DEFAULT_STRAHLER_ORDER_COL,
) -> RoiResult:
    """Select ROI catchments and segments upstream of ordered outlets."""

    segment_id_col = resolve_column_name(segments, id_col)
    segment_id_down_col = resolve_column_name(segments, id_down_col)
    segment_strahler_order_col = resolve_column_name(segments, strahler_order_col)
    catchment_id_col = resolve_column_name(catchments, id_col)

    outlet_list = list(outlet_ids)
    selected_ids: set[Hashable] = set()
    segment_sub = pd.Series(0, index=segments.index, dtype="int64")
    catchment_sub = pd.Series(0, index=catchments.index, dtype="int64")

    outlet_count = len(outlet_list)
    for outlet_index, outlet_id in enumerate(outlet_list):
        selection = find_upstream_selection(
            segments,
            [outlet_id],
            id_col=segment_id_col,
            id_down_col=segment_id_down_col,
        )
        sub_value = outlet_count - outlet_index
        selected_ids.update(selection.ids)

        segment_mask = segments[segment_id_col].isin(selection.ids)
        catchment_mask = catchments[catchment_id_col].isin(selection.ids)
        segment_sub.loc[segment_mask] = sub_value
        catchment_sub.loc[catchment_mask] = sub_value

    roi_segments = segments.loc[segments[segment_id_col].isin(selected_ids)].copy()
    roi_catchments = catchments.loc[
        catchments[catchment_id_col].isin(selected_ids)
    ].copy()
    projected_roi_segments = _project_for_metrics(
        roi_segments,
        source_crs=source_crs,
        destine_crs=destine_crs,
        layer_name="segments",
    )
    projected_roi_catchments = _project_for_metrics(
        roi_catchments,
        source_crs=source_crs,
        destine_crs=destine_crs,
        layer_name="catchments",
    )
    id_down_by_id = segments.set_index(segment_id_col)[segment_id_down_col]
    strahler_order_by_id = segments.set_index(segment_id_col)[segment_strahler_order_col]
    metric_columns = _metric_columns(
        segments,
        roi_segments,
        roi_catchments,
        projected_roi_segments,
        projected_roi_catchments,
        selected_ids=selected_ids,
        segment_id_col=segment_id_col,
        segment_id_down_col=segment_id_down_col,
        catchment_id_col=catchment_id_col,
    )

    normalized_segments = gpd.GeoDataFrame(
        {
            "id": roi_segments[segment_id_col].to_numpy(),
            "id_down": roi_segments[segment_id_down_col].to_numpy(),
            "sub": segment_sub.loc[roi_segments.index].to_numpy(),
            "strahler_order": roi_segments[segment_strahler_order_col].to_numpy(),
            "unit_length": metric_columns["segment_unit_length"].to_numpy(),
            "upstream_length": metric_columns["segment_upstream_length"].to_numpy(),
            "unit_area": metric_columns["segment_unit_area"].to_numpy(),
            "upstream_area": metric_columns["segment_upstream_area"].to_numpy(),
        },
        geometry=projected_roi_segments.geometry.to_numpy(),
        crs=projected_roi_segments.crs,
    )

    normalized_catchments = gpd.GeoDataFrame(
        {
            "id": roi_catchments[catchment_id_col].to_numpy(),
            "id_down": roi_catchments[catchment_id_col].map(id_down_by_id).to_numpy(),
            "sub": catchment_sub.loc[roi_catchments.index].to_numpy(),
            "strahler_order": roi_catchments[catchment_id_col]
            .map(strahler_order_by_id)
            .to_numpy(),
            "unit_length": metric_columns["catchment_unit_length"].to_numpy(),
            "upstream_length": metric_columns["catchment_upstream_length"].to_numpy(),
            "unit_area": metric_columns["catchment_unit_area"].to_numpy(),
            "upstream_area": metric_columns["catchment_upstream_area"].to_numpy(),
        },
        geometry=projected_roi_catchments.geometry.to_numpy(),
        crs=projected_roi_catchments.crs,
    )

    return RoiResult(
        catchments=normalized_catchments.reset_index(drop=True),
        segments=normalized_segments.reset_index(drop=True),
    )


def _project_for_metrics(
    gdf: gpd.GeoDataFrame,
    *,
    source_crs: str | None,
    destine_crs: str,
    layer_name: str,
) -> gpd.GeoDataFrame:
    if source_crs is not None:
        gdf = gdf.set_crs(source_crs, allow_override=True)
    elif gdf.crs is None:
        raise MissingCrsError(
            f"{layer_name} layer has no CRS; supply --source-crs before computing metrics"
        )

    return gdf.to_crs(destine_crs)


def _metric_columns(
    segments: gpd.GeoDataFrame,
    roi_segments: gpd.GeoDataFrame,
    roi_catchments: gpd.GeoDataFrame,
    projected_roi_segments: gpd.GeoDataFrame,
    projected_roi_catchments: gpd.GeoDataFrame,
    *,
    selected_ids: set[Hashable],
    segment_id_col: str,
    segment_id_down_col: str,
    catchment_id_col: str,
) -> dict[str, pd.Series]:
    segment_unit_length_by_id = pd.Series(
        projected_roi_segments.geometry.length.to_numpy() / 1000,
        index=roi_segments[segment_id_col],
        dtype="float64",
    )
    unit_area_by_id = pd.Series(
        projected_roi_catchments.geometry.area.to_numpy() / 1_000_000,
        index=roi_catchments[catchment_id_col],
        dtype="float64",
    )

    upstream_length_by_id, upstream_area_by_id = _accumulate_upstream_metrics(
        roi_segments,
        selected_ids=selected_ids,
        unit_length_by_id=segment_unit_length_by_id,
        unit_area_by_id=unit_area_by_id,
        segment_id_col=segment_id_col,
        segment_id_down_col=segment_id_down_col,
    )

    segment_ids = roi_segments[segment_id_col]
    catchment_ids = roi_catchments[catchment_id_col]
    upstream_length = pd.Series(upstream_length_by_id, dtype="float64")
    upstream_area = pd.Series(upstream_area_by_id, dtype="float64")

    return {
        "segment_unit_length": segment_ids.map(segment_unit_length_by_id),
        "segment_upstream_length": segment_ids.map(upstream_length),
        "segment_unit_area": segment_ids.map(unit_area_by_id),
        "segment_upstream_area": segment_ids.map(upstream_area),
        "catchment_unit_length": catchment_ids.map(segment_unit_length_by_id),
        "catchment_upstream_length": catchment_ids.map(upstream_length),
        "catchment_unit_area": catchment_ids.map(unit_area_by_id),
        "catchment_upstream_area": catchment_ids.map(upstream_area),
    }


def _accumulate_upstream_metrics(
    roi_segments: gpd.GeoDataFrame,
    *,
    selected_ids: set[Hashable],
    unit_length_by_id: pd.Series,
    unit_area_by_id: pd.Series,
    segment_id_col: str,
    segment_id_down_col: str,
) -> tuple[pd.Series, pd.Series]:
    downstream_by_id = dict(
        roi_segments[[segment_id_col, segment_id_down_col]].itertuples(
            index=False,
            name=None,
        )
    )
    upstream_count_by_id = dict.fromkeys(selected_ids, 0)
    for downstream_id in downstream_by_id.values():
        if downstream_id in selected_ids:
            upstream_count_by_id[downstream_id] += 1

    upstream_length_by_id = unit_length_by_id.to_dict()
    upstream_area_by_id = unit_area_by_id.to_dict()
    ready = [
        segment_id
        for segment_id, upstream_count in upstream_count_by_id.items()
        if upstream_count == 0
    ]
    processed_count = 0

    while ready:
        segment_id = ready.pop()
        processed_count += 1
        downstream_id = downstream_by_id.get(segment_id)
        if downstream_id not in selected_ids:
            continue

        upstream_length_by_id[downstream_id] += upstream_length_by_id[segment_id]
        upstream_area_by_id[downstream_id] += upstream_area_by_id[segment_id]
        upstream_count_by_id[downstream_id] -= 1
        if upstream_count_by_id[downstream_id] == 0:
            ready.append(downstream_id)

    if processed_count != len(selected_ids):
        raise TopologyCycleError("Detected topology cycle while computing upstream metrics")

    return (
        pd.Series(upstream_length_by_id, dtype="float64"),
        pd.Series(upstream_area_by_id, dtype="float64"),
    )
