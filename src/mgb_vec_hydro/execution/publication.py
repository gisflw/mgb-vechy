"""Private staging and single-rename publication for output directories."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from mgb_vec_hydro.exceptions import PublicationError


class AtomicOutputDirectory:
    """Build a new output directory privately and publish it as one unit."""

    def __init__(self, target: str | Path):
        self.target = Path(target)
        self.staging: Path | None = None
        self._published = False

    def __enter__(self) -> Path:
        if self.target.exists():
            raise PublicationError(f"Output directory already exists: {self.target}")
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.staging = Path(
            tempfile.mkdtemp(prefix=f".{self.target.name}.tmp-", dir=self.target.parent)
        )
        return self.staging

    def publish(self, expected: tuple[str | Path, ...] = ()) -> Path:
        if self.staging is None:
            raise PublicationError("Output staging directory is not open")
        safe_expected = []
        for path in expected:
            relative = Path(path)
            candidate = (self.staging / relative).resolve()
            try:
                candidate.relative_to(self.staging.resolve())
            except ValueError as exc:
                raise PublicationError(
                    f"Expected output escapes staging directory: {path}"
                ) from exc
            safe_expected.append((relative, candidate))
        missing = [
            str(path) for path, candidate in safe_expected if not candidate.is_file()
        ]
        if missing:
            raise PublicationError(
                "Staged output is missing expected file(s): " + ", ".join(missing)
            )
        if self.target.exists():
            raise PublicationError(f"Output directory already exists: {self.target}")
        try:
            os.replace(self.staging, self.target)
        except OSError as exc:
            raise PublicationError(
                f"Cannot publish output directory: {self.target}"
            ) from exc
        self._published = True
        return self.target

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.staging is not None and not self._published:
            shutil.rmtree(self.staging, ignore_errors=True)
