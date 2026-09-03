import multiprocessing as mp

import geopandas as gpd
import pyarrow as pa
import pytest

from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.execution.executor import WorkerContext
from mgb_vec_hydro.execution.vector import (
    VectorQuery,
    iter_partition_batches,
    iter_vector_batches,
    plan_vector_partitions,
    prepared_vector_source,
)


def test_vector_reader_uses_bounded_spatial_arrow_batches(prepared_execution_dataset):
    query = VectorQuery(
        "segments",
        columns=("id",),
        bounds=(0, 10, 15, 20),
        batch_size=1,
    )
    batches = list(iter_vector_batches(prepared_execution_dataset, query))
    assert batches
    assert all(batch.num_rows <= 1 for batch in batches)
    frame = gpd.GeoDataFrame.from_arrow(pa.Table.from_batches(batches))
    assert set(frame["id"]) == {1, 2}


def test_vector_source_metadata_is_reused_in_worker_context(prepared_execution_dataset):
    context = WorkerContext(mp.get_context("spawn").BoundedSemaphore(1), 2)
    try:
        first = prepared_vector_source(
            prepared_execution_dataset, "segments", context=context
        )
        second = prepared_vector_source(
            prepared_execution_dataset, "segments", context=context
        )
        assert first is second
    finally:
        context.close()


def test_vector_partition_plan_is_stable_and_filters_bbox_false_positives(
    prepared_execution_dataset,
):
    partitions = plan_vector_partitions(
        prepared_execution_dataset,
        "segments",
        "strahler_order",
        batch_size=1,
    )
    assert [partition.value for partition in partitions] == [1, 2]
    assert [partition.feature_count for partition in partitions] == [2, 1]
    assert all(partition.estimated_bytes > 0 for partition in partitions)

    order_one = partitions[0]
    batches = list(
        iter_partition_batches(
            prepared_execution_dataset,
            "segments",
            "strahler_order",
            order_one,
            columns=("id",),
            batch_size=1,
        )
    )
    frame = gpd.GeoDataFrame.from_arrow(pa.Table.from_batches(batches))
    assert set(frame["id"]) == {1, 3}
    assert set(frame["strahler_order"]) == {1}


def test_vector_reader_rejects_unknown_columns_and_bad_bounds(
    prepared_execution_dataset,
):
    with pytest.raises(PreparedDataError, match="lacks column"):
        list(
            iter_vector_batches(
                prepared_execution_dataset,
                VectorQuery("segments", columns=("missing",)),
            )
        )
    with pytest.raises(PreparedDataError, match="inverted"):
        VectorQuery("segments", bounds=(2, 0, 1, 1))
