import pytest
from shapely.geometry import Point

from mgb_vec_hydro.exceptions import InvalidInputSchemaError
from mgb_vec_hydro.execution.vector import (
    conservative_geometry_packet_rows,
    id_predicate,
    inspect_vector_provider,
    iter_provider_batches,
    resolve_provider_field,
    scan_id_fids,
    VectorTable,
    write_vector_table,
    read_vector_table,
    VectorTableCheckpointCodec,
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


def test_provider_inspection_and_case_insensitive_fields(provider):
    assert provider.driver == "GPKG"
    assert resolve_provider_field(provider, "source_id") == "Source_ID"


def test_provider_layer_selects_named_geopackage_layer(tmp_path):
    path = tmp_path / "layers.gpkg"
    frame = VectorTable.from_pydict(
        {"selected_id": [1]}, [Point(0, 0)], crs="EPSG:3857", geometry_type="Point"
    )
    write_vector_table(frame, path, driver="GPKG", layer="ignored")
    write_vector_table(frame, path, driver="GPKG", layer="selected")

    selected = inspect_vector_provider(path, layer="selected")

    assert selected.driver == "GPKG"
    assert selected.fields == ("selected_id",)


def test_filegdb_provider_requires_layer(monkeypatch, tmp_path):
    path = tmp_path / "source.gdb"
    path.mkdir()
    info = {
        "driver": "OpenFileGDB",
        "fields": ["source_id"],
        "geometry_type": "Point",
        "features": 1,
        "crs": "EPSG:3857",
        "fid_column": "OBJECTID",
    }
    monkeypatch.setattr("pyogrio.read_info", lambda *_args, **_kwargs: info)

    with pytest.raises(InvalidInputSchemaError, match="requires an explicit layer"):
        inspect_vector_provider(path)

    provider = inspect_vector_provider(path, layer="selected")
    assert provider.driver == "OpenFileGDB"
    assert provider.layer == "selected"


def test_attribute_stream_is_geometry_free_and_bounded(provider):
    batches = list(
        iter_provider_batches(provider, columns=("Source_ID",), batch_size=1)
    )
    assert len(batches) == 3
    assert all(
        batch.num_rows == 1 and "geom" not in batch.schema.names for batch in batches
    )


def test_safe_predicate_and_single_fid_scan(provider):
    assert id_predicate("Source_ID", [1, 2]) == '"Source_ID" IN (1,2)'
    assert set(scan_id_fids(provider, "Source_ID", batch_size=1)) == {1, 2, 3}


def test_provider_rejects_unknown_columns_and_bounds_packets(provider):
    with pytest.raises(InvalidInputSchemaError, match="lacks column"):
        list(iter_provider_batches(provider, columns=("missing",)))
    assert (
        conservative_geometry_packet_rows(
            provider, memory_limit_bytes=4096, requested_rows=10_000
        )
        == 1
    )


@pytest.mark.parametrize(
    ("driver", "suffix"),
    [("FlatGeobuf", ".fgb"), ("GPKG", ".gpkg"), ("ESRI Shapefile", ".shp")],
)
@pytest.mark.parametrize("ids", [[1, 2], ["one", "two"]])
def test_vector_table_round_trip_preserves_ids_geometry_and_crs(
    tmp_path, driver, suffix, ids
):
    path = tmp_path / f"points{suffix}"
    vector = VectorTable.from_pydict(
        {"id": ids, "value": [1.5, 2.5]},
        [Point(0, 0), Point(1, 1)],
        crs="EPSG:4326",
        geometry_type="Point",
    )

    write_vector_table(vector, path, driver=driver)
    result = read_vector_table(path)

    returned_ids = result.table["id"].to_pylist()
    assert set(returned_ids) == set(ids)
    assert result.crs.to_epsg() == 4326
    assert dict(
        zip(returned_ids, [geometry.wkt for geometry in result.geometries()])
    ) == dict(zip(ids, ["POINT (0 0)", "POINT (1 1)"]))


def test_vector_table_checkpoint_round_trip(tmp_path):
    vector = VectorTable.from_pydict(
        {"id": [1]}, [Point(2, 3)], crs="EPSG:3857", geometry_type="Point"
    )
    path = tmp_path / "packet.arrow"
    codec = VectorTableCheckpointCodec()

    codec.dump(vector, path)
    result = codec.load(path)

    assert result.table.equals(vector.table)
    assert result.crs == vector.crs
    assert result.geometry_column == "geometry"
    assert result.geometry_type == "Point"
