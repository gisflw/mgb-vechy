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
from shapely.geometry import LineString, box

from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
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
