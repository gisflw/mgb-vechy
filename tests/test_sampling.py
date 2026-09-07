import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from mgb_vec_hydro.exceptions import MiniSamplingError
from mgb_vec_hydro.sampling import _discover_hru_classes, _sample


def _discover(values, *, dtype, nodata=None):
    values = np.asarray(values, dtype=dtype)
    with MemoryFile() as memory, memory.open(
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype=dtype,
        nodata=nodata,
        crs="EPSG:3857",
        transform=from_origin(0, values.shape[0], 1, 1),
    ) as dataset:
        dataset.write(values, 1)
        return _discover_hru_classes(dataset)


def _open_raster(memory, values, *, nodata=None):
    values = np.asarray(values)
    dataset = memory.open(
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype=values.dtype,
        nodata=nodata,
        crs="EPSG:3857",
        transform=from_origin(0, values.shape[0], 1, 1),
    )
    dataset.write(values, 1)
    return dataset


def test_discovers_sorted_hru_classes_and_ignores_nodata():
    assert _discover([[9, 1], [3, 255]], dtype="uint8", nodata=255) == (1, 3, 9)


def test_rejects_invalid_hru_rasters():
    with pytest.raises(MiniSamplingError, match=r"domain 1\.\.100"):
        _discover([[1, 0]], dtype="int16")
    with pytest.raises(MiniSamplingError, match="integer data type"):
        _discover([[1.0, 2.0]], dtype="float32")


def test_sample_reads_only_the_geometry_window():
    values = np.arange(100, dtype=np.int16).reshape(10, 10)
    with MemoryFile() as memory, _open_raster(memory, values) as dataset:
        sampled, selected = _sample(
            dataset, box(2, 2, 5, 5), 7, "HAND", False, return_mask=True
        )
    assert selected.shape == (3, 3)
    assert selected.all()
    np.testing.assert_array_equal(sampled, values[5:8, 2:5].ravel())


def test_reach_sampling_preserves_all_touched_behavior():
    values = np.arange(100, dtype=np.int16).reshape(10, 10)
    reach = LineString([(1.1, 8.9), (4.9, 5.1)])
    with MemoryFile() as memory, _open_raster(memory, values) as dataset:
        center_values = _sample(dataset, reach, 7, "DEM", False)
        touched_values = _sample(dataset, reach, 7, "DEM", True)
    assert touched_values.size > center_values.size
    assert set(center_values).issubset(touched_values)


def test_sample_rejects_nodata_and_incomplete_coverage():
    values = np.ones((10, 10), dtype=np.int16)
    values[5, 2] = -9999
    with MemoryFile() as memory, _open_raster(
        memory, values, nodata=-9999
    ) as dataset:
        with pytest.raises(MiniSamplingError, match="contains HAND nodata"):
            _sample(dataset, box(2, 2, 5, 5), 7, "HAND", False)
        with pytest.raises(MiniSamplingError, match="incomplete HAND raster coverage"):
            _sample(dataset, box(9, 9, 11, 11), 7, "HAND", False)
