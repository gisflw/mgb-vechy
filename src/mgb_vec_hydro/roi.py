from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Hashable

import geopandas as gpd
import pandas as pd
from mgb_vec_hydro.topology import (
    DEFAULT_ID_COL,
    DEFAULT_ID_DOWN_COL,
    find_upstream_selection,
    resolve_column_name,
)


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
    id_col: str = DEFAULT_ID_COL,
    id_down_col: str = DEFAULT_ID_DOWN_COL,
) -> RoiResult:
    """Select ROI catchments and segments upstream of ordered outlets."""

    segment_id_col = resolve_column_name(segments, id_col)
    segment_id_down_col = resolve_column_name(segments, id_down_col)
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
    id_down_by_id = segments.set_index(segment_id_col)[segment_id_down_col]

    normalized_segments = gpd.GeoDataFrame(
        {
            "id": roi_segments[segment_id_col].to_numpy(),
            "id_down": roi_segments[segment_id_down_col].to_numpy(),
            "sub": segment_sub.loc[roi_segments.index].to_numpy(),
        },
        geometry=roi_segments.geometry.to_numpy(),
        crs=roi_segments.crs,
    )

    normalized_catchments = gpd.GeoDataFrame(
        {
            "id": roi_catchments[catchment_id_col].to_numpy(),
            "id_down": roi_catchments[catchment_id_col].map(id_down_by_id).to_numpy(),
            "sub": catchment_sub.loc[roi_catchments.index].to_numpy(),
        },
        geometry=roi_catchments.geometry.to_numpy(),
        crs=roi_catchments.crs,
    )

    return RoiResult(
        catchments=normalized_catchments.reset_index(drop=True),
        segments=normalized_segments.reset_index(drop=True),
    )
