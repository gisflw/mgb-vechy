"""Raster-only staging of canonical prepared datasets."""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import rasterio
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.shutil import copy as copy_raster
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

from mgb_vec_hydro.crs_utils import parse_metric_crs
from mgb_vec_hydro.exceptions import PreparedDataError

CONTRACT = "mgb-prepared-dataset"
CONTRACT_VERSION = 3
BLOCK_SIZE = 512
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
RESERVED_RASTER_NAMES = {"dem", "d8"}


@dataclass(frozen=True)
class NamedRaster:
    """A named single-band raster and its scientific sampling role."""

    name: str
    path: Path
    kind: Literal["continuous", "categorical"]


@dataclass(frozen=True)
class PreparationSpec:
    """Raster inputs and normalization choices for a prepared dataset."""

    dem: Path
    crs: str
    resolution: float
    output_dir: Path
    rasters: tuple[NamedRaster, ...] = field(default_factory=tuple)
    d8: Path | None = None
    d8_encoding: Literal["canonical", "esri"] | None = None
    memory_limit_mb: int = 512


@dataclass(frozen=True)
class PreparationReport:
    """Summary of a successfully published prepared dataset."""

    output_dir: Path
    manifest: Path
    raster_count: int


@dataclass(frozen=True)
class GridSpec:
    """Canonical north-up raster grid."""

    crs: CRS
    transform: Affine
    width: int
    height: int

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        left = self.transform.c
        top = self.transform.f
        right = left + self.width * self.transform.a
        bottom = top + self.height * self.transform.e
        return (left, bottom, right, top)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "crs_wkt": self.crs.to_wkt(version="WKT2_2019", pretty=False),
            "transform": list(self.transform)[:6],
            "extent": list(self.bounds),
            "resolution": self.transform.a,
            "width": self.width,
            "height": self.height,
            "nodata": "internal-mask",
        }


class PreparedDataset:
    """A lightweight handle to a staged dataset directory."""

    def __init__(self, root: Path, manifest: dict[str, Any]):
        self.root = root
        self.manifest = manifest

    @classmethod
    def open(cls, root: str | Path) -> PreparedDataset:
        """Load a manifest without scanning its potentially large assets."""
        root = Path(root)
        manifest_path = root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreparedDataError(
                f"Cannot read prepared manifest: {manifest_path}"
            ) from exc
        return cls(root, manifest)

    def asset_path(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str):
            raise PreparedDataError("Manifest asset path must be text")
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise PreparedDataError(
                f"Manifest asset escapes dataset: {relative_path}"
            ) from exc
        return candidate

    def validate(self) -> None:
        """Check the shallow contract and referenced asset paths."""
        manifest = self.manifest
        if (
            manifest.get("contract") != CONTRACT
            or manifest.get("version") != CONTRACT_VERSION
        ):
            raise PreparedDataError("Unsupported prepared dataset contract or version")
        grid = manifest.get("grid")
        assets = manifest.get("assets")
        if not isinstance(grid, dict) or not isinstance(assets, dict):
            raise PreparedDataError("Prepared manifest is missing grid or assets")
        required_grid = {
            "crs_wkt",
            "transform",
            "extent",
            "resolution",
            "width",
            "height",
            "nodata",
        }
        if not required_grid.issubset(grid):
            raise PreparedDataError("Prepared manifest has an incomplete grid")
        if grid["nodata"] != "internal-mask":
            raise PreparedDataError(
                "Prepared manifest has an unsupported nodata convention"
            )
        rasters = assets.get("rasters")
        if set(assets) != {"rasters"}:
            raise PreparedDataError("Prepared manifest must be raster-only")
        if not isinstance(rasters, dict) or "dem" not in rasters:
            raise PreparedDataError("Prepared manifest must define a DEM")
        try:
            transform = Affine(*grid["transform"])
            crs = CRS.from_wkt(grid["crs_wkt"])
            int(grid["width"])
            int(grid["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PreparedDataError(
                "Prepared manifest grid metadata is invalid"
            ) from exc
        for name, asset in rasters.items():
            path = self._validate_asset(asset)
            if asset.get("driver") != "COG":
                raise PreparedDataError(f"Prepared raster {name} is not declared as COG")
            try:
                with rasterio.open(path) as source:
                    if (
                        source.count != 1
                        or source.crs is None
                        or CRS.from_user_input(source.crs) != crs
                        or source.transform != transform
                        or source.width != int(grid["width"])
                        or source.height != int(grid["height"])
                        or source.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG"
                        or source.nodata is not None
                    ):
                        raise PreparedDataError(
                            f"Prepared raster {name} does not match the canonical grid/COG contract"
                        )
            except rasterio.errors.RasterioError as exc:
                raise PreparedDataError(f"Cannot inspect prepared raster: {name}") from exc

    def _validate_asset(self, asset: dict[str, Any]) -> Path:
        if not isinstance(asset, dict) or "path" not in asset:
            raise PreparedDataError("Malformed asset entry in prepared manifest")
        path = self.asset_path(asset["path"])
        if not path.is_file():
            raise PreparedDataError(f"Prepared asset is missing: {asset['path']}")
        return path


def canonical_grid(dem_path: str | Path, crs: str | CRS, resolution: float) -> GridSpec:
    """Derive an origin-anchored target grid from transformed DEM bounds."""
    if not math.isfinite(resolution) or resolution <= 0:
        raise PreparedDataError("Resolution must be a finite positive number")
    target = parse_metric_crs(crs)
    try:
        with rasterio.open(dem_path) as src:
            if src.count != 1:
                raise PreparedDataError("DEM must contain exactly one band")
            if src.crs is None:
                raise PreparedDataError("DEM has no CRS")
            bounds = transform_bounds(src.crs, target, *src.bounds, densify_pts=21)
    except rasterio.errors.RasterioError as exc:
        raise PreparedDataError(f"Cannot open DEM: {dem_path}") from exc
    left = math.floor(bounds[0] / resolution) * resolution
    bottom = math.floor(bounds[1] / resolution) * resolution
    right = math.ceil(bounds[2] / resolution) * resolution
    top = math.ceil(bounds[3] / resolution) * resolution
    width = round((right - left) / resolution)
    height = round((top - bottom) / resolution)
    return GridSpec(
        target, Affine(resolution, 0, left, 0, -resolution, top), width, height
    )


def prepare_dataset(spec: PreparationSpec) -> PreparationReport:
    """Create and atomically publish one prepared dataset."""
    _validate_spec(spec)
    output = Path(spec.output_dir)
    if output.exists():
        raise PreparedDataError(f"Output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        (staging / "rasters").mkdir()
        grid = canonical_grid(spec.dem, spec.crs, spec.resolution)

        raster_assets: dict[str, dict[str, Any]] = {}
        dem_path = staging / "rasters" / "dem.tif"
        _prepare_warped_raster(
            spec.dem, dem_path, grid, "continuous", spec.memory_limit_mb
        )
        raster_assets["dem"] = _raster_asset(dem_path, staging, "continuous")
        for item in sorted(spec.rasters, key=lambda value: value.name):
            target = staging / "rasters" / f"{item.name}.tif"
            _prepare_warped_raster(
                item.path, target, grid, item.kind, spec.memory_limit_mb
            )
            raster_assets[item.name] = _raster_asset(target, staging, item.kind)
        if spec.d8 is not None:
            target = staging / "rasters" / "d8.tif"
            _prepare_d8(spec.d8, target, grid, spec.d8_encoding or "canonical")
            raster_assets["d8"] = _raster_asset(
                target, staging, "d8", encoding="canonical-clockwise"
            )

        manifest = {
            "contract": CONTRACT,
            "version": CONTRACT_VERSION,
            "producer": _producer_version(),
            "grid": grid.to_manifest(),
            "sources": {"rasters": _raster_sources(spec)},
            "assets": {"rasters": raster_assets},
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        PreparedDataset(staging, manifest).validate()
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return PreparationReport(
        output_dir=output,
        manifest=output / "manifest.json",
        raster_count=len(raster_assets),
    )


def _validate_spec(spec: PreparationSpec) -> None:
    if not Path(spec.dem).is_file():
        raise PreparedDataError(f"DEM input is not a local file: {spec.dem}")
    if spec.memory_limit_mb <= 0:
        raise PreparedDataError("Memory limit must be positive")
    names: set[str] = set()
    for item in spec.rasters:
        if not NAME_RE.fullmatch(item.name) or item.name in RESERVED_RASTER_NAMES:
            raise PreparedDataError(f"Invalid or reserved raster name: {item.name}")
        if item.name in names:
            raise PreparedDataError(f"Duplicate raster name: {item.name}")
        names.add(item.name)
        if item.kind not in {"continuous", "categorical"}:
            raise PreparedDataError(f"Invalid raster kind for {item.name}: {item.kind}")
        if not Path(item.path).is_file():
            raise PreparedDataError(f"Raster input is not a local file: {item.path}")
    if (spec.d8 is None) != (spec.d8_encoding is None):
        raise PreparedDataError("--d8 and --d8-encoding must be supplied together")
    if spec.d8_encoding not in {None, "canonical", "esri"}:
        raise PreparedDataError(f"Unsupported D8 encoding: {spec.d8_encoding}")
    if spec.d8 is not None and not Path(spec.d8).is_file():
        raise PreparedDataError(f"D8 input is not a local file: {spec.d8}")


def _prepare_warped_raster(
    source: Path,
    output: Path,
    grid: GridSpec,
    kind: Literal["continuous", "categorical"],
    memory_limit_mb: int,
) -> None:
    dtype = "float32" if kind == "continuous" else "int32"
    resampling = Resampling.bilinear if kind == "continuous" else Resampling.nearest
    intermediate = output.with_suffix(".working.tif")
    try:
        with rasterio.open(source) as src:
            if src.count != 1:
                raise PreparedDataError(
                    f"Raster {source} must contain exactly one band"
                )
            if src.crs is None:
                raise PreparedDataError(f"Raster {source} has no CRS")
            with (
                WarpedVRT(
                    src,
                    crs=grid.crs,
                    transform=grid.transform,
                    width=grid.width,
                    height=grid.height,
                    resampling=resampling,
                    warp_mem_limit=memory_limit_mb,
                ) as vrt,
                _working_raster(intermediate, grid, dtype) as dst,
            ):
                for _, window in dst.block_windows(1):
                    values = vrt.read(1, window=window, masked=True)
                    mask = ~np.ma.getmaskarray(values)
                    if kind == "continuous":
                        raw = values.filled(0)
                        mask &= np.isfinite(raw)
                        data = np.where(mask, raw, 0).astype("float32")
                    else:
                        raw = values.filled(0)
                        valid = raw[mask]
                        if valid.size and (
                            not np.all(np.isfinite(valid))
                            or not np.all(valid == np.floor(valid))
                            or valid.min() < np.iinfo(np.int32).min
                            or valid.max() > np.iinfo(np.int32).max
                        ):
                            raise PreparedDataError(
                                f"Categorical raster {source} contains non-int32 values"
                            )
                        data = raw.astype("int32")
                    dst.write(data, 1, window=window)
                    dst.write_mask(mask.astype("uint8") * 255, window=window)
        _to_cog(intermediate, output, resampling)
    finally:
        intermediate.unlink(missing_ok=True)


def _prepare_d8(source: Path, output: Path, grid: GridSpec, encoding: str) -> None:
    intermediate = output.with_suffix(".working.tif")
    esri = {0: 0, 1: 3, 2: 4, 4: 5, 8: 6, 16: 7, 32: 8, 64: 1, 128: 2}
    try:
        with rasterio.open(source) as src:
            if src.count != 1 or src.crs is None:
                raise PreparedDataError(
                    "D8 raster must be single-band and declare a CRS"
                )
            if (
                CRS.from_user_input(src.crs) != grid.crs
                or src.transform != grid.transform
                or src.shape != (grid.height, grid.width)
            ):
                raise PreparedDataError(
                    "D8 raster must exactly match the canonical grid"
                )
            with _working_raster(intermediate, grid, "uint8") as dst:
                for _, window in src.block_windows(1):
                    values = src.read(1, window=window, masked=True)
                    mask = ~np.ma.getmaskarray(values)
                    raw = values.filled(0)
                    valid_values = set(np.unique(raw[mask]).tolist())
                    allowed = set(range(9)) if encoding == "canonical" else set(esri)
                    unknown = valid_values - allowed
                    if unknown:
                        raise PreparedDataError(
                            "D8 raster contains invalid code(s): "
                            + ", ".join(str(value) for value in sorted(unknown))
                        )
                    if encoding == "canonical":
                        data = raw.astype("uint8")
                    else:
                        data = np.zeros(raw.shape, dtype="uint8")
                        for source_code, target_code in esri.items():
                            data[raw == source_code] = target_code
                    dst.write(data, 1, window=window)
                    dst.write_mask(mask.astype("uint8") * 255, window=window)
        _to_cog(intermediate, output, Resampling.nearest)
    finally:
        intermediate.unlink(missing_ok=True)


def _working_raster(path: Path, grid: GridSpec, dtype: str):
    return rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=grid.width,
        height=grid.height,
        count=1,
        dtype=dtype,
        crs=grid.crs,
        transform=grid.transform,
        tiled=True,
        blockxsize=BLOCK_SIZE,
        blockysize=BLOCK_SIZE,
        compress="DEFLATE",
        nodata=None,
        BIGTIFF="IF_SAFER",
    )


def _to_cog(source: Path, output: Path, overview_resampling: Resampling) -> None:
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        copy_raster(
            source,
            output,
            driver="COG",
            BLOCKSIZE=BLOCK_SIZE,
            COMPRESS="DEFLATE",
            BIGTIFF="IF_SAFER",
            RESAMPLING=overview_resampling.name.upper(),
            OVERVIEW_RESAMPLING=overview_resampling.name.upper(),
        )


def _raster_asset(
    path: Path,
    root: Path,
    kind: str,
    *,
    encoding: str | None = None,
) -> dict[str, Any]:
    with rasterio.open(path) as dataset:
        dtype = dataset.dtypes[0]
        overviews = dataset.overviews(1)
    result = {
        **_file_asset(path, root, role=kind),
        "driver": "COG",
        "dtype": dtype,
        "nodata": "internal-mask",
        "block_size": BLOCK_SIZE,
        "overviews": overviews,
    }
    if encoding is not None:
        result["encoding"] = encoding
    return result


def _file_asset(path: Path, root: Path, *, role: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "role": role,
    }


def _raster_sources(spec: PreparationSpec) -> dict[str, dict[str, Any]]:
    result = {"dem": {"path": str(Path(spec.dem).resolve()), "band": 1}}
    for item in sorted(spec.rasters, key=lambda value: value.name):
        result[item.name] = {
            "path": str(Path(item.path).resolve()),
            "band": 1,
            "kind": item.kind,
        }
    if spec.d8 is not None:
        result["d8"] = {
            "path": str(Path(spec.d8).resolve()),
            "band": 1,
            "encoding": spec.d8_encoding,
        }
    return result


def _producer_version() -> str:
    try:
        return importlib.metadata.version("mgb-vec-hydro")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"
