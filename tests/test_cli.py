import geopandas as gpd
import pytest
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
    "water_course",
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
    assert (output_dir / "roi_catchments.gpkg").exists()
    assert (output_dir / "roi_segments.gpkg").exists()

    roi_catchments = gpd.read_file(output_dir / "roi_catchments.gpkg")
    roi_segments = gpd.read_file(output_dir / "roi_segments.gpkg")
    assert list(roi_catchments.columns) == EXPECTED_COLUMNS
    assert list(roi_segments.columns) == EXPECTED_COLUMNS
    assert roi_catchments.crs == "EPSG:3857"
    assert roi_segments.crs == "EPSG:3857"
    assert list(roi_catchments["strahler_order"]) == [2, 1]
    assert list(roi_segments["strahler_order"]) == [2, 1]


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
    roi_catchments = gpd.read_file(output_dir / "roi_catchments.gpkg")
    roi_segments = gpd.read_file(output_dir / "roi_segments.gpkg")
    assert list(roi_catchments.columns) == EXPECTED_COLUMNS
    assert list(roi_segments.columns) == EXPECTED_COLUMNS
    assert list(roi_catchments["id"]) == [10, 20]
    assert list(roi_segments["id"]) == [10, 20]
    assert list(roi_catchments["strahler_order"]) == [2, 1]
    assert list(roi_segments["strahler_order"]) == [2, 1]


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
    roi_catchments = gpd.read_file(output_dir / "roi_catchments.gpkg")
    roi_segments = gpd.read_file(output_dir / "roi_segments.gpkg")
    assert list(roi_catchments.columns) == EXPECTED_COLUMNS
    assert list(roi_segments.columns) == EXPECTED_COLUMNS
    assert list(roi_catchments["id"]) == [10, 20]
    assert roi_segments["id_down"].isna().iloc[0]
    assert roi_segments["id_down"].iloc[1] == 10
    assert list(roi_segments["strahler_order"]) == [2, 1]


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
    roi_catchments = gpd.read_file(output_dir / "roi_catchments.gpkg")
    roi_segments = gpd.read_file(output_dir / "roi_segments.gpkg")
    assert list(roi_catchments.columns) == EXPECTED_COLUMNS
    assert list(roi_segments.columns) == EXPECTED_COLUMNS
    assert roi_catchments["unit_area"].iloc[0] == 1.0
    assert roi_segments["unit_length"].iloc[0] == 1.0


def test_aggregate_cli_writes_stage2_outputs(tmp_path):
    roi_catchments_path = tmp_path / "roi_catchments.gpkg"
    roi_segments_path = tmp_path / "roi_segments.gpkg"
    output_dir = tmp_path / "out"
    common = {
        "id": [1, 2],
        "id_down": [None, 1],
        "sub": [1, 1],
        "strahler_order": [2, 1],
        "unit_length": [1.0, 1.0],
        "upstream_length": [2.0, 1.0],
        "unit_area": [1.0, 1.0],
        "upstream_area": [2.0, 1.0],
        "water_course": [1, 1],
    }
    roi_catchments = gpd.GeoDataFrame(
        {
            **common,
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
            ],
        },
        crs="EPSG:3857",
    )
    roi_segments = gpd.GeoDataFrame(
        {
            **common,
            "geometry": [
                LineString([(0, 0), (1, 0)]),
                LineString([(1, 0), (2, 0)]),
            ],
        },
        crs="EPSG:3857",
    )
    roi_catchments.to_file(roi_catchments_path, driver="GPKG")
    roi_segments.to_file(roi_segments_path, driver="GPKG")

    result = CliRunner().invoke(
        main,
        [
            "aggregate",
            "--roi-catchments",
            str(roi_catchments_path),
            "--roi-segments",
            str(roi_segments_path),
            "--uparea-min",
            "0",
            "--lmin",
            "0",
            "--output-dir",
            str(output_dir),
            "--output-format",
            "gpkg",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (output_dir / "mini_catchments.gpkg").exists()
    assert (output_dir / "mini_segments.gpkg").exists()
    assert (output_dir / "bho2mini.gpkg").exists()


@pytest.mark.parametrize("legacy_option", ["--roi-areas", "--roi-trecs"])
def test_aggregate_cli_rejects_legacy_input_options(legacy_option):
    result = CliRunner().invoke(main, ["aggregate", legacy_option, "input.gpkg"])

    assert result.exit_code != 0
    assert f"No such option '{legacy_option}'" in result.output
