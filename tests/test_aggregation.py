import pytest
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.aggregation import INPUT_COLUMNS, aggregate_minibasins
from mgb_vec_hydro.exceptions import InvalidInputSchemaError
from mgb_vec_hydro.execution.vector import VectorTable


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
    common = {
        "id": list(ids),
        "id_down": list(id_down),
        "sub": list(sub),
        "strahler_order": strahler_order,
        "unit_length": list(unit_length),
        "upstream_length": upstream_length,
        "unit_area": unit_area,
        "upstream_area": list(upstream_area),
        "water_course": list(water_course),
    }
    catchments = VectorTable.from_pydict(
        common,
        [
            Polygon([(index, 0), (index + 1, 0), (index + 1, 1), (index, 1)])
            for index, _ in enumerate(ids)
        ],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        common,
        [LineString([(index, 0), (index + 1, 0)]) for index, _ in enumerate(ids)],
        crs="EPSG:3857",
        geometry_type="LineString",
    )
    return catchments, segments


def _with_attribute_table(vector, frame):
    import pyarrow as pa

    table = pa.Table.from_pandas(frame, preserve_index=False).append_column(
        vector.geometry_column, vector.table[vector.geometry_column]
    )
    return VectorTable(table, vector.crs, vector.geometry_column, vector.geometry_type)


def test_exact_input_schema_is_accepted():
    catchments, segments = _input_fixture()

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=0,
    )

    assert list(result.catchments.to_pandas().columns) == INPUT_COLUMNS
    assert list(result.segments.to_pandas().columns) == INPUT_COLUMNS


@pytest.mark.parametrize(
    "mutate",
    [
        lambda vector: _with_attribute_table(
            vector,
            vector.to_pandas(decode_geometry=False).drop(
                columns=["geometry", "upstream_area"]
            ),
        ),
        lambda vector: _with_attribute_table(
            vector,
            vector.to_pandas(decode_geometry=False).drop(
                columns=["geometry", "water_course"]
            ),
        ),
        lambda vector: _with_attribute_table(
            vector,
            vector.to_pandas(decode_geometry=False)
            .drop(columns="geometry")
            .assign(extra=1),
        ),
        lambda vector: _with_attribute_table(
            vector,
            vector.to_pandas(decode_geometry=False).drop(columns="geometry")[
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
                ]
            ],
        ),
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
    assert mapping[2] == 2
    assert mapping[4] == 2
    assert mapping[3] == 3
    water_course = dict(
        zip(
            result.segments.to_pandas()["id"],
            result.segments.to_pandas()["water_course"],
        )
    )
    assert water_course[1] == 1
    assert water_course[3] == 3


def test_cocursodag_column_is_not_required():
    catchments, segments = _input_fixture()

    aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)

    assert "cocursodag" not in segments.columns


def test_linear_chain_is_collapsed_before_lmin():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3),
        id_down=(None, 1, 2),
        sub=(1, 1, 1),
        unit_length=(3.0, 0.5, 2.0),
        upstream_area=(10.0, 8.0, 6.0),
        water_course=(1, 1, 1),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=1.0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping == {1: 1, 2: 1, 3: 1}
    assert result.segments.to_pandas()["unit_length"].iloc[0] == pytest.approx(5.5)


def test_short_segments_do_not_merge_across_sub_or_water_course():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3, 4),
        id_down=(None, 1, 2, 1),
        sub=(1, 1, 1, 2),
        unit_length=(3.0, 0.5, 3.0, 3.0),
        upstream_area=(10.0, 8.0, 6.0, 5.0),
        water_course=(1, 2, 1, 2),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=1.0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[2] == 1
    assert 2 not in set(result.segments.to_pandas()["id"])
    assert mapping[3] == 3
    assert mapping[4] == 4


def test_segments_below_uparea_min_are_merged_but_not_output_minis():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3),
        id_down=(None, 1, 2),
        sub=(1, 1, 1),
        unit_length=(5.0, 1.0, 4.0),
        upstream_area=(10.0, 2.0, 8.0),
        water_course=(1, 1, 1),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=5.0,
        lmin=0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert 2 not in set(result.segments.to_pandas()["id"])
    assert mapping[2] == 1
    assert result.segments.to_pandas().loc[
        result.segments.to_pandas()["id"] == 1, "unit_length"
    ].iloc[0] == pytest.approx(9.0)
    assert result.catchments.to_pandas()["unit_area"].sum() == pytest.approx(
        catchments.to_pandas()["unit_area"].sum()
    )


def test_segments_below_uparea_min_do_not_satisfy_lmin():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3),
        id_down=(None, 1, 2),
        sub=(1, 1, 1),
        unit_length=(5.0, 0.6, 0.6),
        upstream_area=(10.0, 8.0, 2.0),
        water_course=(1, 1, 1),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=5.0,
        lmin=1.0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[2] == 1
    assert mapping[3] == 1
    assert result.segments.to_pandas().loc[
        result.segments.to_pandas()["id"] == 1, "unit_length"
    ].iloc[0] == pytest.approx(5.6)


def test_filter_created_linear_link_is_collapsed_before_lmin():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3),
        id_down=(None, 1, 2),
        sub=(1, 1, 1),
        unit_length=(5.0, 1.0, 2.0),
        upstream_area=(10.0, 2.0, 8.0),
        water_course=(1, 1, 1),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=5.0,
        lmin=0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping == {1: 1, 2: 1, 3: 1}
    assert result.segments.to_pandas().loc[
        result.segments.to_pandas()["id"] == 1, "unit_length"
    ].iloc[0] == pytest.approx(7.0)


def test_surviving_confluence_remains_a_chain_boundary():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3, 4),
        id_down=(None, 1, 1, 2),
        unit_length=(2.0, 1.0, 3.0, 1.0),
        upstream_area=(10.0, 7.0, 3.0, 2.0),
        water_course=(1, 1, 1, 1),
    )

    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)
    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))

    assert mapping[1] == 1
    assert mapping[2] == mapping[4] == 2
    assert mapping[3] == 3


def test_excluded_segment_without_water_course_target_falls_back_to_same_sub():
    catchments, segments = _input_fixture(
        ids=(1, 2),
        id_down=(None, 1),
        sub=(1, 1),
        unit_length=(5.0, 1.0),
        upstream_area=(10.0, 2.0),
        water_course=(1, 2),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=5.0,
        lmin=0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[2] == 1
    assert set(result.segments.to_pandas()["id"]) == {1}


def test_lmin_uses_evolving_aggregated_length_until_threshold_is_met():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3),
        id_down=(None, 1, 2),
        sub=(1, 1, 1),
        unit_length=(5.0, 0.4, 0.4),
        upstream_area=(10.0, 8.0, 6.0),
        water_course=(1, 1, 1),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=1.0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert mapping[2] == 1
    assert mapping[3] == 1
    assert result.segments.to_pandas().loc[
        result.segments.to_pandas()["id"] == 1, "unit_length"
    ].iloc[0] == pytest.approx(5.8)


def test_unmergeable_short_mini_is_filtered_and_catchment_is_preserved():
    catchments, segments = _input_fixture(
        ids=(1, 2),
        id_down=(None, 1),
        sub=(1, 1),
        unit_length=(5.0, 0.5),
        upstream_area=(10.0, 8.0),
        water_course=(1, 2),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=1.0,
    )

    mapping = dict(zip(result.mapping["id"], result.mapping["mini_id"]))
    assert set(result.segments.to_pandas()["id"]) == {1}
    assert mapping[2] == 1
    assert result.catchments.to_pandas()["unit_area"].sum() == pytest.approx(
        catchments.to_pandas()["unit_area"].sum()
    )


def test_filtered_short_mini_length_does_not_contribute_to_surviving_reach():
    catchments, segments = _input_fixture(
        ids=(1, 2),
        id_down=(None, 1),
        sub=(1, 1),
        unit_length=(5.0, 0.5),
        upstream_area=(10.0, 8.0),
        water_course=(1, 2),
    )

    result = aggregate_minibasins(
        catchments,
        segments,
        uparea_min=0,
        lmin=1.0,
    )

    assert result.segments.to_pandas().loc[
        result.segments.to_pandas()["id"] == 1, "unit_length"
    ].iloc[0] == pytest.approx(5.0)


def test_all_unmergeable_short_minis_raise_missing_target_error():
    catchments, segments = _input_fixture(
        ids=(1,),
        id_down=(None,),
        sub=(1,),
        unit_length=(0.5,),
        upstream_area=(10.0,),
        water_course=(1,),
    )

    with pytest.raises(
        InvalidInputSchemaError,
        match="no eligible aggregation target in the same sub",
    ):
        aggregate_minibasins(
            catchments,
            segments,
            uparea_min=0,
            lmin=1.0,
        )


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
    assert result.catchments.to_pandas()["unit_area"].sum() == pytest.approx(
        catchments.to_pandas()["unit_area"].sum()
    )


def test_mapping_ids_match_output_ids_and_downstream_references():
    catchments, segments = _input_fixture(
        ids=(1, 2, 3, 4),
        id_down=(None, 1, 1, 2),
        water_course=(1, 1, 3, 1),
        upstream_area=(10.0, 8.0, 6.0, 2.0),
    )

    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)
    output = result.segments.to_pandas()
    output_ids = set(output["id"])

    assert set(result.mapping["mini_id"]) == output_ids
    assert set(output["id_down"].dropna()).issubset(output_ids)


def test_null_geometry_is_rejected():
    catchments, segments = _input_fixture(
        ids=(1,), id_down=(None,), sub=(1,), unit_length=(1.0,), upstream_area=(1.0,)
    )
    invalid = VectorTable.from_pydict(
        segments.table.drop([segments.geometry_column]).to_pydict(),
        [None],
        crs=segments.crs,
        geometry_type="LineString",
    )

    with pytest.raises(InvalidInputSchemaError, match="missing or empty geometry"):
        aggregate_minibasins(catchments, invalid, uparea_min=0, lmin=0)
