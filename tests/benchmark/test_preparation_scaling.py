"""Opt-in peak-memory checks for prepared vector streaming.

Run with ``RUN_PREPARATION_BENCHMARKS=1 pytest
tests/benchmark/test_preparation_scaling.py``.
"""

import multiprocessing
import os
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from mgb_vec_hydro.preparation import PreparationSpec, prepare_dataset

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PREPARATION_BENCHMARKS") != "1",
    reason="preparation scaling benchmarks are opt-in",
)


def _write_sources(root: Path, count: int) -> PreparationSpec:
    ids = pd.Series(np.arange(count), dtype="int64")
    catchments = gpd.GeoDataFrame(
        {"id": ids},
        geometry=[box(value, 0, value + 1, 1) for value in range(count)],
        crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {
            "id": ids,
            "id_down": pd.Series([None, *range(count - 1)], dtype="Int64"),
            "strahler_order": np.ones(count, dtype="int16"),
        },
        geometry=[LineString([(value, 0), (value + 1, 1)]) for value in range(count)],
        crs="EPSG:3857",
    )
    catchments_path = root / "catchments.fgb"
    segments_path = root / "segments.fgb"
    dem_path = root / "dem.tif"
    catchments.to_file(catchments_path, driver="FlatGeobuf")
    segments.to_file(segments_path, driver="FlatGeobuf")
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        width=16,
        height=16,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=from_origin(0, 16, 1, 1),
    ) as dataset:
        dataset.write(np.ones((16, 16), dtype="float32"), 1)
    return PreparationSpec(
        catchments=catchments_path,
        segments=segments_path,
        dem=dem_path,
        crs="EPSG:3857",
        resolution=1,
        output_dir=root / "prepared",
        vector_batch_size=128,
        memory_limit_mb=32,
    )


def _run(spec: PreparationSpec):
    prepare_dataset(spec)


def _peak_rss(spec: PreparationSpec) -> int:
    process = multiprocessing.Process(target=_run, args=(spec,))
    process.start()
    peak_kb = 0
    status = Path(f"/proc/{process.pid}/status")
    while process.is_alive():
        try:
            line = next(
                value for value in status.read_text().splitlines() if value.startswith("VmRSS:")
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
