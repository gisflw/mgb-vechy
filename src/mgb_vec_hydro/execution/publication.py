"""Private staging and rollback-safe publication of output files."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from mgb_vec_hydro.exceptions import PublicationError


class AtomicOutputDirectory:
    """Stage outputs privately, preserving unrelated files in a shared directory."""

    def __init__(self, target: str | Path, *, overwrite: bool = False):
        self.target = Path(target)
        self.overwrite = overwrite
        self.staging: Path | None = None
        self._published = False

    def __enter__(self) -> Path:
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
            candidate.relative_to(self.staging.resolve())
            safe_expected.append((relative, candidate))
        missing = [
            str(path) for path, candidate in safe_expected if not candidate.is_file()
        ]
        if missing:
            raise PublicationError(
                "Staged output is missing expected file(s): " + ", ".join(missing)
            )
        if safe_expected:
            expected_files = {candidate for _, candidate in safe_expected}
            actual_files = {
                path.resolve()
                for path in self.staging.rglob("*")
                if path.is_file()
            }
            nested_directories = [
                path.relative_to(self.staging)
                for path in self.staging.rglob("*")
                if path.is_dir()
            ]
            extras = sorted(
                str(path.relative_to(self.staging))
                for path in actual_files - expected_files
            )
            if nested_directories:
                raise PublicationError(
                    "Published output must be a flat directory; staged directory(s): "
                    + ", ".join(str(path) for path in sorted(nested_directories))
                )
            if extras:
                raise PublicationError(
                    "Staged output contains unexpected file(s): " + ", ".join(extras)
                )
        if not self.target.exists():
            os.replace(self.staging, self.target)
        else:
            files = list(self.staging.iterdir())
            conflicts = [
                self.target / path.name for path in files
                if (self.target / path.name).exists()
            ]
            if conflicts and not self.overwrite:
                raise PublicationError(
                    "Output files already exist: " + ", ".join(map(str, conflicts))
                )
            if any(not path.is_file() for path in conflicts):
                raise PublicationError("Output file path is occupied by a directory")
            # Keep replaced files until the entire stage has been published.
            with tempfile.TemporaryDirectory(dir=self.target.parent) as backup_dir:
                backup = Path(backup_dir)
                moved = []
                published = []
                try:
                    for path in conflicts:
                        os.replace(path, backup / path.name)
                        moved.append(path)
                    for path in files:
                        destination = self.target / path.name
                        os.replace(path, destination)
                        published.append(destination)
                except OSError as error:
                    for path in published:
                        path.unlink()
                    for path in moved:
                        os.replace(backup / path.name, path)
                    raise PublicationError(f"Could not publish outputs: {error}") from error
            self.staging.rmdir()
        self._published = True
        return self.target

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.staging is not None and not self._published:
            shutil.rmtree(self.staging, ignore_errors=True)
