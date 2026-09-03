"""Canonical-grid raster planning, cached reads, and exclusive assembly."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import CRS
from rasterio.enums import MaskFlags, Resampling
from rasterio.shutil import copy as copy_raster
from rasterio.transform import Affine
from rasterio.windows import Window, from_bounds

from mgb_vec_hydro.exceptions import (
    PreparedDataError,
    RasterGridError,
    RasterWriteConflictError,
    WorkMemoryError,
)
from mgb_vec_hydro.execution.executor import WorkerContext
from mgb_vec_hydro.preparation import (
    BLOCK_SIZE,
    NAME_RE,
    GridSpec,
    PreparedDataset,
)

Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class RasterUnit:
    key: str
    bounds: Bounds
    window: Window
    estimated_bytes: int
    spatial_key: int


@dataclass(frozen=True)
class RasterPacket:
    key: str
    units: tuple[RasterUnit, ...]
    estimated_bytes: int


@dataclass(frozen=True)
class RasterPatch:
    product: str
    window: Window
    data: np.ndarray
    valid: np.ndarray


@dataclass(frozen=True)
class RasterProductSpec:
    name: str
    dtype: str
    overview_resampling: Resampling = Resampling.nearest
    tags: dict[str, Any] = field(default_factory=dict)


def prepared_grid(prepared_root: str | Path) -> GridSpec:
    dataset = PreparedDataset.open(prepared_root)
    dataset.validate()
    grid = dataset.manifest["grid"]
    try:
        result = GridSpec(
            CRS.from_wkt(grid["crs_wkt"]),
            Affine(*grid["transform"]),
            int(grid["width"]),
            int(grid["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RasterGridError("Prepared canonical grid is invalid") from exc
    if (
        result.width <= 0
        or result.height <= 0
        or result.transform.b != 0
        or result.transform.d != 0
        or result.transform.a <= 0
        or result.transform.e >= 0
    ):
        raise RasterGridError("Prepared canonical grid must be north-up and non-empty")
    return result


def plan_raster_units(
    grid: GridSpec,
    units: Iterable[tuple[str, Bounds]],
    *,
    bytes_per_cell: int,
    working_factor: float = 1.0,
    fixed_bytes: int = 0,
    block_size: int = BLOCK_SIZE,
) -> tuple[RasterUnit, ...]:
    """Convert complete-unit bounds to windows and deterministic spatial order."""

    if bytes_per_cell <= 0 or working_factor <= 0 or fixed_bytes < 0 or block_size <= 0:
        raise RasterGridError("Raster memory-estimation parameters are invalid")
    planned = []
    keys = set()
    for key, bounds in units:
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(bounds, (tuple, list))
            or len(bounds) != 4
            or not all(math.isfinite(x) for x in bounds)
        ):
            raise RasterGridError("Raster unit keys and bounds must be valid")
        if key in keys:
            raise RasterGridError(f"Duplicate raster unit key: {key}")
        keys.add(key)
        window = _covering_window(grid, bounds)
        cells = int(window.width * window.height)
        estimate = max(
            1, math.ceil(cells * bytes_per_cell * working_factor) + fixed_bytes
        )
        center_col = int(window.col_off + window.width // 2) // block_size
        center_row = int(window.row_off + window.height // 2) // block_size
        planned.append(
            RasterUnit(
                key,
                bounds,
                window,
                estimate,
                _morton_key(center_col, center_row),
            )
        )
    return tuple(sorted(planned, key=lambda unit: (unit.spatial_key, unit.key)))


def packet_raster_units(
    units: Iterable[RasterUnit],
    *,
    memory_limit_bytes: int,
    max_units: int | None = None,
) -> tuple[RasterPacket, ...]:
    """Packet adjacent complete units without exceeding declared result memory."""

    if memory_limit_bytes <= 0 or (max_units is not None and max_units <= 0):
        raise RasterGridError("Raster packet limits must be positive")
    packets: list[RasterPacket] = []
    current: list[RasterUnit] = []
    current_bytes = 0
    for unit in sorted(units, key=lambda value: (value.spatial_key, value.key)):
        if unit.estimated_bytes > memory_limit_bytes:
            raise WorkMemoryError(
                f"Raster unit {unit.key} requires {unit.estimated_bytes} bytes, "
                f"exceeding the {memory_limit_bytes}-byte packet budget"
            )
        full = max_units is not None and len(current) >= max_units
        if current and (
            full or current_bytes + unit.estimated_bytes > memory_limit_bytes
        ):
            packets.append(_packet(current, current_bytes))
            current, current_bytes = [], 0
        current.append(unit)
        current_bytes += unit.estimated_bytes
    if current:
        packets.append(_packet(current, current_bytes))
    return tuple(packets)


class PreparedRasterReader:
    """Read exact windows from manifest-declared COGs, caching worker handles."""

    def __init__(self, prepared_root: str | Path, context: WorkerContext):
        self.root = Path(prepared_root).resolve()
        self.context = context
        self.grid = prepared_grid(self.root)
        self.dataset = PreparedDataset.open(self.root)

    def source(self, name: str):
        try:
            asset = self.dataset.manifest["assets"]["rasters"][name]
        except KeyError as exc:
            raise PreparedDataError(f"Unknown prepared raster asset: {name}") from exc
        if asset.get("driver") != "COG":
            raise PreparedDataError(f"Prepared raster {name} is not a COG")
        path = self.dataset.asset_path(asset["path"])
        key = f"prepared-raster:{path}"

        def open_source():
            source = rasterio.open(path)
            try:
                _require_grid(source, self.grid, name)
            except Exception:
                source.close()
                raise
            return source

        return self.context.resources.get(key, open_source)

    def read(self, name: str, window: Window, *, masked: bool = True) -> np.ndarray:
        source = self.source(name)
        _require_integer_window(window, self.grid)
        with self.context.io_bound():
            return source.read(1, window=window, masked=masked)


class RasterAssembler:
    """Single-process writer for non-overlapping patches and final COGs."""

    def __init__(
        self,
        staging_dir: str | Path,
        grid: GridSpec,
        products: Iterable[RasterProductSpec],
        *,
        block_size: int = BLOCK_SIZE,
    ):
        self.root = Path(staging_dir)
        self.grid = grid
        self.block_size = block_size
        product_list = tuple(products)
        self.specs = {spec.name: spec for spec in product_list}
        if not self.specs or len(self.specs) != len(product_list):
            raise RasterGridError(
                "At least one uniquely named raster product is required"
            )
        invalid_names = [
            spec.name
            for spec in product_list
            if not isinstance(spec.name, str) or not NAME_RE.fullmatch(spec.name)
        ]
        if invalid_names:
            raise RasterGridError(
                "Invalid raster product name(s): " + ", ".join(invalid_names)
            )
        if block_size < 128 or block_size > 4096 or block_size & (block_size - 1):
            raise RasterGridError(
                "Raster block size must be a power of two from 128 through 4096"
            )
        for spec in product_list:
            try:
                np.dtype(spec.dtype)
            except TypeError as exc:
                raise RasterGridError(
                    f"Invalid dtype for raster product {spec.name}: {spec.dtype}"
                ) from exc
        self.root.mkdir(parents=True, exist_ok=True)
        self._sources: dict[str, Any] = {}
        self._finished = False
        try:
            for spec in self.specs.values():
                path = self.root / f".{spec.name}.working.tif"
                source = rasterio.open(
                    path,
                    "w+",
                    driver="GTiff",
                    width=grid.width,
                    height=grid.height,
                    count=1,
                    dtype=np.dtype(spec.dtype).name,
                    crs=grid.crs,
                    transform=grid.transform,
                    tiled=True,
                    blockxsize=block_size,
                    blockysize=block_size,
                    compress="DEFLATE",
                    nodata=None,
                    BIGTIFF="IF_SAFER",
                )
                self._sources[spec.name] = source
                for _, window in source.block_windows(1):
                    source.write_mask(
                        np.zeros(
                            (int(window.height), int(window.width)), dtype="uint8"
                        ),
                        window=window,
                    )
        except Exception:
            self.close()
            raise

    def write(self, patch: RasterPatch) -> None:
        if self._finished:
            raise RasterGridError("Raster assembler is already finalized")
        if patch.product not in self._sources:
            raise RasterGridError(f"Unknown raster product: {patch.product}")
        _require_integer_window(patch.window, self.grid)
        shape = (int(patch.window.height), int(patch.window.width))
        data = np.asarray(patch.data)
        valid = np.asarray(patch.valid, dtype=bool)
        if data.shape != shape or valid.shape != shape:
            raise RasterGridError("Raster patch arrays do not match their window")
        source = self._sources[patch.product]
        existing_valid = source.read_masks(1, window=patch.window) != 0
        if np.any(existing_valid & valid):
            raise RasterWriteConflictError(
                f"Raster patch {patch.product} overlaps previously owned cells"
            )
        existing = source.read(1, window=patch.window)
        existing[valid] = data.astype(source.dtypes[0], copy=False)[valid]
        source.write(existing, 1, window=patch.window)
        source.write_mask(
            ((existing_valid | valid) * 255).astype("uint8"), window=patch.window
        )

    def finish(self) -> dict[str, Path]:
        if self._finished:
            raise RasterGridError("Raster assembler is already finalized")
        self._finished = True
        working = {}
        for name, source in self._sources.items():
            if self.specs[name].tags:
                source.update_tags(**self.specs[name].tags)
            working[name] = Path(source.name)
            source.close()
        self._sources.clear()
        outputs = {}
        try:
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                for name, spec in self.specs.items():
                    output = self.root / f"{name}.tif"
                    copy_raster(
                        working[name],
                        output,
                        driver="COG",
                        BLOCKSIZE=self.block_size,
                        COMPRESS="DEFLATE",
                        BIGTIFF="IF_SAFER",
                        RESAMPLING=spec.overview_resampling.name.upper(),
                        OVERVIEW_RESAMPLING=spec.overview_resampling.name.upper(),
                    )
                    outputs[name] = output
        finally:
            for path in working.values():
                path.unlink(missing_ok=True)
        return outputs

    def close(self) -> None:
        for source in self._sources.values():
            path = Path(source.name)
            source.close()
            path.unlink(missing_ok=True)
        self._sources.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._finished:
            self.close()


def _covering_window(grid: GridSpec, bounds: Bounds) -> Window:
    left, bottom, right, top = bounds
    if left > right or bottom > top:
        raise RasterGridError("Raster unit bounds are inverted")
    raw = from_bounds(left, bottom, right, top, transform=grid.transform)
    col0 = max(0, math.floor(raw.col_off))
    row0 = max(0, math.floor(raw.row_off))
    col1 = min(grid.width, math.ceil(raw.col_off + raw.width))
    row1 = min(grid.height, math.ceil(raw.row_off + raw.height))
    if col0 >= col1 or row0 >= row1:
        raise RasterGridError("Raster unit lies outside the canonical grid")
    return Window(col0, row0, col1 - col0, row1 - row0)


def _require_grid(source, grid: GridSpec, name: str) -> None:
    if (
        source.count != 1
        or source.crs is None
        or CRS.from_user_input(source.crs) != grid.crs
        or source.transform != grid.transform
        or source.width != grid.width
        or source.height != grid.height
        or source.nodata is not None
        or source.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG"
        or MaskFlags.per_dataset not in source.mask_flag_enums[0]
    ):
        raise RasterGridError(
            f"Prepared raster {name} does not match the canonical grid"
        )


def _require_integer_window(window: Window, grid: GridSpec) -> None:
    values = (window.col_off, window.row_off, window.width, window.height)
    if any(int(value) != value for value in values):
        raise RasterGridError("Raster windows must have integer offsets and sizes")
    if (
        window.col_off < 0
        or window.row_off < 0
        or window.width <= 0
        or window.height <= 0
        or window.col_off + window.width > grid.width
        or window.row_off + window.height > grid.height
    ):
        raise RasterGridError("Raster window is outside the canonical grid")


def _packet(units: list[RasterUnit], estimated_bytes: int) -> RasterPacket:
    return RasterPacket(
        f"{units[0].key}..{units[-1].key}", tuple(units), estimated_bytes
    )


def _morton_key(x: int, y: int) -> int:
    result = 0
    for bit in range(32):
        result |= ((x >> bit) & 1) << (2 * bit)
        result |= ((y >> bit) & 1) << (2 * bit + 1)
    return result
