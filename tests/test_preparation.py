import json

import numpy as np
import pytest
import rasterio
from click.testing import CliRunner
from rasterio.transform import from_origin

from mgb_vec_hydro import preparation
from mgb_vec_hydro.cli import main
from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.preparation import (
    NamedRaster,
    PreparationSpec,
    PreparedDataset,
    prepare_dataset,
)


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
    return PreparationSpec(
        dem=dem, crs="EPSG:3857", resolution=10,
        rasters=(NamedRaster("land", land, "categorical"),),
        output_dir=tmp_path / "prepared", memory_limit_mb=16,
    )


def test_prepare_is_raster_only_v3_and_validates_cogs(tmp_path):
    report = prepare_dataset(_spec(tmp_path))
    assert report.raster_count == 2
    dataset = PreparedDataset.open(report.output_dir)
    dataset.validate()
    assert dataset.manifest["version"] == 3
    assert set(dataset.manifest["assets"]) == {"rasters"}
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
    monkeypatch.setattr(preparation, "_prepare_warped_raster", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        prepare_dataset(spec)
    assert not spec.output_dir.exists()
    assert not list(tmp_path.glob(".prepared.tmp-*"))


def test_prepare_cli_has_no_vector_options_and_writes_manifest(tmp_path):
    spec = _spec(tmp_path)
    result = CliRunner().invoke(main, [
        "prepare", "--dem", str(spec.dem), "--crs", "EPSG:3857",
        "--resolution", "10", "--output-dir", str(spec.output_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "Prepared 1 raster" in result.output
    help_result = CliRunner().invoke(main, ["prepare", "--help"])
    assert "--catchments" not in help_result.output
    assert "--segments" not in help_result.output
