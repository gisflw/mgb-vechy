import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import InvalidInputSchemaError, TopologyCycleError
from mgb_vec_hydro.preparation import PreparationSpec, prepare_dataset
from mgb_vec_hydro.roi import ROI_COLUMNS, RoiDataset, RoiSpec, define_roi_dataset


def _inputs(tmp_path, *, orders=(3, 2, 1), areas=(6.0, 5.0, 3.0), downstream=(None, 1, 2)):
    dem = tmp_path / "dem.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", width=3, height=1, count=1, dtype="float32",
        crs="EPSG:3857", transform=from_origin(0, 1000, 1000, 1000),
    ) as target:
        target.write(np.ones((1, 3), dtype="float32"), 1)
    prepare_dataset(PreparationSpec(
        dem=dem, crs="EPSG:3857", resolution=1000, output_dir=tmp_path / "prepared"
    ))
    catchments = gpd.GeoDataFrame(
        {"SOURCE_ID": [1, 2, 3]},
        geometry=[
            Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]),
            Polygon([(1000, 0), (2000, 0), (2000, 1000), (1000, 1000)]),
            Polygon([(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)]),
        ], crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {"SOURCE_ID": [1, 2, 3], "DOWN": list(downstream), "ORDER": list(orders), "UPAREA": list(areas)},
        geometry=[
            LineString([(0, 500), (1000, 500)]),
            LineString([(1000, 500), (2000, 500)]),
            LineString([(2000, 500), (3000, 500)]),
        ], crs="EPSG:3857",
    )
    catchments.to_file(tmp_path / "catchments.gpkg", driver="GPKG")
    segments.to_file(tmp_path / "segments.gpkg", driver="GPKG")
    return RoiSpec(
        prepared=tmp_path / "prepared", catchments=tmp_path / "catchments.gpkg",
        segments=tmp_path / "segments.gpkg", outlet_ids=("1",), id_col="source_id",
        id_down_col="down", strahler_order_col="order", upstream_area_col="uparea",
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
    assert list(segments.upstream_area) == [6.0, 5.0, 3.0]
    assert list(segments.unit_length) == [1.0, 1.0, 1.0]
    assert list(segments.upstream_length) == [3.0, 2.0, 1.0]
    assert segments.crs == "EPSG:3857"


def test_roi_filters_strahler_before_selection(tmp_path):
    spec = _inputs(tmp_path, orders=(3, 0, 1))
    report = define_roi_dataset(spec)
    assert report.segment_count == 1


@pytest.mark.parametrize("areas", [(6.0, -1.0, 3.0), (6.0, float("nan"), 3.0)])
def test_roi_rejects_invalid_selected_provider_area(tmp_path, areas):
    with pytest.raises(InvalidInputSchemaError, match="upstream areas"):
        define_roi_dataset(_inputs(tmp_path, areas=areas))


def test_roi_rejects_selected_cycle_without_publishing(tmp_path):
    spec = _inputs(tmp_path, downstream=(2, 1, 2))
    with pytest.raises(TopologyCycleError):
        define_roi_dataset(spec)
    assert not spec.output_dir.exists()
