"""Bounded Arrow access to raw GeoPackage and FlatGeobuf providers."""

from __future__ import annotations

import math
from collections.abc import Hashable, Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyogrio
from pyproj import CRS

from mgb_vec_hydro.exceptions import InvalidInputSchemaError
from mgb_vec_hydro.execution.executor import WorkerContext

SUPPORTED_PROVIDER_DRIVERS = {"GPKG", "FlatGeobuf"}


@dataclass(frozen=True)
class VectorProvider:
    path: Path
    layer: str | None
    driver: str
    fields: tuple[str, ...]
    geometry_type: str
    feature_count: int
    crs: CRS
    fid_column: str


def inspect_vector_provider(
    path: str | Path,
    *,
    layer: str | None = None,
    source_crs: str | None = None,
) -> VectorProvider:
    """Inspect schema and CRS without reading feature geometry."""
    path = Path(path)
    try:
        info = pyogrio.read_info(path, layer=layer)
    except Exception as exc:
        raise InvalidInputSchemaError(f"Cannot inspect vector provider: {path}") from exc
    driver = info.get("driver")
    if driver not in SUPPORTED_PROVIDER_DRIVERS:
        raise InvalidInputSchemaError(
            "Raw vector provider must be GeoPackage or FlatGeobuf"
        )
    value = source_crs or info.get("crs")
    if value is None:
        raise InvalidInputSchemaError(
            "Vector provider has no CRS; supply a source-CRS override"
        )
    try:
        crs = CRS.from_user_input(value)
    except Exception as exc:
        raise InvalidInputSchemaError("Vector provider CRS is invalid") from exc
    return VectorProvider(
        path.resolve(), layer, driver, tuple(info["fields"]),
        str(info["geometry_type"]), int(info["features"]), crs,
        str(info.get("fid_column") or "fid"),
    )


def resolve_provider_field(provider: VectorProvider, requested: str) -> str:
    exact = [field for field in provider.fields if field == requested]
    matches = exact or [
        field for field in provider.fields if field.casefold() == requested.casefold()
    ]
    if len(matches) != 1:
        reason = "ambiguous" if matches else "missing"
        raise InvalidInputSchemaError(
            f"Vector provider has {reason} required field: {requested}"
        )
    return matches[0]


def iter_provider_batches(
    provider: VectorProvider,
    *,
    columns: Sequence[str],
    batch_size: int = 10_000,
    read_geometry: bool = False,
    where: str | None = None,
    fids: Sequence[int] | None = None,
    return_fids: bool = False,
    context: WorkerContext | None = None,
) -> Iterator[pa.RecordBatch]:
    """Yield hard-bounded Arrow batches under the shared I/O semaphore."""
    if batch_size <= 0:
        raise InvalidInputSchemaError("Vector batch size must be positive")
    unknown = set(columns) - set(provider.fields)
    if unknown:
        raise InvalidInputSchemaError(
            "Vector provider lacks column(s): " + ", ".join(sorted(unknown))
        )
    guard = context.io_bound() if context is not None else nullcontext()
    try:
        with guard, pyogrio.open_arrow(
            provider.path,
            layer=provider.layer,
            columns=list(columns),
            batch_size=batch_size,
            read_geometry=read_geometry,
            where=where,
            fids=list(fids) if fids is not None else None,
            return_fids=return_fids,
            use_pyarrow=True,
        ) as (_, batches):
            yield from batches
    except InvalidInputSchemaError:
        raise
    except Exception as exc:
        raise InvalidInputSchemaError("Cannot stream vector provider") from exc


def id_predicate(field: str, values: Sequence[Hashable]) -> str:
    """Build a quoted provider predicate without interpolating raw SQL."""
    if not values:
        raise InvalidInputSchemaError("ID predicate requires at least one value")
    identifier = '"' + field.replace('"', '""') + '"'
    literals: list[str] = []
    for value in values:
        if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
            literals.append(str(int(value)))
        elif isinstance(value, (float, np.floating)) and math.isfinite(float(value)):
            literals.append(repr(float(value)))
        elif isinstance(value, str):
            literals.append("'" + value.replace("'", "''") + "'")
        else:
            raise InvalidInputSchemaError(f"Unsupported provider ID value: {value!r}")
    return f"{identifier} IN ({','.join(literals)})"


def scan_id_fids(
    provider: VectorProvider,
    id_column: str,
    *,
    batch_size: int = 10_000,
    context: WorkerContext | None = None,
) -> dict[Hashable, int]:
    """Perform one geometry-free scan and verify unique IDs."""
    result: dict[Hashable, int] = {}
    for batch in iter_provider_batches(
        provider, columns=(id_column,), batch_size=batch_size,
        read_geometry=False, return_fids=True, context=context,
    ):
        fid_index = max(batch.schema.get_field_index(provider.fid_column), 0)
        for value, fid in zip(
            batch[id_column].to_pylist(), batch.column(fid_index).to_pylist(), strict=True
        ):
            if value in result:
                raise InvalidInputSchemaError(f"Vector provider contains duplicate ID: {value}")
            result[value] = int(fid)
    return result


def conservative_geometry_packet_rows(
    provider: VectorProvider,
    *,
    memory_limit_bytes: int,
    requested_rows: int = 10_000,
) -> int:
    """Reduce packet admission as the per-feature source estimate nears budget."""
    if memory_limit_bytes <= 0 or requested_rows <= 0:
        raise InvalidInputSchemaError("Geometry packet limits must be positive")
    try:
        source_bytes = max(1, provider.path.stat().st_size)
    except OSError as exc:
        raise InvalidInputSchemaError("Cannot estimate vector provider size") from exc
    per_feature = max(4096, math.ceil(source_bytes / max(1, provider.feature_count)) * 4)
    return max(1, min(requested_rows, memory_limit_bytes // per_feature))
