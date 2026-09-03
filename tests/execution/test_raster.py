import json
import multiprocessing as mp

import numpy as np
import pytest
import rasterio
from rasterio.windows import Window

from mgb_vec_hydro.exceptions import (
    PreparedDataError,
    RasterGridError,
    RasterWriteConflictError,
    WorkMemoryError,
)
from mgb_vec_hydro.execution.executor import WorkerContext
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.raster import (
    PreparedRasterReader,
    RasterAssembler,
    RasterPatch,
    RasterProductSpec,
    packet_raster_units,
    plan_raster_units,
    prepared_grid,
)


def test_raster_units_use_covering_windows_spatial_order_and_bounded_packets(
    prepared_execution_dataset,
):
    grid = prepared_grid(prepared_execution_dataset)
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


def test_prepared_raster_reader_reuses_handle_and_reads_exact_window(
    prepared_execution_dataset,
):
    context = WorkerContext(mp.get_context("spawn").BoundedSemaphore(1), 2)
    try:
        reader = PreparedRasterReader(prepared_execution_dataset, context)
        assert reader.source("dem") is reader.source("dem")
        values = reader.read("dem", Window(1, 0, 1, 2))
        np.testing.assert_array_equal(values, [[1], [4]])
        with pytest.raises(RasterGridError, match="integer"):
            reader.read("dem", Window(0.5, 0, 1, 1))
    finally:
        context.close()


def test_prepared_raster_reader_rejects_manifest_grid_mismatch(
    prepared_execution_dataset,
):
    manifest_path = prepared_execution_dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["grid"]["transform"][0] = 20
    manifest_path.write_text(json.dumps(manifest))

    context = WorkerContext(mp.get_context("spawn").BoundedSemaphore(1), 2)
    try:
        with pytest.raises(PreparedDataError, match="canonical grid/COG"):
            PreparedRasterReader(prepared_execution_dataset, context)
    finally:
        context.close()


def test_raster_assembler_rejects_overlap_and_publishes_cog_atomically(
    tmp_path, prepared_execution_dataset
):
    grid = prepared_grid(prepared_execution_dataset)
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
        np.testing.assert_array_equal(result.read(1)[0], [1, 3, 4])
    assert [path.name for path in (tmp_path / "products").iterdir()] == ["hand.tif"]


def test_raster_planner_rejects_units_outside_grid(prepared_execution_dataset):
    grid = prepared_grid(prepared_execution_dataset)
    with pytest.raises(RasterGridError, match="outside"):
        plan_raster_units(
            grid,
            [("outside", (100, 100, 110, 110))],
            bytes_per_cell=4,
        )
