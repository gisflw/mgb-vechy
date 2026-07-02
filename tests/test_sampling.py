import numpy as np
import pandas as pd
import pytest
from click.testing import CliRunner
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from mgb_vec_hydro.cli import main
from mgb_vec_hydro.exceptions import MiniSamplingError
from mgb_vec_hydro.sampling import (
    _discover_hru_classes,
    _sample,
    _validate_finite_results,
)


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


class _ReadTracker:
    def __init__(self, dataset):
        self.dataset = dataset
        self.windows = []

    def __getattr__(self, name):
        return getattr(self.dataset, name)

    def read(self, *args, **kwargs):
        self.windows.append(kwargs.get("window"))
        return self.dataset.read(*args, **kwargs)


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


def test_sample_reads_only_geometry_window_and_returns_local_mask():
    values = np.arange(100, dtype=np.int16).reshape(10, 10)
    with MemoryFile() as memory, _open_raster(memory, values) as dataset:
        tracked = _ReadTracker(dataset)
        sampled, selected = _sample(
            tracked, box(2, 2, 5, 5), 7, "HAND", False, return_mask=True
        )

    assert tracked.windows[0] is not None
    assert selected.shape == (3, 3)
    assert selected.all()
    np.testing.assert_array_equal(sampled, values[5:8, 2:5].ravel())


def test_sample_preserves_all_touched_for_reach_lines():
    values = np.arange(100, dtype=np.int16).reshape(10, 10)
    reach = LineString([(1.1, 8.9), (4.9, 5.1)])
    with MemoryFile() as memory, _open_raster(memory, values) as dataset:
        center_values = _sample(dataset, reach, 7, "DEM", False)
        touched_values = _sample(dataset, reach, 7, "DEM", True)

    assert touched_values.size > center_values.size
    assert set(center_values).issubset(touched_values)


def test_sample_returns_equal_local_masks_for_aligned_rasters():
    values = np.ones((10, 10), dtype=np.int16)
    geometry = box(2.2, 2.2, 5.2, 5.2)
    with MemoryFile() as first_memory, MemoryFile() as second_memory:
        with _open_raster(first_memory, values) as first, _open_raster(
            second_memory, values * 2
        ) as second:
            _, first_mask = _sample(
                first, geometry, 7, "HAND", False, return_mask=True
            )
            _, second_mask = _sample(
                second, geometry, 7, "LTND", False, return_mask=True
            )

    np.testing.assert_array_equal(first_mask, second_mask)


def test_sample_rejects_local_window_without_selected_cells():
    values = np.ones((10, 10), dtype=np.int16)
    geometry = box(2.01, 2.01, 2.02, 2.02)
    with MemoryFile() as memory, _open_raster(
        memory, values
    ) as dataset, pytest.raises(MiniSamplingError, match="no sampled HAND raster cells"):
        _sample(dataset, geometry, 7, "HAND", False)


def test_sample_rejects_nodata_inside_local_window():
    values = np.ones((10, 10), dtype=np.int16)
    values[5, 2] = -9999
    with MemoryFile() as memory, _open_raster(
        memory, values, nodata=-9999
    ) as dataset, pytest.raises(MiniSamplingError, match="contains HAND nodata"):
        _sample(dataset, box(2, 2, 5, 5), 7, "HAND", False)


def test_sample_rejects_non_finite_value_inside_local_window():
    values = np.ones((10, 10), dtype=np.float32)
    values[5, 2] = np.inf
    with MemoryFile() as memory, _open_raster(
        memory, values
    ) as dataset, pytest.raises(MiniSamplingError, match="non-finite HAND"):
        _sample(dataset, box(2, 2, 5, 5), 7, "HAND", False)


def test_sample_rejects_incomplete_raster_coverage():
    values = np.ones((10, 10), dtype=np.int16)
    with MemoryFile() as memory, _open_raster(
        memory, values
    ) as dataset, pytest.raises(MiniSamplingError, match="incomplete HAND raster coverage"):
        _sample(dataset, box(9, 9, 11, 11), 7, "HAND", False)


def test_sampled_results_allow_null_downstream_id_for_outlet():
    _validate_finite_results(
        pd.DataFrame({"id": [1, 2], "id_down": [np.nan, 1], "slope": [2.0, 3.0]})
    )


@pytest.mark.parametrize(
    "column, value",
    [
        ("id_down", np.inf),
        ("slope", np.nan),
        ("slope", np.inf),
    ],
)
def test_sampled_results_reject_other_non_finite_values(column, value):
    result = pd.DataFrame({"id": [1], "id_down": [np.nan], "slope": [2.0]})
    result.loc[0, column] = value

    with pytest.raises(MiniSamplingError, match="contain non-finite values"):
        _validate_finite_results(result)


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
