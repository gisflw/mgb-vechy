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
            "id": [1, 2],
            "source_area": [10, 20],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
            ],
        },
        crs="EPSG:4326",
    )
    segments = gpd.GeoDataFrame(
        {
            "id": [1, 2],
            "id_down": [None, 1],
            "source_length": [1.0, 2.0],
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

    roi_areas = gpd.read_file(output_dir / "roi_areas.gpkg")
    roi_trecs = gpd.read_file(output_dir / "roi_trecs.gpkg")
    assert list(roi_areas.columns) == ["id", "id_down", "sub", "geometry"]
    assert list(roi_trecs.columns) == ["id", "id_down", "sub", "geometry"]


def test_define_roi_cli_reports_package_errors(tmp_path):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    catchments = gpd.GeoDataFrame(
        {
            "id": [1],
            "geometry": [Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
        },
        crs="EPSG:4326",
    )
    segments = gpd.GeoDataFrame(
        {
            "id": [1],
            "id_down": [None],
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


def test_define_roi_cli_accepts_custom_id_columns(tmp_path):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    output_dir = tmp_path / "out"

    catchments = gpd.GeoDataFrame(
        {
            "cotrecho": [10, 20],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
            ],
        },
        crs="EPSG:4326",
    )
    segments = gpd.GeoDataFrame(
        {
            "cotrecho": [10, 20],
            "nutrjus": [None, 10],
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
            "10",
            "--id-col",
            "cotrecho",
            "--id-down-col",
            "nutrjus",
            "--output-dir",
            str(output_dir),
            "--output-format",
            "gpkg",
        ],
    )

    assert result.exit_code == 0, result.output
    roi_areas = gpd.read_file(output_dir / "roi_areas.gpkg")
    roi_trecs = gpd.read_file(output_dir / "roi_trecs.gpkg")
    assert list(roi_areas.columns) == ["id", "id_down", "sub", "geometry"]
    assert list(roi_trecs.columns) == ["id", "id_down", "sub", "geometry"]
    assert list(roi_areas["id"]) == [10, 20]
    assert list(roi_trecs["id"]) == [10, 20]
