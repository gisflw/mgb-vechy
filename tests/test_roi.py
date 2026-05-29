import geopandas as gpd
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.roi import define_roi


def _segments_gdf():
    return gpd.GeoDataFrame(
        {
            "seg_id": [1, 2, 3, 4, 5, 6],
            "seg_id_down": [None, 1, 2, 1, 4, None],
            "catch_id": [10, 20, 30, 40, 50, 60],
            "length": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "geometry": [
                LineString([(0, 0), (1, 0)]),
                LineString([(1, 0), (2, 0)]),
                LineString([(2, 0), (3, 0)]),
                LineString([(1, 1), (2, 1)]),
                LineString([(2, 1), (3, 1)]),
                LineString([(0, 2), (1, 2)]),
            ],
        },
        crs="EPSG:4326",
    )


def _catchments_gdf():
    return gpd.GeoDataFrame(
        {
            "catch_id": [10, 20, 30, 40, 50, 60],
            "area": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
                Polygon([(2, 0), (3, 0), (3, 1), (2, 1)]),
                Polygon([(1, 1), (2, 1), (2, 2), (1, 2)]),
                Polygon([(2, 1), (3, 1), (3, 2), (2, 2)]),
                Polygon([(0, 2), (1, 2), (1, 3), (0, 3)]),
            ],
        },
        crs="EPSG:4326",
    )


def test_define_roi_returns_union_of_upstream_segments_and_catchments():
    roi = define_roi(_catchments_gdf(), _segments_gdf(), outlet_ids=[2, 4])

    assert set(roi.segments["seg_id"]) == {2, 3, 4, 5}
    assert set(roi.catchments["catch_id"]) == {20, 30, 40, 50}


def test_define_roi_assigns_sub_from_downstream_to_upstream_order():
    roi = define_roi(_catchments_gdf(), _segments_gdf(), outlet_ids=[1, 2])

    segment_sub = dict(zip(roi.segments["seg_id"], roi.segments["sub"]))
    catchment_sub = dict(zip(roi.catchments["catch_id"], roi.catchments["sub"]))

    assert segment_sub[1] == 2
    assert segment_sub[4] == 2
    assert segment_sub[5] == 2
    assert segment_sub[2] == 1
    assert segment_sub[3] == 1
    assert catchment_sub[10] == 2
    assert catchment_sub[40] == 2
    assert catchment_sub[50] == 2
    assert catchment_sub[20] == 1
    assert catchment_sub[30] == 1


def test_define_roi_supports_bho_column_mapping():
    segments = gpd.GeoDataFrame(
        {
            "cotrecho": [10, 20, 30],
            "nutrjus": [None, 10, 20],
            "cobacia": [100, 200, 300],
            "geometry": [
                LineString([(0, 0), (1, 0)]),
                LineString([(1, 0), (2, 0)]),
                LineString([(2, 0), (3, 0)]),
            ],
        },
        crs="EPSG:4326",
    )
    catchments = gpd.GeoDataFrame(
        {
            "cobacia": [100, 200, 300],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
                Polygon([(2, 0), (3, 0), (3, 1), (2, 1)]),
            ],
        },
        crs="EPSG:4326",
    )

    roi = define_roi(
        catchments,
        segments,
        outlet_ids=[20],
        seg_id_col="cotrecho",
        seg_id_down_col="nutrjus",
        catch_id_col="cobacia",
    )

    assert list(roi.segments["cotrecho"]) == [20, 30]
    assert list(roi.catchments["cobacia"]) == [200, 300]
    assert list(roi.segments["sub"]) == [1, 1]
    assert list(roi.catchments["sub"]) == [1, 1]
