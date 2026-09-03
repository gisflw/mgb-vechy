"""Bounded reads and deterministic partition planning for prepared vectors."""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import pyarrow as pa
import pyarrow.compute as pc
import pyogrio

from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.execution.executor import WorkerContext
from mgb_vec_hydro.preparation import PreparedDataset

Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class VectorQuery:
    asset: str
    columns: tuple[str, ...] = ()
    bounds: Bounds | None = None
    batch_size: int = 10_000

    def __post_init__(self) -> None:
        if not isinstance(self.asset, str) or not self.asset:
            raise PreparedDataError("Vector asset name must be non-empty")
        if (
            isinstance(self.batch_size, bool)
            or not isinstance(self.batch_size, int)
            or self.batch_size <= 0
        ):
            raise PreparedDataError("Vector query batch size must be positive")
        if self.bounds is not None:
            if len(self.bounds) != 4 or not all(math.isfinite(x) for x in self.bounds):
                raise PreparedDataError(
                    "Vector query bounds must be four finite values"
                )
            if self.bounds[0] > self.bounds[2] or self.bounds[1] > self.bounds[3]:
                raise PreparedDataError("Vector query bounds are inverted")


@dataclass(frozen=True)
class VectorPartition:
    key: str
    value: Any
    bounds: Bounds
    feature_count: int
    estimated_bytes: int


@dataclass(frozen=True)
class PreparedVectorSource:
    root: Path
    name: str
    path: Path
    fields: tuple[str, ...]
    feature_count: int


def prepared_vector_source(
    prepared_root: str | Path,
    asset_name: str,
    *,
    context: WorkerContext | None = None,
) -> PreparedVectorSource:
    """Resolve and validate one manifest-declared FlatGeobuf asset."""

    root = Path(prepared_root).resolve()
    cache_key = f"prepared-vector:{root}:{asset_name}"

    def load() -> PreparedVectorSource:
        dataset = PreparedDataset.open(root)
        dataset.validate()
        try:
            asset = dataset.manifest["assets"]["vectors"][asset_name]
        except KeyError as exc:
            raise PreparedDataError(
                f"Unknown prepared vector asset: {asset_name}"
            ) from exc
        if asset.get("driver") != "FlatGeobuf":
            raise PreparedDataError(f"Prepared vector {asset_name} is not FlatGeobuf")
        fields = asset.get("fields")
        count = asset.get("feature_count")
        if not isinstance(fields, list) or not isinstance(count, int) or count < 0:
            raise PreparedDataError(
                f"Prepared vector {asset_name} has invalid manifest metadata"
            )
        path = dataset.asset_path(asset["path"])
        try:
            actual = pyogrio.read_info(path)
        except Exception as exc:
            raise PreparedDataError(
                f"Cannot inspect prepared vector asset: {asset_name}"
            ) from exc
        if (
            actual.get("driver") != "FlatGeobuf"
            or tuple(actual["fields"]) != tuple(fields)
            or actual.get("features") != count
        ):
            raise PreparedDataError(
                f"Prepared vector {asset_name} does not match its manifest"
            )
        return PreparedVectorSource(root, asset_name, path, tuple(fields), count)

    return context.resources.get(cache_key, load) if context is not None else load()


def iter_vector_batches(
    prepared_root: str | Path,
    query: VectorQuery,
    *,
    context: WorkerContext | None = None,
) -> Iterator[pa.RecordBatch]:
    """Yield spatially filtered Arrow batches without materializing a layer."""

    source = prepared_vector_source(prepared_root, query.asset, context=context)
    unknown = set(query.columns) - set(source.fields)
    if unknown:
        raise PreparedDataError(
            f"Prepared vector {query.asset} lacks column(s): "
            + ", ".join(sorted(unknown))
        )
    guard = context.io_bound() if context is not None else nullcontext()
    try:
        with (
            guard,
            pyogrio.open_arrow(
                source.path,
                columns=list(query.columns) or None,
                bbox=query.bounds,
                batch_size=query.batch_size,
                use_pyarrow=True,
            ) as (_, batches),
        ):
            yield from batches
    except PreparedDataError:
        raise
    except Exception as exc:
        raise PreparedDataError(
            f"Cannot read prepared vector asset: {query.asset}"
        ) from exc


def plan_vector_partitions(
    prepared_root: str | Path,
    asset_name: str,
    partition_column: str,
    *,
    batch_size: int = 10_000,
) -> tuple[VectorPartition, ...]:
    """Scan bounded batches and summarize domain-defined partition values.

    The executor does not interpret the field.  A later aggregation stage may,
    for example, pass ``water_course`` as the partition column.
    """

    accumulators: dict[tuple[str, str], dict[str, Any]] = {}
    query = VectorQuery(asset_name, (partition_column,), batch_size=batch_size)
    for batch in iter_vector_batches(prepared_root, query):
        frame = gpd.GeoDataFrame.from_arrow(batch)
        for value, group in frame.groupby(partition_column, sort=False, dropna=False):
            if _is_null(value):
                raise PreparedDataError(
                    f"Partition column {partition_column} contains null values"
                )
            stable = (type(value).__name__, str(value))
            bounds = tuple(float(number) for number in group.total_bounds)
            state = accumulators.setdefault(
                stable,
                {
                    "value": value.item() if hasattr(value, "item") else value,
                    "bounds": list(bounds),
                    "count": 0,
                    "bytes": 0,
                },
            )
            state["bounds"][0] = min(state["bounds"][0], bounds[0])
            state["bounds"][1] = min(state["bounds"][1], bounds[1])
            state["bounds"][2] = max(state["bounds"][2], bounds[2])
            state["bounds"][3] = max(state["bounds"][3], bounds[3])
            state["count"] += len(group)
            # Charge the complete source batch to every represented partition.
            # This may overestimate shared buffers, but never underestimates a
            # partition because another group happened to occupy the batch.
            state["bytes"] += max(1, batch.nbytes)

    return tuple(
        VectorPartition(
            key=f"{partition_column}:{kind}:{text}",
            value=state["value"],
            bounds=tuple(state["bounds"]),
            feature_count=state["count"],
            estimated_bytes=state["bytes"],
        )
        for (kind, text), state in sorted(accumulators.items())
    )


def iter_partition_batches(
    prepared_root: str | Path,
    asset_name: str,
    partition_column: str,
    partition: VectorPartition,
    *,
    columns: tuple[str, ...] = (),
    batch_size: int = 10_000,
    context: WorkerContext | None = None,
) -> Iterator[pa.RecordBatch]:
    """Read a partition bbox and remove spatial-index false positives."""

    selected = tuple(dict.fromkeys((*columns, partition_column)))
    query = VectorQuery(asset_name, selected, partition.bounds, batch_size)
    for batch in iter_vector_batches(prepared_root, query, context=context):
        values = batch.column(batch.schema.get_field_index(partition_column))
        mask = pc.equal(values, pa.scalar(partition.value, type=values.type))
        filtered = batch.filter(pc.fill_null(mask, False))
        if filtered.num_rows:
            yield filtered


def _is_null(value: Any) -> bool:
    try:
        return not pa.scalar(value).is_valid
    except (pa.ArrowInvalid, TypeError, ValueError):
        return False
