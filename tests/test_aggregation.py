import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.aggregation import INPUT_COLUMNS, aggregate_minibasins
from mgb_vec_hydro.exceptions import InvalidInputSchemaError


def _input_fixture(
    *,
    ids=(1, 2, 3, 4),
    id_down=(None, 1, 1, 2),
    sub=(1, 1, 1, 1),
    unit_length=(1.0, 1.0, 1.0, 1.0),
    upstream_area=(10.0, 6.0, 4.0, 2.0),
    water_course=None,
):
    unit_area = [float(value) for value in ids]
    upstream_length = list(upstream_area)
    strahler_order = list(reversed(range(1, len(ids) + 1)))
    if water_course is None:
        water_course = ids
    catchments = gpd.GeoDataFrame(
        {
            "id": list(ids),
            "id_down": list(id_down),
            "sub": list(sub),
            "strahler_order": strahler_order,
            "unit_length": list(unit_length),
            "upstream_length": upstream_length,
            "unit_area": unit_area,
            "upstream_area": list(upstream_area),
            "water_course": list(water_course),
            "geometry": [
                Polygon([(index, 0), (index + 1, 0), (index + 1, 1), (index, 1)])
                for index, _ in enumerate(ids)
            ],
        },
        crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {
            "id": list(ids),
            "id_down": list(id_down),
            "sub": list(sub),
            "strahler_order": strahler_order,
            "unit_length": list(unit_length),
            "upstream_length": upstream_length,
            "unit_area": unit_area,
            "upstream_area": list(upstream_area),
            "water_course": list(water_course),
            "geometry": [
                LineString([(index, 0), (index + 1, 0)])
                for index, _ in enumerate(ids)
            ],
        },
        crs="EPSG:3857",
    )
    return catchments, segments


def test_exact_input_schema_is_accepted():
    catchments, segments = _input_fixture()

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=0,
    )

    assert list(result.catchments.columns) == INPUT_COLUMNS
    assert list(result.segments.columns) == INPUT_COLUMNS


@pytest.mark.parametrize(
    "mutate",
    [
        lambda gdf: gdf.drop(columns=["upstream_area"]),
        lambda gdf: gdf.drop(columns=["water_course"]),
        lambda gdf: gdf.assign(extra=1),
        lambda gdf: gdf[
            [
                "id_down",
                "id",
                "sub",
                "strahler_order",
                "unit_length",
                "upstream_length",
                "unit_area",
                "water_course",
                "upstream_area",
                "geometry",
            ]
        ],
    ],
)
def test_input_schema_rejects_missing_extra_or_reordered_columns(mutate):
    catchments, segments = _input_fixture()

    with pytest.raises(InvalidInputSchemaError, match="exact input columns"):
        aggregate_minibasins(mutate(catchments), segments, uparea_min=0, lmin=0)


def test_confluence_continuing_domain_uses_greatest_upstream_area():
    catchments, segments = _input_fixture(water_course=(1, 1, 3, 1))

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[2] == 1
    assert mapping[4] == 1
    assert mapping[3] == 3
    water_course = dict(zip(result.segments["id"], result.segments["water_course"]))
    assert water_course[1] == 1
    assert water_course[3] == 3


def test_cocursodag_column_is_not_required():
    catchments, segments = _input_fixture()

    aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)

    assert "cocursodag" not in segments.columns


def test_short_chain_merges_into_greatest_upstream_area_adjacent_group():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3),
        id_down=(None, 1, 2),
        sub=(1, 2, 3),
        unit_length=(3.0, 0.5, 3.0),
        upstream_area=(10.0, 8.0, 6.0),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=1.0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[2] == 1
    assert mapping[3] == 3


def test_catchments_are_assigned_once():
    catchments, segments = _input_fixture()

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=0,
    )

    assert len(result.mapping) == len(catchments)
    assert result.mapping["id"].is_unique
    assert result.catchments["unit_area"].sum() == pytest.approx(
        catchments["unit_area"].sum()
    )
