import numpy as np
import pyarrow as pa
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import Window

from mgb_vec_hydro.execution.memory import (
    ArrowPacketStore,
    MemorySizing,
    raster_cache,
    sqlite_cache,
)
from mgb_vec_hydro.execution.raster import (
    RasterAssembler,
    RasterPatch,
    RasterProductSpec,
)
from mgb_vec_hydro.preparation import GridSpec


def test_shared_sizing_and_caches_restore():
    import pyogrio
    from rasterio.env import get_gdal_config

    sizing = MemorySizing(32 * 1024**3, 4)
    assert sizing.limit_bytes == 32 * 1024**3
    assert sizing.coordinator_cache_bytes == 4 * 1024**3
    assert sizing.worker_cache_bytes == 1024**3
    assert sizing.packet_bytes == 4 * 1024**3
    assert MemorySizing(1024, 8).worker_cache_bytes == 16
    previous = get_gdal_config("GDAL_CACHEMAX")
    sqlite_previous = pyogrio.get_gdal_config_option("OGR_SQLITE_CACHE")
    with raster_cache(16), sqlite_cache(1024**2):
        assert get_gdal_config("GDAL_CACHEMAX") == 16
        assert float(pyogrio.get_gdal_config_option("OGR_SQLITE_CACHE")) == 1
    assert get_gdal_config("GDAL_CACHEMAX") == previous
    assert pyogrio.get_gdal_config_option("OGR_SQLITE_CACHE") == sqlite_previous


def test_arrow_slices_charge_backing_buffers_spill_release_and_cleanup(tmp_path):
    table = pa.table({"id": np.arange(1000)}).slice(0, 1)
    assert table.get_total_buffer_size() > table.nbytes
    root = tmp_path / "packets"
    with pytest.raises(RuntimeError), ArrowPacketStore(root, table.get_total_buffer_size()) as store:
        store.put(0, table)
        assert store.retained_bytes == table.get_total_buffer_size()
        store.put(1, table)
        assert list(root.glob("*.arrow"))
        assert store.pop(0).equals(table)
        assert store.retained_bytes == 0
        assert store.pop(1).equals(table)
        assert not list(root.iterdir())
        store.put(2, table)
        store.put(3, table)
        raise RuntimeError("cleanup")
    assert not root.exists()
    assert not store._tables


@pytest.mark.parametrize("memory_bytes", [0, 32 * 1024**2])
def test_raster_working_storage_preserves_pixels_masks_and_cleans(tmp_path, memory_bytes):
    grid = GridSpec(rasterio.crs.CRS.from_epsg(3857), from_origin(0, 4, 1, 1), 4, 4)
    data = np.arange(16, dtype="int32").reshape(4, 4)
    valid = data % 2 == 0
    with RasterAssembler(
        tmp_path, grid, [RasterProductSpec("cells", "int32", tags={"units": "m"})],
        scratch_memory_bytes=memory_bytes,
    ) as assembler:
        assert bool(assembler._memory_files) == bool(memory_bytes)
        assembler.write_block(RasterPatch("cells", Window(0, 0, 4, 4), data, valid))
        np.testing.assert_array_equal(assembler.read("cells", Window(0, 0, 4, 4)).mask, ~valid)
        files = assembler.finish()
    assert not assembler._memory_files
    assert not list(tmp_path.glob(".*.working.tif*"))
    with rasterio.open(files["cells"]) as source:
        np.testing.assert_array_equal(source.read(1), data)
        np.testing.assert_array_equal(source.read_masks(1), valid * 255)
        assert source.tags()["units"] == "m"
        assert source.units == ("m",)
        assert source.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"


@pytest.mark.parametrize("memory_bytes", [0, 32 * 1024**2])
def test_raster_storage_cleanup_when_compression_fails(tmp_path, monkeypatch, memory_bytes):
    grid = GridSpec(rasterio.crs.CRS.from_epsg(3857), from_origin(0, 4, 1, 1), 4, 4)
    assembler = RasterAssembler(
        tmp_path, grid, [RasterProductSpec("cells", "int32")], scratch_memory_bytes=memory_bytes,
    )
    memories = list(assembler._memory_files.values())

    def fail(*args, **kwargs):
        raise RuntimeError("compression failed")

    monkeypatch.setattr("mgb_vec_hydro.execution.raster.copy_raster", fail)
    with pytest.raises(RuntimeError, match="compression failed"):
        assembler.finish()
    assert not assembler._sources
    assert all(memory.closed for memory in memories)
    assert not list(tmp_path.glob(".*.working.tif*"))
