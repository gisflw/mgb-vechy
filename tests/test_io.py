import geopandas as gpd
import pytest
from shapely.geometry import Point

from mgb_vec_hydro.exceptions import UnsupportedOutputFormatError
from mgb_vec_hydro.io import output_paths, read_vector, write_vector


def test_output_paths_use_legacy_roi_names(tmp_path):
    paths = output_paths(tmp_path, "gpkg")

    assert paths.catchments == tmp_path / "roi_areas.gpkg"
    assert paths.segments == tmp_path / "roi_trecs.gpkg"


def test_output_paths_reject_unsupported_format(tmp_path):
    with pytest.raises(UnsupportedOutputFormatError, match="xyz"):
        output_paths(tmp_path, "xyz")


def test_write_and_read_vector_round_trip_gpkg(tmp_path):
    gdf = gpd.GeoDataFrame(
        {"value": [1], "geometry": [Point(0, 0)]},
        crs="EPSG:4326",
    )
    path = tmp_path / "points.gpkg"

    write_vector(gdf, path, output_format="gpkg")
    result = read_vector(path)

    assert list(result["value"]) == [1]
    assert result.crs == gdf.crs
