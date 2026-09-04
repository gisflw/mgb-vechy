import geopandas as gpd
import pandas as pd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import InvalidInputSchemaError, TopologyCycleError
from mgb_vec_hydro.roi import ROI_COLUMNS, RoiDataset, RoiSpec, define_roi_dataset


def _inputs(tmp_path, *, orders=(3, 2, 1), downstream=(None, 1, 2)):
    catchments = gpd.GeoDataFrame(
        {"SOURCE_ID": [1, 2, 3], "UNITAREA": [1.0, 1.0, 1.0]},
        geometry=[
            Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]),
            Polygon([(1000, 0), (2000, 0), (2000, 1000), (1000, 1000)]),
            Polygon([(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)]),
        ], crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {"SOURCE_ID": [1, 2, 3], "DOWN": list(downstream), "ORDER": list(orders)},
        geometry=[
            LineString([(0, 500), (1000, 500)]),
            LineString([(1000, 500), (2000, 500)]),
            LineString([(2000, 500), (3000, 500)]),
        ], crs="EPSG:3857",
    )
    catchments.to_file(tmp_path / "catchments.gpkg", driver="GPKG")
    segments.to_file(tmp_path / "segments.gpkg", driver="GPKG")
    return RoiSpec(
        crs="EPSG:3857", catchments=tmp_path / "catchments.gpkg",
        segments=tmp_path / "segments.gpkg", outlet_ids=("1",), id_col="source_id",
        id_down_col="down", strahler_order_col="order",
        output_dir=tmp_path / "roi", workers=1, batch_size=1,
    )


def test_roi_publishes_versioned_normalized_fgb_and_provider_area(tmp_path):
    report = define_roi_dataset(_inputs(tmp_path))
    assert report.segment_count == 3
    dataset = RoiDataset.open(report.output_dir)
    dataset.validate()
    segments = gpd.read_file(dataset.path("segments")).sort_values("id")
    catchments = gpd.read_file(dataset.path("catchments")).sort_values("id")
    assert list(segments.columns) == ROI_COLUMNS
    assert list(catchments.columns) == ROI_COLUMNS
    assert list(segments.upstream_area) == pytest.approx([3.0, 2.0, 1.0], rel=1e-2)
    assert list(segments.unit_length) == pytest.approx([1.0, 1.0, 1.0], rel=1e-5)
    assert list(segments.upstream_length) == pytest.approx([3.0, 2.0, 1.0], rel=1e-5)
    assert segments.crs == "EPSG:3857"


def test_roi_filters_strahler_before_selection(tmp_path):
    spec = _inputs(tmp_path, orders=(3, 0, 1))
    report = define_roi_dataset(spec)
    assert report.segment_count == 1


def test_roi_accepts_geographic_output_crs(tmp_path):
    spec = _inputs(tmp_path)
    spec = RoiSpec(
        **{**spec.__dict__, "crs": "EPSG:4326", "output_dir": tmp_path / "roi-geographic"}
    )
    report = define_roi_dataset(spec)
    dataset = RoiDataset.open(report.output_dir)
    segments = gpd.read_file(dataset.path("segments"))
    assert segments.crs.to_epsg() == 4326
    assert segments.unit_length.iloc[0] == pytest.approx(1.0, rel=1e-5)


def test_roi_ignores_unrelated_null_catchment_ids(tmp_path):
    spec = _inputs(tmp_path)
    catchments = gpd.read_file(spec.catchments)
    catchments = catchments.set_crs("EPSG:3857", allow_override=True)
    catchments.loc[len(catchments)] = [None, 1.0, Polygon([(10_000, 0), (11_000, 0), (11_000, 1_000), (10_000, 1_000)])]
    catchments.loc[len(catchments)] = [None, 1.0, Polygon([(12_000, 0), (13_000, 0), (13_000, 1_000), (12_000, 1_000)])]
    catchments["SOURCE_ID"] = pd.array(catchments["SOURCE_ID"], dtype="Int64")
    catchments.to_file(spec.catchments, driver="GPKG", layer="catchments", mode="w")
    spec = RoiSpec(**{**spec.__dict__, "catchments_source_crs": "EPSG:3857"})

    report = define_roi_dataset(spec)

    assert report.catchment_count == 3
    assert report.segment_count == 3


def test_roi_rejects_selected_cycle_without_publishing(tmp_path):
    spec = _inputs(tmp_path, downstream=(2, 1, 2))
    with pytest.raises(TopologyCycleError):
        define_roi_dataset(spec)
    assert not spec.output_dir.exists()
