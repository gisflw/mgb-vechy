import geopandas as gpd
from click.testing import CliRunner
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.cli import main


def test_define_roi_cli_writes_outputs(tmp_path):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    output_dir = tmp_path / "out"

    catchments = gpd.GeoDataFrame(
        {
            "catch_id": [10, 20],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
            ],
        },
        crs="EPSG:4326",
    )
    segments = gpd.GeoDataFrame(
        {
            "seg_id": [1, 2],
            "seg_id_down": [None, 1],
            "catch_id": [10, 20],
            "geometry": [
                LineString([(0, 0), (1, 0)]),
                LineString([(1, 0), (2, 0)]),
            ],
        },
        crs="EPSG:4326",
    )
    catchments.to_file(catchments_path, driver="GPKG")
    segments.to_file(segments_path, driver="GPKG")

    result = CliRunner().invoke(
        main,
        [
            "define-roi",
            "--catchments",
            str(catchments_path),
            "--segments",
            str(segments_path),
            "--outlet-id",
            "1",
            "--output-dir",
            str(output_dir),
            "--output-format",
            "gpkg",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (output_dir / "roi_areas.gpkg").exists()
    assert (output_dir / "roi_trecs.gpkg").exists()


def test_define_roi_cli_reports_package_errors(tmp_path):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    catchments = gpd.GeoDataFrame(
        {
            "catch_id": [10],
            "geometry": [Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
        },
        crs="EPSG:4326",
    )
    segments = gpd.GeoDataFrame(
        {
            "seg_id": [1],
            "seg_id_down": [None],
            "catch_id": [10],
            "geometry": [LineString([(0, 0), (1, 0)])],
        },
        crs="EPSG:4326",
    )
    catchments.to_file(catchments_path, driver="GPKG")
    segments.to_file(segments_path, driver="GPKG")

    result = CliRunner().invoke(
        main,
        [
            "define-roi",
            "--catchments",
            str(catchments_path),
            "--segments",
            str(segments_path),
            "--outlet-id",
            "1",
            "--output-dir",
            str(tmp_path / "out"),
            "--output-format",
            "xyz",
        ],
    )

    assert result.exit_code != 0
    assert "Unsupported output format" in result.output
