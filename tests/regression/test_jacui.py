"""Opt-in scientific regression against the two captured Jacui datasets."""

import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyogrio
import pytest
import rasterio
import shapely

FIXTURE = Path(__file__).parent / "jacui"
spec = importlib.util.spec_from_file_location("jacui_benchmark", FIXTURE / "benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_JACUI_REGRESSION") != "1",
    reason="set RUN_JACUI_REGRESSION=1 to run the local Jacui fixtures",
)


@pytest.mark.parametrize("network", ("bhae", "tdxhydro"))
def test_jacui_scientific_products(network, tmp_path):
    expected = FIXTURE / "expected" / network
    # Explicitly requested regressions fail if fixtures are missing.
    assert (FIXTURE / "input" / "dem.tif").exists(), "Capture Jacui fixtures first"
    output = tmp_path / network
    stage = os.environ.get("JACUI_STAGE", "all")
    benchmark.run(network, output, stage=stage,
                  command=os.environ.get("MGB_REGRESSION_COMMAND"))
    products = list(output.glob("*.fgb")) + list(output.glob("*.tif")) + list(output.glob("*.csv"))
    stage_products = {
        "define-roi": {"roi_catchments.fgb", "roi_segments.fgb"},
        "aggregate": {"mini_catchments.fgb", "mini_segments.fgb", "source_to_mini.csv"},
        "prepare": {"dem.tif", "hru.tif", "grid_catchments.tif", "grid_segments.tif"},
        "terrain-products": {"hand.tif", "ltnd.tif", "undrained_cells.csv"},
        "sample-minis": {"sampled_minis.csv"},
    }
    required = set().union(*stage_products.values()) if stage == "all" else stage_products[stage]
    if stage in {"all", "sample-minis"}:
        required.update(path.name for path in expected.glob("nodata_*.csv"))
    assert {path.name for path in products} == required
    if stage in {"all", "define-roi"}:
        fields = json.loads((FIXTURE / "config.json").read_text())["networks"][network]
        _, table = pyogrio.read_arrow(
            output / "roi_segments.fgb", columns=["id", "sub"], read_geometry=False
        )
        subs = dict(zip(table["id"].to_pylist(), table["sub"].to_pylist()))
        outlets = [int(value) for value in fields["outlet_ids"]]
        assert len(outlets) == 3
        assert [subs[outlet] for outlet in outlets] == [3, 2, 1]
    for actual in products:
        reference = expected / actual.name
        assert reference.exists(), f"Unexpected product: {actual.name}"
        if actual.suffix == ".fgb":
            _compare_vector(actual, reference)
        elif actual.suffix == ".tif":
            _compare_raster(actual, reference)
        else:
            left, right = pd.read_csv(actual), pd.read_csv(reference)
            # Sampling has no promised ascending-ID row order.
            if actual.name == "sampled_minis.csv":
                left = left.sort_values("id").reset_index(drop=True)
                right = right.sort_values("id").reset_index(drop=True)
            pd.testing.assert_frame_equal(left, right, check_exact=False, rtol=1e-10, atol=1e-10)


def _compare_vector(actual, reference):
    metadata, left = pyogrio.read_arrow(actual)
    expected_metadata, right = pyogrio.read_arrow(reference)
    assert metadata["crs"] == expected_metadata["crs"]
    assert left.column_names == right.column_names
    assert left.schema.types == right.schema.types
    geometry = metadata["geometry_name"] or "wkb_geometry"
    left, right = left.to_pandas(), right.to_pandas()
    if actual.name.startswith("roi_"):
        left = left.sort_values("id").reset_index(drop=True)
        right = right.sort_values("id").reset_index(drop=True)
    np.testing.assert_array_equal(shapely.equals(shapely.from_wkb(left[geometry].to_numpy()),
                                                shapely.from_wkb(right[geometry].to_numpy())), True)
    pd.testing.assert_frame_equal(left.drop(columns=geometry), right.drop(columns=geometry),
                                  check_exact=False, rtol=1e-10, atol=1e-10)


def _compare_raster(actual, reference):
    with rasterio.open(actual) as left, rasterio.open(reference) as right:
        assert (left.crs, left.shape, left.count, left.dtypes) == (
            right.crs, right.shape, right.count, right.dtypes)
        # Cropping source rasters introduces roundoff in affine translations.
        np.testing.assert_allclose(left.transform, right.transform, rtol=0, atol=1e-12)
        assert left.nodata == right.nodata
        assert left.mask_flag_enums == right.mask_flag_enums
        assert left.units == right.units
        actual_tags, expected_tags = left.tags(), right.tags()
        if "mini_index" in expected_tags:
            actual_index = np.asarray(json.loads(actual_tags.pop("mini_index")))
            expected_index = np.asarray(json.loads(expected_tags.pop("mini_index")))
            np.testing.assert_array_equal(actual_index[:, 0], expected_index[:, 0])
            np.testing.assert_allclose(actual_index[:, 1:], expected_index[:, 1:], rtol=0, atol=1e-12)
        assert actual_tags == expected_tags
        assert left.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        for _, window in right.block_windows():
            a, b = left.read(1, window=window, masked=True), right.read(1, window=window, masked=True)
            np.testing.assert_array_equal(np.ma.getmaskarray(a), np.ma.getmaskarray(b))
            valid = ~np.ma.getmaskarray(b)
            if np.issubdtype(b.dtype, np.integer):
                np.testing.assert_array_equal(a.data[valid], b.data[valid])
            else:
                np.testing.assert_allclose(a.data[valid], b.data[valid], rtol=1e-6, atol=1e-6)
