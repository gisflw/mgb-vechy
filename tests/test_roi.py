import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import MissingColumnsError
from mgb_vec_hydro.roi import define_roi


def _segments_gdf():
    return gpd.GeoDataFrame(
        {
            "id": [1, 2, 3, 4, 5, 6],
            "id_down": [None, 1, 2, 1, 4, None],
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
            "id": [1, 2, 3, 4, 5, 6],
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

    assert set(roi.segments["id"]) == {2, 3, 4, 5}
    assert set(roi.catchments["id"]) == {2, 3, 4, 5}


def test_define_roi_outputs_normalized_columns_only():
    roi = define_roi(_catchments_gdf(), _segments_gdf(), outlet_ids=[2, 4])

    assert list(roi.segments.columns) == ["id", "id_down", "sub", "geometry"]
    assert list(roi.catchments.columns) == ["id", "id_down", "sub", "geometry"]
    assert dict(zip(roi.catchments["id"], roi.catchments["id_down"])) == {
        2: 1,
        3: 2,
        4: 1,
        5: 4,
    }


def test_define_roi_assigns_sub_from_downstream_to_upstream_order():
    roi = define_roi(_catchments_gdf(), _segments_gdf(), outlet_ids=[1, 2])

    segment_sub = dict(zip(roi.segments["id"], roi.segments["sub"]))
    catchment_sub = dict(zip(roi.catchments["id"], roi.catchments["sub"]))

    assert segment_sub[1] == 2
    assert segment_sub[4] == 2
    assert segment_sub[5] == 2
    assert segment_sub[2] == 1
    assert segment_sub[3] == 1
    assert catchment_sub[1] == 2
    assert catchment_sub[4] == 2
    assert catchment_sub[5] == 2
    assert catchment_sub[2] == 1
    assert catchment_sub[3] == 1


def test_define_roi_supports_custom_input_column_names_with_normalized_output():
    segments = gpd.GeoDataFrame(
        {
            "cotrecho": [10, 20, 30],
            "nutrjus": [None, 10, 20],
            "legacy_attr": ["x", "y", "z"],
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
            "cotrecho": [10, 20, 30],
            "legacy_area": [1.0, 2.0, 3.0],
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
        id_col="cotrecho",
        id_down_col="nutrjus",
    )

    assert list(roi.segments.columns) == ["id", "id_down", "sub", "geometry"]
    assert list(roi.catchments.columns) == ["id", "id_down", "sub", "geometry"]
    assert list(roi.segments["id"]) == [20, 30]
    assert list(roi.catchments["id"]) == [20, 30]
    assert list(roi.segments["id_down"]) == [10, 20]
    assert list(roi.catchments["id_down"]) == [10, 20]
    assert list(roi.segments["sub"]) == [1, 1]
    assert list(roi.catchments["sub"]) == [1, 1]


def test_define_roi_requires_shared_id_column_on_catchments():
    catchments = _catchments_gdf().rename(columns={"id": "area_id"})

    with pytest.raises(MissingColumnsError, match="id"):
        define_roi(catchments, _segments_gdf(), outlet_ids=[2])
