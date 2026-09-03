"""Opt-in feature-count and raster-extent checks for shared readers.

Run with ``RUN_EXECUTION_BENCHMARKS=1 pytest
tests/benchmark/execution/test_io_scaling.py``.
"""

import json
import multiprocessing
import os
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from pyproj import CRS
from rasterio.shutil import copy as copy_raster
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import LineString

from mgb_vec_hydro.execution.executor import WorkerContext
from mgb_vec_hydro.execution.raster import PreparedRasterReader
from mgb_vec_hydro.execution.vector import (
    inspect_vector_provider,
    iter_provider_batches,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_EXECUTION_BENCHMARKS") != "1",
    reason="execution I/O scaling benchmarks are opt-in",
)


def _write_prepared(root, feature_count, raster_size):
    (root / "vectors").mkdir(parents=True)
    (root / "rasters").mkdir()
    ids = np.arange(feature_count, dtype="int64")
    vectors = gpd.GeoDataFrame(
        {"id": ids, "group": ids % 8},
        geometry=[LineString([(value, 0), (value + 1, 1)]) for value in ids],
        crs="EPSG:3857",
    )
    for name in ("catchments", "segments"):
        vectors.to_file(root / "vectors" / f"{name}.fgb", driver="FlatGeobuf")

    working = root / "rasters" / "dem.working.tif"
    transform = from_origin(0, raster_size, 1, 1)
    with rasterio.open(
        working,
        "w",
        driver="GTiff",
        width=raster_size,
        height=raster_size,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=transform,
        tiled=True,
        blockxsize=512,
        blockysize=512,
    ) as target:
        block = np.ones((512, 512), dtype="float32")
        for _, window in target.block_windows(1):
            shape = (int(window.height), int(window.width))
            target.write(block[: shape[0], : shape[1]], 1, window=window)
            target.write_mask(np.full(shape, 255, dtype="uint8"), window=window)
    copy_raster(working, root / "rasters" / "dem.tif", driver="COG", BLOCKSIZE=512)
    working.unlink()

    raster_asset = {
        "path": "rasters/dem.tif",
        "role": "continuous",
        "driver": "COG",
    }
    manifest = {
        "contract": "mgb-prepared-dataset",
        "version": 3,
        "grid": {
            "crs_wkt": CRS.from_epsg(3857).to_wkt(),
            "transform": list(transform)[:6],
            "extent": [0, 0, raster_size, raster_size],
            "resolution": 1,
            "width": raster_size,
            "height": raster_size,
            "nodata": "internal-mask",
        },
        "sources": {"rasters": {}},
        "assets": {"rasters": {"dem": raster_asset}},
    }
    (root / "manifest.json").write_text(json.dumps(manifest))


def _measure_vector(root, result_queue):
    started = time.perf_counter()
    provider = inspect_vector_provider(root / "vectors/segments.fgb")
    count = sum(
        batch.num_rows
        for batch in iter_provider_batches(
            provider, columns=("group",), batch_size=128
        )
    )
    result_queue.put((count, time.perf_counter() - started))


def _measure_raster(root, result_queue):
    context = multiprocessing.get_context("spawn")
    worker_context = WorkerContext(context.BoundedSemaphore(1), 2)
    started = time.perf_counter()
    cells = 0
    try:
        reader = PreparedRasterReader(root, worker_context)
        for row in range(0, reader.grid.height, 256):
            for col in range(0, reader.grid.width, 256):
                window = Window(
                    col,
                    row,
                    min(256, reader.grid.width - col),
                    min(256, reader.grid.height - row),
                )
                cells += reader.read("dem", window).size
    finally:
        worker_context.close()
    result_queue.put((cells, time.perf_counter() - started))


def _peak_rss(root, target):
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=target, args=(root, result_queue))
    process.start()
    peak = 0
    status = Path(f"/proc/{process.pid}/status")
    while process.is_alive():
        try:
            line = next(
                value
                for value in status.read_text().splitlines()
                if value.startswith("VmRSS:")
            )
            peak = max(peak, int(line.split()[1]))
        except (FileNotFoundError, StopIteration):
            pass
        time.sleep(0.01)
    process.join()
    assert process.exitcode == 0
    count, elapsed = result_queue.get()
    return peak, count, elapsed


def test_vector_feature_count_scaling(tmp_path, record_property):
    small_root, large_root = tmp_path / "small", tmp_path / "large"
    _write_prepared(small_root, 2_000, 512)
    _write_prepared(large_root, 10_000, 512)
    small = _peak_rss(small_root, _measure_vector)
    large = _peak_rss(large_root, _measure_vector)
    record_property("small_vector_peak_rss_kb", small[0])
    record_property("large_vector_peak_rss_kb", large[0])
    record_property("large_vector_features_per_second", large[1] / large[2])
    assert large[1] == 10_000
    assert large[0] < small[0] * 2


def test_raster_extent_scaling(tmp_path, record_property):
    small_root, large_root = tmp_path / "small", tmp_path / "large"
    _write_prepared(small_root, 8, 512)
    _write_prepared(large_root, 8, 2048)
    small = _peak_rss(small_root, _measure_raster)
    large = _peak_rss(large_root, _measure_raster)
    record_property("small_raster_peak_rss_kb", small[0])
    record_property("large_raster_peak_rss_kb", large[0])
    record_property("large_raster_cells_per_second", large[1] / large[2])
    assert large[1] == 2048 * 2048
    assert large[0] < small[0] * 2
