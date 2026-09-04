"""Raw-provider ROI selection and normalized Stage 1 publication."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyogrio
from pyproj import CRS, Geod, Transformer
from shapely.ops import transform as shapely_transform

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    InvalidInputSchemaError,
    OutletNotFoundError,
    PreparedDataError,
    TopologyCycleError,
)
from mgb_vec_hydro.execution.checkpoints import (
    CheckpointStore,
    JsonCheckpointCodec,
    execution_fingerprint,
)
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.vector import (
    conservative_geometry_packet_rows,
    id_predicate,
    inspect_vector_provider,
)
from mgb_vec_hydro.crs_utils import parse_crs

DEFAULT_STRAHLER_ORDER_COL = "strahler_order"
ROI_CONTRACT = "mgb-roi-dataset"
ROI_CONTRACT_VERSION = 2
ROI_COLUMNS = [
    "id", "id_down", "sub", "strahler_order", "unit_length",
    "upstream_length", "unit_area", "upstream_area", "water_course", "geometry",
]


@dataclass(frozen=True)
class RoiSpec:
    crs: str
    catchments: Path
    segments: Path
    outlet_ids: tuple[str, ...]
    id_col: str
    id_down_col: str
    strahler_order_col: str
    output_dir: Path
    catchments_layer: str | None = None
    segments_layer: str | None = None
    catchments_source_crs: str | None = None
    segments_source_crs: str | None = None
    workers: int = 4
    memory_limit_mb: int = 512
    io_slots: int = 2
    batch_size: int = 10_000
    checkpoint_dir: Path | None = None


@dataclass(frozen=True)
class RoiReport:
    output_dir: Path
    manifest: Path
    catchment_count: int
    segment_count: int


class RoiDataset:
    """Lightweight reader and validator for the versioned ROI directory."""

    def __init__(self, root: Path, manifest: dict[str, Any]):
        self.root = root
        self.manifest = manifest

    @classmethod
    def open(cls, root: str | Path) -> RoiDataset:
        root = Path(root)
        manifest_path = root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreparedDataError(f"Cannot read ROI manifest: {manifest_path}") from exc
        return cls(root, manifest)

    def validate(self) -> None:
        value = self.manifest
        if value.get("contract") != ROI_CONTRACT or value.get("version") != ROI_CONTRACT_VERSION:
            raise PreparedDataError("Unsupported ROI dataset contract or version")
        assets = value.get("assets")
        if not isinstance(assets, dict) or set(assets) != {"catchments", "segments"}:
            raise PreparedDataError("ROI manifest must define catchments and segments")
        try:
            expected_crs = CRS.from_wkt(value["crs_wkt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PreparedDataError("ROI manifest CRS is invalid") from exc
        for name in ("catchments", "segments"):
            asset = assets[name]
            if not isinstance(asset, dict) or asset.get("driver") != "FlatGeobuf":
                raise PreparedDataError(f"ROI {name} asset must be FlatGeobuf")
            relative = asset.get("path")
            if not isinstance(relative, str):
                raise PreparedDataError(f"ROI {name} asset path is invalid")
            path = (self.root / relative).resolve()
            try:
                path.relative_to(self.root.resolve())
            except ValueError as exc:
                raise PreparedDataError(f"ROI asset escapes dataset: {relative}") from exc
            if not path.is_file():
                raise PreparedDataError(f"ROI asset is missing: {relative}")
            info = pyogrio.read_info(path)
            if list(info["fields"]) != ROI_COLUMNS[:-1] or info["features"] != asset.get("feature_count"):
                raise PreparedDataError(f"ROI {name} asset does not match its manifest")
            if info.get("crs") is None or CRS.from_user_input(info["crs"]) != expected_crs:
                raise PreparedDataError(f"ROI {name} asset has an incompatible CRS")

    def path(self, name: str) -> Path:
        self.validate()
        try:
            return self.root / self.manifest["assets"][name]["path"]
        except KeyError as exc:
            raise PreparedDataError(f"Unknown ROI asset: {name}") from exc


@dataclass(frozen=True)
class _Provider:
    path: Path
    layer: str | None
    info: dict[str, Any]
    crs: CRS
    fields: dict[str, str]


def define_roi_dataset(spec: RoiSpec) -> RoiReport:
    """Select from raw providers and atomically publish a normalized ROI."""
    _validate_spec(spec)
    target_crs = parse_crs(spec.crs)
    checkpoint = _roi_checkpoint(spec, target_crs)
    segment_provider = _provider(
        spec.segments, spec.segments_layer, spec.segments_source_crs,
        {"id": spec.id_col, "id_down": spec.id_down_col,
         "strahler_order": spec.strahler_order_col}, "segments",
    )
    catchment_provider = _provider(
        spec.catchments, spec.catchments_layer, spec.catchments_source_crs,
        {"id": spec.id_col}, "catchments",
    )
    topology, segment_fids = _read_topology(segment_provider, spec.batch_size)
    outlet_ids = _coerce_outlets(spec.outlet_ids, topology["id"])
    selected_ids, sub_by_id = _select_topology(topology, outlet_ids)
    selected = topology.loc[topology["id"].isin(selected_ids)].copy()
    _validate_selected_attributes(selected)

    memory_bytes = spec.memory_limit_mb * 1024 * 1024
    metrics = _compute_selected_metrics(
        selected_ids, segment_provider, catchment_provider, segment_fids,
        batch_size=spec.batch_size, memory_bytes=memory_bytes,
    )
    upstream_length, upstream_area = _upstream_metrics(selected, metrics)
    metric_attributes = _metric_attributes(
        selected, sub_by_id, metrics, upstream_length, upstream_area,
    )
    water_course = _water_course_by_segment(metric_attributes)

    output = Path(spec.output_dir)
    publisher = AtomicOutputDirectory(output)
    with publisher as staging:
        vectors = staging / "vectors"
        vectors.mkdir()
        catchments_path = vectors / "roi_catchments.fgb"
        segments_path = vectors / "roi_segments.fgb"
        _write_selected_outputs(
            selected, sub_by_id, metrics, upstream_length, upstream_area,
            water_course, segment_provider, catchment_provider, segment_fids,
            target_crs, batch_size=spec.batch_size, memory_bytes=memory_bytes,
            catchments_path=catchments_path, segments_path=segments_path,
        )
        manifest = {
            "contract": ROI_CONTRACT,
            "version": ROI_CONTRACT_VERSION,
            "crs_wkt": target_crs.to_wkt(version="WKT2_2019", pretty=False),
            "assets": {
                "catchments": _asset(catchments_path, staging, len(selected_ids)),
                "segments": _asset(segments_path, staging, len(selected_ids)),
            },
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        RoiDataset(staging, manifest).validate()
        publisher.publish((
            "manifest.json", "vectors/roi_catchments.fgb", "vectors/roi_segments.fgb"
        ))

    if checkpoint is not None:
        checkpoint.cleanup()
    return RoiReport(
        output, output / "manifest.json", len(selected_ids), len(selected_ids)
    )


def _roi_checkpoint(
    spec: RoiSpec, target_crs: CRS
) -> CheckpointStore[Any] | None:
    if spec.checkpoint_dir is None:
        return None
    sources = []
    for path in (spec.catchments, spec.segments):
        stat = Path(path).stat()
        sources.append(
            {"path": str(Path(path).resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        )
    fingerprint = execution_fingerprint(
        algorithm="define-roi",
        version="1",
        prepared_manifest={"crs_wkt": target_crs.to_wkt(version="WKT2_2019", pretty=False)},
        parameters={
            "sources": sources,
            "outlets": list(spec.outlet_ids),
            "columns": [spec.id_col, spec.id_down_col, spec.strahler_order_col],
            "layers": [spec.catchments_layer, spec.segments_layer],
            "source_crs": [spec.catchments_source_crs, spec.segments_source_crs],
        },
        work_items=(),
    )
    return CheckpointStore(spec.checkpoint_dir, fingerprint, JsonCheckpointCodec())


def define_roi(spec: RoiSpec) -> RoiReport:
    """Public Stage 1 entry point."""
    return define_roi_dataset(spec)


def _validate_spec(spec: RoiSpec) -> None:
    for label, path in (("catchments", spec.catchments), ("segments", spec.segments)):
        if not Path(path).exists():
            raise PreparedDataError(f"{label} input does not exist: {path}")
    if not spec.outlet_ids:
        raise InvalidInputSchemaError("At least one outlet ID is required")
    for name, value in (("workers", spec.workers), ("memory limit", spec.memory_limit_mb), ("I/O slots", spec.io_slots), ("batch size", spec.batch_size)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidInputSchemaError(f"{name} must be a positive integer")
    if spec.workers > 4:
        raise InvalidInputSchemaError("workers cannot exceed four")
    parse_crs(spec.crs)


def _provider(path: Path, layer: str | None, override: str | None, requested: dict[str, str], label: str) -> _Provider:
    source = inspect_vector_provider(path, layer=layer, source_crs=override)
    info = {
        "fields": source.fields,
        "driver": source.driver,
        "crs": source.crs,
        "geometry_type": source.geometry_type,
        "features": source.feature_count,
        "fid_column": source.fid_column,
    }
    fields = {name: _resolve_field(info, value, label) for name, value in requested.items()}
    return _Provider(Path(path), layer, info, source.crs, fields)


def _resolve_field(info: dict[str, Any], requested: str, label: str) -> str:
    fields = list(info.get("fields", ()))
    exact = [field for field in fields if field == requested]
    matches = exact or [field for field in fields if field.casefold() == requested.casefold()]
    if len(matches) != 1:
        reason = "ambiguous" if matches else "missing"
        raise InvalidInputSchemaError(
            f"{label} provider has {reason} required field: {requested}"
        )
    return matches[0]


def _read_topology(provider: _Provider, batch_size: int) -> tuple[pd.DataFrame, dict[Hashable, int]]:
    columns = list(provider.fields.values())
    batches: list[pa.RecordBatch] = []
    try:
        with pyogrio.open_arrow(
            provider.path, layer=provider.layer, columns=columns,
            read_geometry=False, return_fids=True, batch_size=batch_size,
            use_pyarrow=True,
        ) as (_, stream):
            for batch in stream:
                order = batch[provider.fields["strahler_order"]]
                if not (pa.types.is_integer(order.type) or pa.types.is_floating(order.type) or pa.types.is_decimal(order.type)):
                    raise InvalidInputSchemaError("Strahler order must be numeric")
                numeric = pc.cast(order, pa.float64())
                mask = pc.fill_null(
                    pc.and_(pc.is_finite(numeric), pc.greater_equal(numeric, 1.0)),
                    False,
                )
                filtered = batch.filter(mask)
                if filtered.num_rows:
                    batches.append(filtered)
    except InvalidInputSchemaError:
        raise
    except Exception as exc:
        raise InvalidInputSchemaError(
            "Cannot stream segment topology attributes"
        ) from exc
    if not batches:
        raise InvalidInputSchemaError("No segments remain after Strahler filtering")
    table = pa.Table.from_batches(batches).combine_chunks()
    rename = {actual: normalized for normalized, actual in provider.fields.items()}
    frame = table.to_pandas().rename(columns=rename)
    fid_name = provider.info.get("fid_column") or "fid"
    fid_column = fid_name if fid_name in frame else frame.columns[0]
    duplicated = frame.loc[frame["id"].duplicated(keep=False), "id"].tolist()
    if duplicated:
        raise DuplicateSegmentIdError(
            "Found duplicate segment ID(s): " + ", ".join(map(str, duplicated[:20]))
        )
    fids = dict(zip(frame["id"], frame[fid_column], strict=True))
    return (
        frame[["id", "id_down", "strahler_order"]].reset_index(drop=True),
        fids,
    )


def _coerce_outlets(values: Iterable[str], ids: pd.Series) -> list[Hashable]:
    dtype = ids.dtype
    result: list[Hashable] = []
    for value in values:
        try:
            if pd.api.types.is_integer_dtype(dtype):
                result.append(int(value))
            elif pd.api.types.is_float_dtype(dtype):
                result.append(float(value))
            else:
                result.append(value)
        except ValueError as exc:
            raise OutletNotFoundError(
                f"Outlet ID is incompatible with provider IDs: {value}"
            ) from exc
    return result


def _select_topology(frame: pd.DataFrame, outlets: list[Hashable]) -> tuple[set[Hashable], dict[Hashable, int]]:
    ids = set(frame["id"].tolist())
    missing = [value for value in outlets if value not in ids]
    if missing:
        raise OutletNotFoundError(
            "Outlet segment ID(s) not found after Strahler filtering: "
            + ", ".join(map(str, missing))
        )
    upstream: dict[Hashable, list[Hashable]] = defaultdict(list)
    downstream = dict(frame[["id", "id_down"]].itertuples(index=False, name=None))
    for segment_id, downstream_id in downstream.items():
        if downstream_id in ids:
            upstream[downstream_id].append(segment_id)
    selected: set[Hashable] = set()
    sub_by_id: dict[Hashable, int] = {}
    count = len(outlets)
    for outlet_index, outlet in enumerate(outlets):
        stack = [outlet]
        domain: set[Hashable] = set()
        while stack:
            current = stack.pop()
            if current in domain:
                continue
            domain.add(current)
            stack.extend(upstream.get(current, ()))
        sub = count - outlet_index
        selected.update(domain)
        sub_by_id.update(dict.fromkeys(domain, sub))
    _topological_order(selected, downstream)
    outlet_set = set(outlets)
    for segment_id in selected - outlet_set:
        if downstream[segment_id] not in selected:
            raise InvalidInputSchemaError(
                f"Selected segment {segment_id} does not connect toward a selected outlet"
            )
    return selected, sub_by_id


def _topological_order(ids: set[Hashable], downstream: dict[Hashable, Hashable]) -> list[Hashable]:
    upstream_count = dict.fromkeys(ids, 0)
    for downstream_id in downstream.values():
        if downstream_id in ids:
            upstream_count[downstream_id] += 1
    ready = sorted(
        (value for value, count in upstream_count.items() if count == 0),
        key=str, reverse=True,
    )
    order: list[Hashable] = []
    while ready:
        current = ready.pop()
        order.append(current)
        target = downstream.get(current)
        if target in upstream_count:
            upstream_count[target] -= 1
            if upstream_count[target] == 0:
                ready.append(target)
                ready.sort(key=str, reverse=True)
    if len(order) != len(ids):
        raise TopologyCycleError("Detected topology cycle in the selected ROI")
    return order


def _validate_selected_attributes(frame: pd.DataFrame) -> None:
    order = pd.to_numeric(frame["strahler_order"], errors="coerce").to_numpy(dtype=float)
    if not np.all(np.isfinite(order)) or not np.all(order == np.floor(order)):
        raise InvalidInputSchemaError(
            "Selected Strahler orders must be finite integral values"
        )


def _iter_selected(
    provider: _Provider,
    ids: set[Hashable],
    *,
    batch_size: int,
    memory_bytes: int,
    known_fids: dict[Hashable, int] | None = None,
) -> Iterable[gpd.GeoDataFrame]:
    ordered = sorted(ids, key=lambda value: (type(value).__name__, str(value)))
    source = inspect_vector_provider(
        provider.path, layer=provider.layer, source_crs=provider.crs.to_string()
    )
    rows_per_packet = conservative_geometry_packet_rows(
        source, memory_limit_bytes=memory_bytes, requested_rows=batch_size
    )
    frames: list[gpd.GeoDataFrame] = []
    # Use provider-side predicates for ordinary selections. If unsupported,
    # make exactly one ID/FID scan and issue bounded random-FID reads.
    if len(ordered) <= 2_000:
        try:
            packet_size = min(rows_per_packet, 400)
            for start in range(0, len(ordered), packet_size):
                values = ordered[start : start + packet_size]
                frames.append(pyogrio.read_dataframe(
                    provider.path, layer=provider.layer,
                    columns=list(dict.fromkeys(provider.fields.values())),
                    where=id_predicate(provider.fields["id"], values),
                    use_arrow=True,
                ))
        except (
            pyogrio.errors.DataLayerError,
            pyogrio.errors.DataSourceError,
            pyogrio.errors.FeatureError,
            pyogrio.errors.FieldError,
            pyogrio.errors.GeometryError,
            RuntimeError,
            ValueError,
        ):
            frames.clear()
    if not frames:
        fid_by_id = known_fids or _scan_fids(provider, batch_size)
        missing = ids - set(fid_by_id)
        if missing:
            raise InvalidInputSchemaError(
                "Selected catchment ID(s) are missing: "
                + ", ".join(map(str, sorted(missing, key=str)))
            )
        selected_fids = [fid_by_id[value] for value in ordered]
        try:
            for start in range(0, len(selected_fids), rows_per_packet):
                frames.append(pyogrio.read_dataframe(
                    provider.path, layer=provider.layer,
                    columns=list(dict.fromkeys(provider.fields.values())),
                    fids=selected_fids[start : start + rows_per_packet],
                    use_arrow=True,
                ))
        except Exception as exc:
            raise InvalidInputSchemaError(
                "Cannot read selected provider geometries by FID"
            ) from exc
    if not frames:
        raise InvalidInputSchemaError("Provider returned no selected geometries")
    frame = pd.concat(frames, ignore_index=True)
    frame = gpd.GeoDataFrame(frame, geometry=frames[0].geometry.name, crs=frames[0].crs)
    frame = frame.rename(columns={actual: normalized for normalized, actual in provider.fields.items()})
    if frame.geometry.name != "geometry":
        frame = frame.rename_geometry("geometry")
    returned = frame["id"]
    duplicates = returned[returned.duplicated(keep=False)].tolist()
    if duplicates:
        raise InvalidInputSchemaError(
            "Provider returned duplicate selected ID(s): "
            + ", ".join(map(str, duplicates[:20]))
        )
    missing = ids - set(returned.tolist())
    extra = set(returned.tolist()) - ids
    if missing or extra:
        raise InvalidInputSchemaError(
            "Provider selection did not return the exact requested IDs"
        )
    estimate = int(frame.memory_usage(index=True, deep=True).sum())
    estimate += sum(len(value) if value else 0 for value in frame.geometry.to_wkb())
    if estimate > memory_bytes:
        raise InvalidInputSchemaError(
            "Selected geometry packet exceeds the configured memory budget"
        )
    yield frame[[*provider.fields.keys(), "geometry"]]


def _scan_fids(provider: _Provider, batch_size: int) -> dict[Hashable, int]:
    result: dict[Hashable, int] = {}
    try:
        with pyogrio.open_arrow(
            provider.path, layer=provider.layer,
            columns=[provider.fields["id"]], read_geometry=False,
            return_fids=True, batch_size=batch_size, use_pyarrow=True,
        ) as (metadata, batches):
            fid_name = metadata.get("fid_column") or "fid"
            for batch in batches:
                fid_index = batch.schema.get_field_index(fid_name)
                fid_index = max(fid_index, 0)
                values = zip(
                    batch[provider.fields["id"]].to_pylist(),
                    batch.column(fid_index).to_pylist(), strict=True,
                )
                for segment_id, fid in values:
                    if segment_id in result:
                        raise InvalidInputSchemaError(
                            f"Provider contains duplicate ID: {segment_id}"
                        )
                    result[segment_id] = fid
    except InvalidInputSchemaError:
        raise
    except Exception as exc:
        raise InvalidInputSchemaError("Cannot scan provider IDs and FIDs") from exc
    return result


def _iter_selected_bounded(
    provider: _Provider, ids: set[Hashable], *, batch_size: int,
    memory_bytes: int, known_fids: dict[Hashable, int] | None = None,
) -> Iterable[gpd.GeoDataFrame]:
    """Yield selected geometries without concatenating packets."""
    source = inspect_vector_provider(
        provider.path, layer=provider.layer, source_crs=provider.crs.to_string()
    )
    rows_per_packet = conservative_geometry_packet_rows(
        source, memory_limit_bytes=memory_bytes, requested_rows=batch_size
    )
    ordered = sorted(ids, key=lambda value: (type(value).__name__, str(value)))
    if known_fids is not None:
        missing = ids - set(known_fids)
        if missing:
            raise InvalidInputSchemaError("Selected provider ID(s) are missing")
        requests = [
            ("fids", [known_fids[value] for value in ordered[start : start + rows_per_packet]])
            for start in range(0, len(ordered), rows_per_packet)
        ]
    else:
        packet_size = min(rows_per_packet, 400)
        requests = [
            ("where", ordered[start : start + packet_size])
            for start in range(0, len(ordered), packet_size)
        ]
    seen: set[Hashable] = set()
    for mode, request in requests:
        try:
            read_options = {mode: request}
            if mode == "where":
                read_options[mode] = id_predicate(provider.fields["id"], request)
            frame = pyogrio.read_dataframe(
                provider.path, layer=provider.layer,
                columns=list(dict.fromkeys(provider.fields.values())),
                use_arrow=True, **read_options,
            )
        except Exception as exc:
            raise InvalidInputSchemaError("Cannot read selected provider geometries") from exc
        frame = gpd.GeoDataFrame(frame, geometry=frame.geometry.name, crs=provider.crs)
        frame = frame.rename(columns={actual: normalized for normalized, actual in provider.fields.items()})
        if frame.geometry.name != "geometry":
            frame = frame.rename_geometry("geometry")
        frame = frame.sort_values("id", key=lambda values: values.map(lambda value: (type(value).__name__, str(value))))
        returned = frame["id"].tolist()
        if seen.intersection(returned):
            raise InvalidInputSchemaError("Provider returned duplicate selected IDs")
        seen.update(returned)
        estimate = int(frame.memory_usage(index=True, deep=True).sum())
        estimate += sum(len(value) if value else 0 for value in frame.geometry.to_wkb())
        if estimate > memory_bytes:
            raise InvalidInputSchemaError("Selected geometry packet exceeds the configured memory budget")
        yield frame[[*provider.fields.keys(), "geometry"]]
    if seen != ids:
        raise InvalidInputSchemaError("Provider selection did not return the exact requested IDs")


@lru_cache(maxsize=16)
def _geodetic_tools(crs_text: str) -> tuple[Transformer, Geod]:
    crs = CRS.from_wkt(crs_text)
    geodetic = crs.geodetic_crs
    if geodetic is None or crs.ellipsoid.semi_major_metre is None:
        raise InvalidInputSchemaError("Source CRS has no usable geodetic ellipsoid")
    ellipsoid = crs.ellipsoid
    if ellipsoid.inverse_flattening == 0:
        geod = Geod(a=ellipsoid.semi_major_metre, b=ellipsoid.semi_minor_metre)
    else:
        geod = Geod(a=ellipsoid.semi_major_metre, rf=ellipsoid.inverse_flattening)
    return Transformer.from_crs(crs, geodetic, always_xy=True), geod


def _geometry_metric(geometry, provider: _Provider, kind: str) -> float:
    _validate_geometry(
        gpd.GeoDataFrame({"geometry": [geometry]}, geometry="geometry", crs=provider.crs),
        kind, {"LineString", "MultiLineString"} if kind == "segments" else {"Polygon", "MultiPolygon"},
    )
    transformer, geod = _geodetic_tools(provider.crs.to_wkt())
    geographic = shapely_transform(transformer.transform, geometry)
    if kind == "segments":
        value = geod.geometry_length(geographic) / 1000.0
    else:
        value = abs(geod.geometry_area_perimeter(geographic)[0]) / 1_000_000.0
    if not np.isfinite(value) or value < 0:
        raise InvalidInputSchemaError(f"Selected {kind} produced an invalid metric")
    return float(value)


def _compute_selected_metrics(ids, segments, catchments, segment_fids, *, batch_size, memory_bytes):
    metrics = {}
    for frame in _iter_selected_bounded(segments, ids, batch_size=batch_size, memory_bytes=memory_bytes, known_fids=segment_fids):
        for row in frame.itertuples(index=False):
            metrics.setdefault(row.id, {})["unit_length"] = _geometry_metric(row.geometry, segments, "segments")
    for frame in _iter_selected_bounded(catchments, ids, batch_size=batch_size, memory_bytes=memory_bytes):
        for row in frame.itertuples(index=False):
            if row.id not in metrics:
                raise InvalidInputSchemaError(f"Catchment ID is not present in selected segments: {row.id}")
            metrics[row.id]["unit_area"] = _geometry_metric(row.geometry, catchments, "catchments")
    if set(metrics) != ids or any("unit_area" not in value for value in metrics.values()):
        raise InvalidInputSchemaError("Selected segment and catchment IDs do not match")
    return metrics


def _upstream_metrics(attrs, metrics):
    downstream = dict(attrs[["id", "id_down"]].itertuples(index=False, name=None))
    length = {key: value["unit_length"] for key, value in metrics.items()}
    area = {key: value["unit_area"] for key, value in metrics.items()}
    for segment_id in _topological_order(set(metrics), downstream):
        target = downstream.get(segment_id)
        if target in length:
            length[target] += length[segment_id]
            area[target] += area[segment_id]
    return length, area


def _metric_attributes(attrs, sub_by_id, metrics, upstream_length, upstream_area):
    result = attrs.copy()
    result["sub"] = result["id"].map(sub_by_id)
    result["unit_length"] = result["id"].map(lambda value: metrics[value]["unit_length"])
    result["upstream_length"] = result["id"].map(upstream_length)
    result["unit_area"] = result["id"].map(lambda value: metrics[value]["unit_area"])
    result["upstream_area"] = result["id"].map(upstream_area)
    result["strahler_order"] = result["strahler_order"].astype("int64")
    return result


def _write_selected_outputs(attrs, sub_by_id, metrics, upstream_length, upstream_area, water_course,
                            segment_provider, catchment_provider, segment_fids, target_crs, *,
                            batch_size, memory_bytes, catchments_path, segments_path):
    lookup = attrs.set_index("id").to_dict("index")
    for provider, fids, path, kind in ((segment_provider, segment_fids, segments_path, "segments"), (catchment_provider, None, catchments_path, "catchments")):
        first = True
        for frame in _iter_selected_bounded(provider, set(lookup), batch_size=batch_size, memory_bytes=memory_bytes, known_fids=fids):
            rows = []
            for row in frame.itertuples(index=False):
                item = lookup[row.id]
                values = {
                    "id": row.id, "id_down": item["id_down"], "sub": sub_by_id[row.id],
                    "strahler_order": int(item["strahler_order"]),
                    "unit_length": metrics[row.id]["unit_length"], "upstream_length": upstream_length[row.id],
                    "unit_area": metrics[row.id]["unit_area"], "upstream_area": upstream_area[row.id],
                    "water_course": water_course[row.id], "geometry": row.geometry,
                }
                rows.append(values)
            output = gpd.GeoDataFrame(rows, columns=ROI_COLUMNS, geometry="geometry", crs=provider.crs).to_crs(target_crs)
            _validate_geometry(output, kind, {"LineString", "MultiLineString"} if kind == "segments" else {"Polygon", "MultiPolygon"})
            _write_fgb(output, path, append=not first)
            first = False


def _validate_geometry(frame: gpd.GeoDataFrame, name: str, allowed: set[str]) -> None:
    if frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise InvalidInputSchemaError(
            f"Selected {name} contain null or empty geometry"
        )
    bad_types = sorted(set(frame.geom_type) - allowed)
    if bad_types:
        raise InvalidInputSchemaError(
            f"Selected {name} have invalid geometry type(s): " + ", ".join(bad_types)
        )
    if not frame.geometry.is_valid.all():
        raise InvalidInputSchemaError(f"Selected {name} contain invalid geometry")


def _water_course_by_segment(segments: gpd.GeoDataFrame) -> dict[Hashable, Hashable]:
    result: dict[Hashable, Hashable] = {}
    for _, group in segments.groupby("sub", sort=False):
        ids = set(group["id"].tolist())
        downstream = dict(group[["id", "id_down"]].itertuples(index=False, name=None))
        upstream: dict[Hashable, list[Hashable]] = defaultdict(list)
        for segment_id, target in downstream.items():
            if target in ids:
                upstream[target].append(segment_id)
        attrs = group.set_index("id")
        for segment_id in reversed(_topological_order(ids, downstream)):
            result.setdefault(segment_id, segment_id)
            children = upstream.get(segment_id, ())
            if children:
                main = max(
                    children,
                    key=lambda child: (
                        attrs.at[child, "upstream_area"],
                        attrs.at[child, "unit_length"], str(child),
                    ),
                )
                for child in children:
                    result[child] = result[segment_id] if child == main else child
    return result


def _write_fgb(frame: gpd.GeoDataFrame, path: Path, *, append: bool = False) -> None:
    pyogrio.write_dataframe(
        frame, path, driver="FlatGeobuf", append=append, use_arrow=True,
        SPATIAL_INDEX="YES",
    )


def _asset(path: Path, root: Path, count: int) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "driver": "FlatGeobuf", "feature_count": count,
        "fields": ROI_COLUMNS[:-1],
    }
