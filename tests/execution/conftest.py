"""Small on-disk contracts used by shared raster-execution tests."""

import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rasterio
from pyproj import CRS
from rasterio.transform import from_origin


@pytest.fixture
def prepared_execution_dataset(tmp_path):
    """Build the prepared-data contract directly, without running three stages."""
    root = tmp_path / "prepared"
    (root / "rasters").mkdir(parents=True)
    (root / "domain").mkdir()
    transform = from_origin(0, 20, 10, 10)

    def write_cog(relative_path, values, dtype):
        path = root / relative_path
        with rasterio.open(
            path,
            "w",
            driver="COG",
            width=3,
            height=2,
            count=1,
            dtype=dtype,
            crs="EPSG:3857",
            transform=transform,
        ) as target:
            target.write(np.asarray(values, dtype=dtype), 1)
            target.write_mask(np.full((2, 3), 255, dtype="uint8"))

    write_cog("rasters/dem.tif", [[0, 1, 2], [3, 4, 5]], "float32")
    write_cog("domain/mini_ownership.tif", [[1, 2, 3], [1, 2, 3]], "int32")
    write_cog("domain/drainage.tif", [[1, 1, 1], [0, 0, 0]], "uint8")
    pq.write_table(pa.table({"mini_id": [1, 2, 3]}), root / "mini_index.parquet")

    grid = {
        "crs_wkt": CRS.from_epsg(3857).to_wkt(version="WKT2_2019", pretty=False),
        "transform": list(transform)[:6],
        "extent": [0.0, 0.0, 30.0, 20.0],
        "resolution": 10.0,
        "width": 3,
        "height": 2,
        "nodata": "internal-mask",
    }
    assets = {
        "rasters": {"dem": {"path": "rasters/dem.tif", "driver": "COG"}},
        "mini_ownership": {"path": "domain/mini_ownership.tif"},
        "drainage": {"path": "domain/drainage.tif"},
        "mini_index": {"path": "mini_index.parquet"},
    }
    manifest = {
        "contract": "mgb-prepared-dataset",
        "version": 4,
        "grid": grid,
        "assets": assets,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root
