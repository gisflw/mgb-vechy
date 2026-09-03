"""Opt-in memory and throughput checks for shared local execution.

Run with ``RUN_EXECUTION_BENCHMARKS=1 pytest
tests/benchmark/execution/test_scaling.py``.
"""

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from mgb_vec_hydro.execution.executor import ExecutionConfig, LocalExecutor, WorkItem

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_EXECUTION_BENCHMARKS") != "1",
    reason="execution scaling benchmarks are opt-in",
)


def _fixed_payload_worker(size, context):
    return bytes(size)


def _run(task_count, workers, result_queue):
    payload_size = 128 * 1024
    items = (
        WorkItem(str(index), index, payload_size, payload_size)
        for index in range(task_count)
    )
    started = time.perf_counter()
    report = LocalExecutor(
        ExecutionConfig(
            workers=workers,
            memory_limit_bytes=payload_size * workers,
            max_in_flight=workers,
        )
    ).run(items, _fixed_payload_worker, lambda result: None)
    result_queue.put((report.peak_admitted_bytes, time.perf_counter() - started))


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


def _measure(task_count, workers):
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=_run, args=(task_count, workers, result_queue))
    process.start()
    peak = 0
    while process.is_alive():
        peak = max(peak, _tree_rss_kb(process.pid))
        time.sleep(0.01)
    process.join()
    assert process.exitcode == 0
    admitted, elapsed = result_queue.get()
    return peak, admitted, elapsed


def test_task_count_does_not_cause_unbounded_memory_growth(record_property):
    small = _measure(50, 2)
    large = _measure(500, 2)
    record_property("small_peak_rss_kb", small[0])
    record_property("large_peak_rss_kb", large[0])
    record_property("small_seconds", small[2])
    record_property("large_seconds", large[2])
    assert small[1] == large[1] == 256 * 1024
    assert large[0] < small[0] * 1.5


def test_worker_count_throughput_is_measured(record_property):
    serial = _measure(200, 1)
    parallel = _measure(200, 2)
    record_property("serial_seconds", serial[2])
    record_property("parallel_seconds", parallel[2])
    assert serial[1] == 128 * 1024
    assert parallel[1] == 256 * 1024
