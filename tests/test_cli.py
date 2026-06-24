import geopandas as gpd
from click.testing import CliRunner
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.cli import main


EXPECTED_COLUMNS = [
    "id",
    "id_down",
    "sub",
    "strahler_order",
    "unit_length",
    "upstream_length",
    "unit_area",
    "upstream_area",
    "geometry",
]


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
            "strahler_order": [2, 1],
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
            "--destine-crs",
            "EPSG:3857",
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
    assert list(roi_areas.columns) == EXPECTED_COLUMNS
    assert list(roi_trecs.columns) == EXPECTED_COLUMNS
    assert roi_areas.crs == "EPSG:3857"
    assert roi_trecs.crs == "EPSG:3857"
    assert list(roi_areas["strahler_order"]) == [2, 1]
    assert list(roi_trecs["strahler_order"]) == [2, 1]


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
            "strahler_order": [1],
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
            "--destine-crs",
            "EPSG:3857",
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
            "ordem": [2, 1],
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
            "--strahler-order-col",
            "ordem",
            "--destine-crs",
            "EPSG:3857",
            "--output-dir",
            str(output_dir),
            "--output-format",
            "gpkg",
        ],
    )

    assert result.exit_code == 0, result.output
    roi_areas = gpd.read_file(output_dir / "roi_areas.gpkg")
    roi_trecs = gpd.read_file(output_dir / "roi_trecs.gpkg")
    assert list(roi_areas.columns) == EXPECTED_COLUMNS
    assert list(roi_trecs.columns) == EXPECTED_COLUMNS
    assert list(roi_areas["id"]) == [10, 20]
    assert list(roi_trecs["id"]) == [10, 20]
    assert list(roi_areas["strahler_order"]) == [2, 1]
    assert list(roi_trecs["strahler_order"]) == [2, 1]


def test_define_roi_cli_resolves_input_columns_case_insensitively(tmp_path):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    output_dir = tmp_path / "out"

    catchments = gpd.GeoDataFrame(
        {
            "LINKNO": [10, 20],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
            ],
        },
        crs="EPSG:4326",
    )
    segments = gpd.GeoDataFrame(
        {
            "LINKNO": [10, 20],
            "DSLINKNO": [None, 10],
            "STRAHLER_ORDER": [2, 1],
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
            "linkno",
            "--id-down-col",
            "dslinkno",
            "--destine-crs",
            "EPSG:3857",
            "--output-dir",
            str(output_dir),
            "--output-format",
            "gpkg",
        ],
    )

    assert result.exit_code == 0, result.output
    roi_areas = gpd.read_file(output_dir / "roi_areas.gpkg")
    roi_trecs = gpd.read_file(output_dir / "roi_trecs.gpkg")
    assert list(roi_areas.columns) == EXPECTED_COLUMNS
    assert list(roi_trecs.columns) == EXPECTED_COLUMNS
    assert list(roi_areas["id"]) == [10, 20]
    assert roi_trecs["id_down"].isna().iloc[0]
    assert roi_trecs["id_down"].iloc[1] == 10
    assert list(roi_trecs["strahler_order"]) == [2, 1]


def test_define_roi_cli_accepts_source_crs_for_layers_without_crs(tmp_path):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    output_dir = tmp_path / "out"

    catchments = gpd.GeoDataFrame(
        {
            "id": [1],
            "geometry": [Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])],
        }
    )
    segments = gpd.GeoDataFrame(
        {
            "id": [1],
            "id_down": [None],
            "strahler_order": [1],
            "geometry": [LineString([(0, 0), (1000, 0)])],
        }
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
            "--source-crs",
            "EPSG:3857",
            "--destine-crs",
            "EPSG:3857",
            "--output-dir",
            str(output_dir),
            "--output-format",
            "gpkg",
        ],
    )

    assert result.exit_code == 0, result.output
    roi_areas = gpd.read_file(output_dir / "roi_areas.gpkg")
    roi_trecs = gpd.read_file(output_dir / "roi_trecs.gpkg")
    assert list(roi_areas.columns) == EXPECTED_COLUMNS
    assert list(roi_trecs.columns) == EXPECTED_COLUMNS
    assert roi_areas["unit_area"].iloc[0] == 1.0
    assert roi_trecs["unit_length"].iloc[0] == 1.0
