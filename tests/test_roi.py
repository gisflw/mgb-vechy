import pytest
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import TopologyCycleError
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
    report = define_roi_dataset(_inputs(tmp_path))
    assert report.segment_count == 3
    assert report.catchments == report.output_dir / "roi_catchments.fgb"
    assert report.segments == report.output_dir / "roi_segments.fgb"
    assert sorted(path.name for path in report.output_dir.iterdir()) == [
        "roi_catchments.fgb",
        "roi_segments.fgb",
    ]
    assert not (report.output_dir / "manifest.json").exists()
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


def test_roi_rejects_selected_cycle_without_publishing(tmp_path):
    spec = _inputs(tmp_path, downstream=(2, 1, 2))
    with pytest.raises(TopologyCycleError):
        define_roi_dataset(spec)
    assert not spec.output_dir.exists()
