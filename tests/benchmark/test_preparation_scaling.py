"""Opt-in peak-memory checks for Arrow ROI vector streaming.

Run with ``RUN_PREPARATION_BENCHMARKS=1 pytest
tests/benchmark/test_preparation_scaling.py``.
"""

import multiprocessing
import os
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Polygon, box

from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
from mgb_vec_hydro.preparation import PreparationSpec, prepare_dataset
from mgb_vec_hydro.roi import RoiSpec, define_roi_dataset

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PREPARATION_BENCHMARKS") != "1",
    reason="preparation scaling benchmarks are opt-in",
)


def _write_sources(root: Path, count: int) -> RoiSpec:
    ids = np.arange(count, dtype="int64")
    catchments = VectorTable.from_pydict(
        {"id": ids},
        [box(value, 0, value + 1, 1) for value in range(count)],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        {
            "id": ids,
            "id_down": [None, *range(count - 1)],
            "strahler_order": np.ones(count, dtype="int16"),
        },
        [LineString([(value, 0), (value + 1, 1)]) for value in range(count)],
        crs="EPSG:3857",
        geometry_type="LineString",
    )
    catchments_path = root / "catchments.fgb"
    segments_path = root / "segments.fgb"
    write_vector_table(catchments, catchments_path, driver="FlatGeobuf")
    write_vector_table(segments, segments_path, driver="FlatGeobuf")
    return RoiSpec(
        catchments=catchments_path,
        segments=segments_path,
        crs="EPSG:3857",
        outlet_ids=("0",),
        id_col="id",
        id_down_col="id_down",
        strahler_order_col="strahler_order",
        output_dir=root / "roi",
        workers=1,
        batch_size=128,
        memory_limit_mb=32,
    )


def _run(spec: RoiSpec):
    define_roi_dataset(spec)


def _peak_rss(spec: RoiSpec) -> int:
    process = multiprocessing.Process(target=_run, args=(spec,))
    process.start()
    peak_kb = 0
    status = Path(f"/proc/{process.pid}/status")
    while process.is_alive():
        try:
            line = next(
                value
                for value in status.read_text().splitlines()
                if value.startswith("VmRSS:")
            )
            peak_kb = max(peak_kb, int(line.split()[1]))
        except (FileNotFoundError, StopIteration):
            pass
        time.sleep(0.02)
    process.join()
    assert process.exitcode == 0
    return peak_kb


def test_peak_memory_does_not_scale_with_feature_count(tmp_path, record_property):
    small_root = tmp_path / "small"
    large_root = tmp_path / "large"
    small_root.mkdir()
    large_root.mkdir()
    small = _peak_rss(_write_sources(small_root, 2_000))
    large = _peak_rss(_write_sources(large_root, 10_000))

    record_property("small_peak_rss_kb", small)
    record_property("large_peak_rss_kb", large)
    assert large < small * 2


def _write_preparation_inputs(root: Path, size: int):
    transform = from_origin(0, size, 1, 1)
    dem = root / "dem.tif"
    with rasterio.open(
        dem,
        "w",
        driver="GTiff",
        width=size,
        height=size,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=transform,
    ) as target:
        rows = np.arange(size, dtype="float32")
        target.write(np.broadcast_to(rows[:, None], (size, size)), 1)
    attributes = {
        "id": [1],
        "id_down": [None],
        "sub": [1],
        "strahler_order": [1],
        "unit_length": [1.0],
        "upstream_length": [1.0],
        "unit_area": [1.0],
        "upstream_area": [1.0],
        "water_course": [1],
    }
    catchments = root / "mini_catchments.fgb"
    segments = root / "mini_segments.fgb"
    write_vector_table(
        VectorTable.from_pydict(
            attributes,
            [Polygon([(0, 0), (size, 0), (size, size), (0, size)])],
            crs="EPSG:3857",
            geometry_type="Polygon",
        ),
        catchments,
        driver="FlatGeobuf",
    )
    write_vector_table(
        VectorTable.from_pydict(
            attributes,
            [LineString([(0, size / 2), (size, size / 2)])],
            crs="EPSG:3857",
            geometry_type="LineString",
        ),
        segments,
        driver="FlatGeobuf",
    )
    return dem, catchments, segments


def test_preparation_reports_serial_and_parallel_throughput(tmp_path, record_property):
    dem, catchments, segments = _write_preparation_inputs(tmp_path, 1536)
    for workers in (1, 4):
        report = prepare_dataset(
            PreparationSpec(
                dem=dem,
                mini_catchments=catchments,
                mini_segments=segments,
                output_dir=tmp_path / f"prepared-{workers}",
                workers=workers,
                memory_limit_mb=256,
                io_slots=2,
            )
        )
        record_property(f"workers_{workers}_total_seconds", report.timings["total"])
        record_property(
            f"workers_{workers}_execution_seconds", report.execution.wall_seconds
        )
        record_property(
            f"workers_{workers}_peak_admitted_bytes",
            report.execution.peak_admitted_bytes,
        )
        record_property(
            f"workers_{workers}_worker_processes",
            len({value["worker_pid"] for value in report.execution.worker_diagnostics}),
        )
