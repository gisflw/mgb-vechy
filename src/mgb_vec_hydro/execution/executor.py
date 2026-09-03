"""Bounded local-process execution.

This module deliberately knows nothing about hydrology.  Scientific stages
provide small serializable work payloads and a top-level worker callable.
"""

from __future__ import annotations

import multiprocessing as mp
import multiprocessing.reduction
import pickle
import queue
import time
import traceback
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from mgb_vec_hydro.exceptions import (
    ExecutionCancelledError,
    ExecutionConfigurationError,
    WorkerExecutionError,
    WorkMemoryError,
)

PayloadT = TypeVar("PayloadT")
ResultT = TypeVar("ResultT")

if TYPE_CHECKING:
    from mgb_vec_hydro.execution.checkpoints import CheckpointStore


@dataclass(frozen=True)
class ExecutionConfig:
    """Resource limits for one local execution run."""

    workers: int
    memory_limit_bytes: int
    max_in_flight: int | None = None
    io_slots: int | None = None
    resource_cache_size: int = 8

    def __post_init__(self) -> None:
        integer_fields = {
            "workers": self.workers,
            "memory_limit_bytes": self.memory_limit_bytes,
            "resource_cache_size": self.resource_cache_size,
        }
        if self.max_in_flight is not None:
            integer_fields["max_in_flight"] = self.max_in_flight
        if self.io_slots is not None:
            integer_fields["io_slots"] = self.io_slots
        invalid = [
            name
            for name, value in integer_fields.items()
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ]
        if invalid:
            raise ExecutionConfigurationError(
                "Execution limits must be positive integers: " + ", ".join(invalid)
            )

    @property
    def in_flight_limit(self) -> int:
        return self.max_in_flight or self.workers

    @property
    def io_limit(self) -> int:
        return self.io_slots or self.workers


@dataclass(frozen=True)
class WorkItem(Generic[PayloadT]):
    """One deterministic, independently executable piece of work."""

    key: str
    ordinal: int
    estimated_bytes: int
    payload: PayloadT

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ExecutionConfigurationError("Work item keys must be non-empty text")
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
        ):
            raise ExecutionConfigurationError("Work item ordinals must be non-negative")
        if (
            isinstance(self.estimated_bytes, bool)
            or not isinstance(self.estimated_bytes, int)
            or self.estimated_bytes <= 0
        ):
            raise ExecutionConfigurationError(
                "Work item memory estimates must be positive"
            )


@dataclass(frozen=True)
class WorkerOutput(Generic[ResultT]):
    """Optional rich return value from a worker callable."""

    value: ResultT
    timings: Mapping[str, float] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkResult(Generic[ResultT]):
    key: str
    ordinal: int
    value: ResultT
    timings: Mapping[str, float] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskFailure:
    key: str
    ordinal: int
    exception_type: str
    message: str
    remote_traceback: str


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    key: str | None
    ordinal: int | None
    submitted: int
    completed: int
    reduced: int
    resumed: int
    admitted_bytes: int


@dataclass(frozen=True)
class ExecutionReport:
    task_count: int
    submitted: int
    completed: int
    reduced: int
    resumed: int
    peak_admitted_bytes: int
    wall_seconds: float
    timings: Mapping[str, float]
    worker_diagnostics: tuple[Mapping[str, Any], ...]
    cancelled: bool = False
    failures: tuple[TaskFailure, ...] = ()


class WorkerResourceCache:
    """Small LRU cache of context-managed resources in one worker process."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._resources: OrderedDict[str, tuple[Any, Callable[[], Any]]] = OrderedDict()

    def get(self, key: str, opener: Callable[[], Any]) -> Any:
        if key in self._resources:
            value = self._resources.pop(key)
            self._resources[key] = value
            return value[0]
        resource = opener()
        if hasattr(resource, "__enter__") and hasattr(resource, "__exit__"):
            value = resource.__enter__()
            closer = lambda: resource.__exit__(None, None, None)
        else:
            value = resource
            closer = getattr(resource, "close", lambda: None)
        self._resources[key] = (value, closer)
        while len(self._resources) > self.capacity:
            _, (_, old_closer) = self._resources.popitem(last=False)
            old_closer()
        return value

    def close(self) -> None:
        while self._resources:
            _, (_, closer) = self._resources.popitem(last=False)
            closer()


class WorkerContext:
    """Resources and shared I/O admission made available to worker functions."""

    def __init__(self, io_semaphore, resource_cache_size: int):
        self._io_semaphore = io_semaphore
        self.resources = WorkerResourceCache(resource_cache_size)

    @contextmanager
    def io_bound(self):
        self._io_semaphore.acquire()
        try:
            yield
        finally:
            self._io_semaphore.release()

    def close(self) -> None:
        self.resources.close()


@dataclass(frozen=True)
class _WorkerFailure:
    item: WorkItem[Any]
    exception_type: str
    message: str
    remote_traceback: str


def _worker_loop(task_queue, result_queue, io_semaphore, cache_size, worker) -> None:
    context = WorkerContext(io_semaphore, cache_size)
    try:
        while True:
            item = task_queue.get()
            if item is None:
                return
            try:
                started = time.perf_counter()
                raw = worker(item.payload, context)
                elapsed = time.perf_counter() - started
                if isinstance(raw, WorkerOutput):
                    timings = dict(raw.timings)
                    timings.setdefault("worker", elapsed)
                    result = WorkResult(
                        item.key, item.ordinal, raw.value, timings, raw.diagnostics
                    )
                else:
                    result = WorkResult(
                        item.key, item.ordinal, raw, {"worker": elapsed}, {}
                    )
                multiprocessing.reduction.ForkingPickler.dumps(result)
                result_queue.put(result)
            except BaseException as exc:  # noqa: BLE001 - report interrupts from workers
                result_queue.put(
                    _WorkerFailure(
                        item,
                        type(exc).__name__,
                        str(exc),
                        traceback.format_exc(),
                    )
                )
                return
    finally:
        context.close()


class LocalExecutor:
    """Run a lazy plan with persistent spawned workers and bounded buffering."""

    def __init__(self, config: ExecutionConfig):
        self.config = config

    def run(
        self,
        items: Iterable[WorkItem[PayloadT]],
        worker: Callable[[PayloadT, WorkerContext], ResultT | WorkerOutput[ResultT]],
        reduce: Callable[[WorkResult[ResultT]], Mapping[str, float] | None],
        *,
        checkpoint: CheckpointStore[ResultT] | None = None,
        progress: Callable[[ProgressEvent], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ExecutionReport:
        started = time.perf_counter()
        _require_picklable(worker, "Worker callable")
        ctx = mp.get_context("spawn")
        task_queue = ctx.Queue(maxsize=self.config.in_flight_limit)
        result_queue = ctx.Queue(maxsize=self.config.in_flight_limit)
        io_semaphore = ctx.BoundedSemaphore(self.config.io_limit)
        processes = [
            ctx.Process(
                target=_worker_loop,
                args=(
                    task_queue,
                    result_queue,
                    io_semaphore,
                    self.config.resource_cache_size,
                    worker,
                ),
                name=f"mgb-worker-{index}",
            )
            for index in range(self.config.workers)
        ]
        try:
            for process in processes:
                process.start()
        except BaseException:
            self._terminate(processes)
            raise

        iterator = iter(items)
        source_done = False
        next_item: WorkItem[PayloadT] | None = None
        expected_ordinal = 0
        next_reduce = 0
        admitted: dict[int, WorkItem[PayloadT]] = {}
        resumed_pending: dict[int, WorkItem[PayloadT]] = {}
        buffered: dict[int, WorkResult[ResultT]] = {}
        admitted_bytes = 0
        peak_admitted = 0
        submitted = completed = reduced = resumed = task_count = 0
        failures: list[TaskFailure] = []
        timings: defaultdict[str, float] = defaultdict(float)
        diagnostics: list[Mapping[str, Any]] = []

        def event(kind: str, item: WorkItem[Any] | None = None) -> None:
            if progress is not None:
                progress(
                    ProgressEvent(
                        kind,
                        item.key if item else None,
                        item.ordinal if item else None,
                        submitted,
                        completed,
                        reduced,
                        resumed,
                        admitted_bytes,
                    )
                )

        def report(*, was_cancelled=False) -> ExecutionReport:
            return ExecutionReport(
                task_count,
                submitted,
                completed,
                reduced,
                resumed,
                peak_admitted,
                time.perf_counter() - started,
                dict(timings),
                tuple(diagnostics),
                was_cancelled,
                tuple(failures),
            )

        try:
            event("started")
            while not source_done or admitted or buffered or next_item is not None:
                if cancelled is not None and cancelled():
                    raise ExecutionCancelledError("Execution was cancelled")

                made_progress = False
                while next_reduce in buffered:
                    result = buffered.pop(next_reduce)
                    item = admitted.pop(next_reduce, None)
                    was_submitted = item is not None
                    if item is None:
                        item = resumed_pending.pop(next_reduce)
                    if checkpoint is not None and was_submitted:
                        checkpoint_started = time.perf_counter()
                        checkpoint.save(item, result)
                        timings["checkpoint_write"] += (
                            time.perf_counter() - checkpoint_started
                        )
                    write_started = time.perf_counter()
                    reduction_timings = reduce(result)
                    timings["reduction"] += time.perf_counter() - write_started
                    if reduction_timings:
                        for name, value in reduction_timings.items():
                            timings[name] += float(value)
                    for name, value in result.timings.items():
                        timings[name] += float(value)
                    if result.diagnostics:
                        diagnostics.append(dict(result.diagnostics))
                    if was_submitted:
                        admitted_bytes -= item.estimated_bytes
                    reduced += 1
                    next_reduce += 1
                    event("reduced", item)
                    made_progress = True

                while len(admitted) + len(buffered) < self.config.in_flight_limit:
                    if next_item is None and not source_done:
                        try:
                            planning_started = time.perf_counter()
                            next_item = next(iterator)
                            timings["planning"] += (
                                time.perf_counter() - planning_started
                            )
                        except StopIteration:
                            timings["planning"] += (
                                time.perf_counter() - planning_started
                            )
                            source_done = True
                            break
                        if next_item.ordinal != expected_ordinal:
                            raise ExecutionConfigurationError(
                                "Work item ordinals must be contiguous and start at zero"
                            )
                        expected_ordinal += 1
                        task_count += 1
                        if next_item.estimated_bytes > self.config.memory_limit_bytes:
                            raise WorkMemoryError(
                                f"Work item {next_item.key} requires "
                                f"{next_item.estimated_bytes} bytes, exceeding the "
                                f"{self.config.memory_limit_bytes}-byte budget"
                            )
                    if next_item is None:
                        break
                    if (
                        admitted_bytes + next_item.estimated_bytes
                        > self.config.memory_limit_bytes
                    ):
                        break
                    item = next_item
                    next_item = None
                    if checkpoint is not None and checkpoint.contains(item):
                        buffered[item.ordinal] = checkpoint.load(item)
                        resumed_pending[item.ordinal] = item
                        resumed += 1
                        event("resumed", item)
                    else:
                        _require_picklable(item, f"Work item {item.key}")
                        task_queue.put(item)
                        admitted[item.ordinal] = item
                        admitted_bytes += item.estimated_bytes
                        peak_admitted = max(peak_admitted, admitted_bytes)
                        submitted += 1
                        event("submitted", item)
                    made_progress = True

                if buffered and next_reduce in buffered:
                    continue
                if admitted:
                    coordination_started = time.perf_counter()
                    try:
                        outcome = result_queue.get(timeout=0.1)
                    except queue.Empty:
                        timings["coordination"] += (
                            time.perf_counter() - coordination_started
                        )
                        dead = [p for p in processes if not p.is_alive()]
                        if dead:
                            raise WorkerExecutionError(
                                f"Worker process exited unexpectedly with code {dead[0].exitcode}"
                            )
                        continue
                    timings["coordination"] += (
                        time.perf_counter() - coordination_started
                    )
                    if isinstance(outcome, _WorkerFailure):
                        failure = TaskFailure(
                            outcome.item.key,
                            outcome.item.ordinal,
                            outcome.exception_type,
                            outcome.message,
                            outcome.remote_traceback,
                        )
                        failures.append(failure)
                        raise WorkerExecutionError(
                            f"Work item {failure.key} failed: "
                            f"{failure.exception_type}: {failure.message}"
                        )
                    buffered[outcome.ordinal] = outcome
                    completed += 1
                    event("completed", admitted.get(outcome.ordinal))
                    continue
                if not made_progress and source_done and next_item is None:
                    break

            for _ in processes:
                task_queue.put(None)
            for process in processes:
                process.join()
            bad = [process for process in processes if process.exitcode != 0]
            if bad:
                raise WorkerExecutionError(
                    f"Worker process exited unexpectedly with code {bad[0].exitcode}"
                )
            final_report = report()
            event("finished")
            return final_report
        except KeyboardInterrupt as exc:
            self._terminate(processes)
            error = ExecutionCancelledError(
                "Execution was interrupted", report=report(was_cancelled=True)
            )
            raise error from exc
        except ExecutionCancelledError as exc:
            self._terminate(processes)
            exc.report = report(was_cancelled=True)
            raise
        except BaseException as exc:
            self._terminate(processes)
            if isinstance(exc, WorkerExecutionError):
                exc.report = report()
            raise
        finally:
            task_queue.close()
            result_queue.close()

    @staticmethod
    def _terminate(processes) -> None:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()


def _require_picklable(value: Any, label: str) -> None:
    try:
        multiprocessing.reduction.ForkingPickler.dumps(value)
    except (
        AttributeError,
        pickle.PickleError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ExecutionConfigurationError(
            f"{label} must be pickleable for spawned workers"
        ) from exc
