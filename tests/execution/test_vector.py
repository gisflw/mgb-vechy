import pytest
from shapely.geometry import Point

from mgb_vec_hydro.execution.vector import (
    VectorTable,
    conservative_geometry_packet_rows,
    inspect_vector_provider,
    iter_provider_batches,
    read_vector_table,
    resolve_provider_field,
    scan_id_fids,
    write_vector_table,
)


@pytest.fixture
def provider(tmp_path):
    path = tmp_path / "source.gpkg"
    write_vector_table(
        VectorTable.from_pydict(
            {"Source_ID": [1, 2, 3], "value": [4, 5, 6]},
            [Point(0, 0), Point(1, 0), Point(2, 0)],
            crs="EPSG:3857",
            geometry_type="Point",
        ),
        path,
        driver="GPKG",
    )
    return inspect_vector_provider(path)


def test_provider_inspection_resolves_fields_case_insensitively(provider):
    assert provider.driver == "GPKG"
    assert resolve_provider_field(provider, "source_id") == "Source_ID"


def test_attribute_stream_and_geometry_packets_are_bounded(provider):
    batches = list(
        iter_provider_batches(provider, columns=("Source_ID",), batch_size=1)
    )
    assert len(batches) == 3
    assert all(
        batch.num_rows == 1 and "geom" not in batch.schema.names for batch in batches
    )
    assert (
        conservative_geometry_packet_rows(
            provider, memory_limit_bytes=4096, requested_rows=10_000
        )
        == 1
    )


def test_flatgeobuf_round_trip_preserves_ids_geometry_and_crs(tmp_path):
    path = tmp_path / "points.fgb"
    vector = VectorTable.from_pydict(
        {"id": [1, 2], "value": [1.5, 2.5]},
        [Point(0, 0), Point(1, 1)],
        crs="EPSG:4326",
        geometry_type="Point",
    )
    write_vector_table(vector, path, driver="FlatGeobuf")

    result = read_vector_table(path)

    assert set(result.table["id"].to_pylist()) == {1, 2}
    assert result.crs.to_epsg() == 4326
    assert {geometry.wkt for geometry in result.geometries()} == {
        "POINT (0 0)",
        "POINT (1 1)",
    }


def test_flatgeobuf_arrow_reads_split_large_fid_selections(tmp_path):
    count = 5_000
    path = tmp_path / "many-points.fgb"
    write_vector_table(
        VectorTable.from_pydict(
            {"id": range(count)},
            [Point(value, 0) for value in range(count)],
            crs="EPSG:3857",
            geometry_type="Point",
        ),
        path,
        driver="FlatGeobuf",
    )
    provider = inspect_vector_provider(path)
    fids = scan_id_fids(provider, "id")

    batches = iter_provider_batches(
        provider,
        columns=("id",),
        fids=tuple(fids.values()),
        batch_size=count,
    )
    ids = [value for batch in batches for value in batch["id"].to_pylist()]

    assert len(ids) == count
    assert set(ids) == set(range(count))


def test_geopackage_large_fid_selection_uses_one_native_request(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import pyarrow as pa

    import mgb_vec_hydro.execution.vector as module

    calls = []

    @contextmanager
    def open_arrow(*args, **kwargs):
        calls.append(kwargs["fids"])
        yield {}, [pa.record_batch({"id": kwargs["fids"]})]

    monkeypatch.setattr(module.pyogrio, "open_arrow", open_arrow)
    provider = module.VectorProvider(tmp_path / "source.gpkg", None, "GPKG", ("id",), "Point", 5000, "EPSG:3857", "fid")
    batches = list(iter_provider_batches(provider, columns=("id",), fids=range(5000)))
    assert len(calls) == 1
    assert len(calls[0]) == 5000
    assert sum(batch.num_rows for batch in batches) == 5000
