"""Small direct COG inputs used by shared raster-execution tests."""

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin


@pytest.fixture
def prepared_execution_dataset(tmp_path):
    """Build flat explicit COG inputs without a dataset manifest."""
    root = tmp_path / "prepared"
    root.mkdir()
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

    write_cog("dem.tif", [[0, 1, 2], [3, 4, 5]], "float32")
    write_cog("mini_ownership.tif", [[1, 2, 3], [1, 2, 3]], "int32")
    write_cog("drainage.tif", [[1, 1, 1], [0, 0, 0]], "uint8")
    return root
