import pandas as pd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import InvalidInputSchemaError, TopologyCycleError
import mgb_vec_hydro.roi as roi_module
from mgb_vec_hydro.roi import ROI_COLUMNS, RoiDataset, RoiSpec, define_roi_dataset
from mgb_vec_hydro.execution.vector import (
    VectorTable,
    read_vector_table,
    write_vector_table,
)


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


def test_roi_publishes_versioned_normalized_fgb_and_provider_area(tmp_path):
    report = define_roi_dataset(_inputs(tmp_path))
    assert report.segment_count == 3
    dataset = RoiDataset.open(report.output_dir)
    dataset.validate()
    segment_vector = read_vector_table(dataset.path("segments"))
    catchment_vector = read_vector_table(dataset.path("catchments"))
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


def test_roi_accepts_geographic_output_crs(tmp_path):
    spec = _inputs(tmp_path)
    spec = RoiSpec(
        **{
            **spec.__dict__,
            "crs": "EPSG:4326",
            "output_dir": tmp_path / "roi-geographic",
        }
    )
    report = define_roi_dataset(spec)
    dataset = RoiDataset.open(report.output_dir)
    segments = read_vector_table(dataset.path("segments"))
    assert segments.crs.to_epsg() == 4326
    assert segments.to_pandas().unit_length.iloc[0] == pytest.approx(1.0, rel=1e-5)


def test_roi_ignores_unrelated_null_catchment_ids(tmp_path):
    spec = _inputs(tmp_path)
    catchments = read_vector_table(spec.catchments).to_pandas()
    geometries = catchments.pop("geometry").tolist() + [
        Polygon([(10_000, 0), (11_000, 0), (11_000, 1_000), (10_000, 1_000)]),
        Polygon([(12_000, 0), (13_000, 0), (13_000, 1_000), (12_000, 1_000)]),
    ]
    data = {
        "SOURCE_ID": pd.array([*catchments["SOURCE_ID"], None, None], dtype="Int64"),
        "UNITAREA": [*catchments["UNITAREA"], 1.0, 1.0],
    }
    spec.catchments.unlink()
    write_vector_table(
        VectorTable.from_pydict(
            data, geometries, crs="EPSG:3857", geometry_type="Polygon"
        ),
        spec.catchments,
        driver="GPKG",
        layer="catchments",
    )
    spec = RoiSpec(**{**spec.__dict__, "catchments_source_crs": "EPSG:3857"})

    report = define_roi_dataset(spec)

    assert report.catchment_count == 3
    assert report.segment_count == 3


def test_roi_rejects_selected_cycle_without_publishing(tmp_path):
    spec = _inputs(tmp_path, downstream=(2, 1, 2))
    with pytest.raises(TopologyCycleError):
        define_roi_dataset(spec)
    assert not spec.output_dir.exists()


def test_roi_arrow_packets_resume_after_publication_failure(tmp_path, monkeypatch):
    spec = _inputs(tmp_path)
    spec = RoiSpec(**{**spec.__dict__, "checkpoint_dir": tmp_path / "checkpoints"})
    original = roi_module._write_cached_outputs

    def fail_after_packets(*args, **kwargs):
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr(roi_module, "_write_cached_outputs", fail_after_packets)
    with pytest.raises(RuntimeError, match="injected"):
        define_roi_dataset(spec)
    assert len(list((spec.checkpoint_dir / "completed").glob("*.json"))) > 0
    assert not spec.output_dir.exists()

    monkeypatch.setattr(roi_module, "_write_cached_outputs", original)
    report = define_roi_dataset(spec)

    assert report.segment_count == 3
    assert report.output_dir.exists()
    assert not spec.checkpoint_dir.exists()
