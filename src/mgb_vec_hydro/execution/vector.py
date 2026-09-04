"""Bounded Arrow access to raw vector providers."""

from __future__ import annotations

import math
from collections.abc import Hashable, Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.ipc as ipc
import pyogrio
from pyproj import CRS
import shapely

from mgb_vec_hydro.exceptions import InvalidInputSchemaError
from mgb_vec_hydro.execution.executor import WorkerContext

SUPPORTED_PROVIDER_DRIVERS = {"GPKG", "FlatGeobuf", "OpenFileGDB"}


@dataclass(frozen=True)
class VectorTable:
    """Arrow-native vector table with explicit geometry and CRS metadata."""

    table: pa.Table
    crs: CRS
    geometry_column: str
    geometry_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.table, pa.Table):
            raise InvalidInputSchemaError("Vector data must be a PyArrow table")
        if self.geometry_column not in self.table.column_names:
            raise InvalidInputSchemaError(
                f"Vector geometry column is missing: {self.geometry_column}"
            )
        field = self.table.schema.field(self.geometry_column)
        if not (pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type)):
            raise InvalidInputSchemaError("Vector geometry must use WKB binary values")
        try:
            object.__setattr__(self, "crs", CRS.from_user_input(self.crs))
        except Exception as exc:
            raise InvalidInputSchemaError("Vector CRS is invalid") from exc
        if not isinstance(self.geometry_type, str) or not self.geometry_type:
            raise InvalidInputSchemaError("Vector geometry type is missing")

    def __len__(self) -> int:
        return self.table.num_rows

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(
            "geometry" if name == self.geometry_column else name
            for name in self.table.column_names
        )

    def geometries(self) -> np.ndarray:
        """Decode the WKB column with Shapely's vectorized reader."""
        try:
            values = (
                self.table[self.geometry_column]
                .combine_chunks()
                .to_numpy(zero_copy_only=False)
            )
            return shapely.from_wkb(values, on_invalid="raise")
        except Exception as exc:
            raise InvalidInputSchemaError(
                "Vector contains invalid WKB geometry"
            ) from exc

    def to_pandas(self, *, decode_geometry: bool = True):
        """Return a Pandas view, optionally decoding its geometry objects."""
        frame = self.table.to_pandas()
        if decode_geometry:
            frame[self.geometry_column] = self.geometries()
            if self.geometry_column != "geometry":
                frame = frame.rename(columns={self.geometry_column: "geometry"})
        return frame

    @classmethod
    def from_pydict(
        cls,
        data: dict,
        geometries,
        *,
        crs: str | CRS,
        geometry_type: str = "Unknown",
        geometry_column: str = "geometry",
    ) -> "VectorTable":
        """Construct a vector table from attributes and Shapely geometries."""
        arrays = {name: pa.array(values) for name, values in data.items()}
        if "id" in arrays:
            for name, array in tuple(arrays.items()):
                if pa.types.is_null(array.type) and name in {"id_down", "down"}:
                    arrays[name] = pa.array(data[name], type=arrays["id"].type)
        table = pa.Table.from_pydict(arrays).append_column(
            geometry_column,
            pa.array(shapely.to_wkb(list(geometries)), type=pa.binary()),
        )
        return cls(table, CRS.from_user_input(crs), geometry_column, geometry_type)


class VectorTableCheckpointCodec:
    """Durable Arrow IPC codec retaining VectorTable spatial metadata."""

    suffix = ".arrow"

    def dump(self, value: VectorTable, path: Path) -> None:
        metadata = dict(value.table.schema.metadata or {})
        metadata.update(
            {
                b"mgb:crs_wkt": value.crs.to_wkt(
                    version="WKT2_2019", pretty=False
                ).encode(),
                b"mgb:geometry_column": value.geometry_column.encode(),
                b"mgb:geometry_type": value.geometry_type.encode(),
            }
        )
        table = value.table.replace_schema_metadata(metadata)
        with path.open("wb") as stream, ipc.new_file(stream, table.schema) as writer:
            writer.write_table(table)

    def load(self, path: Path) -> VectorTable:
        with path.open("rb") as stream:
            table = ipc.open_file(stream).read_all()
        metadata = table.schema.metadata or {}
        try:
            crs = CRS.from_wkt(metadata[b"mgb:crs_wkt"].decode())
            geometry_column = metadata[b"mgb:geometry_column"].decode()
            geometry_type = metadata[b"mgb:geometry_type"].decode()
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise InvalidInputSchemaError(
                "Arrow vector checkpoint metadata is invalid"
            ) from exc
        return VectorTable(table, crs, geometry_column, geometry_type)


def geometry_column_name(table: pa.Table, hinted: str | None = None) -> str:
    """Resolve the sole GeoArrow WKB field in a Pyogrio Arrow table."""
    if hinted and hinted in table.column_names:
        return hinted
    matches = []
    for field in table.schema:
        metadata = field.metadata or {}
        if metadata.get(b"ARROW:extension:name") == b"geoarrow.wkb":
            matches.append(field.name)
    if len(matches) != 1:
        raise InvalidInputSchemaError(
            "Vector must contain exactly one GeoArrow WKB column"
        )
    return matches[0]


def vector_table_from_arrow(metadata: dict, table: pa.Table) -> VectorTable:
    """Build a validated VectorTable from Pyogrio Arrow output."""
    value = metadata.get("crs")
    if value is None:
        raise InvalidInputSchemaError("Vector provider has no CRS")
    return VectorTable(
        table=table,
        crs=CRS.from_user_input(value),
        geometry_column=geometry_column_name(table, metadata.get("geometry_name")),
        geometry_type=str(metadata.get("geometry_type") or "Unknown"),
    )


def read_vector_table(
    path: str | Path,
    *,
    layer: str | None = None,
    columns: Sequence[str] | None = None,
) -> VectorTable:
    """Read a vector provider directly into an Arrow-native table."""
    try:
        metadata, table = pyogrio.read_arrow(path, layer=layer, columns=columns)
        return vector_table_from_arrow(metadata, table)
    except InvalidInputSchemaError:
        raise
    except Exception as exc:
        raise InvalidInputSchemaError(f"Cannot read vector provider: {path}") from exc


def write_vector_table(
    vector: VectorTable,
    path: str | Path,
    *,
    driver: str,
    layer: str | None = None,
    append: bool = False,
    spatial_index: bool = True,
) -> None:
    """Write an Arrow-native vector table through Pyogrio/GDAL."""
    options = (
        {"SPATIAL_INDEX": "YES" if spatial_index else "NO"}
        if driver in {"FlatGeobuf", "GPKG"}
        else {}
    )
    try:
        pyogrio.write_arrow(
            vector.table,
            path,
            layer=layer,
            driver=driver,
            geometry_name=vector.geometry_column,
            geometry_type=vector.geometry_type,
            crs=vector.crs.to_wkt(version="WKT2_2019", pretty=False),
            append=append,
            layer_options=options,
        )
    except Exception as exc:
        raise InvalidInputSchemaError(f"Cannot write vector provider: {path}") from exc


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
        raise InvalidInputSchemaError(
            f"Cannot inspect vector provider: {path}"
        ) from exc
    driver = info.get("driver")
    if driver not in SUPPORTED_PROVIDER_DRIVERS:
        raise InvalidInputSchemaError(
            "Raw vector provider must be GeoPackage, FlatGeobuf, or FileGDB"
        )
    if driver == "OpenFileGDB" and layer is None:
        raise InvalidInputSchemaError("FileGDB provider requires an explicit layer")
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
        path.resolve(),
        layer,
        driver,
        tuple(info["fields"]),
        str(info["geometry_type"]),
        int(info["features"]),
        crs,
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
        with (
            guard,
            pyogrio.open_arrow(
                provider.path,
                layer=provider.layer,
                columns=list(columns),
                batch_size=batch_size,
                read_geometry=read_geometry,
                where=where,
                fids=list(fids) if fids is not None else None,
                return_fids=return_fids,
                use_pyarrow=True,
            ) as (_, batches),
        ):
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
        provider,
        columns=(id_column,),
        batch_size=batch_size,
        read_geometry=False,
        return_fids=True,
        context=context,
    ):
        fid_index = max(batch.schema.get_field_index(provider.fid_column), 0)
        for value, fid in zip(
            batch[id_column].to_pylist(),
            batch.column(fid_index).to_pylist(),
            strict=True,
        ):
            if value in result:
                raise InvalidInputSchemaError(
                    f"Vector provider contains duplicate ID: {value}"
                )
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
    per_feature = max(
        4096, math.ceil(source_bytes / max(1, provider.feature_count)) * 4
    )
    return max(1, min(requested_rows, memory_limit_bytes // per_feature))
