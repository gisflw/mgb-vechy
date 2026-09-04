"""Fixtures used only by shared-execution tests."""

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from mgb_vec_hydro.preparation import PreparationSpec, prepare_dataset
from mgb_vec_hydro.roi import RoiSpec, define_roi_dataset
from mgb_vec_hydro.aggregation import AggregationSpec, aggregate_roi_dataset


@pytest.fixture
def prepared_execution_dataset(tmp_path):
    catchments = gpd.GeoDataFrame(
        {"id": pd.Series([1, 2, 3], dtype="int64")},
        geometry=[box(0, 0, 10, 20), box(10, 0, 20, 20), box(20, 0, 30, 20)],
        crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {
            "id": pd.Series([1, 2, 3], dtype="int64"),
            "id_down": pd.Series([2, 3, None], dtype="Int64"),
            "strahler_order": pd.Series([1, 2, 1], dtype="int16"),
            "up": [3.0, 2.0, 1.0],
            "length": [1.0, 1.0, 1.0],
        },
        geometry=[
            LineString([(0, 15), (10, 15)]),
            LineString([(10, 15), (20, 15)]),
            LineString([(20, 15), (30, 15)]),
        ],
        crs="EPSG:3857",
    )
    catchments["area"] = [1.0, 1.0, 1.0]
    catchments_path = tmp_path / "catchments.fgb"
    segments_path = tmp_path / "segments.fgb"
    dem_path = tmp_path / "dem.tif"
    catchments.to_file(catchments_path, driver="FlatGeobuf")
    segments.to_file(segments_path, driver="FlatGeobuf")
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        width=3,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=from_origin(0, 20, 10, 10),
    ) as target:
        target.write(np.arange(6, dtype="float32").reshape(2, 3), 1)
    define_roi_dataset(RoiSpec(crs="EPSG:3857", catchments=catchments_path, segments=segments_path, outlet_ids=("3",), id_col="id", id_down_col="id_down", strahler_order_col="strahler_order", output_dir=tmp_path / "roi", workers=1))
    aggregate_roi_dataset(AggregationSpec(roi=tmp_path / "roi", uparea_min=0, lmin=0, output_dir=tmp_path / "minis", workers=1))
    report = prepare_dataset(PreparationSpec(dem=dem_path, minis=tmp_path / "minis", output_dir=tmp_path / "prepared", memory_limit_mb=16, buffer_cells=0))
    return report.output_dir
