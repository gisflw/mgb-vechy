import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
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


def _sources(
    tmp_path,
    *,
    invalid=False,
    catchment_ids=None,
    segment_ids=None,
    orders=None,
):
    catchment_ids = catchment_ids or [1, 2, None]
    segment_ids = segment_ids or [1, 2]
    orders = orders or [2, 1]
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    dem_path = tmp_path / "dem.tif"
    categorical_path = tmp_path / "land.tif"
    d8_path = tmp_path / "d8.tif"

    polygons = []
    for index in range(len(catchment_ids)):
        left = index * 10
        if invalid and index == 0:
            polygon = Polygon(
                [(left, 0), (left + 10, 10), (left, 10), (left + 10, 0), (left, 0)]
            )
        else:
            polygon = Polygon([(left, 0), (left + 10, 0), (left + 10, 10), (left, 10)])
        polygons.append(polygon)
    catchments = gpd.GeoDataFrame(
        {"source_id": pd.Series(catchment_ids, dtype="Int64"), "unused": "drop"},
        geometry=polygons,
        crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {
            "source_id": pd.Series(segment_ids, dtype="int64"),
            "down": pd.Series([None, *segment_ids[:-1]], dtype="Int64"),
            "order": pd.Series(orders, dtype="float64"),
            "unused": 1,
        },
        geometry=[
            LineString([(index * 10, 5), (index * 10 + 10, 5)])
            for index in range(len(segment_ids))
        ],
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
    with rasterio.open(
        categorical_path, "w", dtype="int16", nodata=-1, **profile
    ) as dst:
        dst.write(np.array([[1, 1, 2, 2], [1, -1, 2, 2]], dtype="int16"), 1)
    with rasterio.open(d8_path, "w", dtype="uint8", **profile) as dst:
        dst.write(np.array([[1, 2, 4, 8], [16, 32, 64, 128]], dtype="uint8"), 1)
    return catchments_path, segments_path, dem_path, categorical_path, d8_path


def _spec(tmp_path, **changes):
    source_options = {
        name: changes.pop(name)
        for name in ("invalid", "catchment_ids", "segment_ids", "orders")
        if name in changes
    }
    catchments, segments, dem, categorical, d8 = _sources(tmp_path, **source_options)
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


def test_prepare_dataset_writes_v2_vectors_rasters_and_minimal_manifest(tmp_path):
    report = prepare_dataset(_spec(tmp_path))
    assert report.catchment_count == 2
    assert report.segment_count == 2
    assert report.filtered_catchments == 1
    assert report.filtered_segments == 0
    assert not (report.output_dir / "indexes").exists()

    dataset = PreparedDataset.open(report.output_dir)
    dataset.validate()
    manifest = dataset.manifest
    assert manifest["version"] == 2
    assert manifest["filtered"] == {"catchments": 1, "segments": 0}
    assert set(manifest["assets"]) == {"vectors", "rasters"}
    assert set(manifest["assets"]["rasters"]) == {"dem", "land", "d8"}
    assert "sha256" not in manifest["assets"]["vectors"]["catchments"]
    assert manifest["sources"]["vectors"]["segments"]["columns"] == {
        "id": "source_id",
        "id_down": "down",
        "strahler_order": "order",
    }

    catchments = gpd.read_file(
        report.output_dir / "vectors/catchments.fgb"
    ).sort_values("id")
    segments = gpd.read_file(report.output_dir / "vectors/segments.fgb").sort_values(
        "id"
    )
    assert list(catchments.columns) == ["id", "geometry"]
    assert list(segments.columns) == ["id", "id_down", "strahler_order", "geometry"]
    assert list(catchments["id"]) == [1, 2]
    assert list(segments["id"]) == [1, 2]
    assert catchments.crs == segments.crs
    assert pyogrio.read_info(report.output_dir / "vectors/catchments.fgb")[
        "capabilities"
    ]["fast_spatial_filter"]

    for name, dtype in (("dem", "float32"), ("land", "int32"), ("d8", "uint8")):
        with rasterio.open(report.output_dir / f"rasters/{name}.tif") as src:
            assert src.dtypes == (dtype,)
            assert src.nodata is None
            assert src.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
            assert src.transform == from_origin(0, 20, 10, 10)
    with rasterio.open(report.output_dir / "rasters/d8.tif") as src:
        np.testing.assert_array_equal(src.read(1), [[3, 4, 5, 6], [7, 8, 1, 2]])


def test_prepare_filters_strahler_and_corresponding_catchments(tmp_path):
    spec = _spec(
        tmp_path,
        catchment_ids=[1, 2, 3, 4, -1, 5],
        segment_ids=[1, 2, 3, 4, 5],
        orders=[2, 0, None, np.nan, 1.5],
    )
    report = prepare_dataset(spec)
    assert report.segment_count == 2
    assert report.filtered_segments == 3
    assert report.catchment_count == 2
    assert report.filtered_catchments == 4
    segments = gpd.read_file(report.output_dir / "vectors/segments.fgb").sort_values(
        "id"
    )
    catchments = gpd.read_file(
        report.output_dir / "vectors/catchments.fgb"
    ).sort_values("id")
    assert list(segments["id"]) == [1, 5]
    assert list(segments["strahler_order"]) == [2, 1.5]
    assert list(catchments["id"]) == [1, 5]


def test_prepare_preserves_invalid_geometry_without_repair(tmp_path):
    report = prepare_dataset(_spec(tmp_path, invalid=True))
    catchments = gpd.read_file(report.output_dir / "vectors/catchments.fgb")
    assert not catchments.loc[catchments["id"] == 1, "geometry"].iloc[0].is_valid


def test_prepare_does_not_reject_duplicate_ids(tmp_path):
    report = prepare_dataset(
        _spec(
            tmp_path,
            catchment_ids=[1, 1],
            segment_ids=[1, 1],
            orders=[2, 2],
        )
    )

    assert report.catchment_count == 2
    assert report.segment_count == 2


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


def test_prepared_dataset_open_is_lazy_and_validate_rejects_unsafe_path(tmp_path):
    report = prepare_dataset(_spec(tmp_path))
    manifest = json.loads(report.manifest.read_text())
    manifest["assets"]["vectors"]["catchments"]["path"] = "../outside.fgb"
    report.manifest.write_text(json.dumps(manifest))

    dataset = PreparedDataset.open(report.output_dir)
    with pytest.raises(PreparedDataError, match="escapes dataset"):
        dataset.validate()


def test_prepared_dataset_validate_rejects_missing_asset(tmp_path):
    report = prepare_dataset(_spec(tmp_path))
    (report.output_dir / "vectors/catchments.fgb").unlink()
    with pytest.raises(PreparedDataError, match="asset is missing"):
        PreparedDataset.open(report.output_dir).validate()


def test_prepare_cli_reports_staged_and_filtered_counts(tmp_path):
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
    assert "Prepared 2 catchments and 2 segments" in result.output
    assert "Filtered 1 catchments and 0 segments" in result.output
    PreparedDataset.open(tmp_path / "cli-prepared").validate()


def test_prepare_dataset_rejects_invalid_d8_codes(tmp_path):
    spec = _spec(tmp_path)
    with rasterio.open(spec.d8, "r+") as dataset:
        values = dataset.read(1)
        values[0, 0] = 7
        dataset.write(values, 1)
    with pytest.raises(PreparedDataError, match="invalid code"):
        prepare_dataset(spec)
    assert not spec.output_dir.exists()


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
