import json

import numpy as np
import pytest
import rasterio
from click.testing import CliRunner
from rasterio.transform import from_origin
import geopandas as gpd
from shapely.geometry import Polygon, LineString

from mgb_vec_hydro import preparation
from mgb_vec_hydro.cli import main
from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.preparation import (
    NamedRaster,
    PreparationSpec,
    PreparedDataset,
    prepare_dataset,
)
from mgb_vec_hydro.roi import RoiSpec, define_roi_dataset
from mgb_vec_hydro.aggregation import AggregationSpec, aggregate_roi_dataset


def _raster(path, *, dtype="float32", values=None):
    values = np.asarray(values if values is not None else [[1, 2], [3, 4]], dtype=dtype)
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=2, count=1, dtype=dtype,
        crs="EPSG:3857", transform=from_origin(0, 20, 10, 10),
    ) as target:
        target.write(values, 1)


def _spec(tmp_path):
    dem = tmp_path / "dem.tif"
    land = tmp_path / "land.tif"
    _raster(dem)
    _raster(land, dtype="int16", values=[[1, 1], [2, 2]])
    catchments = gpd.GeoDataFrame({"id": [1], "area": [1.0]}, geometry=[Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])], crs="EPSG:3857")
    segments = gpd.GeoDataFrame({"id": [1], "down": [None], "order": [1], "up": [1.0], "length": [1.0]}, geometry=[LineString([(0, 10), (20, 10)])], crs="EPSG:3857")
    catchments.to_file(tmp_path / "catchments.fgb", driver="FlatGeobuf")
    segments.to_file(tmp_path / "segments.fgb", driver="FlatGeobuf")
    define_roi_dataset(RoiSpec(crs="EPSG:3857", catchments=tmp_path / "catchments.fgb", segments=tmp_path / "segments.fgb", outlet_ids=("1",), id_col="id", id_down_col="down", strahler_order_col="order", upstream_area_col="up", unit_length_col="length", unit_area_col="area", output_dir=tmp_path / "roi", workers=1))
    aggregate_roi_dataset(AggregationSpec(roi=tmp_path / "roi", uparea_min=0, lmin=0, output_dir=tmp_path / "minis", workers=1))
    return PreparationSpec(
        dem=dem, minis=tmp_path / "minis",
        rasters=(NamedRaster("land", land, "categorical"),),
        output_dir=tmp_path / "prepared", memory_limit_mb=16, buffer_cells=0,
    )


def test_prepare_clips_domain_and_validates_cogs(tmp_path):
    report = prepare_dataset(_spec(tmp_path))
    assert report.raster_count == 2
    dataset = PreparedDataset.open(report.output_dir)
    dataset.validate()
    assert dataset.manifest["version"] == 4
    assert set(dataset.manifest["assets"]) == {"rasters", "mini_ownership", "drainage", "mini_index"}
    assert not (report.output_dir / "vectors").exists()
    for name in ("dem", "land"):
        with rasterio.open(report.output_dir / f"rasters/{name}.tif") as source:
            assert source.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"


def test_v2_and_vector_manifest_are_rejected(tmp_path):
    report = prepare_dataset(_spec(tmp_path))
    manifest = json.loads(report.manifest.read_text())
    manifest["version"] = 2
    report.manifest.write_text(json.dumps(manifest))
    with pytest.raises(PreparedDataError, match="Unsupported"):
        PreparedDataset.open(report.output_dir).validate()


def test_prepare_rejects_existing_output_and_cleans_failure(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    monkeypatch.setattr(preparation, "_prepare_clipped_raster", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        prepare_dataset(spec)
    assert not spec.output_dir.exists()
    assert not list(tmp_path.glob(".prepared.tmp-*"))


def test_prepare_cli_has_no_vector_options_and_writes_manifest(tmp_path):
    spec = _spec(tmp_path)
    result = CliRunner().invoke(main, [
        "prepare", "--dem", str(spec.dem), "--minis", str(spec.minis),
        "--buffer-cells", "0", "--output-dir", str(spec.output_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "Prepared 1 raster" in result.output
    help_result = CliRunner().invoke(main, ["prepare", "--help"])
    assert "--catchments" not in help_result.output
    assert "--segments" not in help_result.output
