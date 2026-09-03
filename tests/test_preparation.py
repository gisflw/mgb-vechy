import json
import sqlite3

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from click.testing import CliRunner
from rasterio.transform import from_origin
from shapely.geometry import LineString, Polygon

import mgb_vec_hydro.preparation as preparation_module
from mgb_vec_hydro.cli import main
from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.preparation import (
    NamedRaster,
    PreparationSpec,
    PreparedDataset,
    canonical_grid,
    prepare_dataset,
)


def _sources(tmp_path, *, invalid=False, mismatched=False):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    dem_path = tmp_path / "dem.tif"
    categorical_path = tmp_path / "land.tif"
    d8_path = tmp_path / "d8.tif"
    first_polygon = (
        Polygon([(0, 0), (20, 20), (0, 20), (20, 0), (0, 0)])
        if invalid
        else Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    )
    catchments = gpd.GeoDataFrame(
        {
            "source_id": pd.Series([1, 2, None], dtype="Int64"),
            "unused": ["a", "b", "drop"],
            "geometry": [
                first_polygon,
                Polygon([(20, 0), (40, 0), (40, 20), (20, 20)]),
                Polygon([(40, 0), (50, 0), (50, 10), (40, 10)]),
            ],
        },
        crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {
            "source_id": [1, 3 if mismatched else 2],
            "down": pd.Series([None, 1], dtype="Int64"),
            "order": [2, 1],
            "unused": [10, 20],
            "geometry": [
                LineString([(0, 10), (20, 10)]),
                LineString([(20, 10), (40, 10)]),
            ],
        },
        crs="EPSG:3857",
    )
    catchments.to_file(catchments_path, driver="GPKG")
    segments.to_file(segments_path, driver="GPKG")
    profile = {
        "driver": "GTiff",
        "width": 4,
        "height": 2,
        "count": 1,
        "crs": "EPSG:3857",
        "transform": from_origin(0, 20, 10, 10),
    }
    with rasterio.open(dem_path, "w", dtype="float32", nodata=-9999, **profile) as dst:
        dst.write(np.array([[1, 2, 3, 4], [5, -9999, 7, 8]], dtype="float32"), 1)
    with rasterio.open(categorical_path, "w", dtype="int16", nodata=-1, **profile) as dst:
        dst.write(np.array([[1, 1, 2, 2], [1, -1, 2, 2]], dtype="int16"), 1)
    with rasterio.open(d8_path, "w", dtype="uint8", **profile) as dst:
        dst.write(np.array([[1, 2, 4, 8], [16, 32, 64, 128]], dtype="uint8"), 1)
    return catchments_path, segments_path, dem_path, categorical_path, d8_path


def _spec(tmp_path, **changes):
    catchments, segments, dem, categorical, d8 = _sources(
        tmp_path,
        invalid=changes.pop("invalid", False),
        mismatched=changes.pop("mismatched", False),
    )
    values = {
        "catchments": catchments,
        "segments": segments,
        "dem": dem,
        "crs": "EPSG:3857",
        "resolution": 10,
        "output_dir": tmp_path / "prepared",
        "id_col": "source_id",
        "id_down_col": "down",
        "strahler_order_col": "order",
        "rasters": (NamedRaster("land", categorical, "categorical"),),
        "d8": d8,
        "d8_encoding": "esri",
        "vector_batch_size": 1,
        "memory_limit_mb": 16,
    }
    values.update(changes)
    return PreparationSpec(**values)


def test_canonical_grid_snaps_transformed_bounds_to_origin(tmp_path):
    _, _, dem, _, _ = _sources(tmp_path)

    grid = canonical_grid(dem, "EPSG:3857", 12)

    assert grid.transform == from_origin(0, 24, 12, 12)
    assert grid.width == 4
    assert grid.height == 2
    assert grid.bounds == (0, 0, 48, 24)


def test_prepare_dataset_writes_contract_cogs_vectors_and_lookup(tmp_path):
    report = prepare_dataset(_spec(tmp_path))

    assert report.feature_count == 2
    assert report.dropped_catchments == 1
    assert report.dropped_segments == 0
    dataset = PreparedDataset.open(report.output_dir, verify_hashes=True)
    assert dataset.manifest["id_type"] == "int64"
    assert set(dataset.manifest["assets"]["rasters"]) == {"dem", "land", "d8"}
    catchments = gpd.read_file(report.output_dir / "vectors/catchments.fgb").sort_values("id")
    segments = gpd.read_file(report.output_dir / "vectors/segments.fgb").sort_values("id")
    assert list(catchments.columns) == ["id", "geometry"]
    assert list(segments.columns) == ["id", "id_down", "strahler_order", "geometry"]
    assert list(catchments["id"]) == [1, 2]
    assert set(catchments.geom_type) == {"MultiPolygon"}
    assert set(segments.geom_type) == {"MultiLineString"}
    with sqlite3.connect(report.output_dir / "indexes/features.sqlite") as connection:
        rows = connection.execute(
            "SELECT id, id_down, strahler_order, catchment_fid, segment_fid "
            "FROM features ORDER BY id"
        ).fetchall()
    assert rows == [(1, None, 2, 1, 1), (2, 1, 1, 0, 0)]
    for name, dtype in (("dem", "float32"), ("land", "int32"), ("d8", "uint8")):
        with rasterio.open(report.output_dir / f"rasters/{name}.tif") as src:
            assert src.dtypes == (dtype,)
            assert src.nodata is None
            assert src.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
            assert src.transform == from_origin(0, 20, 10, 10)
    with rasterio.open(report.output_dir / "rasters/d8.tif") as src:
        np.testing.assert_array_equal(src.read(1), [[3, 4, 5, 6], [7, 8, 1, 2]])


def test_prepare_dataset_rejects_mismatched_ids_without_publication(tmp_path):
    spec = _spec(tmp_path, mismatched=True)

    with pytest.raises(PreparedDataError, match="ID sets differ"):
        prepare_dataset(spec)

    assert not spec.output_dir.exists()
    assert not list(tmp_path.glob(".prepared.tmp-*"))


def test_prepare_dataset_requires_opt_in_geometry_repair(tmp_path):
    spec = _spec(tmp_path, invalid=True)
    with pytest.raises(PreparedDataError, match="invalid geometry"):
        prepare_dataset(spec)
    repaired_spec = PreparationSpec(
        **{**spec.__dict__, "output_dir": tmp_path / "repaired", "repair_invalid_geometries": True}
    )

    report = prepare_dataset(repaired_spec)

    assert report.repaired_catchments == 1


def test_prepared_dataset_detects_changed_asset(tmp_path):
    report = prepare_dataset(_spec(tmp_path))
    manifest = json.loads(report.manifest.read_text())
    manifest["assets"]["lookup"]["sha256"] = "0" * 64
    report.manifest.write_text(json.dumps(manifest))

    with pytest.raises(PreparedDataError, match="checksum mismatch"):
        PreparedDataset.open(report.output_dir, verify_hashes=True)


def test_prepare_dataset_rejects_existing_output(tmp_path):
    spec = _spec(tmp_path)
    spec.output_dir.mkdir()

    with pytest.raises(PreparedDataError, match="already exists"):
        prepare_dataset(spec)


def test_prepare_dataset_cleans_staging_on_cancellation(tmp_path, monkeypatch):
    spec = _spec(tmp_path)

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(preparation_module, "_prepare_vectors", cancel)
    with pytest.raises(KeyboardInterrupt):
        prepare_dataset(spec)

    assert not spec.output_dir.exists()
    assert not list(tmp_path.glob(".prepared.tmp-*"))


def test_prepared_dataset_rejects_escaping_asset_path(tmp_path):
    report = prepare_dataset(_spec(tmp_path))
    manifest = json.loads(report.manifest.read_text())
    manifest["assets"]["lookup"]["path"] = "../outside.sqlite"
    report.manifest.write_text(json.dumps(manifest))

    with pytest.raises(PreparedDataError, match="escapes dataset"):
        PreparedDataset.open(report.output_dir)


def test_prepare_cli_writes_manifest_and_reports_normalization(tmp_path):
    catchments, segments, dem, categorical, _ = _sources(tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "prepare",
            "--catchments",
            str(catchments),
            "--segments",
            str(segments),
            "--dem",
            str(dem),
            "--id-col",
            "source_id",
            "--id-down-col",
            "down",
            "--strahler-order-col",
            "order",
            "--crs",
            "EPSG:3857",
            "--resolution",
            "10",
            "--categorical-raster",
            "land",
            str(categorical),
            "--vector-batch-size",
            "1",
            "--output-dir",
            str(tmp_path / "cli-prepared"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Prepared 2 shared" in result.output
    assert "1 catchments, 0 segments" in result.output
    PreparedDataset.open(tmp_path / "cli-prepared", verify_hashes=True)


def test_prepare_dataset_is_deterministic(tmp_path):
    first_spec = _spec(tmp_path)
    second_spec = PreparationSpec(
        **{**first_spec.__dict__, "output_dir": tmp_path / "prepared-again"}
    )

    first = prepare_dataset(first_spec)
    second = prepare_dataset(second_spec)

    assert first.manifest.read_bytes() == second.manifest.read_bytes()
    first_manifest = json.loads(first.manifest.read_text())
    for group in ("vectors", "rasters"):
        first_assets = first_manifest["assets"][group]
        second_assets = json.loads(second.manifest.read_text())["assets"][group]
        assert {
            name: asset["sha256"] for name, asset in first_assets.items()
        } == {name: asset["sha256"] for name, asset in second_assets.items()}


def test_prepare_dataset_rejects_invalid_d8_codes(tmp_path):
    spec = _spec(tmp_path)
    with rasterio.open(spec.d8, "r+") as dataset:
        values = dataset.read(1)
        values[0, 0] = 7
        dataset.write(values, 1)

    with pytest.raises(PreparedDataError, match="invalid code"):
        prepare_dataset(spec)

    assert not spec.output_dir.exists()


def test_prepare_dataset_preserves_string_identifier_contract(tmp_path):
    spec = _spec(tmp_path)
    catchments = gpd.read_file(spec.catchments)
    segments = gpd.read_file(spec.segments)
    catchments["source_id"] = ["a", "b", ""]
    segments["source_id"] = ["a", "b"]
    segments["down"] = pd.Series([None, "a"], dtype="string")
    catchments_path = tmp_path / "string-catchments.gpkg"
    segments_path = tmp_path / "string-segments.gpkg"
    catchments.to_file(catchments_path, driver="GPKG")
    segments.to_file(segments_path, driver="GPKG")
    string_spec = PreparationSpec(
        **{
            **spec.__dict__,
            "catchments": catchments_path,
            "segments": segments_path,
            "output_dir": tmp_path / "string-prepared",
        }
    )

    report = prepare_dataset(string_spec)

    assert report.dropped_catchments == 1
    assert json.loads(report.manifest.read_text())["id_type"] == "string"
    with sqlite3.connect(report.output_dir / "indexes/features.sqlite") as connection:
        assert connection.execute(
            "SELECT id, id_down FROM features ORDER BY id"
        ).fetchall() == [("a", None), ("b", "a")]


def test_prepare_dataset_handles_null_and_valid_integer_ids_in_one_batch(tmp_path):
    spec = _spec(tmp_path)
    one_batch_spec = PreparationSpec(
        **{**spec.__dict__, "vector_batch_size": 100}
    )

    report = prepare_dataset(one_batch_spec)

    assert report.feature_count == 2
    assert report.dropped_catchments == 1


def test_prepare_dataset_preserves_nullable_and_nonpositive_strahler(tmp_path):
    spec = _spec(tmp_path)
    segments = gpd.read_file(spec.segments)
    segments["down"] = segments["down"].astype("Int64")
    segments["order"] = pd.Series([None, -1], dtype="Int64")
    segments_path = tmp_path / "nullable-order-segments.gpkg"
    segments.to_file(segments_path, driver="GPKG")
    nullable_spec = PreparationSpec(
        **{
            **spec.__dict__,
            "segments": segments_path,
            "output_dir": tmp_path / "nullable-order-prepared",
        }
    )

    report = prepare_dataset(nullable_spec)

    prepared = gpd.read_file(report.output_dir / "vectors/segments.fgb").sort_values("id")
    assert prepared["strahler_order"].isna().iloc[0]
    assert prepared["strahler_order"].iloc[1] == -1


def test_prepare_dataset_rejects_d8_on_a_different_grid(tmp_path):
    spec = _spec(tmp_path)
    shifted = tmp_path / "shifted-d8.tif"
    with rasterio.open(
        shifted,
        "w",
        driver="GTiff",
        width=4,
        height=2,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(1, 20, 10, 10),
    ) as dataset:
        dataset.write(np.ones((2, 4), dtype="uint8"), 1)
    shifted_spec = PreparationSpec(**{**spec.__dict__, "d8": shifted})

    with pytest.raises(PreparedDataError, match="exactly match"):
        prepare_dataset(shifted_spec)


@pytest.mark.parametrize("name", ["dem", "d8", "Has Spaces", "../bad"])
def test_prepare_dataset_rejects_reserved_or_unsafe_raster_names(tmp_path, name):
    spec = _spec(tmp_path)
    invalid = PreparationSpec(
        **{
            **spec.__dict__,
            "rasters": (NamedRaster(name, spec.rasters[0].path, "categorical"),),
        }
    )

    with pytest.raises(PreparedDataError, match="Invalid or reserved"):
        prepare_dataset(invalid)
