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
import geopandas as gpd
import pandas as pd
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.shutil import copy as copy_raster
from rasterio.transform import Affine
from rasterio.features import rasterize
from rasterio.windows import from_bounds, Window

from mgb_vec_hydro.exceptions import PreparedDataError

CONTRACT = "mgb-prepared-dataset"
CONTRACT_VERSION = 4
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
    output_dir: Path
    minis: Path
    rasters: tuple[NamedRaster, ...] = field(default_factory=tuple)
    d8: Path | None = None
    d8_encoding: Literal["canonical", "esri"] | None = None
    memory_limit_mb: int = 512
    buffer_cells: int = 1


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
        if set(assets) != {"rasters", "mini_ownership", "drainage", "mini_index"}:
            raise PreparedDataError("Prepared manifest has an invalid asset layout")
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
        for name in ("mini_ownership", "drainage"):
            asset = assets[name]
            path = self._validate_asset(asset)
            with rasterio.open(path) as source:
                expected_dtype = "int32" if name == "mini_ownership" else "uint8"
                if (source.count != 1 or source.crs is None or CRS.from_user_input(source.crs) != crs
                    or source.transform != transform or source.shape != (int(grid["height"]), int(grid["width"]))
                    or source.nodata is not None or source.dtypes[0] != expected_dtype
                    or source.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG"):
                    raise PreparedDataError(f"Prepared raster {name} does not match the canonical grid")
        index = self._validate_asset(assets["mini_index"])
        if index.suffix != ".parquet":
            raise PreparedDataError("Prepared mini index must be Parquet")

    def _validate_asset(self, asset: dict[str, Any]) -> Path:
        if not isinstance(asset, dict) or "path" not in asset:
            raise PreparedDataError("Malformed asset entry in prepared manifest")
        path = self.asset_path(asset["path"])
        if not path.is_file():
            raise PreparedDataError(f"Prepared asset is missing: {asset['path']}")
        return path


def _aggregation_inputs(root: Path) -> tuple[Path, Path, Path, CRS]:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        roi = Path(manifest["roi"])
        crs = CRS.from_wkt(manifest["crs_wkt"])
        assets = manifest["assets"]
        catchments = root / assets["catchments"]["path"]
        segments = root / assets["segments"]["path"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PreparedDataError("Cannot read aggregation manifest") from exc
    if not roi.is_dir() or not catchments.is_file() or not segments.is_file():
        raise PreparedDataError("Aggregation manifest has missing ROI or mini assets")
    return roi, catchments, segments, crs


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
        roi_root, mini_catchments, mini_segments, target_crs = _aggregation_inputs(Path(spec.minis))
        roi_manifest = json.loads((roi_root / "manifest.json").read_text(encoding="utf-8"))
        if CRS.from_wkt(roi_manifest["crs_wkt"]) != target_crs:
            raise PreparedDataError("Aggregation and ROI CRS do not match")
        roi_catchments = roi_root / roi_manifest["assets"]["catchments"]["path"]
        with rasterio.open(spec.dem) as dem:
            _require_source_grid(dem, target_crs, "DEM")
            domain = gpd.read_file(roi_catchments).geometry.union_all()
            if domain.is_empty:
                raise PreparedDataError("ROI domain is empty")
            buffered_domain = domain.buffer(spec.buffer_cells * abs(dem.transform.a))
            window = from_bounds(*buffered_domain.bounds, transform=dem.transform).round_offsets().round_lengths()
            if (window.col_off < 0 or window.row_off < 0
                or window.col_off + window.width > dem.width
                or window.row_off + window.height > dem.height):
                raise PreparedDataError("DEM does not cover the buffered ROI domain")
            grid = GridSpec(target_crs, rasterio.windows.transform(window, dem.transform), int(window.width), int(window.height))
            mask = rasterize([(buffered_domain, 1)], out_shape=(grid.height, grid.width), transform=grid.transform, fill=0, dtype="uint8").astype(bool)

        raster_assets: dict[str, dict[str, Any]] = {}
        dem_path = staging / "rasters" / "dem.tif"
        _prepare_clipped_raster(spec.dem, dem_path, grid, window, mask, "continuous")
        raster_assets["dem"] = _raster_asset(dem_path, staging, "continuous")
        for item in sorted(spec.rasters, key=lambda value: value.name):
            target = staging / "rasters" / f"{item.name}.tif"
            _prepare_clipped_raster(item.path, target, grid, window, mask, item.kind)
            raster_assets[item.name] = _raster_asset(target, staging, item.kind)
        if spec.d8 is not None:
            target = staging / "rasters" / "d8.tif"
            _prepare_clipped_d8(spec.d8, target, grid, window, mask, spec.d8_encoding or "canonical")
            raster_assets["d8"] = _raster_asset(
                target, staging, "d8", encoding="canonical-clockwise"
            )

        domain_assets, mini_index = _prepare_domain_rasters(staging, grid, mini_catchments, mini_segments)
        manifest = {
            "contract": CONTRACT,
            "version": CONTRACT_VERSION,
            "producer": _producer_version(),
            "grid": grid.to_manifest(),
            "inputs": {"minis": str(Path(spec.minis).resolve()), "roi": str(roi_root.resolve())},
            "sources": {"rasters": _raster_sources(spec)},
            "assets": {"rasters": raster_assets, **domain_assets, "mini_index": _file_asset(mini_index, staging, role="dense mini-label index")},
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
    if spec.buffer_cells < 0:
        raise PreparedDataError("buffer-cells must be non-negative")
    if not Path(spec.minis).is_dir():
        raise PreparedDataError(f"minis input is not a directory: {spec.minis}")
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


def _require_source_grid(source, crs: CRS, name: str, grid: GridSpec | None = None) -> None:
    if source.count != 1 or source.crs is None:
        raise PreparedDataError(f"{name} must be single-band and declare a CRS")
    if CRS.from_user_input(source.crs) != crs:
        raise PreparedDataError(f"{name} CRS does not match the ROI CRS")
    transform = source.transform
    if transform.b != 0 or transform.d != 0 or transform.a <= 0 or transform.e >= 0:
        raise PreparedDataError(f"{name} must use a north-up raster grid")
    if grid is not None:
        if not (math.isclose(transform.a, grid.transform.a) and math.isclose(transform.e, grid.transform.e)):
            raise PreparedDataError(f"{name} resolution does not match the DEM grid")
        col = (grid.transform.c - transform.c) / transform.a
        row = (grid.transform.f - transform.f) / transform.e
        if not (math.isclose(col, round(col), abs_tol=1e-7) and math.isclose(row, round(row), abs_tol=1e-7)):
            raise PreparedDataError(f"{name} origin is not aligned to the DEM grid")
        if (grid.bounds[0] < source.bounds.left - 1e-7 or grid.bounds[1] < source.bounds.bottom - 1e-7
            or grid.bounds[2] > source.bounds.right + 1e-7 or grid.bounds[3] > source.bounds.top + 1e-7):
            raise PreparedDataError(f"{name} does not cover the buffered ROI domain")


def _prepare_clipped_raster(source: Path, output: Path, grid: GridSpec, window: Window, domain_mask: np.ndarray, kind: Literal["continuous", "categorical"]) -> None:
    dtype = "float32" if kind == "continuous" else "int32"
    resampling = Resampling.bilinear if kind == "continuous" else Resampling.nearest
    intermediate = output.with_suffix(".working.tif")
    try:
        with rasterio.open(source) as src, _working_raster(intermediate, grid, dtype) as dst:
            _require_source_grid(src, grid.crs, str(source), grid)
            source_window = from_bounds(*grid.bounds, transform=src.transform).round_offsets().round_lengths()
            values = src.read(1, window=source_window, masked=True)
            valid = ~np.ma.getmaskarray(values) & domain_mask
            raw = values.filled(0)
            if kind == "continuous":
                valid &= np.isfinite(raw)
                data = np.where(valid, raw, 0).astype(dtype)
            else:
                source_values = raw[valid]
                if source_values.size and (not np.all(np.isfinite(source_values)) or not np.all(source_values == np.floor(source_values))):
                    raise PreparedDataError(f"Categorical raster {source} contains non-integral values")
                data = raw.astype(dtype)
            dst.write(data, 1)
            dst.write_mask(valid.astype("uint8") * 255)
        _to_cog(intermediate, output, resampling)
    finally:
        intermediate.unlink(missing_ok=True)


def _prepare_clipped_d8(source: Path, output: Path, grid: GridSpec, window: Window, domain_mask: np.ndarray, encoding: str) -> None:
    intermediate = output.with_suffix(".working.tif")
    esri = {0: 0, 1: 3, 2: 4, 4: 5, 8: 6, 16: 7, 32: 8, 64: 1, 128: 2}
    try:
        with rasterio.open(source) as src, _working_raster(intermediate, grid, "uint8") as dst:
            _require_source_grid(src, grid.crs, "D8", grid)
            source_window = from_bounds(*grid.bounds, transform=src.transform).round_offsets().round_lengths()
            values = src.read(1, window=source_window, masked=True)
            valid = ~np.ma.getmaskarray(values) & domain_mask
            raw = values.filled(0)
            allowed = set(range(9)) if encoding == "canonical" else set(esri)
            unknown = set(np.unique(raw[valid]).tolist()) - allowed
            if unknown:
                raise PreparedDataError("D8 raster contains invalid code(s): " + ", ".join(map(str, sorted(unknown))))
            data = raw.astype("uint8") if encoding == "canonical" else np.vectorize(lambda value: esri.get(value, 0), otypes=["uint8"])(raw)
            dst.write(data, 1)
            dst.write_mask(valid.astype("uint8") * 255)
        _to_cog(intermediate, output, Resampling.nearest)
    finally:
        intermediate.unlink(missing_ok=True)


def _prepare_domain_rasters(staging: Path, grid: GridSpec, catchment_path: Path, segment_path: Path) -> tuple[dict[str, dict[str, Any]], Path]:
    """Rasterize complete aggregated minis before terrain processing."""
    from mgb_vec_hydro.execution.raster import RasterAssembler, RasterPatch, RasterProductSpec
    catchments = gpd.read_file(catchment_path).set_index("id")
    segments = gpd.read_file(segment_path).set_index("id")
    if set(catchments.index) != set(segments.index):
        raise PreparedDataError("Mini catchments and segments do not have matching IDs")
    ordered = sorted(catchments.index, key=lambda value: (type(value).__name__, str(value)))
    labels = {mini_id: label for label, mini_id in enumerate(ordered, start=1)}
    raster_root = staging / "rasters"
    with RasterAssembler(raster_root, grid, (RasterProductSpec("mini_ownership", "int32"), RasterProductSpec("drainage", "uint8"))) as assembler:
        for mini_id in ordered:
            catchment, segment = catchments.loc[mini_id].geometry, segments.loc[mini_id].geometry
            if catchment is None or segment is None or catchment.is_empty or segment.is_empty:
                raise PreparedDataError(f"Mini {mini_id} has invalid geometry")
            win = from_bounds(*catchment.bounds, transform=grid.transform).round_offsets().round_lengths().intersection(Window(0, 0, grid.width, grid.height))
            shape = (int(win.height), int(win.width))
            transform = rasterio.windows.transform(win, grid.transform)
            valid = rasterize([(catchment, 1)], out_shape=shape, transform=transform, dtype="uint8", all_touched=False).astype(bool)
            drainage = rasterize([(segment, 1)], out_shape=shape, transform=transform, dtype="uint8", all_touched=True).astype(bool) & valid
            if not valid.any() or not drainage.any():
                raise PreparedDataError(f"Mini {mini_id} has no rasterized ownership or drainage cells")
            assembler.write(RasterPatch("mini_ownership", win, np.full(shape, labels[mini_id], dtype="int32"), valid))
            assembler.write(RasterPatch("drainage", win, drainage.astype("uint8"), valid))
        paths = assembler.finish()
    index = staging / "mini_index.parquet"
    bounds = [catchments.loc[mini_id].geometry.bounds for mini_id in ordered]
    pd.DataFrame({
        "mini_label": np.arange(1, len(ordered) + 1, dtype="int32"), "mini_id": ordered,
        "minx": [value[0] for value in bounds], "miny": [value[1] for value in bounds],
        "maxx": [value[2] for value in bounds], "maxy": [value[3] for value in bounds],
    }).to_parquet(index, index=False)
    return ({name: _raster_asset(path, staging, name) for name, path in paths.items()}, index)


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
