import pytest
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.aggregation import INPUT_COLUMNS, aggregate_minibasins
from mgb_vec_hydro.exceptions import InvalidInputSchemaError
from mgb_vec_hydro.execution.vector import VectorTable


def _inputs(
    *,
    ids=(1, 2, 3, 4),
    id_down=(None, 1, 1, 2),
    sub=(1, 1, 1, 1),
    unit_length=(1.0, 1.0, 1.0, 1.0),
    upstream_area=(10.0, 6.0, 4.0, 2.0),
    water_course=None,
):
    if water_course is None:
        water_course = ids
    common = {
        "id": list(ids),
        "id_down": list(id_down),
        "sub": list(sub),
        "strahler_order": list(reversed(range(1, len(ids) + 1))),
        "unit_length": list(unit_length),
        "upstream_length": list(upstream_area),
        "unit_area": [float(value) for value in ids],
        "upstream_area": list(upstream_area),
        "water_course": list(water_course),
    }
    catchments = VectorTable.from_pydict(
        common,
        [
            Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)])
            for i in range(len(ids))
        ],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        common,
        [LineString([(i, 0), (i + 1, 0)]) for i in range(len(ids))],
        crs="EPSG:3857",
        geometry_type="LineString",
    )
    return catchments, segments


def _with_attributes(vector, **updates):
    attributes = vector.table.drop([vector.geometry_column]).to_pydict()
    attributes.update(updates)
    return VectorTable.from_pydict(
        attributes,
        vector.geometries(),
        crs=vector.crs,
        geometry_type=vector.geometry_type,
    )


def test_accepts_the_canonical_schema_and_rejects_a_missing_column():
    catchments, segments = _inputs()
    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)
    assert list(result.catchments.to_pandas().columns) == INPUT_COLUMNS
    assert list(result.segments.to_pandas().columns) == INPUT_COLUMNS

    attributes = catchments.table.drop(["upstream_area", catchments.geometry_column])
    invalid = VectorTable.from_pydict(
        attributes.to_pydict(),
        catchments.geometries(),
        crs=catchments.crs,
        geometry_type="Polygon",
    )
    with pytest.raises(InvalidInputSchemaError, match="exact input columns"):
        aggregate_minibasins(invalid, segments, uparea_min=0, lmin=0)


def test_metric_provenance_is_shared_between_catchments_and_segments():
    catchments, segments = _inputs(
        ids=(1, 2),
        id_down=(None, 1),
        sub=(1, 1),
        unit_length=(2.0, 3.0),
        upstream_area=(300.0, 200.0),
        water_course=(1, 1),
    )
    catchments = _with_attributes(
        catchments,
        unit_length=(70.0, 80.0),
        upstream_length=(700.0, 800.0),
        unit_area=(10.0, 20.0),
        upstream_area=(30.0, 20.0),
    )
    segments = _with_attributes(
        segments,
        upstream_length=(5.0, 3.0),
        unit_area=(100.0, 200.0),
        upstream_area=(300.0, 200.0),
    )

    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)
    catchment_attributes = result.catchments.to_pandas().drop(columns="geometry")
    segment_attributes = result.segments.to_pandas().drop(columns="geometry")

    assert catchment_attributes.equals(segment_attributes)
    row = segment_attributes.iloc[0]
    assert row["unit_length"] == pytest.approx(5.0)
    assert row["upstream_length"] == pytest.approx(5.0)
    assert row["unit_area"] == pytest.approx(30.0)
    assert row["upstream_area"] == pytest.approx(30.0)


def test_confluence_continues_the_branch_with_greatest_upstream_area():
    catchments, segments = _inputs(water_course=(1, 1, 3, 1))
    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)
    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[4] == mapping[2] == 2
    assert mapping[3] == 3


def test_linear_chain_collapses_using_the_evolving_length():
    catchments, segments = _inputs(
        ids=(1, 2, 3),
        id_down=(None, 1, 2),
        sub=(1, 1, 1),
        unit_length=(5.0, 0.4, 0.4),
        upstream_area=(10.0, 8.0, 6.0),
        water_course=(1, 1, 1),
    )
    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=1.0)
    assert dict(zip(result.mapping["id"], result.mapping["mini_id"])) == {
        1: 1,
        2: 1,
        3: 1,
    }
    assert result.segments.to_pandas()["unit_length"].iloc[0] == pytest.approx(5.8)


def test_upstream_area_threshold_merges_excluded_segments():
    catchments, segments = _inputs(
        ids=(1, 2, 3),
        id_down=(None, 1, 2),
        sub=(1, 1, 1),
        unit_length=(5.0, 1.0, 4.0),
        upstream_area=(10.0, 2.0, 8.0),
        water_course=(1, 1, 1),
    )
    result = aggregate_minibasins(catchments, segments, uparea_min=5.0, lmin=0)
    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[2] == 1
    assert 2 not in set(result.segments.to_pandas()["id"])
    assert result.segments.to_pandas().loc[
        result.segments.to_pandas()["id"] == 1, "unit_length"
    ].iloc[0] == pytest.approx(9.0)


def test_short_segments_do_not_merge_across_domains():
    catchments, segments = _inputs(
        id_down=(None, 1, 2, 1),
        sub=(1, 1, 1, 2),
        unit_length=(3.0, 0.5, 3.0, 3.0),
        upstream_area=(10.0, 8.0, 6.0, 5.0),
        water_course=(1, 2, 1, 2),
    )
    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=1.0)
    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[2] == 1
    assert mapping[3] == 3
    assert mapping[4] == 4


def test_every_catchment_maps_once_and_downstream_ids_are_valid():
    catchments, segments = _inputs(water_course=(1, 1, 3, 1))
    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)
    output = result.segments.to_pandas()
    output_ids = set(output["id"])
    assert len(result.mapping) == len(catchments)
    assert result.mapping["id"].is_unique
    assert set(result.mapping["mini_id"]) == output_ids
    assert set(output["id_down"].dropna()).issubset(output_ids)
    assert result.catchments.to_pandas()["unit_area"].sum() == pytest.approx(
        catchments.to_pandas()["unit_area"].sum()
    )


def test_rejects_a_short_mini_without_an_eligible_target():
    catchments, segments = _inputs(
        ids=(1,),
        id_down=(None,),
        sub=(1,),
        unit_length=(0.5,),
        upstream_area=(10.0,),
        water_course=(1,),
    )
    with pytest.raises(InvalidInputSchemaError, match="no eligible aggregation target"):
        aggregate_minibasins(catchments, segments, uparea_min=0, lmin=1.0)
