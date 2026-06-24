import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import MissingColumnsError, MissingCrsError
from mgb_vec_hydro.roi import define_roi


EXPECTED_COLUMNS = [
    "id",
    "id_down",
    "sub",
    "strahler_order",
    "unit_length",
    "upstream_length",
    "unit_area",
    "upstream_area",
    "geometry",
]


def _segments_gdf():
    return gpd.GeoDataFrame(
        {
            "id": [1, 2, 3, 4, 5, 6],
            "id_down": [None, 1, 2, 1, 4, None],
            "length": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "strahler_order": [3, 2, 1, 2, 1, 1],
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
    roi = define_roi(
        _catchments_gdf(),
        _segments_gdf(),
        outlet_ids=[2, 4],
        destine_crs="EPSG:3857",
    )

    assert set(roi.segments["id"]) == {2, 3, 4, 5}
    assert set(roi.catchments["id"]) == {2, 3, 4, 5}


def test_define_roi_outputs_normalized_columns_with_strahler_order():
    roi = define_roi(
        _catchments_gdf(),
        _segments_gdf(),
        outlet_ids=[2, 4],
        destine_crs="EPSG:3857",
    )

    assert list(roi.segments.columns) == EXPECTED_COLUMNS
    assert list(roi.catchments.columns) == EXPECTED_COLUMNS
    assert dict(zip(roi.catchments["id"], roi.catchments["id_down"])) == {
        2: 1,
        3: 2,
        4: 1,
        5: 4,
    }
    assert dict(zip(roi.segments["id"], roi.segments["strahler_order"])) == {
        2: 2,
        3: 1,
        4: 2,
        5: 1,
    }
    assert dict(zip(roi.catchments["id"], roi.catchments["strahler_order"])) == {
        2: 2,
        3: 1,
        4: 2,
        5: 1,
    }


def test_define_roi_assigns_sub_from_downstream_to_upstream_order():
    roi = define_roi(
        _catchments_gdf(),
        _segments_gdf(),
        outlet_ids=[1, 2],
        destine_crs="EPSG:3857",
    )

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
            "stream_order": [3, 2, 1],
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
        destine_crs="EPSG:3857",
        id_col="cotrecho",
        id_down_col="nutrjus",
        strahler_order_col="stream_order",
    )

    assert list(roi.segments.columns) == EXPECTED_COLUMNS
    assert list(roi.catchments.columns) == EXPECTED_COLUMNS
    assert list(roi.segments["id"]) == [20, 30]
    assert list(roi.catchments["id"]) == [20, 30]
    assert list(roi.segments["id_down"]) == [10, 20]
    assert list(roi.catchments["id_down"]) == [10, 20]
    assert list(roi.segments["sub"]) == [1, 1]
    assert list(roi.catchments["sub"]) == [1, 1]
    assert list(roi.segments["strahler_order"]) == [2, 1]
    assert list(roi.catchments["strahler_order"]) == [2, 1]


def test_define_roi_resolves_input_columns_case_insensitively():
    segments = gpd.GeoDataFrame(
        {
            "LINKNO": [10, 20, 30],
            "DSLINKNO": [None, 10, 20],
            "STRAHLER_ORDER": [3, 2, 1],
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
            "LINKNO": [10, 20, 30],
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
        destine_crs="EPSG:3857",
        id_col="linkno",
        id_down_col="dslinkno",
    )

    assert list(roi.segments.columns) == EXPECTED_COLUMNS
    assert list(roi.catchments.columns) == EXPECTED_COLUMNS
    assert list(roi.segments["id"]) == [20, 30]
    assert list(roi.catchments["id"]) == [20, 30]
    assert list(roi.catchments["id_down"]) == [10, 20]
    assert list(roi.catchments["strahler_order"]) == [2, 1]


def test_define_roi_requires_shared_id_column_on_catchments():
    catchments = _catchments_gdf().rename(columns={"id": "area_id"})

    with pytest.raises(MissingColumnsError, match="id"):
        define_roi(catchments, _segments_gdf(), outlet_ids=[2], destine_crs="EPSG:3857")


def test_define_roi_requires_strahler_order_column_on_segments():
    segments = _segments_gdf().drop(columns=["strahler_order"])

    with pytest.raises(MissingColumnsError, match="strahler_order"):
        define_roi(_catchments_gdf(), segments, outlet_ids=[2], destine_crs="EPSG:3857")


def test_define_roi_computes_unit_and_upstream_metrics():
    segments = gpd.GeoDataFrame(
        {
            "id": [1, 2, 3],
            "id_down": [None, 1, 2],
            "strahler_order": [3, 2, 1],
            "geometry": [
                LineString([(0, 0), (1000, 0)]),
                LineString([(1000, 0), (3000, 0)]),
                LineString([(3000, 0), (6000, 0)]),
            ],
        },
        crs="EPSG:3857",
    )
    catchments = gpd.GeoDataFrame(
        {
            "id": [1, 2, 3],
            "geometry": [
                Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]),
                Polygon([(1000, 0), (3000, 0), (3000, 1000), (1000, 1000)]),
                Polygon([(3000, 0), (6000, 0), (6000, 1000), (3000, 1000)]),
            ],
        },
        crs="EPSG:3857",
    )

    roi = define_roi(catchments, segments, outlet_ids=[1], destine_crs="EPSG:3857")

    assert list(roi.segments["unit_length"]) == [1.0, 2.0, 3.0]
    assert list(roi.catchments["unit_length"]) == [1.0, 2.0, 3.0]
    assert list(roi.segments["upstream_length"]) == [6.0, 5.0, 3.0]
    assert list(roi.catchments["upstream_length"]) == [6.0, 5.0, 3.0]
    assert list(roi.segments["unit_area"]) == [1.0, 2.0, 3.0]
    assert list(roi.catchments["unit_area"]) == [1.0, 2.0, 3.0]
    assert list(roi.segments["upstream_area"]) == [6.0, 5.0, 3.0]
    assert list(roi.catchments["upstream_area"]) == [6.0, 5.0, 3.0]


def test_define_roi_source_crs_overrides_existing_layer_crs():
    segments = gpd.GeoDataFrame(
        {
            "id": [1],
            "id_down": [None],
            "strahler_order": [1],
            "geometry": [LineString([(0, 0), (1000, 0)])],
        },
        crs="EPSG:4326",
    )
    catchments = gpd.GeoDataFrame(
        {
            "id": [1],
            "geometry": [Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])],
        },
        crs="EPSG:4326",
    )

    roi = define_roi(
        catchments,
        segments,
        outlet_ids=[1],
        source_crs="EPSG:3857",
        destine_crs="EPSG:3857",
    )

    assert roi.segments.crs == "EPSG:3857"
    assert roi.catchments.crs == "EPSG:3857"
    assert roi.segments["unit_length"].iloc[0] == 1.0
    assert roi.catchments["unit_area"].iloc[0] == 1.0


def test_define_roi_requires_source_crs_when_layer_crs_is_missing():
    catchments = _catchments_gdf().set_crs(None, allow_override=True)

    with pytest.raises(MissingCrsError, match="catchments layer has no CRS"):
        define_roi(catchments, _segments_gdf(), outlet_ids=[2], destine_crs="EPSG:3857")
