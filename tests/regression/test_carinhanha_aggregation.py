from pathlib import Path

import geopandas as gpd
import pytest

from mgb_vec_hydro.aggregation import INPUT_COLUMNS, aggregate_minibasins


pytest.importorskip("geopandas")

ROOT = Path(__file__).resolve().parents[2]
CARINHANHA = ROOT / "tests" / "carinhanha"
LEGACY_ROI_SEGMENTS = CARINHANHA / "output" / "roi_trecs.shp"
LEGACY_ROI_CATCHMENTS = CARINHANHA / "output" / "roi_areas.shp"


def test_carinhanha_aggregation_regression_properties():
    legacy_segments = gpd.read_file(LEGACY_ROI_SEGMENTS)
    legacy_catchments = gpd.read_file(LEGACY_ROI_CATCHMENTS)
    roi_segments, roi_catchments = _legacy_roi_to_input(
        legacy_segments,
        legacy_catchments,
    )

    result = aggregate_minibasins(
        roi_catchments,
        roi_segments,
        uparea_min=30,
        lmin=6,
    )

    assert len(result.segments) == 115
    assert len(result.catchments) == 115
    assert len(result.mapping) == len(roi_catchments)
    assert list(result.segments.columns) == INPUT_COLUMNS
    assert list(result.catchments.columns) == INPUT_COLUMNS
    assert list(result.mapping.columns) == ["id", "mini_id", "sub", "geometry"]
    assert result.catchments["unit_area"].sum() == pytest.approx(
        roi_catchments["unit_area"].sum()
    )
    assert result.segments["id"].is_unique
    assert result.catchments["id"].is_unique
    assert result.mapping["id"].is_unique
    assert list(result.segments["id"].head(5)) == [
        100864,
        116794,
        126455,
        127420,
        128532,
    ]


def _legacy_roi_to_input(
    legacy_segments: gpd.GeoDataFrame,
    legacy_catchments: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    segment_ids = set(legacy_segments["cotrecho"])
    id_down = legacy_segments["nutrjus"].where(
        legacy_segments["nutrjus"].isin(segment_ids),
        None,
    )
    common = {
        "id": legacy_segments["cotrecho"].to_numpy(),
        "id_down": id_down.to_numpy(),
        "sub": legacy_segments["sub"].to_numpy(),
        "strahler_order": legacy_segments["nustrahler"].to_numpy(),
        "unit_length": legacy_segments["nucomptrec"].to_numpy(),
        "upstream_length": legacy_segments["nucomptrec"].to_numpy(),
        "unit_area": legacy_segments["nuareacont"].to_numpy(),
        "upstream_area": legacy_segments["nuareamont"].to_numpy(),
    }
    catchment_geometry = (
        legacy_catchments.set_index("cotrecho")
        .loc[legacy_segments["cotrecho"], "geometry"]
        .to_numpy()
    )

    roi_segments = gpd.GeoDataFrame(
        {**common, "geometry": legacy_segments.geometry.to_numpy()},
        crs=legacy_segments.crs,
    )
    roi_catchments = gpd.GeoDataFrame(
        {**common, "geometry": catchment_geometry},
        crs=legacy_catchments.crs,
    )
    return roi_segments[INPUT_COLUMNS], roi_catchments[INPUT_COLUMNS]
