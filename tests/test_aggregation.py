import pandas as pd
import pytest
from shapely.geometry import LineString, Polygon

import mgb_vec_hydro.aggregation as aggregation_module
from mgb_vec_hydro.aggregation import (
    AGGREGATION_COLUMNS,
    AggregationSpec,
    aggregate_minibasins,
    aggregate_roi_dataset,
)
from mgb_vec_hydro.exceptions import InvalidInputSchemaError
from mgb_vec_hydro.execution.vector import (
    VectorTable,
    read_vector_table,
    write_vector_table,
)


def _inputs(
    *,
    ids=(1, 2, 3, 4),
    id_down=(None, 1, 1, 2),
    sub=(1, 1, 1, 1),
    unit_length=(1.0, 1.0, 1.0, 1.0),
    unit_area=None,
    upstream_area=(10.0, 6.0, 4.0, 2.0),
    water_course=None,
):
    if water_course is None:
        water_course = ids
    if unit_area is None:
        unit_area = [float(value) for value in ids]
    common = {
        "id": list(ids),
        "id_down": list(id_down),
        "sub": list(sub),
        "strahler_order": list(reversed(range(1, len(ids) + 1))),
        "unit_length": list(unit_length),
        "upstream_length": list(upstream_area),
        "unit_area": list(unit_area),
        "upstream_area": list(upstream_area),
        "water_course": list(water_course),
    }
    catchments = VectorTable.from_pydict(
        common,
        [Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)]) for i in range(len(ids))],
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
    assert list(result.catchments.to_pandas().columns) == AGGREGATION_COLUMNS
    assert list(result.segments.to_pandas().columns) == AGGREGATION_COLUMNS

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
    assert mapping[3] == 1
    output = result.segments.to_pandas()
    assert output["p_order"].tolist() == [1, 1, 2]
    assert output["id_down"].tolist() == [3, 3, -1]


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
    assert len(result.segments) == 1
    assert result.segments.to_pandas()["unit_length"].iloc[0] == pytest.approx(9.0)


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
    assert mapping[2] == mapping[1]
    assert mapping[3] != mapping[1]
    assert mapping[4] != mapping[1]


def test_every_catchment_maps_once_and_downstream_ids_are_valid():
    catchments, segments = _inputs(water_course=(1, 1, 3, 1))
    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)
    output = result.segments.to_pandas()
    output_ids = set(output["id"])
    assert len(result.mapping) == len(catchments)
    assert result.mapping["id"].is_unique
    assert set(result.mapping["mini_id"]) == output_ids
    assert set(output["id_down"]).issubset(output_ids | {-1})
    assert output["id"].tolist() == list(range(1, len(output) + 1))
    assert result.catchments.to_pandas()["unit_area"].sum() == pytest.approx(
        catchments.to_pandas()["unit_area"].sum()
    )


def test_processing_order_sort_and_dense_ids_are_legacy_compatible():
    catchments, segments = _inputs(
        ids=("mouth", "large-head", "small-head", "other-mouth"),
        id_down=(None, "mouth", "mouth", None),
        sub=(2, 2, 2, 1),
        upstream_area=(10.0, 6.0, 4.0, 3.0),
        unit_area=(1.0, 1.0, 1.0, 1.0),
        water_course=("mouth", "large-head", "small-head", "other-mouth"),
    )
    result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=0)
    output = result.segments.to_pandas()

    assert output[["sub", "p_order", "upstream_area"]].values.tolist() == [
        [1.0, 1.0, 3.0],
        [2.0, 1.0, 4.0],
        [2.0, 1.0, 6.0],
        [2.0, 2.0, 10.0],
    ]
    assert output["id"].tolist() == [1, 2, 3, 4]
    assert output["id_down"].tolist() == [-1, 4, 4, -1]
    assert set(result.mapping["mini_id"]) == {1, 2, 3, 4}


def test_dataset_defers_geometry_and_preserves_physical_sort(tmp_path, monkeypatch):
    catchments, segments = _inputs(
        ids=("mouth", "large-head", "small-head", "other-mouth"),
        id_down=(None, "mouth", "mouth", None),
        sub=(2, 2, 2, 1),
        unit_area=(1.0, 1.0, 1.0, 1.0),
        upstream_area=(10.0, 6.0, 4.0, 3.0),
        water_course=("mouth", "large-head", "small-head", "other-mouth"),
    )
    catchment_path = tmp_path / "catchments.fgb"
    segment_path = tmp_path / "segments.fgb"
    write_vector_table(catchments, catchment_path, driver="FlatGeobuf")
    write_vector_table(segments, segment_path, driver="FlatGeobuf")

    attribute_reads = []
    original = aggregation_module.iter_provider_batches

    def recording_reader(*args, **kwargs):
        attribute_reads.append(kwargs.get("read_geometry", False))
        yield from original(*args, **kwargs)

    monkeypatch.setattr(aggregation_module, "iter_provider_batches", recording_reader)
    report = aggregate_roi_dataset(
        AggregationSpec(
            roi_catchments=catchment_path,
            roi_segments=segment_path,
            uparea_min=0,
            lmin=0,
            output_dir=tmp_path / "output",
            workers=1,
            batch_size=2,
        )
    )

    assert attribute_reads and not any(attribute_reads)
    for path in (report.mini_catchments, report.mini_segments):
        vector = read_vector_table(path)
        output = vector.to_pandas(decode_geometry=False)
        assert list(vector.columns) == AGGREGATION_COLUMNS
        assert output["id"].tolist() == [1, 2, 3, 4]
        assert output[["sub", "p_order", "upstream_area"]].values.tolist() == [
            [1.0, 1.0, 3.0],
            [2.0, 1.0, 4.0],
            [2.0, 1.0, 6.0],
            [2.0, 2.0, 10.0],
        ]
    mapping_frame = pd.read_csv(report.source_to_mini)
    mapping = dict(zip(mapping_frame["id"], mapping_frame["mini_id"]))
    assert mapping == {"large-head": 3, "mouth": 4, "other-mouth": 1, "small-head": 2}


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
