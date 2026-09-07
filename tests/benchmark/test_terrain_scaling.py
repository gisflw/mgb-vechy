"""Opt-in timing checks for bounded terrain processing.

Run synthetic scaling checks with ``RUN_TERRAIN_BENCHMARKS=1 pytest
tests/benchmark``. Set ``BHAE_DEM``, ``BHAE_MINI_OWNERSHIP``, ``BHAE_DRAINAGE``,
and ``BHAE_MINI_INDEX`` to exercise the
complete Stage 4 BHAE pipeline without making its one-minute target a portable
release gate.
"""

import multiprocessing
import os
import time
from pathlib import Path

import numpy as np
import pytest
from affine import Affine

from mgb_vec_hydro.execution.executor import ExecutionConfig, LocalExecutor, WorkItem
from mgb_vec_hydro.terrain import (
    TerrainSpec,
    compute_flow_directions,
    compute_hand,
    compute_ltnd,
    create_terrain_dataset,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TERRAIN_BENCHMARKS") != "1",
    reason="terrain scaling benchmarks are opt-in",
)


def _fixed_mini_worker(side, context):
    rows = np.arange(side, dtype=np.float64)[:, None]
    cols = np.arange(side, dtype=np.float64)[None, :]
    elevation = rows + cols
    labels = np.zeros((side, side), dtype=np.int32)
    drainage = np.zeros((side, side), dtype=bool)
    drainage[0, :] = True
    transform = Affine(90, 0, 0, 0, -90, 0)

    direction, rank = compute_flow_directions(elevation, labels, drainage, transform)
    hand = compute_hand(elevation, direction, rank)
    ltnd = compute_ltnd(direction, transform, rank)
    return float(hand[-1, -1] + ltnd[-1, -1])


def _run_fixed_minis(mini_count, result_queue):
    side = 256
    unit_bytes = side * side * 128 + 8 * 1024**2
    items = tuple(
        WorkItem(f"mini-{index:04d}", index, unit_bytes, side)
        for index in range(mini_count)
    )
    started = time.perf_counter()
    report = LocalExecutor(
        ExecutionConfig(
            workers=2,
            memory_limit_bytes=unit_bytes * 2,
            max_in_flight=2,
            io_slots=2,
        )
    ).run(items, _fixed_mini_worker, lambda result: None)
    result_queue.put(
        (report.peak_admitted_bytes, time.perf_counter() - started, unit_bytes)
    )


def _tree_rss_kb(pid):
    total = 0
    pending = [pid]
    while pending:
        current = pending.pop()
        try:
            lines = Path(f"/proc/{current}/status").read_text().splitlines()
            rss = next(line for line in lines if line.startswith("VmRSS:"))
            total += int(rss.split()[1])
            children = Path(f"/proc/{current}/task/{current}/children")
            pending.extend(int(value) for value in children.read_text().split())
        except (FileNotFoundError, StopIteration):
            continue
    return total


def _measure_fixed_minis(mini_count):
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_run_fixed_minis,
        args=(mini_count, result_queue),
    )
    process.start()
    peak_rss_kb = 0
    while process.is_alive():
        peak_rss_kb = max(peak_rss_kb, _tree_rss_kb(process.pid))
        time.sleep(0.01)
    process.join()
    assert process.exitcode == 0
    admitted_bytes, elapsed, unit_bytes = result_queue.get()
    return peak_rss_kb, admitted_bytes, elapsed, unit_bytes


def test_fixed_size_mini_count_has_bounded_memory_and_linear_work(record_property):
    small = _measure_fixed_minis(16)
    large = _measure_fixed_minis(64)

    assert small[1] == large[1] == small[3] * 2
    assert large[0] < small[0] * 1.35
    assert large[2] < small[2] * 5.5
    record_property("small_minis", 16)
    record_property("large_minis", 64)
    record_property("small_peak_rss_kb", small[0])
    record_property("large_peak_rss_kb", large[0])
    record_property("small_seconds", small[2])
    record_property("large_seconds", large[2])
    record_property("large_minis_per_second", 64 / large[2])


def test_full_bhae_performance_target(tmp_path, record_property):
    names = (
        "BHAE_DEM",
        "BHAE_MINI_OWNERSHIP",
        "BHAE_DRAINAGE",
        "BHAE_MINI_INDEX",
    )
    values = {name: os.environ.get(name) for name in names}
    if not all(values.values()):
        pytest.skip("explicit BHAE raster and index inputs are not configured")

    report = create_terrain_dataset(
        TerrainSpec(
            dem=Path(values["BHAE_DEM"]),
            mini_ownership=Path(values["BHAE_MINI_OWNERSHIP"]),
            drainage=Path(values["BHAE_DRAINAGE"]),
            mini_index=Path(values["BHAE_MINI_INDEX"]),
            d8=Path(os.environ["BHAE_D8"]) if os.environ.get("BHAE_D8") else None,
            output_dir=tmp_path / "terrain",
        )
    )

    assert report.mini_count == 267
    assert report.owned_cells == 19_097_931
    assert report.drainage_cells == 117_151
    memory_limit_bytes = 512 * 1024**2
    assert report.domain_execution.peak_admitted_bytes <= memory_limit_bytes
    assert report.terrain_execution.peak_admitted_bytes <= memory_limit_bytes
    record_property("minis", report.mini_count)
    record_property("owned_cells", report.owned_cells)
    record_property(
        "domain_peak_admitted_bytes",
        report.domain_execution.peak_admitted_bytes,
    )
    record_property(
        "terrain_peak_admitted_bytes",
        report.terrain_execution.peak_admitted_bytes,
    )
    for name, seconds in report.timings.items():
        record_property(f"{name}_seconds", seconds)
    record_property("one_minute_target_met", report.timings["total"] <= 60)
