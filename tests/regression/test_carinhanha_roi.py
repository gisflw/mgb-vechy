from pathlib import Path

import pytest

from mgb_vec_hydro.execution.vector import read_vector_table
from mgb_vec_hydro.roi import RoiSpec, define_roi_dataset

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
def test_carinhanha_roi_matches_reference_properties(tmp_path):
    expected_segments = read_vector_table(EXPECTED_SEGMENTS).to_pandas()
    expected_catchments = read_vector_table(EXPECTED_CATCHMENTS).to_pandas()
    report = define_roi_dataset(
        RoiSpec(
            catchments=CATCHMENTS_INPUT,
            segments=SEGMENTS_INPUT,
            outlet_ids=tuple(map(str, OUTLET_SEGMENT_IDS)),
            crs="ESRI:102033",
            id_col="cotrecho",
            id_down_col="nutrjus",
            strahler_order_col="nustrahler",
            output_dir=tmp_path / "roi",
        )
    )
    dataset_segments = read_vector_table(
        report.output_dir / "vectors" / "roi_segments.fgb"
    )
    dataset_catchments = read_vector_table(
        report.output_dir / "vectors" / "roi_catchments.fgb"
    )
    segments = dataset_segments.to_pandas()
    catchments = dataset_catchments.to_pandas()

    assert len(segments) == len(expected_segments)
    assert len(catchments) == len(expected_catchments)
    assert set(segments["id"]) == set(expected_segments["cotrecho"])
    assert set(catchments["id"]) == set(expected_catchments["cotrecho"])
    assert set(segments["id_down"]) == set(expected_segments["nutrjus"])
    assert dataset_segments.crs.to_authority() == ("ESRI", "102033")
    assert dataset_catchments.crs.to_authority() == ("ESRI", "102033")
    assert dict(zip(segments["id"], segments["sub"])) == dict(
        zip(expected_segments["cotrecho"], expected_segments["sub"])
    )
    assert dict(zip(catchments["id"], catchments["sub"])) == dict(
        zip(expected_catchments["cotrecho"], expected_catchments["sub"])
    )
