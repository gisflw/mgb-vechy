"""Opt-in scaling checks for the Arrow ROI and aggregation pipeline.

Run with ``RUN_VECTOR_BENCHMARKS=1 pytest -s
tests/benchmark/test_vector_pipeline_scaling.py``.
"""

import os
from pathlib import Path
import time

import numpy as np
import pytest
from shapely.geometry import LineString, box

from mgb_vec_hydro.aggregation import aggregate_minibasins
from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
from mgb_vec_hydro.roi import RoiSpec, define_roi_dataset

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_VECTOR_BENCHMARKS") != "1",
    reason="vector pipeline scaling benchmarks are opt-in",
)


def _normalized_network(count: int):
    ids = np.arange(1, count + 1, dtype="int64")
    downstream = [None, *ids[:-1]]
    common = {
        "id": ids, "id_down": downstream, "sub": np.ones(count, dtype="int64"),
        "strahler_order": np.ones(count, dtype="int64"),
        "unit_length": np.ones(count), "upstream_length": np.arange(count, 0, -1, dtype=float),
        "unit_area": np.ones(count), "upstream_area": np.arange(count, 0, -1, dtype=float),
        "water_course": np.ones(count, dtype="int64"),
    }
    catchments = VectorTable.from_pydict(
        common, [box(value, 0, value + 1, 1) for value in range(count)],
        crs="EPSG:3857", geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        common, [LineString([(value, 0.5), (value + 1, 0.5)]) for value in range(count)],
        crs="EPSG:3857", geometry_type="LineString",
    )
    return catchments, segments


def test_aggregation_work_scales_near_linearly(record_property):
    measurements = []
    for count in (256, 1024):
        catchments, segments = _normalized_network(count)
        started = time.perf_counter()
        result = aggregate_minibasins(catchments, segments, uparea_min=0, lmin=6)
        measurements.append(time.perf_counter() - started)
        assert len(result.mapping) == count
    record_property("aggregation_256_seconds", measurements[0])
    record_property("aggregation_1024_seconds", measurements[1])
    assert measurements[1] <= measurements[0] * 6


def _raw_roi(root: Path, count: int) -> RoiSpec:
    ids = np.arange(1, count + 1, dtype="int64")
    catchments = VectorTable.from_pydict(
        {"id": ids}, [box(value, 0, value + 1, 1) for value in range(count)],
        crs="EPSG:3857", geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        {"id": ids, "down": [None, *ids[:-1]], "order": np.ones(count, dtype="int16")},
        [LineString([(value, 0.5), (value + 1, 0.5)]) for value in range(count)],
        crs="EPSG:3857", geometry_type="LineString",
    )
    catchment_path, segment_path = root / "catchments.fgb", root / "segments.fgb"
    write_vector_table(catchments, catchment_path, driver="FlatGeobuf")
    write_vector_table(segments, segment_path, driver="FlatGeobuf")
    return RoiSpec(
        crs="EPSG:3857", catchments=catchment_path, segments=segment_path,
        outlet_ids=("1",), id_col="id", id_down_col="down",
        strahler_order_col="order", output_dir=root / "roi", workers=1,
        memory_limit_mb=64, batch_size=128,
    )


def test_roi_reports_arrow_geometry_execution(tmp_path, record_property):
    report = define_roi_dataset(_raw_roi(tmp_path, 512))
    record_property("roi_512_total_seconds", report.timings["total"])
    record_property("roi_512_geometry_seconds", report.timings["geometry_execution"])
    assert report.catchment_count == report.segment_count == 512
