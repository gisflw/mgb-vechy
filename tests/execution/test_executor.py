import json
import time

import pytest

from mgb_vec_hydro.exceptions import WorkerExecutionError, WorkMemoryError
from mgb_vec_hydro.execution.checkpoints import (
    CheckpointStore,
    JsonCheckpointCodec,
    execution_fingerprint,
)
from mgb_vec_hydro.execution.executor import (
    ExecutionConfig,
    LocalExecutor,
    WorkerOutput,
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


def _items():
    return [
        WorkItem("first", 0, 10, (0.02, 1)),
        WorkItem("second", 1, 10, (0.001, 2)),
        WorkItem("third", 2, 10, (0.001, 3)),
    ]


def test_executor_is_bounded_and_reduces_deterministically():
    values = []
    report = LocalExecutor(
        ExecutionConfig(workers=2, memory_limit_bytes=20, max_in_flight=2)
    ).run(_items(), _delayed_worker, lambda result: values.append(result.value))

    assert values == [2, 4, 6]
    assert report.task_count == report.submitted == report.completed == 3
    assert report.peak_admitted_bytes <= 20


def test_executor_rejects_single_oversized_item():
    item = WorkItem("large", 0, 11, (0, 1))
    with pytest.raises(WorkMemoryError, match="large"):
        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=10)).run(
            [item], _delayed_worker, lambda result: None
        )


def test_checkpoint_resume_skips_completed_work(tmp_path):
    items = _items()
    fingerprint = execution_fingerprint(
        algorithm="test",
        version="1",
        prepared_manifest={"version": 4},
        parameters={"x": 1},
        work_items=items,
    )
    checkpoint = CheckpointStore(
        tmp_path / "resume", fingerprint, JsonCheckpointCodec()
    )

    def interrupt(_result):
        raise RuntimeError("stop after durable result")

    with pytest.raises(RuntimeError, match="durable"):
        LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=20)).run(
            items, _delayed_worker, interrupt, checkpoint=checkpoint
        )

    resumed = []
    report = LocalExecutor(ExecutionConfig(workers=1, memory_limit_bytes=20)).run(
        items,
        _delayed_worker,
        lambda result: resumed.append(result.value),
        checkpoint=CheckpointStore(
            tmp_path / "resume", fingerprint, JsonCheckpointCodec()
        ),
    )
    assert resumed == [2, 4, 6]
    assert report.resumed == 1
    assert report.submitted == 2


def test_atomic_output_directory_publishes_or_cleans_up(tmp_path):
    target = tmp_path / "output"
    publication = AtomicOutputDirectory(target)
    with publication as staging:
        (staging / "result.json").write_text(json.dumps({"ok": True}))
        publication.publish(("result.json",))
    assert json.loads((target / "result.json").read_text()) == {"ok": True}

    failed = tmp_path / "failed"
    with pytest.raises(RuntimeError), AtomicOutputDirectory(failed) as staging:
        (staging / "partial").write_text("partial")
        raise RuntimeError("failure")
    assert not failed.exists()
    assert not list(tmp_path.glob(".failed.tmp-*"))


def test_worker_failure_discards_staged_output(tmp_path):
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
