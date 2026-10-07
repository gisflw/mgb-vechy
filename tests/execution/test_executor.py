import json
import time

import pytest

from mgb_vec_hydro.exceptions import WorkerExecutionError, WorkMemoryError
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


def test_publication_reuses_directory_and_rolls_back_replacements(tmp_path, monkeypatch):
    import os
    from mgb_vec_hydro.exceptions import PublicationError

    target = tmp_path / "output"
    target.mkdir()
    (target / "other-stage.txt").write_text("keep")
    publication = AtomicOutputDirectory(target)
    with publication as staging:
        (staging / "result.txt").write_text("old")
        publication.publish(("result.txt",))
    with pytest.raises(PublicationError, match="Output files already exist"):
        publication = AtomicOutputDirectory(target)
        with publication as staging:
            (staging / "result.txt").write_text("unconfirmed")
            publication.publish(("result.txt",))
    assert (target / "result.txt").read_text() == "old"

    replace = os.replace

    def fail_new_file(source, destination):
        if str(destination) == str(target / "new.txt"):
            raise OSError("publication failed")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_new_file)
    with pytest.raises(PublicationError, match="publication failed"):
        publication = AtomicOutputDirectory(target, overwrite=True)
        with publication as staging:
            (staging / "result.txt").write_text("replacement")
            (staging / "new.txt").write_text("new")
            publication.publish(("result.txt", "new.txt"))
    assert (target / "result.txt").read_text() == "old"
    assert not (target / "new.txt").exists()
    monkeypatch.setattr(os, "replace", replace)
    publication = AtomicOutputDirectory(target, overwrite=True)
    with publication as staging:
        (staging / "result.txt").write_text("replacement")
        publication.publish(("result.txt",))
    assert (target / "result.txt").read_text() == "replacement"
    assert (target / "other-stage.txt").read_text() == "keep"
    assert not list(tmp_path.glob(".output.tmp-*"))


def test_buffered_results_are_admitted_only_once():
    events = []
    items = [WorkItem(str(i), i, 1, (delay, i))
             for i, delay in enumerate((0.4, 1.0, 0.001, 0.001, 0.001))]
    LocalExecutor(ExecutionConfig(workers=3, memory_limit_bytes=4, max_in_flight=4)).run(
        items, _delayed_worker, lambda result: None, progress=events.append,
    )
    submitted = next(i for i, event in enumerate(events) if event.kind == "submitted" and event.ordinal == 4)
    completed = next(i for i, event in enumerate(events) if event.kind == "completed" and event.ordinal == 1)
    assert submitted < completed
    assert max(event.admitted_bytes for event in events) <= 4
