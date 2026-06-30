import numpy as np
import pytest
from click.testing import CliRunner
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from mgb_vec_hydro.cli import main
from mgb_vec_hydro.exceptions import MiniSamplingError
from mgb_vec_hydro.sampling import _discover_hru_classes


def _discover(values, *, dtype, nodata=None):
    values = np.asarray(values, dtype=dtype)
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            height=values.shape[0],
            width=values.shape[1],
            count=1,
            dtype=dtype,
            nodata=nodata,
            crs="EPSG:3857",
            transform=from_origin(0, values.shape[0], 1, 1),
            tiled=True,
            blockxsize=16,
            blockysize=16,
        ) as dataset:
            dataset.write(values, 1)
            return _discover_hru_classes(dataset)


def test_discovers_sorted_hru_classes_blockwise():
    values = np.tile(np.array([[9, 1], [3, 9]], dtype=np.uint8), (16, 16))

    assert _discover(values, dtype="uint8") == (1, 3, 9)


def test_ignores_declared_hru_nodata():
    assert _discover([[1, 255], [3, 1]], dtype="uint8", nodata=255) == (1, 3)


@pytest.mark.parametrize("value", [0, 101])
def test_rejects_hru_values_outside_domain(value):
    with pytest.raises(MiniSamplingError, match=r"domain 1\.\.100"):
        _discover([[1, value]], dtype="int16")


def test_rejects_floating_point_hru_raster():
    with pytest.raises(MiniSamplingError, match="integer data type"):
        _discover([[1.0, 2.0]], dtype="float32")


def test_rejects_hru_raster_without_valid_classes():
    with pytest.raises(MiniSamplingError, match="no valid class"):
        _discover([[255, 255]], dtype="uint8", nodata=255)


def test_hru_discovery_does_not_call_numpy_unique(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("np.unique must not be used for HRU discovery")

    monkeypatch.setattr(np, "unique", fail)
    assert _discover([[1, 2]], dtype="uint8") == (1, 2)


def test_sample_minis_cli_infers_hru_classes():
    result = CliRunner().invoke(main, ["sample-minis", "--help"])

    assert result.exit_code == 0
    assert "--catchments" in result.output
    assert "--segments" in result.output
    assert "--hru" in result.output
    assert "--mini-catchments" not in result.output
    assert "--mini-reaches" not in result.output
    assert "--hru-raster" not in result.output
    assert "--hru-class-id" not in result.output
