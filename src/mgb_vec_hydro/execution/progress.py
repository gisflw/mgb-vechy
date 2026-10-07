"""Coordinator-only stage progress and non-overlapping elapsed timings."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .executor import ProgressEvent


@dataclass(frozen=True)
class StageProgress:
    phase: str
    completed: int = 0
    total: int | None = None


ProgressCallback = Callable[[StageProgress], None]


class StageReporter:
    def __init__(self, progress: ProgressCallback | None = None):
        self.progress = progress
        self.started = self.boundary = time.perf_counter()
        self.phase = "preparing"
        self.total: int | None = None
        self.timings: dict[str, float] = {}
        self._emit(0)

    def _emit(self, completed: int) -> None:
        if self.progress is not None:
            self.progress(StageProgress(self.phase, completed, self.total))

    def enter(self, phase: str, total: int | None = None) -> None:
        now = time.perf_counter()
        self.timings[f"{self.phase}_wall"] = now - self.boundary
        self.boundary = now
        self.phase, self.total = phase, total
        self._emit(0)

    def execution_progress(self, event: ProgressEvent) -> None:
        if event.kind == "reduced":
            self._emit(event.reduced)

    def finish(self, timings: dict[str, float]) -> None:
        now = time.perf_counter()
        self.timings[f"{self.phase}_wall"] = now - self.boundary
        timings.update(self.timings)
        timings["total"] = now - self.started
