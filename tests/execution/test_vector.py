import geopandas as gpd
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
)


@pytest.fixture
def provider(tmp_path):
    path = tmp_path / "source.gpkg"
    gpd.GeoDataFrame(
        {"Source_ID": [1, 2, 3], "value": [4, 5, 6]},
        geometry=[Point(0, 0), Point(1, 0), Point(2, 0)],
        crs="EPSG:3857",
    ).to_file(path, driver="GPKG")
    return inspect_vector_provider(path)


def test_provider_inspection_and_case_insensitive_fields(provider):
    assert provider.driver == "GPKG"
    assert resolve_provider_field(provider, "source_id") == "Source_ID"


def test_attribute_stream_is_geometry_free_and_bounded(provider):
    batches = list(iter_provider_batches(provider, columns=("Source_ID",), batch_size=1))
    assert len(batches) == 3
    assert all(batch.num_rows == 1 and "geom" not in batch.schema.names for batch in batches)


def test_safe_predicate_and_single_fid_scan(provider):
    assert id_predicate("Source_ID", [1, 2]) == '"Source_ID" IN (1,2)'
    assert set(scan_id_fids(provider, "Source_ID", batch_size=1)) == {1, 2, 3}


def test_provider_rejects_unknown_columns_and_bounds_packets(provider):
    with pytest.raises(InvalidInputSchemaError, match="lacks column"):
        list(iter_provider_batches(provider, columns=("missing",)))
    assert conservative_geometry_packet_rows(
        provider, memory_limit_bytes=4096, requested_rows=10_000
    ) == 1
