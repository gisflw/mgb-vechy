import multiprocessing as mp

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import Window

from mgb_vec_hydro.exceptions import (
    RasterGridError,
    RasterWriteConflictError,
    WorkMemoryError,
)
from mgb_vec_hydro.execution.executor import WorkerContext
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.raster import (
    AlignedRasterReader,
    CoveringRasterReader,
    RasterAssembler,
    RasterPatch,
    RasterProductSpec,
    grid_from_dem,
    packet_raster_units,
    packet_raster_units_by_block,
    plan_raster_blocks,
    plan_raster_units,
)


def test_raster_blocks_are_row_major_and_include_edge_windows():
    from mgb_vec_hydro.preparation import GridSpec

    grid = GridSpec(
        rasterio.crs.CRS.from_epsg(3857),
        from_origin(0, 5, 1, 1),
        5,
        3,
    )
    assert tuple(plan_raster_blocks(grid, block_size=2)) == (
        Window(0, 0, 2, 2),
        Window(2, 0, 2, 2),
        Window(4, 0, 1, 2),
        Window(0, 2, 2, 1),
        Window(2, 2, 2, 1),
        Window(4, 2, 1, 1),
    )
    with pytest.raises(RasterGridError, match="positive integer"):
        plan_raster_blocks(grid, block_size=0)


def test_covering_raster_reader_maps_target_windows_and_reuses_handle(
    tmp_path, prepared_execution_dataset
):
    grid = grid_from_dem(prepared_execution_dataset / "dem.tif")
    covering = tmp_path / "covering.tif"
    values = np.arange(20, dtype="int16").reshape(4, 5)
    with rasterio.open(
        covering,
        "w",
        driver="GTiff",
        width=5,
        height=4,
        count=1,
        dtype="int16",
        crs="EPSG:3857",
        transform=from_origin(-10, 30, 10, 10),
    ) as target:
        target.write(values, 1)

    context = WorkerContext(mp.get_context("spawn").BoundedSemaphore(1), 2)
    try:
        reader = CoveringRasterReader(grid, {"source": covering}, context)
        assert reader.source("source") is reader.source("source")
        np.testing.assert_array_equal(
            reader.read("source", Window(1, 0, 2, 2)),
            values[1:3, 2:4],
        )
    finally:
        context.close()


def test_covering_raster_reader_rejects_noncovering_or_misaligned_sources(
    tmp_path, prepared_execution_dataset
):
    grid = grid_from_dem(prepared_execution_dataset / "dem.tif")
    path = tmp_path / "small.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=from_origin(0, 20, 10, 10),
    ) as target:
        target.write(np.ones((2, 2), dtype="float32"), 1)
    context = WorkerContext(mp.get_context("spawn").BoundedSemaphore(1), 2)
    try:
        reader = CoveringRasterReader(grid, {"small": path}, context)
        with pytest.raises(RasterGridError, match="does not cover"):
            reader.source("small")
    finally:
        context.close()


def test_raster_units_use_covering_windows_spatial_order_and_bounded_packets(
    prepared_execution_dataset,
):
    grid = grid_from_dem(prepared_execution_dataset / "dem.tif")
    units = plan_raster_units(
        grid,
        [
            ("right", (20, 10, 30, 20)),
            ("left", (0, 10, 10, 20)),
            ("middle", (10, 10, 20, 20)),
        ],
        bytes_per_cell=4,
        working_factor=2,
        block_size=1,
    )
    assert [unit.key for unit in units] == ["left", "middle", "right"]
    assert all(unit.window.width == 1 and unit.window.height == 1 for unit in units)
    packets = packet_raster_units(units, memory_limit_bytes=16, max_units=2)
    assert [[unit.key for unit in packet.units] for packet in packets] == [
        ["left", "middle"],
        ["right"],
    ]
    with pytest.raises(WorkMemoryError, match="left"):
        packet_raster_units(units, memory_limit_bytes=7)


def test_aligned_raster_reader_reuses_handle_and_reads_exact_window(
    prepared_execution_dataset,
):
    context = WorkerContext(mp.get_context("spawn").BoundedSemaphore(1), 2)
    try:
        reader = AlignedRasterReader(
            grid_from_dem(prepared_execution_dataset / "dem.tif"),
            {"dem": prepared_execution_dataset / "dem.tif"},
            context,
        )
        assert reader.source("dem") is reader.source("dem")
        values = reader.read("dem", Window(1, 0, 1, 2))
        np.testing.assert_array_equal(values, [[1], [4]])
        with pytest.raises(RasterGridError, match="integer"):
            reader.read("dem", Window(0.5, 0, 1, 1))
    finally:
        context.close()


def test_direct_raster_paths_reject_missing_and_mismatched_grid_inputs(
    tmp_path, prepared_execution_dataset
):
    dem_path = prepared_execution_dataset / "dem.tif"
    grid = grid_from_dem(dem_path)
    mismatched = tmp_path / "mismatched.tif"
    with rasterio.open(
        mismatched,
        "w",
        driver="COG",
        width=3,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0, 20, 10, 10),
    ) as target:
        target.write(np.ones((2, 3), dtype="float32"), 1)
        target.write_mask(np.full((2, 3), 255, dtype="uint8"))

    context = WorkerContext(mp.get_context("spawn").BoundedSemaphore(1), 2)
    try:
        reader = AlignedRasterReader(
            grid,
            {"missing": tmp_path / "missing.tif", "mismatched": mismatched},
            context,
        )
        with pytest.raises(RasterGridError, match="missing"):
            reader.source("missing")
        with pytest.raises(RasterGridError, match="canonical grid"):
            reader.source("mismatched")
    finally:
        context.close()


def test_raster_assembler_rejects_overlap_and_publishes_cog_atomically(
    tmp_path, prepared_execution_dataset
):
    grid = grid_from_dem(prepared_execution_dataset / "dem.tif")
    publication = AtomicOutputDirectory(tmp_path / "products")
    with publication as staging:
        with RasterAssembler(
            staging,
            grid,
            [RasterProductSpec("hand", "float32", tags={"kind": "test"})],
        ) as assembler:
            first = RasterPatch(
                "hand",
                Window(0, 0, 2, 1),
                np.array([[1, 2]], dtype="float32"),
                np.array([[True, False]]),
            )
            second = RasterPatch(
                "hand",
                Window(1, 0, 2, 1),
                np.array([[3, 4]], dtype="float32"),
                np.array([[True, True]]),
            )
            assembler.write(first)
            assembler.write(second)
            with pytest.raises(RasterWriteConflictError, match="overlaps"):
                assembler.write(first)
            outputs = assembler.finish()
        assert outputs["hand"] == staging / "hand.tif"
        publication.publish(("hand.tif",))

    with rasterio.open(tmp_path / "products" / "hand.tif") as result:
        assert result.tags()["kind"] == "test"
        assert result.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        np.testing.assert_array_equal(result.dataset_mask()[0], [255, 255, 255])
        np.testing.assert_array_equal(result.dataset_mask()[1], [0, 0, 0])
        np.testing.assert_array_equal(result.read(1)[0], [1, 3, 4])
    assert [path.name for path in (tmp_path / "products").iterdir()] == ["hand.tif"]

    context = WorkerContext(mp.get_context("spawn").BoundedSemaphore(1), 2)
    try:
        reader = AlignedRasterReader(
            grid, {"hand": tmp_path / "products" / "hand.tif"}, context
        )
        values = reader.read("hand", Window(0, 0, 3, 1))
        np.testing.assert_array_equal(values, [[1, 3, 4]])
        assert reader.source("hand") is reader.source("hand")
    finally:
        context.close()


def test_raster_assembler_exclusive_block_write_avoids_reads_and_rejects_reuse(
    tmp_path, prepared_execution_dataset
):
    grid = grid_from_dem(prepared_execution_dataset / "dem.tif")
    with RasterAssembler(
        tmp_path, grid, [RasterProductSpec("labels", "int32")], block_size=128
    ) as assembler:
        # The tiny fixture is one edge block even though its dimensions are < 128.
        block = RasterPatch(
            "labels",
            Window(0, 0, grid.width, grid.height),
            np.arange(grid.width * grid.height, dtype="int32").reshape(
                grid.height, grid.width
            ),
            np.ones((grid.height, grid.width), dtype=bool),
        )
        assembler.write_block(block)
        with pytest.raises(RasterWriteConflictError, match="already written"):
            assembler.write_block(block)
        assembler.replace(
            RasterPatch(
                "labels",
                block.window,
                np.full(block.data.shape, 7, dtype="int32"),
                block.valid,
            )
        )
        outputs = assembler.finish()
    with rasterio.open(outputs["labels"]) as result:
        np.testing.assert_array_equal(result.read(1), np.full(block.data.shape, 7))


def test_block_packets_charge_overlapping_blocks_once(prepared_execution_dataset):
    grid = grid_from_dem(prepared_execution_dataset / "dem.tif")
    units = plan_raster_units(
        grid,
        [
            ("left", (0, 10, 10, 20)),
            ("middle", (10, 10, 20, 20)),
            ("right", (20, 10, 30, 20)),
        ],
        bytes_per_cell=4,
        block_size=2,
    )
    packets = packet_raster_units_by_block(
        grid,
        units,
        memory_limit_bytes=10,
        bytes_per_cell=2,
        max_units=3,
        block_size=2,
    )
    assert [[unit.key for unit in packet.units] for packet in packets] == [
        ["left", "middle"],
        ["right"],
    ]
    assert packets[0].estimated_bytes == 8
    assert packets[0].blocks == (Window(0, 0, 2, 2),)
    with pytest.raises(WorkMemoryError, match="right"):
        packet_raster_units_by_block(
            grid,
            units[-1:],
            memory_limit_bytes=3,
            bytes_per_cell=2,
            block_size=2,
        )
