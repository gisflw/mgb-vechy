"""Durable, coordinator-owned checkpoints for bounded execution results."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from mgb_vec_hydro.exceptions import CheckpointError
from mgb_vec_hydro.execution.executor import WorkItem, WorkResult

ResultT = TypeVar("ResultT")


class CheckpointCodec(Protocol[ResultT]):
    """Serialize one bounded result value to a coordinator-owned artifact."""

    suffix: str

    def dump(self, value: ResultT, path: Path) -> None: ...

    def load(self, path: Path) -> ResultT: ...


class JsonCheckpointCodec:
    suffix = ".json"

    def dump(self, value: Any, path: Path) -> None:
        path.write_text(
            json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )

    def load(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))


def execution_fingerprint(
    *,
    algorithm: str,
    version: str,
    prepared_manifest: Mapping[str, Any] | None,
    parameters: Mapping[str, Any],
    work_items: Iterable[WorkItem[Any]],
) -> str:
    """Hash canonical job identity, including the ordered work-plan identity."""

    value = {
        "algorithm": algorithm,
        "version": version,
        "prepared_manifest": prepared_manifest,
        "parameters": parameters,
    }
    digest = hashlib.sha256(b"mgb-execution-fingerprint-v1\n")
    try:
        digest.update(_canonical_json(value))
        digest.update(b"\n")
        for item in work_items:
            digest.update(
                _canonical_json(
                    {
                        "key": item.key,
                        "ordinal": item.ordinal,
                        "estimated_bytes": item.estimated_bytes,
                    }
                )
            )
            digest.update(b"\n")
    except (TypeError, ValueError) as exc:
        raise CheckpointError("Job identity is not canonical JSON") from exc
    return digest.hexdigest()


class CheckpointStore(Generic[ResultT]):
    """Directory of independently atomic result artifacts and markers."""

    VERSION = 1

    def __init__(
        self,
        root: str | Path,
        fingerprint: str,
        codec: CheckpointCodec[ResultT],
    ):
        self.root = Path(root)
        self.fingerprint = fingerprint
        self.codec = codec
        if (
            not isinstance(codec.suffix, str)
            or not codec.suffix.startswith(".")
            or Path(codec.suffix).name != codec.suffix
        ):
            raise CheckpointError(
                "Checkpoint codec suffix must be a safe file extension"
            )
        self.artifacts = self.root / "artifacts"
        self.markers = self.root / "completed"
        self._open()

    def _open(self) -> None:
        header = self.root / "checkpoint.json"
        if self.root.exists():
            try:
                metadata = json.loads(header.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CheckpointError(
                    f"Cannot read checkpoint metadata: {header}"
                ) from exc
            expected = {
                "contract": "mgb-execution-checkpoint",
                "version": self.VERSION,
                "fingerprint": self.fingerprint,
                "codec_suffix": self.codec.suffix,
            }
            if metadata != expected:
                raise CheckpointError("Checkpoint is incompatible with this job")
        else:
            self.root.mkdir(parents=True)
            self.artifacts.mkdir()
            self.markers.mkdir()
            self._atomic_json(
                header,
                {
                    "contract": "mgb-execution-checkpoint",
                    "version": self.VERSION,
                    "fingerprint": self.fingerprint,
                    "codec_suffix": self.codec.suffix,
                },
            )
        if not self.artifacts.is_dir() or not self.markers.is_dir():
            raise CheckpointError("Checkpoint is missing artifact directories")

    def _stem(self, item: WorkItem[Any]) -> str:
        digest = hashlib.sha256(item.key.encode("utf-8")).hexdigest()[:20]
        return f"{item.ordinal:012d}-{digest}"

    def contains(self, item: WorkItem[Any]) -> bool:
        return (self.markers / f"{self._stem(item)}.json").is_file()

    def save(self, item: WorkItem[Any], result: WorkResult[ResultT]) -> None:
        stem = self._stem(item)
        artifact = self.artifacts / f"{stem}{self.codec.suffix}"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{stem}-", suffix=self.codec.suffix, dir=self.artifacts
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            self.codec.dump(result.value, temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            digest = _file_sha256(temporary)
            os.replace(temporary, artifact)
            self._atomic_json(
                self.markers / f"{stem}.json",
                {
                    "key": item.key,
                    "ordinal": item.ordinal,
                    "estimated_bytes": item.estimated_bytes,
                    "artifact": artifact.name,
                    "sha256": digest,
                    "timings": dict(result.timings),
                    "diagnostics": dict(result.diagnostics),
                },
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            artifact.unlink(missing_ok=True)
            raise

    def load(self, item: WorkItem[Any]) -> WorkResult[ResultT]:
        marker_path = self.markers / f"{self._stem(item)}.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"Cannot read checkpoint marker: {marker_path}"
            ) from exc
        identity = (
            marker.get("key"),
            marker.get("ordinal"),
            marker.get("estimated_bytes"),
        )
        if identity != (item.key, item.ordinal, item.estimated_bytes):
            raise CheckpointError(f"Checkpoint work identity mismatch for {item.key}")
        artifact_name = marker.get("artifact")
        if (
            not isinstance(artifact_name, str)
            or Path(artifact_name).name != artifact_name
        ):
            raise CheckpointError(f"Invalid checkpoint artifact for {item.key}")
        artifact = self.artifacts / artifact_name
        try:
            if _file_sha256(artifact) != marker["sha256"]:
                raise CheckpointError(f"Checkpoint artifact is corrupt for {item.key}")
            value = self.codec.load(artifact)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointError(
                f"Cannot load checkpoint artifact for {item.key}"
            ) from exc
        return WorkResult(
            item.key,
            item.ordinal,
            value,
            marker.get("timings", {}),
            marker.get("diagnostics", {}),
        )

    def cleanup(self) -> None:
        expected = {"checkpoint.json", "artifacts", "completed"}
        try:
            actual = {path.name for path in self.root.iterdir()}
        except OSError as exc:
            raise CheckpointError(
                f"Cannot inspect checkpoint directory: {self.root}"
            ) from exc
        if actual != expected:
            raise CheckpointError(
                "Refusing to clean a checkpoint directory with unexpected contents"
            )
        shutil.rmtree(self.artifacts)
        shutil.rmtree(self.markers)
        (self.root / "checkpoint.json").unlink()
        self.root.rmdir()

    @staticmethod
    def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CheckpointError(f"Cannot read checkpoint artifact: {path}") from exc
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
