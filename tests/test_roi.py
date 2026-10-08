import json
from dataclasses import replace

import pytest
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import InvalidInputSchemaError, TopologyCycleError
from mgb_vec_hydro.execution.vector import (
    VectorTable,
    read_vector_table,
    write_vector_table,
)
from mgb_vec_hydro.roi import ROI_COLUMNS, RoiSpec, define_roi_dataset


def _inputs(tmp_path, *, orders=(3, 2, 1), downstream=(None, 1, 2)):
    catchments = VectorTable.from_pydict(
        {"SOURCE_ID": [1, 2, 3], "UNITAREA": [1.0, 1.0, 1.0]},
        [
            Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]),
            Polygon([(1000, 0), (2000, 0), (2000, 1000), (1000, 1000)]),
            Polygon([(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)]),
        ],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        {"SOURCE_ID": [1, 2, 3], "DOWN": list(downstream), "ORDER": list(orders)},
        [
            LineString([(0, 500), (1000, 500)]),
            LineString([(1000, 500), (2000, 500)]),
            LineString([(2000, 500), (3000, 500)]),
        ],
        crs="EPSG:3857",
        geometry_type="LineString",
    )
    write_vector_table(catchments, tmp_path / "catchments.gpkg", driver="GPKG")
    write_vector_table(segments, tmp_path / "segments.gpkg", driver="GPKG")
    return RoiSpec(
        crs="EPSG:3857",
        catchments=tmp_path / "catchments.gpkg",
        segments=tmp_path / "segments.gpkg",
        outlet_ids=("1",),
        id_col="source_id",
        id_down_col="down",
        strahler_order_col="order",
        output_dir=tmp_path / "roi",
        workers=1,
        batch_size=1,
    )


def test_roi_publishes_flat_normalized_fgb_and_provider_area(tmp_path):
    spec = replace(_inputs(tmp_path), outlet_ids=("1", "1"))
    updates = []
    report = define_roi_dataset(spec, progress=updates.append)
    operations = {update.operation for update in updates}
    assert "Reading segment topology" in operations
    assert "Selecting upstream segments" in operations
    assert "Calculating upstream metrics" in operations
    assert "Writing roi_segments.fgb" in operations
    assert "Publishing outputs" in operations
    phase_times = [report.timings[f"{phase}_wall"] for phase in ("preparing", "processing", "finalizing")]
    assert all(seconds >= 0 for seconds in phase_times)
    assert sum(phase_times) == pytest.approx(report.timings["total"])
    assert report.segment_count == 3
    assert report.catchments == report.output_dir / "roi_catchments.fgb"
    assert report.segments == report.output_dir / "roi_segments.fgb"
    assert sorted(path.name for path in report.output_dir.iterdir()) == [
        "manifest-define-roi.json",
        "roi_catchments.fgb",
        "roi_segments.fgb",
    ]
    manifest = json.loads((report.output_dir / "manifest-define-roi.json").read_text())
    assert manifest["step"] == "define-roi"
    assert manifest["parameters"]["catchments"] == str(spec.catchments.resolve())
    assert manifest["parameters"]["outlet_ids"] == ["1", "1"]
    assert manifest["parameters"]["workers"] == 1
    assert not any(path.is_dir() for path in report.output_dir.iterdir())
    segment_vector = read_vector_table(report.segments)
    catchment_vector = read_vector_table(report.catchments)
    segments = segment_vector.to_pandas().sort_values("id")
    catchments = catchment_vector.to_pandas().sort_values("id")
    assert list(segments.columns) == ROI_COLUMNS
    assert list(catchments.columns) == ROI_COLUMNS
    assert list(segments.upstream_area) == pytest.approx([3.0, 2.0, 1.0], rel=1e-2)
    assert list(segments.unit_length) == pytest.approx([1.0, 1.0, 1.0], rel=1e-5)
    assert list(segments.upstream_length) == pytest.approx([3.0, 2.0, 1.0], rel=1e-5)
    assert segment_vector.crs.to_epsg() == 3857


def test_roi_filters_strahler_before_selection(tmp_path):
    spec = _inputs(tmp_path, orders=(3, 0, 1))
    report = define_roi_dataset(spec)
    assert report.segment_count == 1


def test_roi_ignores_duplicate_catchment_ids_outside_selected_ids(tmp_path):
    spec = _inputs(tmp_path)
    catchments = VectorTable.from_pydict(
        {"SOURCE_ID": [1, 2, 3, -1, -1]},
        [Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)]) for i in range(5)],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    write_vector_table(catchments, spec.catchments, driver="GPKG")

    report = define_roi_dataset(spec)

    assert report.catchment_count == 3


def test_roi_rejects_duplicate_selected_catchment_ids_across_batches(tmp_path):
    spec = _inputs(tmp_path)
    catchments = VectorTable.from_pydict(
        {"SOURCE_ID": [1, 1, 2, 3]},
        [Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)]) for i in range(4)],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    write_vector_table(catchments, spec.catchments, driver="GPKG")

    with pytest.raises(InvalidInputSchemaError, match="duplicate ID: 1"):
        define_roi_dataset(spec)
    assert not spec.output_dir.exists()


def test_roi_rejects_missing_selected_catchment(tmp_path):
    spec = _inputs(tmp_path)
    catchments = VectorTable.from_pydict(
        {"SOURCE_ID": [1, 3]},
        [
            Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
            Polygon([(2, 0), (3, 0), (3, 1), (2, 1)]),
        ],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    write_vector_table(catchments, spec.catchments, driver="GPKG")

    with pytest.raises(InvalidInputSchemaError, match="Selected catchment ID"):
        define_roi_dataset(spec)
    assert not spec.output_dir.exists()


def test_roi_rejects_selected_cycle_without_publishing(tmp_path):
    spec = _inputs(tmp_path, downstream=(2, 1, 2))
    with pytest.raises(TopologyCycleError):
        define_roi_dataset(spec)
    assert not spec.output_dir.exists()


def test_roi_manifest_failure_cleans_staging(tmp_path, monkeypatch):
    spec = _inputs(tmp_path)

    def fail_manifest(*_args):
        raise OSError("manifest write failed")

    monkeypatch.setattr("mgb_vec_hydro.roi.write_manifest", fail_manifest)
    with pytest.raises(OSError, match="manifest write failed"):
        define_roi_dataset(spec)
    assert not spec.output_dir.exists()


def test_roi_selects_more_than_ogrsql_fid_limit(tmp_path):
    count = 5000
    segments = VectorTable.from_pydict(
        {"id": range(count), "id_down": [None] + list(range(count - 1)), "strahler_order": [1] * count},
        [LineString([(i, 0), (i + 1, 0)]) for i in range(count)],
        crs="EPSG:3857", geometry_type="LineString",
    )
    catchments = VectorTable.from_pydict(
        {"id": range(count)},
        [Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)]) for i in range(count)],
        crs="EPSG:3857", geometry_type="Polygon",
    )
    write_vector_table(segments, tmp_path / "segments.fgb", driver="FlatGeobuf")
    write_vector_table(catchments, tmp_path / "catchments.fgb", driver="FlatGeobuf")
    report = define_roi_dataset(RoiSpec(
        crs="EPSG:3857", catchments=tmp_path / "catchments.fgb", segments=tmp_path / "segments.fgb",
        outlet_ids=(0,), id_col="id", id_down_col="id_down", strahler_order_col="strahler_order", output_dir=tmp_path / "roi", workers=1,
    ))
    assert report.segment_count == count
    assert set(read_vector_table(report.segments).table["id"].to_pylist()) == set(range(count))
