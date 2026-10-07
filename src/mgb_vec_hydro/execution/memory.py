"""Soft memory sizing and bounded Arrow scratch storage."""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyogrio
from pyarrow import ipc
from rasterio.env import get_gdal_config, set_gdal_config

from mgb_vec_hydro.exceptions import ExecutionConfigurationError
from mgb_vec_hydro.execution.executor import ExecutionConfig


@dataclass(frozen=True)
class MemorySizing:
    """Independent sizing hints, not partitions of an RSS ceiling."""

    limit_bytes: int
    workers: int

    def __post_init__(self):
        ExecutionConfig(self.workers, self.limit_bytes)

    @property
    def coordinator_cache_bytes(self):
        return self.limit_bytes // 8

    @property
    def worker_cache_bytes(self):
        return self.limit_bytes // (8 * self.workers)

    @property
    def packet_bytes(self):
        return max(1, self.limit_bytes // (2 * self.workers))


@contextmanager
def raster_cache(cache_bytes):
    previous = get_gdal_config("GDAL_CACHEMAX")
    set_gdal_config("GDAL_CACHEMAX", cache_bytes)
    try:
        yield
    finally:
        set_gdal_config("GDAL_CACHEMAX", previous)


@contextmanager
def sqlite_cache(cache_bytes):
    previous = pyogrio.get_gdal_config_option("OGR_SQLITE_CACHE")
    # OGR_SQLITE_CACHE is expressed in MiB, unlike GDAL_CACHEMAX's bytes.
    pyogrio.set_gdal_config_options({"OGR_SQLITE_CACHE": cache_bytes / 1024**2})
    try:
        yield
    finally:
        pyogrio.set_gdal_config_options({"OGR_SQLITE_CACHE": previous})


class ArrowPacketStore:
    """Keep complete Arrow backing buffers within budget; spill the rest to IPC."""

    def __init__(self, root: Path, memory_limit_bytes: int):
        if (
            isinstance(memory_limit_bytes, bool)
            or not isinstance(memory_limit_bytes, int)
            or memory_limit_bytes < 0
        ):
            raise ExecutionConfigurationError("Scratch memory must be non-negative")
        self.root = root
        self.root.mkdir()
        self.limit_bytes = memory_limit_bytes
        self.retained_bytes = 0
        self.peak_retained_bytes = 0
        self._tables = {}
        self.keys = []

    def put(self, key: int, table: pa.Table):
        if key in self.keys:
            raise ExecutionConfigurationError(f"Duplicate scratch packet: {key}")
        self.keys.append(key)
        size = table.get_total_buffer_size()
        if self.retained_bytes + size <= self.limit_bytes:
            self._tables[key] = table
            self.retained_bytes += size
            self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)
        else:
            with (
                self._path(key).open("wb") as stream,
                ipc.new_file(stream, table.schema) as writer,
            ):
                writer.write_table(table)

    def pop(self, key: int):
        if key in self._tables:
            table = self._tables.pop(key)
            self.retained_bytes -= table.get_total_buffer_size()
            return table
        path = self._path(key)
        with path.open("rb") as stream:
            table = ipc.open_file(stream).read_all()
        path.unlink()
        return table

    def _path(self, key):
        return self.root / f"{key:012d}.arrow"

    def close(self):
        self._tables.clear()
        self.retained_bytes = 0
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
