"""Creation and validation of bounded-memory prepared input datasets."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyogrio
import rasterio
import shapely
from pyproj import CRS
from rasterio.enums import MaskFlags, Resampling
from rasterio.shutil import copy as copy_raster
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.validation import explain_validity

from mgb_vec_hydro.crs_utils import parse_metric_crs
from mgb_vec_hydro.exceptions import PreparedDataError

CONTRACT = "mgb-prepared-dataset"
CONTRACT_VERSION = 1
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
    """Inputs and normalization choices for a prepared dataset."""

    catchments: Path
    segments: Path
    dem: Path
    crs: str
    resolution: float
    output_dir: Path
    id_col: str = "id"
    id_down_col: str = "id_down"
    strahler_order_col: str = "strahler_order"
    catchments_layer: str | None = None
    segments_layer: str | None = None
    catchments_source_crs: str | None = None
    segments_source_crs: str | None = None
    rasters: tuple[NamedRaster, ...] = field(default_factory=tuple)
    d8: Path | None = None
    d8_encoding: Literal["canonical", "esri"] | None = None
    repair_invalid_geometries: bool = False
    vector_batch_size: int = 10_000
    memory_limit_mb: int = 512


@dataclass(frozen=True)
class PreparationReport:
    """Summary of a successfully published prepared dataset."""

    output_dir: Path
    manifest: Path
    feature_count: int
    dropped_catchments: int
    dropped_segments: int
    repaired_catchments: int
    repaired_segments: int


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
    """A validated handle to a prepared dataset directory."""

    def __init__(self, root: Path, manifest: dict[str, Any]):
        self.root = root
        self.manifest = manifest

    @classmethod
    def open(
        cls, root: str | Path, *, verify_hashes: bool = False
    ) -> PreparedDataset:
        root = Path(root)
        manifest_path = root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreparedDataError(f"Cannot read prepared manifest: {manifest_path}") from exc
        dataset = cls(root, manifest)
        dataset.validate(verify_hashes=verify_hashes)
        return dataset

    def asset_path(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str):
            raise PreparedDataError("Manifest asset path must be text")
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise PreparedDataError(f"Manifest asset escapes dataset: {relative_path}") from exc
        return candidate

    def validate(self, *, verify_hashes: bool = False) -> None:
        manifest = self.manifest
        if manifest.get("contract") != CONTRACT or manifest.get("version") != CONTRACT_VERSION:
            raise PreparedDataError("Unsupported prepared dataset contract or version")
        grid = manifest.get("grid")
        assets = manifest.get("assets")
        if not isinstance(grid, dict) or not isinstance(assets, dict):
            raise PreparedDataError("Prepared manifest is missing grid or assets")
        required_grid = {"crs_wkt", "transform", "extent", "resolution", "width", "height", "nodata"}
        if not required_grid.issubset(grid):
            raise PreparedDataError("Prepared manifest has an incomplete grid")
        if grid["nodata"] != "internal-mask":
            raise PreparedDataError("Prepared manifest has an unsupported nodata convention")
        vectors = assets.get("vectors")
        rasters = assets.get("rasters")
        lookup = assets.get("lookup")
        if not isinstance(vectors, dict) or set(vectors) != {"catchments", "segments"}:
            raise PreparedDataError("Prepared manifest must define catchments and segments")
        if not isinstance(rasters, dict) or "dem" not in rasters:
            raise PreparedDataError("Prepared manifest must define a DEM")
        if not isinstance(lookup, dict):
            raise PreparedDataError("Prepared manifest must define a feature lookup")

        try:
            expected_transform = Affine(*grid["transform"])
            expected_crs = CRS.from_wkt(grid["crs_wkt"])
            expected_shape = (int(grid["height"]), int(grid["width"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PreparedDataError("Prepared manifest grid metadata is invalid") from exc
        for name, asset in rasters.items():
            path = self._validate_asset(asset, verify_hashes)
            role = asset.get("role")
            expected_dtype = {
                "continuous": "float32",
                "categorical": "int32",
                "d8": "uint8",
            }.get(role)
            if expected_dtype is None or (name == "dem" and role != "continuous"):
                raise PreparedDataError(f"Prepared raster {name} has an invalid role")
            try:
                with rasterio.open(path) as src:
                    if src.count != 1:
                        raise PreparedDataError(f"Prepared raster {name} is not single-band")
                    if CRS.from_user_input(src.crs) != expected_crs:
                        raise PreparedDataError(f"Prepared raster {name} has the wrong CRS")
                    if src.shape != expected_shape or src.transform != expected_transform:
                        raise PreparedDataError(f"Prepared raster {name} is not on the canonical grid")
                    if not src.profile.get("tiled"):
                        raise PreparedDataError(f"Prepared raster {name} is not tiled")
                    if src.block_shapes[0][0] > BLOCK_SIZE or src.block_shapes[0][1] > BLOCK_SIZE:
                        raise PreparedDataError(f"Prepared raster {name} has invalid tiling")
                    if src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG":
                        raise PreparedDataError(f"Prepared raster {name} is not a COG")
                    if src.nodata is not None:
                        raise PreparedDataError(f"Prepared raster {name} must use an internal mask")
                    if src.dtypes[0] != expected_dtype:
                        raise PreparedDataError(f"Prepared raster {name} has the wrong dtype")
                    if MaskFlags.per_dataset not in src.mask_flag_enums[0]:
                        raise PreparedDataError(f"Prepared raster {name} has no internal validity mask")
            except rasterio.errors.RasterioError as exc:
                raise PreparedDataError(f"Cannot open prepared raster {name}") from exc

        expected_fields = {
            "catchments": ["id"],
            "segments": ["id", "id_down", "strahler_order"],
        }
        for name, asset in vectors.items():
            path = self._validate_asset(asset, verify_hashes)
            try:
                info = pyogrio.read_info(path)
            except Exception as exc:
                raise PreparedDataError(f"Cannot open prepared vector {name}") from exc
            if info["driver"] != "FlatGeobuf" or list(info["fields"]) != expected_fields[name]:
                raise PreparedDataError(f"Prepared vector {name} has the wrong format or schema")
            expected_geometry = "MultiPolygon" if name == "catchments" else "MultiLineString"
            if info["geometry_type"] != expected_geometry:
                raise PreparedDataError(f"Prepared vector {name} has the wrong geometry type")
            if CRS.from_user_input(info["crs"]) != expected_crs:
                raise PreparedDataError(f"Prepared vector {name} has the wrong CRS")
            if not info["capabilities"].get("fast_spatial_filter"):
                raise PreparedDataError(f"Prepared vector {name} has no spatial index")
            if int(info["features"]) != int(manifest.get("feature_count", -1)):
                raise PreparedDataError(f"Prepared vector {name} count does not match manifest")

        lookup_path = self._validate_asset(lookup, verify_hashes)
        try:
            with sqlite3.connect(f"file:{lookup_path}?mode=ro", uri=True) as connection:
                count = connection.execute("SELECT COUNT(*) FROM features").fetchone()[0]
                id_sql_type = next(
                    row[2] for row in connection.execute("PRAGMA table_info(features)") if row[1] == "id"
                )
        except sqlite3.Error as exc:
            raise PreparedDataError("Prepared feature lookup is invalid") from exc
        if count != int(manifest["feature_count"]):
            raise PreparedDataError("Prepared feature lookup count does not match manifest")
        expected_sql_type = "INTEGER" if manifest.get("id_type") == "int64" else "TEXT"
        if id_sql_type != expected_sql_type:
            raise PreparedDataError("Prepared feature lookup ID type does not match manifest")

    def _validate_asset(self, asset: dict[str, Any], verify_hashes: bool) -> Path:
        if not isinstance(asset, dict) or "path" not in asset or "sha256" not in asset:
            raise PreparedDataError("Malformed asset entry in prepared manifest")
        path = self.asset_path(asset["path"])
        if not path.is_file():
            raise PreparedDataError(f"Prepared asset is missing: {asset['path']}")
        if "bytes" in asset and path.stat().st_size != int(asset["bytes"]):
            raise PreparedDataError(f"Prepared asset size mismatch: {asset['path']}")
        if verify_hashes and _sha256(path) != asset["sha256"]:
            raise PreparedDataError(f"Prepared asset checksum mismatch: {asset['path']}")
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
    return GridSpec(target, Affine(resolution, 0, left, 0, -resolution, top), width, height)


def prepare_dataset(spec: PreparationSpec) -> PreparationReport:
    """Create and atomically publish one prepared dataset."""
    _validate_spec(spec)
    output = Path(spec.output_dir)
    if output.exists():
        raise PreparedDataError(f"Output directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        (staging / "vectors").mkdir()
        (staging / "rasters").mkdir()
        (staging / "indexes").mkdir()
        grid = canonical_grid(spec.dem, spec.crs, spec.resolution)
        index_path = staging / "indexes" / "features.sqlite"
        vector_result = _prepare_vectors(spec, grid, staging, index_path)

        raster_assets: dict[str, dict[str, Any]] = {}
        dem_path = staging / "rasters" / "dem.tif"
        _prepare_warped_raster(spec.dem, dem_path, grid, "continuous", spec.memory_limit_mb)
        raster_assets["dem"] = _raster_asset(dem_path, staging, "continuous", spec.dem)
        for item in sorted(spec.rasters, key=lambda value: value.name):
            target = staging / "rasters" / f"{item.name}.tif"
            _prepare_warped_raster(item.path, target, grid, item.kind, spec.memory_limit_mb)
            raster_assets[item.name] = _raster_asset(target, staging, item.kind, item.path)
        if spec.d8 is not None:
            target = staging / "rasters" / "d8.tif"
            _prepare_d8(spec.d8, target, grid, spec.d8_encoding or "canonical")
            raster_assets["d8"] = _raster_asset(
                target, staging, "d8", spec.d8, encoding="canonical-clockwise"
            )

        manifest = {
            "contract": CONTRACT,
            "version": CONTRACT_VERSION,
            "producer": _producer_version(),
            "grid": grid.to_manifest(),
            "id_type": vector_result["id_type"],
            "feature_count": vector_result["feature_count"],
            "normalization": vector_result["normalization"],
            "source_crs_overrides": {
                "catchments": spec.catchments_source_crs,
                "segments": spec.segments_source_crs,
            },
            "vector_mapping": {
                "id": spec.id_col,
                "id_down": spec.id_down_col,
                "strahler_order": spec.strahler_order_col,
            },
            "assets": {
                "vectors": vector_result["assets"],
                "rasters": raster_assets,
                "lookup": _file_asset(index_path, staging, role="feature-lookup"),
            },
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
        PreparedDataset.open(staging, verify_hashes=True)
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    normalization = vector_result["normalization"]
    return PreparationReport(
        output_dir=output,
        manifest=output / "manifest.json",
        feature_count=vector_result["feature_count"],
        dropped_catchments=normalization["dropped_null_ids"]["catchments"],
        dropped_segments=normalization["dropped_null_ids"]["segments"],
        repaired_catchments=normalization["repaired_geometries"]["catchments"],
        repaired_segments=normalization["repaired_geometries"]["segments"],
    )


def _validate_spec(spec: PreparationSpec) -> None:
    for label, value in (("catchments", spec.catchments), ("segments", spec.segments), ("DEM", spec.dem)):
        if not Path(value).is_file():
            raise PreparedDataError(f"{label} input is not a local file: {value}")
    if spec.vector_batch_size <= 0:
        raise PreparedDataError("Vector batch size must be positive")
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


def _prepare_vectors(
    spec: PreparationSpec, grid: GridSpec, staging: Path, index_path: Path
) -> dict[str, Any]:
    connection = sqlite3.connect(index_path)
    try:
        return _prepare_vectors_with_connection(spec, grid, staging, connection)
    finally:
        connection.close()


def _prepare_vectors_with_connection(
    spec: PreparationSpec,
    grid: GridSpec,
    staging: Path,
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    catchment_info = pyogrio.read_info(spec.catchments, layer=spec.catchments_layer)
    segment_info = pyogrio.read_info(spec.segments, layer=spec.segments_layer)
    catchment_id = _resolve_field(catchment_info, spec.id_col, "catchments")
    segment_id = _resolve_field(segment_info, spec.id_col, "segments")
    segment_down = _resolve_field(segment_info, spec.id_down_col, "segments")
    segment_order = _resolve_field(segment_info, spec.strahler_order_col, "segments")
    id_type = _field_id_type(segment_info, segment_id)
    if _field_id_type(catchment_info, catchment_id) != id_type:
        raise PreparedDataError("Catchment and segment identifiers use incompatible types")
    if _field_id_type(segment_info, segment_down, nullable=True) != id_type:
        raise PreparedDataError("Segment downstream identifiers do not match segment ID type")

    sql_type = "INTEGER" if id_type == "int64" else "TEXT"
    connection.executescript(
        f"""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE segment_source (
            id {sql_type} PRIMARY KEY,
            id_down {sql_type},
            strahler_order INTEGER
        );
        CREATE TABLE catchment_source (id {sql_type} PRIMARY KEY);
        """
    )
    stats = {
        "dropped_null_ids": {"catchments": 0, "segments": 0},
        "repaired_geometries": {"catchments": 0, "segments": 0},
    }
    catchment_output = staging / "vectors" / "catchments.fgb"
    segment_output = staging / "vectors" / "segments.fgb"
    _prepare_vector_layer(
        spec.catchments,
        spec.catchments_layer,
        catchment_output,
        grid.crs,
        spec.catchments_source_crs,
        [catchment_id],
        {catchment_id: "id"},
        "catchments",
        "polygon",
        id_type,
        spec,
        connection,
        stats,
    )
    _prepare_vector_layer(
        spec.segments,
        spec.segments_layer,
        segment_output,
        grid.crs,
        spec.segments_source_crs,
        [segment_id, segment_down, segment_order],
        {segment_id: "id", segment_down: "id_down", segment_order: "strahler_order"},
        "segments",
        "line",
        id_type,
        spec,
        connection,
        stats,
    )
    only_catchments = connection.execute(
        "SELECT id FROM catchment_source EXCEPT SELECT id FROM segment_source LIMIT 5"
    ).fetchall()
    only_segments = connection.execute(
        "SELECT id FROM segment_source EXCEPT SELECT id FROM catchment_source LIMIT 5"
    ).fetchall()
    if only_catchments or only_segments:
        raise PreparedDataError(
            "Catchment and segment valid ID sets differ; "
            f"catchment-only={only_catchments}, segment-only={only_segments}"
        )
    connection.executescript(
        f"""
        CREATE TABLE features (
            id {sql_type} PRIMARY KEY,
            id_down {sql_type},
            strahler_order INTEGER,
            catchment_fid INTEGER,
            segment_fid INTEGER,
            catchment_minx REAL, catchment_miny REAL,
            catchment_maxx REAL, catchment_maxy REAL,
            segment_minx REAL, segment_miny REAL,
            segment_maxx REAL, segment_maxy REAL
        );
        INSERT INTO features(id, id_down, strahler_order)
        SELECT id, id_down, strahler_order FROM segment_source ORDER BY id;
        DROP TABLE catchment_source;
        DROP TABLE segment_source;
        CREATE INDEX features_id_down_idx ON features(id_down);
        """
    )
    _index_vector_features(connection, catchment_output, "catchment", spec.vector_batch_size)
    _index_vector_features(connection, segment_output, "segment", spec.vector_batch_size)
    connection.executescript(
        """
        CREATE UNIQUE INDEX features_catchment_fid_idx ON features(catchment_fid);
        CREATE UNIQUE INDEX features_segment_fid_idx ON features(segment_fid);
        """
    )
    count = connection.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    missing = connection.execute(
        "SELECT COUNT(*) FROM features WHERE catchment_fid IS NULL OR segment_fid IS NULL"
    ).fetchone()[0]
    if missing:
        raise PreparedDataError("Could not index every prepared vector feature")
    connection.commit()
    connection.execute("VACUUM")

    return {
        "id_type": id_type,
        "feature_count": count,
        "normalization": stats,
        "assets": {
            "catchments": _vector_asset(
                catchment_output, staging, spec.catchments, spec.catchments_layer,
                ["id"], [id_type], count, "MultiPolygon"
            ),
            "segments": _vector_asset(
                segment_output, staging, spec.segments, spec.segments_layer,
                ["id", "id_down", "strahler_order"],
                [id_type, id_type, "int64"], count, "MultiLineString",
                nullable_fields=["id_down", "strahler_order"],
            ),
        },
    }


def _prepare_vector_layer(
    source: Path,
    layer: str | None,
    output: Path,
    target_crs: CRS,
    source_crs: str | None,
    columns: list[str],
    renames: dict[str, str],
    name: str,
    geometry_family: str,
    id_type: str,
    spec: PreparationSpec,
    connection: sqlite3.Connection,
    stats: dict[str, dict[str, int]],
) -> None:
    def normalized_batches():
        with pyogrio.open_arrow(
            source,
            layer=layer,
            columns=columns,
            batch_size=spec.vector_batch_size,
            use_pyarrow=True,
        ) as (metadata, batches):
            declared_crs = source_crs or metadata.get("crs")
            if declared_crs is None:
                raise PreparedDataError(f"{name} layer has no CRS")
            for batch in batches:
                frame = gpd.GeoDataFrame.from_arrow(batch).rename(columns=renames)
                if source_crs is not None:
                    frame = frame.set_crs(source_crs, allow_override=True)
                frame = frame.to_crs(target_crs)
                null_mask = frame["id"].isna()
                if id_type == "string":
                    null_mask |= frame["id"].map(
                        lambda value: isinstance(value, str) and value.strip() == ""
                    )
                stats["dropped_null_ids"][name] += int(null_mask.sum())
                frame = frame.loc[~null_mask].copy()
                if frame.empty:
                    continue
                frame["id"] = _normalize_ids(frame["id"], id_type, f"{name} ID")
                if name == "segments":
                    frame["id_down"] = _normalize_ids(
                        frame["id_down"], id_type, "segment downstream ID", nullable=True
                    )
                    frame["strahler_order"] = _normalize_strahler(frame["strahler_order"])
                repaired = _normalize_geometries(
                    frame,
                    geometry_family,
                    repair=spec.repair_invalid_geometries,
                    layer_name=name,
                )
                stats["repaired_geometries"][name] += repaired
                ordered = ["id"] if name == "catchments" else ["id", "id_down", "strahler_order"]
                frame = frame[ordered + [frame.geometry.name]].rename_geometry("geometry")
                frame = frame.reset_index(drop=True)
                rows = (
                    [(_sqlite_value(value),) for value in frame["id"].tolist()]
                    if name == "catchments"
                    else [
                        tuple(_sqlite_value(value) for value in row)
                        for row in frame[["id", "id_down", "strahler_order"]].itertuples(
                            index=False, name=None
                        )
                    ]
                )
                table = "catchment_source" if name == "catchments" else "segment_source"
                placeholders = "(?)" if name == "catchments" else "(?, ?, ?)"
                try:
                    connection.executemany(f"INSERT INTO {table} VALUES {placeholders}", rows)
                except sqlite3.IntegrityError as exc:
                    raise PreparedDataError(f"Found duplicate non-null ID in {name}") from exc
                table = pa.table(frame.to_arrow()).replace_schema_metadata(None)
                yield from table.to_batches()

    stream_errors: list[BaseException] = []

    def tracked_batches():
        try:
            yield from normalized_batches()
        except BaseException as exc:
            stream_errors.append(exc)
            raise

    try:
        batches = iter(tracked_batches())
        first = next(batches, None)
        if first is None:
            raise PreparedDataError(f"{name} layer has no identifiable features")
        reader = pa.RecordBatchReader.from_batches(
            first.schema,
            chain((first,), batches),
        )
        pyogrio.write_arrow(
            reader,
            output,
            driver="FlatGeobuf",
            geometry_name="geometry",
            geometry_type="MultiPolygon" if geometry_family == "polygon" else "MultiLineString",
            crs=target_crs.to_wkt(),
            layer_options={"SPATIAL_INDEX": "YES"},
        )
    except PreparedDataError:
        raise
    except Exception as exc:
        if stream_errors:
            cause = stream_errors[0]
            if isinstance(cause, PreparedDataError):
                raise cause
            raise PreparedDataError(
                f"Failed to prepare {name} vector layer: {cause}"
            ) from cause
        raise PreparedDataError(f"Failed to prepare {name} vector layer: {exc}") from exc


def _normalize_ids(
    values: pd.Series, id_type: str, label: str, *, nullable: bool = False
) -> pd.Series:
    result = []
    for value in values.tolist():
        if pd.isna(value):
            if nullable:
                result.append(None)
                continue
            raise PreparedDataError(f"{label} contains a null value")
        if id_type == "int64":
            if isinstance(value, (bool, np.bool_)):
                raise PreparedDataError(f"{label} must contain only signed integers")
            if isinstance(value, (float, np.floating)):
                if not math.isfinite(float(value)) or not float(value).is_integer():
                    raise PreparedDataError(f"{label} must contain only signed integers")
                number = int(value)
            elif isinstance(value, (int, np.integer)):
                number = int(value)
            else:
                raise PreparedDataError(f"{label} must contain only signed integers")
            if not -(2**63) <= number < 2**63:
                raise PreparedDataError(f"{label} is outside signed int64 range")
            result.append(number)
        else:
            if not isinstance(value, str):
                raise PreparedDataError(f"{label} must contain only strings")
            if value.strip() == "":
                if nullable:
                    result.append(None)
                    continue
                raise PreparedDataError(f"{label} contains an empty value")
            result.append(value)
    if id_type == "int64":
        dtype = "Int64" if nullable else "int64"
    else:
        dtype = "string"
    return pd.Series(result, index=values.index, dtype=dtype)


def _normalize_strahler(values: pd.Series) -> pd.Series:
    result = []
    for value in values.tolist():
        if pd.isna(value):
            result.append(None)
            continue
        if isinstance(value, (bool, np.bool_)):
            raise PreparedDataError("Strahler order must contain only integers or nulls")
        if isinstance(value, (float, np.floating)) and not float(value).is_integer():
            raise PreparedDataError("Strahler order must contain only integers or nulls")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise PreparedDataError("Strahler order must contain only integers or nulls") from exc
        result.append(number)
    return pd.Series(result, index=values.index, dtype="Int64")


def _sqlite_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, np.integer):
        return int(value)
    return value


def _normalize_geometries(
    frame: gpd.GeoDataFrame,
    family: str,
    *,
    repair: bool,
    layer_name: str,
) -> int:
    repaired = 0
    geometries = []
    for identifier, geometry in frame[["id", frame.geometry.name]].itertuples(index=False, name=None):
        if geometry is None or geometry.is_empty:
            raise PreparedDataError(f"{layer_name} feature {identifier!r} has empty geometry")
        if not geometry.is_valid:
            if not repair:
                raise PreparedDataError(
                    f"{layer_name} feature {identifier!r} has invalid geometry: "
                    f"{explain_validity(geometry)}"
                )
            geometry = _extract_family(shapely.make_valid(geometry), family)
            repaired += 1
        geometry = _extract_family(geometry, family)
        if geometry is None or geometry.is_empty or not geometry.is_valid:
            raise PreparedDataError(
                f"{layer_name} feature {identifier!r} cannot be normalized to {family} geometry"
            )
        geometries.append(geometry)
    frame.geometry = geometries
    return repaired


def _extract_family(geometry, family: str):
    if family == "polygon":
        if isinstance(geometry, Polygon):
            return MultiPolygon([geometry])
        if isinstance(geometry, MultiPolygon):
            return geometry
        accepted = (Polygon, MultiPolygon)
    else:
        if isinstance(geometry, LineString):
            return MultiLineString([geometry])
        if isinstance(geometry, MultiLineString):
            return geometry
        accepted = (LineString, MultiLineString)
    if not isinstance(geometry, GeometryCollection):
        return None
    parts = []
    for child in geometry.geoms:
        converted = _extract_family(child, family)
        if isinstance(converted, accepted):
            parts.extend(converted.geoms if hasattr(converted, "geoms") else [converted])
    if not parts:
        return None
    return MultiPolygon(parts) if family == "polygon" else MultiLineString(parts)


def _index_vector_features(
    connection: sqlite3.Connection, path: Path, prefix: str, batch_size: int
) -> None:
    with pyogrio.open_arrow(
        path, columns=["id"], return_fids=True, batch_size=batch_size, use_pyarrow=True
    ) as (metadata, batches):
        fid_column = metadata["fid_column"]
        for batch in batches:
            frame = gpd.GeoDataFrame.from_arrow(batch)
            bounds = frame.geometry.bounds
            rows = [
                (int(fid), float(row.minx), float(row.miny), float(row.maxx), float(row.maxy), identifier)
                for fid, identifier, row in zip(
                    frame[fid_column].tolist(), frame["id"].tolist(), bounds.itertuples(index=False)
                )
            ]
            connection.executemany(
                f"""
                UPDATE features SET
                    {prefix}_fid=?, {prefix}_minx=?, {prefix}_miny=?,
                    {prefix}_maxx=?, {prefix}_maxy=?
                WHERE id=?
                """,
                rows,
            )


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
                raise PreparedDataError(f"Raster {source} must contain exactly one band")
            if src.crs is None:
                raise PreparedDataError(f"Raster {source} has no CRS")
            with WarpedVRT(
                src,
                crs=grid.crs,
                transform=grid.transform,
                width=grid.width,
                height=grid.height,
                resampling=resampling,
                warp_mem_limit=memory_limit_mb,
            ) as vrt, _working_raster(intermediate, grid, dtype) as dst:
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
                            raise PreparedDataError(f"Categorical raster {source} contains non-int32 values")
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
                raise PreparedDataError("D8 raster must be single-band and declare a CRS")
            if (
                CRS.from_user_input(src.crs) != grid.crs
                or src.transform != grid.transform
                or src.shape != (grid.height, grid.width)
            ):
                raise PreparedDataError("D8 raster must exactly match the canonical grid")
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


def _resolve_field(info: dict[str, Any], requested: str, layer: str) -> str:
    fields = list(info["fields"])
    if requested in fields:
        return requested
    matches = [field for field in fields if field.casefold() == requested.casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PreparedDataError(f"{layer} is missing required field: {requested}")
    raise PreparedDataError(f"{layer} has ambiguous field name: {requested}")


def _field_id_type(info: dict[str, Any], field: str, nullable: bool = False) -> str:
    position = list(info["fields"]).index(field)
    ogr_type = info["ogr_types"][position]
    if ogr_type in {"OFTInteger", "OFTInteger64"}:
        return "int64"
    if ogr_type in {"OFTString", "OFTWideString"}:
        return "string"
    qualifier = "nullable " if nullable else ""
    raise PreparedDataError(f"Identifier field {field} must be a {qualifier}integer or string")


def _vector_asset(
    path: Path,
    root: Path,
    source: Path,
    layer: str | None,
    fields: list[str],
    field_types: list[str],
    count: int,
    geometry_type: str,
    nullable_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        **_file_asset(path, root, role="vector"),
        "driver": "FlatGeobuf",
        "fields": fields,
        "field_types": field_types,
        "nullable_fields": nullable_fields or [],
        "geometry_type": geometry_type,
        "feature_count": count,
        "source": _vector_source_record(source, layer),
    }


def _raster_asset(
    path: Path,
    root: Path,
    kind: str,
    source: Path,
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
        "source": _raster_source_record(source),
    }
    if encoding is not None:
        result["encoding"] = encoding
    return result


def _file_asset(path: Path, root: Path, *, role: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "role": role,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _source_record(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(Path(path).resolve()),
        "bytes": Path(path).stat().st_size,
        "sha256": _sha256(path),
    }
    return result


def _vector_source_record(path: Path, layer: str | None) -> dict[str, Any]:
    info = pyogrio.read_info(path, layer=layer)
    result = {
        **_source_record(path),
        "driver": info["driver"],
        "layer": layer or info["layer_name"],
        "crs": info["crs"],
        "geometry_type": info["geometry_type"],
        "fields": list(info["fields"]),
        "field_types": list(info["ogr_types"]),
    }
    return result


def _raster_source_record(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as src:
        return {
            **_source_record(path),
            "driver": src.driver,
            "band": 1,
            "crs_wkt": CRS.from_user_input(src.crs).to_wkt(version="WKT2_2019", pretty=False),
            "transform": list(src.transform)[:6],
            "width": src.width,
            "height": src.height,
            "dtype": src.dtypes[0],
            "nodata": _manifest_number(src.nodata),
        }


def _manifest_number(value):
    if value is None or math.isfinite(float(value)):
        return value
    if math.isnan(float(value)):
        return "NaN"
    return "Infinity" if value > 0 else "-Infinity"


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _producer_version() -> str:
    try:
        return importlib.metadata.version("mgb-vec-hydro")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"
