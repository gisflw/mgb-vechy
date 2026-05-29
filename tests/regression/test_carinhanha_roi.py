from pathlib import Path

import geopandas as gpd
import pytest

from mgb_vec_hydro.roi import define_roi


pytest.importorskip("geopandas")

ROOT = Path(__file__).resolve().parents[2]
CARINHANHA = ROOT / "tests" / "carinhanha"
SEGMENTS_INPUT = ROOT / "data" / "geoft_bhae_trecho_drenagem.gpkg"
CATCHMENTS_INPUT = ROOT / "data" / "geoft_bhae_area_drenagem.gpkg"
EXPECTED_SEGMENTS = CARINHANHA / "output" / "roi_trecs.shp"
EXPECTED_CATCHMENTS = CARINHANHA / "output" / "roi_areas.shp"
OUTLET_SEGMENT_IDS = [90497, 416, 159713]


@pytest.mark.skipif(
    not SEGMENTS_INPUT.exists() or not CATCHMENTS_INPUT.exists(),
    reason="reference BHO input vectors are not available",
)
def test_carinhanha_roi_matches_reference_properties():
    segments = gpd.read_file(SEGMENTS_INPUT)
    catchments = gpd.read_file(CATCHMENTS_INPUT)
    expected_segments = gpd.read_file(EXPECTED_SEGMENTS)
    expected_catchments = gpd.read_file(EXPECTED_CATCHMENTS)

    roi = define_roi(
        catchments,
        segments,
        outlet_ids=OUTLET_SEGMENT_IDS,
        seg_id_col="cotrecho",
        seg_id_down_col="nutrjus",
        catch_id_col="cobacia",
    )

    assert len(roi.segments) == len(expected_segments)
    assert len(roi.catchments) == len(expected_catchments)
    assert set(roi.segments["cotrecho"]) == set(expected_segments["cotrecho"])
    assert set(roi.catchments["cobacia"].astype(str)) == set(
        expected_catchments["cobacia"].astype(str)
    )
    assert set(roi.segments["nutrjus"]) == set(expected_segments["nutrjus"])
    assert roi.segments.crs == segments.crs
    assert roi.catchments.crs == catchments.crs
    assert dict(zip(roi.segments["cotrecho"], roi.segments["sub"])) == dict(
        zip(expected_segments["cotrecho"], expected_segments["sub"])
    )
    assert dict(zip(roi.catchments["cobacia"].astype(str), roi.catchments["sub"])) == dict(
        zip(expected_catchments["cobacia"].astype(str), expected_catchments["sub"])
    )
