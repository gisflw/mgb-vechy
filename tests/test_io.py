import pytest
from shapely.geometry import Point

from mgb_vec_hydro.exceptions import UnsupportedOutputFormatError
from mgb_vec_hydro.io import (
    aggregation_output_paths,
    output_paths,
    read_vector,
    write_vector,
)
from mgb_vec_hydro.execution.vector import VectorTable


def test_output_paths_use_roi_dataset_names(tmp_path):
    paths = output_paths(tmp_path, "gpkg")

    assert paths.catchments == tmp_path / "roi_catchments.gpkg"
    assert paths.segments == tmp_path / "roi_segments.gpkg"


def test_output_paths_reject_unsupported_format(tmp_path):
    with pytest.raises(UnsupportedOutputFormatError, match="xyz"):
        output_paths(tmp_path, "xyz")


def test_aggregation_output_paths_use_mini_dataset_names(tmp_path):
    paths = aggregation_output_paths(tmp_path, "fgb")

    assert paths.catchments == tmp_path / "mini_catchments.fgb"
    assert paths.segments == tmp_path / "mini_segments.fgb"
    assert paths.mapping == tmp_path / "bho2mini.fgb"


def test_write_and_read_vector_round_trip_gpkg(tmp_path):
    vector = VectorTable.from_pydict(
        {"value": [1]}, [Point(0, 0)], crs="EPSG:4326", geometry_type="Point"
    )
    path = tmp_path / "points.gpkg"

    write_vector(vector, path, output_format="gpkg")
    result = read_vector(path)

    assert result.table["value"].to_pylist() == [1]
    assert result.crs == vector.crs
