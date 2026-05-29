from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Hashable

import geopandas as gpd
import pandas as pd

from mgb_vec_hydro.topology import (
    DEFAULT_CATCH_ID_COL,
    DEFAULT_SEG_ID_COL,
    DEFAULT_SEG_ID_DOWN_COL,
    find_upstream_selection,
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
    seg_id_col: str = DEFAULT_SEG_ID_COL,
    seg_id_down_col: str = DEFAULT_SEG_ID_DOWN_COL,
    catch_id_col: str = DEFAULT_CATCH_ID_COL,
) -> RoiResult:
    """Select ROI catchments and segments upstream of ordered outlets."""

    outlet_list = list(outlet_ids)
    selected_segments: set[Hashable] = set()
    selected_catchments: set[Hashable] = set()
    segment_sub = pd.Series(0, index=segments.index, dtype="int64")
    catchment_sub = pd.Series(0, index=catchments.index, dtype="int64")

    outlet_count = len(outlet_list)
    for outlet_index, outlet_id in enumerate(outlet_list):
        selection = find_upstream_selection(
            segments,
            [outlet_id],
            seg_id_col=seg_id_col,
            seg_id_down_col=seg_id_down_col,
            catch_id_col=catch_id_col,
        )
        sub_value = outlet_count - outlet_index
        selected_segments.update(selection.segment_ids)
        selected_catchments.update(selection.catchment_ids)

        segment_mask = segments[seg_id_col].isin(selection.segment_ids)
        catchment_mask = catchments[catch_id_col].isin(selection.catchment_ids)
        segment_sub.loc[segment_mask] = sub_value
        catchment_sub.loc[catchment_mask] = sub_value

    roi_segments = segments.loc[segments[seg_id_col].isin(selected_segments)].copy()
    roi_catchments = catchments.loc[
        catchments[catch_id_col].isin(selected_catchments)
    ].copy()

    roi_segments.insert(0, "sub", segment_sub.loc[roi_segments.index].to_numpy())
    roi_catchments.insert(
        0,
        "sub",
        catchment_sub.loc[roi_catchments.index].to_numpy(),
    )

    roi_segments = roi_segments.reset_index(drop=True)
    roi_catchments = roi_catchments.reset_index(drop=True)

    return _apply_legacy_bho_column_order(
        roi_catchments,
        roi_segments,
        seg_id_col=seg_id_col,
        seg_id_down_col=seg_id_down_col,
        catch_id_col=catch_id_col,
    )


BHO_SEGMENT_COLUMNS = [
    "sub",
    "cotrecho",
    "cobacia",
    "nucomptrec",
    "nuareacont",
    "nuareamont",
    "nutrjus",
    "cocursodag",
    "nustrahler",
    "centroid_x",
    "centroid_y",
    "geometry",
]

BHO_CATCHMENT_COLUMNS = [
    "sub",
    "cotrecho",
    "cobacia",
    "nuareacont",
    "cocursodag",
    "centroid_x",
    "centroid_y",
    "geometry",
]


def _apply_legacy_bho_column_order(
    catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    *,
    seg_id_col: str,
    seg_id_down_col: str,
    catch_id_col: str,
) -> RoiResult:
    if (seg_id_col, seg_id_down_col, catch_id_col) != (
        "cotrecho",
        "nutrjus",
        "cobacia",
    ):
        return RoiResult(catchments=catchments, segments=segments)

    segment_columns = [
        column for column in BHO_SEGMENT_COLUMNS if column in segments.columns
    ]
    catchment_columns = [
        column for column in BHO_CATCHMENT_COLUMNS if column in catchments.columns
    ]

    remaining_segment_columns = [
        column for column in segments.columns if column not in segment_columns
    ]
    remaining_catchment_columns = [
        column for column in catchments.columns if column not in catchment_columns
    ]

    return RoiResult(
        catchments=catchments[catchment_columns + remaining_catchment_columns],
        segments=segments[segment_columns + remaining_segment_columns],
    )
