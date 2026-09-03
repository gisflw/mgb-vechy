import json
import os
import threading
import time

import pytest

from mgb_vec_hydro.exceptions import (
    CheckpointError,
    ExecutionCancelledError,
    ExecutionConfigurationError,
    WorkerExecutionError,
    WorkMemoryError,
)
from mgb_vec_hydro.execution.checkpoints import (
    CheckpointStore,
    JsonCheckpointCodec,
    execution_fingerprint,
)
from mgb_vec_hydro.execution.executor import (
    ExecutionConfig,
    LocalExecutor,
    WorkerContext,
    WorkerOutput,
    WorkerResourceCache,
    WorkItem,
)
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory


def _delayed_worker(payload, context):
    delay, value = payload
    time.sleep(delay)
    return WorkerOutput(value * 2, {"compute": delay}, {"value": value})


def _failing_worker(payload, context):
    if payload == "fail":
        raise ValueError("deliberate failure")
    return payload


def _slow_worker(payload, context):
    time.sleep(payload)
    return payload


def _unpickleable_result_worker(payload, context):
    return lambda: payload


def _cached_resource_worker(payload, context):
    resource = context.resources.get("shared", object)
    return os.getpid(), id(resource)


def _items():
    return [
        WorkItem("first", 0, 10, (0.15, 1)),
        WorkItem("second", 1, 10, (0.01, 2)),
        WorkItem("third", 2, 10, (0.01, 3)),
    ]


@pytest.mark.parametrize("workers", [1, 2])
def test_executor_reduces_deterministically_and_reports_resources(workers):
    values = []
    events = []
    report = LocalExecutor(
        ExecutionConfig(workers=workers, memory_limit_bytes=20, max_in_flight=2)
    ).run(
        _items(),
        _delayed_worker,
        lambda result: values.append(result.value),
        progress=events.append,
    )

    assert values == [2, 4, 6]
    assert (
        report.task_count == report.submitted == report.completed == report.reduced == 3
    )
    assert report.peak_admitted_bytes <= 20
    assert report.timings["compute"] == pytest.approx(0.17)
    assert [item["value"] for item in report.worker_diagnostics] == [1, 2, 3]
    assert events[0].kind == "started"
    assert events[-1].kind == "finished"


def test_executor_rejects_single_oversized_item():
    item = WorkItem("large", 0, 11, (0, 1))
    with pytest.raises(WorkMemoryError, match="large"):
        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=10)).run(
            [item], _delayed_worker, lambda result: None
        )


def test_executor_rejects_non_pickleable_worker_before_starting_processes():
    with pytest.raises(ExecutionConfigurationError, match="Worker callable"):
        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=1)).run(
            [WorkItem("one", 0, 1, 1)], lambda payload, context: payload, lambda _: None
        )


def test_executor_consumes_lazy_plan_only_to_in_flight_bound():
    pulled = 0
    pulled_at_reduction = []

    def items():
        nonlocal pulled
        for ordinal in range(5):
            pulled += 1
            yield WorkItem(str(ordinal), ordinal, 1, (0.03, ordinal))

    LocalExecutor(
        ExecutionConfig(workers=1, memory_limit_bytes=2, max_in_flight=2)
    ).run(
        items(),
        _delayed_worker,
        lambda result: pulled_at_reduction.append(pulled),
    )
    assert pulled_at_reduction[0] == 2
    assert pulled == 5


def test_worker_resource_cache_reuses_and_evicts_resources():
    closed = []

    class Resource:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    cache = WorkerResourceCache(1)
    first = cache.get("first", lambda: Resource("first"))
    assert cache.get("first", lambda: Resource("unused")) is first
    cache.get("second", lambda: Resource("second"))
    assert closed == ["first"]
    cache.close()
    assert closed == ["first", "second"]


def test_persistent_worker_reuses_its_resource_cache():
    observed = []
    LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=2)).run(
        [WorkItem("one", 0, 1, 1), WorkItem("two", 1, 1, 2)],
        _cached_resource_worker,
        lambda result: observed.append(result.value),
    )
    assert observed[0] == observed[1]


def test_worker_context_limits_concurrent_io():
    context = WorkerContext(threading.BoundedSemaphore(1), 1)
    active = 0
    peak = 0
    lock = threading.Lock()

    def use_io():
        nonlocal active, peak
        with context.io_bound():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1

    threads = [threading.Thread(target=use_io) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1


def test_worker_failure_has_remote_context_and_report():
    items = [WorkItem("bad", 0, 1, "fail")]
    with pytest.raises(WorkerExecutionError, match="deliberate failure") as caught:
        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=1)).run(
            items, _failing_worker, lambda result: None
        )
    assert caught.value.report.failures[0].exception_type == "ValueError"
    assert "_failing_worker" in caught.value.report.failures[0].remote_traceback


def test_unpickleable_worker_result_is_a_structured_failure():
    with pytest.raises(WorkerExecutionError, match="pickle") as caught:
        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=1)).run(
            [WorkItem("bad-result", 0, 1, 1)],
            _unpickleable_result_worker,
            lambda result: None,
        )
    assert caught.value.report.failures[0].key == "bad-result"


def test_executor_cancellation_terminates_running_worker():
    started = time.monotonic()
    items = [WorkItem("slow", 0, 1, 5)]
    with pytest.raises(ExecutionCancelledError) as caught:
        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=1)).run(
            items,
            _slow_worker,
            lambda result: None,
            cancelled=lambda: time.monotonic() - started > 0.2,
        )
    assert caught.value.report.cancelled
    assert time.monotonic() - started < 3


def test_checkpoint_resume_skips_completed_work_and_waits_for_publication_cleanup(
    tmp_path,
):
    items = _items()
    fingerprint = execution_fingerprint(
        algorithm="test",
        version="1",
        prepared_manifest={"version": 2},
        parameters={"x": 1},
        work_items=items,
    )
    checkpoint = CheckpointStore(
        tmp_path / "resume", fingerprint, JsonCheckpointCodec()
    )
    reduced = []

    def interrupt(result):
        reduced.append(result.value)
        if len(reduced) == 1:
            raise RuntimeError("stop after durable result")

    with pytest.raises(RuntimeError):
        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=20)).run(
            items, _delayed_worker, interrupt, checkpoint=checkpoint
        )
    assert checkpoint.contains(items[0])

    resumed_values = []
    report = LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=20)).run(
        items,
        _delayed_worker,
        lambda result: resumed_values.append(result.value),
        checkpoint=CheckpointStore(
            tmp_path / "resume", fingerprint, JsonCheckpointCodec()
        ),
    )
    assert resumed_values == [2, 4, 6]
    assert report.resumed == 1
    assert report.submitted == 2
    assert (tmp_path / "resume").exists()
    checkpoint.cleanup()
    assert not (tmp_path / "resume").exists()


def test_execution_fingerprint_is_stable_and_covers_ordered_plan():
    items = _items()

    def fingerprint(plan, parameters=None):
        return execution_fingerprint(
            algorithm="test",
            version="1",
            prepared_manifest={"version": 2},
            parameters=parameters or {"x": 1},
            work_items=(item for item in plan),
        )

    assert fingerprint(items) == fingerprint(items)
    assert fingerprint(items) != fingerprint(list(reversed(items)))
    assert fingerprint(items) != fingerprint(items, {"x": 2})


def test_checkpoint_rejects_identity_change_and_corruption(tmp_path):
    item = WorkItem("one", 0, 1, (0, 1))
    store = CheckpointStore(tmp_path / "resume", "original", JsonCheckpointCodec())
    LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=1)).run(
        [item],
        _delayed_worker,
        lambda result: None,
        checkpoint=store,
    )
    with pytest.raises(CheckpointError, match="incompatible"):
        CheckpointStore(tmp_path / "resume", "changed", JsonCheckpointCodec())

    artifact = next((tmp_path / "resume" / "artifacts").iterdir())
    artifact.write_text("corrupt")
    existing = CheckpointStore(tmp_path / "resume", "original", JsonCheckpointCodec())
    with pytest.raises(CheckpointError, match="corrupt"):
        existing.load(item)


def test_atomic_output_directory_publishes_once_and_cleans_failures(tmp_path):
    target = tmp_path / "output"
    publication = AtomicOutputDirectory(target)
    with publication as staging:
        (staging / "result.json").write_text(json.dumps({"ok": True}))
        publication.publish(("result.json",))
    assert json.loads((target / "result.json").read_text()) == {"ok": True}

    failed_target = tmp_path / "failed"
    with pytest.raises(RuntimeError), AtomicOutputDirectory(failed_target) as staging:
        (staging / "partial").write_text("partial")
        raise RuntimeError("failure")
    assert not failed_target.exists()
    assert not list(tmp_path.glob(".failed.tmp-*"))


def test_worker_failure_discards_coordinator_staged_output(tmp_path):
    target = tmp_path / "worker-failed"
    publication = AtomicOutputDirectory(target)
    with pytest.raises(WorkerExecutionError), publication as staging:

        def write_result(result):
            (staging / f"{result.key}.txt").write_text(str(result.value))

        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=2)).run(
            [WorkItem("good", 0, 1, "good"), WorkItem("bad", 1, 1, "fail")],
            _failing_worker,
            write_result,
        )
    assert not target.exists()
    assert not list(tmp_path.glob(".worker-failed.tmp-*"))
