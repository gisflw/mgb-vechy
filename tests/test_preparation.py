import json

import numpy as np
import pytest
import rasterio
from pyproj import CRS
from rasterio.transform import from_origin
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.aggregation import AggregationSpec, aggregate_roi_dataset
from mgb_vec_hydro.exceptions import (
    ExecutionConfigurationError,
    PreparedDataError,
    WorkerExecutionError,
    WorkMemoryError,
)
from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
from mgb_vec_hydro.preparation import (
    GridSpec,
    NamedRaster,
    PreparationSpec,
    _BlockConnectivity,
    _label_components,
    _plan_connectivity_correction,
    _ownership_index,
    _rasterize_drainage_block,
    _rasterize_ownership_block,
    prepare_dataset,
    read_mini_index,
)
from mgb_vec_hydro.roi import RoiSpec, define_roi_dataset


def _write_raster(path, *, dtype="float32", values=None):
    values = np.asarray(values if values is not None else [[1, 2], [3, 4]], dtype=dtype)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype=dtype,
        crs="EPSG:3857",
        transform=from_origin(0, 20, 10, 10),
    ) as target:
        target.write(values, 1)


def _write_multiblock_raster(path, values, dtype):
    values = np.asarray(values, dtype=dtype)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype=dtype,
        crs="EPSG:3857",
        transform=from_origin(0, values.shape[0], 1, 1),
    ) as target:
        target.write(values, 1)


def _write_multiblock_minis(tmp_path, size):
    attributes = {
        "id": [1],
        "id_down": [-1],
        "sub": [1],
        "p_order": [1],
        "unit_length": [1.0],
        "upstream_length": [1.0],
        "unit_area": [1.0],
        "upstream_area": [1.0],
    }
    catchments = VectorTable.from_pydict(
        attributes,
        [Polygon([(0, 0), (size, 0), (size, size), (0, size)])],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        attributes,
        [LineString([(0, size / 2), (size, size / 2)])],
        crs="EPSG:3857",
        geometry_type="LineString",
    )
    catchment_path = tmp_path / "mini_catchments.fgb"
    segment_path = tmp_path / "mini_segments.fgb"
    write_vector_table(catchments, catchment_path, driver="FlatGeobuf")
    write_vector_table(segments, segment_path, driver="FlatGeobuf")
    return catchment_path, segment_path


@pytest.mark.parametrize("dem_scale", [1.0, 0.01])
def test_prepare_scales_only_dem_and_preserves_grid_and_masks(tmp_path, dem_scale):
    catchments, segments = _write_multiblock_minis(tmp_path, 2)
    dem = tmp_path / "dem.tif"
    land = tmp_path / "land.tif"
    values = np.array([[1200, -9999], [3400, 5600]], dtype="float32")
    _write_multiblock_raster(dem, values, "float32")
    _write_multiblock_raster(land, [[1, 2], [3, 4]], "int16")
    with rasterio.open(dem, "r+") as source:
        source.write_mask(np.array([[255, 0], [255, 255]], dtype="uint8"))
    report = prepare_dataset(PreparationSpec(
        dem=dem, mini_catchments=catchments, mini_segments=segments,
        output_dir=tmp_path / "prepared", workers=1,
        dem_scale=dem_scale, rasters=(
            NamedRaster("land", land, "categorical"),
            NamedRaster("other", dem, "continuous"),
        ),
    ))
    with rasterio.open(dem) as source, rasterio.open(report.dem) as output:
        valid = source.dataset_mask() != 0
        np.testing.assert_allclose(output.read(1)[valid], values[valid] * dem_scale)
        np.testing.assert_array_equal(output.dataset_mask(), source.dataset_mask())
        assert output.transform == source.transform
        assert output.crs == source.crs
        assert output.tags()["units"] == "m"
        assert output.units == ("m",)
        assert float(output.tags()["dem_scale"]) == dem_scale
    with rasterio.open(report.rasters["land"]) as output:
        np.testing.assert_array_equal(output.read(1), [[1, 2], [3, 4]])
    with rasterio.open(report.rasters["other"]) as output:
        np.testing.assert_array_equal(output.read(1)[valid], values[valid])


@pytest.mark.parametrize("dem_scale", [0, -1, float("nan"), float("inf"), True, "0.01"])
def test_prepare_rejects_invalid_dem_scale_before_publication(tmp_path, dem_scale):
    dem = tmp_path / "dem.tif"
    _write_raster(dem)
    output = tmp_path / "prepared"
    with pytest.raises(PreparedDataError, match="DEM scale"):
        prepare_dataset(PreparationSpec(
            dem=dem, mini_catchments=tmp_path / "catchments",
            mini_segments=tmp_path / "segments", output_dir=output,
            dem_scale=dem_scale,
        ))
    assert not output.exists()


@pytest.mark.parametrize("value,scale", [(1e40, 1e-5), (1e35, 1e-45)])
def test_prepare_scales_before_float32_range_conversion(tmp_path, value, scale):
    catchments, segments = _write_multiblock_minis(tmp_path, 2)
    dem = tmp_path / "dem.tif"
    _write_multiblock_raster(dem, np.full((2, 2), value), "float64")
    report = prepare_dataset(PreparationSpec(
        dem=dem, dem_scale=scale, mini_catchments=catchments,
        mini_segments=segments, output_dir=tmp_path / "prepared", workers=1,
    ))
    with rasterio.open(report.dem) as source:
        np.testing.assert_allclose(source.read(1), value * scale, rtol=1e-6)


def test_prepare_pipeline_publishes_valid_canonical_dataset(tmp_path):
    dem = tmp_path / "dem.tif"
    land = tmp_path / "land.tif"
    _write_raster(dem)
    _write_raster(land, dtype="int16", values=[[1, 1], [2, 2]])
    catchments = VectorTable.from_pydict(
        {"id": [1], "area": [1.0]},
        [Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        {"id": [1], "down": [None], "order": [1]},
        [LineString([(0, 10), (20, 10)])],
        crs="EPSG:3857",
        geometry_type="LineString",
    )
    write_vector_table(catchments, tmp_path / "catchments.fgb", driver="FlatGeobuf")
    write_vector_table(segments, tmp_path / "segments.fgb", driver="FlatGeobuf")
    define_roi_dataset(
        RoiSpec(
            crs="EPSG:3857",
            catchments=tmp_path / "catchments.fgb",
            segments=tmp_path / "segments.fgb",
            outlet_ids=("1",),
            id_col="id",
            id_down_col="down",
            strahler_order_col="order",
            output_dir=tmp_path / "roi",
            workers=1,
        )
    )
    aggregation = aggregate_roi_dataset(
        AggregationSpec(
            roi_catchments=tmp_path / "roi" / "roi_catchments.fgb",
            roi_segments=tmp_path / "roi" / "roi_segments.fgb",
            uparea_min=0,
            lmin=0,
            output_dir=tmp_path / "minis",
            workers=1,
        )
    )
    assert sorted(path.name for path in aggregation.output_dir.iterdir()) == [
        "manifest-aggregate.json",
        "mini_catchments.fgb",
        "mini_segments.fgb",
        "source_to_mini.csv",
    ]
    aggregate_manifest = json.loads(
        (aggregation.output_dir / "manifest-aggregate.json").read_text()
    )
    assert aggregate_manifest["parameters"]["uparea_min"] == 0
    assert aggregate_manifest["parameters"]["roi_catchments"] == str(
        (tmp_path / "roi/roi_catchments.fgb").resolve()
    )
    assert not any(path.is_dir() for path in aggregation.output_dir.iterdir())

    prepare_spec = PreparationSpec(
        dem=dem,
        mini_catchments=tmp_path / "minis" / "mini_catchments.fgb",
        mini_segments=tmp_path / "minis" / "mini_segments.fgb",
        rasters=(NamedRaster("land", land, "categorical"),),
        output_dir=tmp_path / "prepared",
        memory_limit_mb=16,
    )
    report = prepare_dataset(prepare_spec)

    assert report.raster_count == 2
    assert sorted(path.name for path in report.output_dir.iterdir()) == [
        "cells.tif",
        "dem.tif",
        "drainage.tif",
        "land.tif",
        "manifest-prepare.json",
    ]
    prepare_manifest = json.loads(
        (report.output_dir / "manifest-prepare.json").read_text()
    )
    assert prepare_manifest["parameters"]["dem"] == str(prepare_spec.dem.resolve())
    assert prepare_manifest["parameters"]["rasters"] == [
        {"name": "land", "path": str(land.resolve()), "kind": "categorical"}
    ]
    assert not any(path.is_dir() for path in report.output_dir.iterdir())
    assert [row[0] for row in read_mini_index(report.mini_ownership)] == [1]
    for name in ("dem", "land"):
        with rasterio.open(report.output_dir / f"{name}.tif") as source:
            assert source.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"


def test_parallel_preparation_matches_serial_across_multiple_blocks(tmp_path):
    size = 1024
    dem = tmp_path / "large-dem.tif"
    land = tmp_path / "large-land.tif"
    d8 = tmp_path / "large-d8.tif"
    values = np.arange(size * size, dtype="float32").reshape(size, size)
    _write_multiblock_raster(dem, values, "float32")
    _write_multiblock_raster(land, (values.astype("int32") % 7) + 1, "int32")
    _write_multiblock_raster(d8, np.full((size, size), 64, dtype="uint8"), "uint8")
    catchments, segments = _write_multiblock_minis(tmp_path, size)

    reports = {}
    for workers in (1, 2):
        reports[workers] = prepare_dataset(
            PreparationSpec(
                dem=dem,
                mini_catchments=catchments,
                mini_segments=segments,
                rasters=(NamedRaster("land", land, "categorical"),),
                d8=d8,
                d8_encoding="esri",
                output_dir=tmp_path / f"prepared-{workers}",
                workers=workers,
                io_slots=2,
                memory_limit_mb=128,
            )
        )

    assert reports[2].execution.task_count == 4
    assert reports[2].execution.submitted == 4
    assert reports[2].execution.peak_admitted_bytes <= 128 * 1024 * 1024
    assert (
        len(
            {
                diagnostic["worker_pid"]
                for diagnostic in reports[2].execution.worker_diagnostics
            }
        )
        == 2
    )
    for name in ("dem", "land", "d8", "cells", "drainage"):
        with (
            rasterio.open(reports[1].output_dir / f"{name}.tif") as serial,
            rasterio.open(reports[2].output_dir / f"{name}.tif") as parallel,
        ):
            assert serial.profile == parallel.profile
            np.testing.assert_array_equal(serial.read(1), parallel.read(1))
            np.testing.assert_array_equal(
                serial.dataset_mask(), parallel.dataset_mask()
            )
    assert read_mini_index(reports[1].mini_ownership) == read_mini_index(reports[2].mini_ownership)
    with rasterio.open(reports[2].d8) as normalized:
        assert np.all(normalized.read(1, masked=True).compressed() == 1)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("workers", 0, "workers"),
        ("io_slots", 0, "io_slots"),
    ],
)
def test_preparation_rejects_invalid_execution_limits(tmp_path, field, value, message):
    dem = tmp_path / "dem.tif"
    _write_raster(dem)
    catchments, segments = _write_multiblock_minis(tmp_path, 20)
    arguments = {
        "dem": dem,
        "mini_catchments": catchments,
        "mini_segments": segments,
        "output_dir": tmp_path / "prepared",
        field: value,
    }
    with pytest.raises(ExecutionConfigurationError, match=message):
        prepare_dataset(PreparationSpec(**arguments))


def test_preparation_enforces_block_memory_and_cleans_worker_failures(tmp_path):
    dem = tmp_path / "dem.tif"
    invalid = tmp_path / "invalid.tif"
    _write_raster(dem)
    _write_raster(invalid, values=[[1.5, 1], [2, 2]])
    catchments, segments = _write_multiblock_minis(tmp_path, 20)
    common = {
        "dem": dem,
        "mini_catchments": catchments,
        "mini_segments": segments,
        "workers": 1,
    }
    with pytest.raises(WorkMemoryError, match="block-"):
        prepare_dataset(
            PreparationSpec(
                **common,
                output_dir=tmp_path / "too-small",
                memory_limit_mb=1,
            )
        )
    assert not (tmp_path / "too-small").exists()

    with pytest.raises(WorkerExecutionError, match="non-integral"):
        prepare_dataset(
            PreparationSpec(
                **common,
                rasters=(NamedRaster("invalid", invalid, "categorical"),),
                output_dir=tmp_path / "worker-failed",
                memory_limit_mb=32,
            )
        )
    assert not (tmp_path / "worker-failed").exists()
    assert not list(tmp_path.glob(".worker-failed.tmp-*"))


def test_joint_ownership_covers_union_and_shared_boundary_is_stable():
    transform = from_origin(0, 2, 1, 1)
    left = Polygon([(0, 0), (1.5, 0), (1.5, 2), (0, 2)])
    right = Polygon([(1.5, 0), (4, 0), (4, 2), (1.5, 2)])
    geometries = np.asarray([left, right], dtype=object)
    labels = np.asarray([1, 2], dtype="int32")

    first, valid = _rasterize_ownership_block(geometries, labels, (2, 4), transform)
    second, second_valid = _rasterize_ownership_block(
        geometries[::-1], labels[::-1], (2, 4), transform
    )

    assert np.all(valid)
    np.testing.assert_array_equal(valid, second_valid)
    np.testing.assert_array_equal(first, second)
    # Pixel centers on the shared boundary belong to the lowest dense label.
    np.testing.assert_array_equal(first[:, 1], [1, 1])
    assert np.all(first != 0)


def test_joint_ownership_rejects_true_cell_overlap():
    transform = from_origin(0, 2, 1, 1)
    geometries = np.asarray(
        [
            Polygon([(0, 0), (3, 0), (3, 2), (0, 2)]),
            Polygon([(1, 0), (4, 0), (4, 2), (1, 2)]),
        ],
        dtype=object,
    )
    with pytest.raises(PreparedDataError, match="overlap at a raster cell"):
        _rasterize_ownership_block(
            geometries, np.asarray([1, 2], dtype="int32"), (2, 4), transform
        )


def test_components_use_eight_neighbors_and_deterministic_statistics():
    owned = np.eye(3, dtype=bool)
    drainage = np.eye(3, dtype=bool)
    components, sizes, drain_counts, first = _label_components(owned, drainage)
    assert components.max() == 1
    np.testing.assert_array_equal(sizes, [0, 3])
    np.testing.assert_array_equal(drain_counts, [0, 3])
    np.testing.assert_array_equal(first, [9, 0])


class _ArrayAssembler:
    def __init__(self, ownership, drainage):
        self.arrays = {"cells": ownership, "drainage": drainage}

    def read(self, product, window, *, masked=True):
        row = int(window.row_off)
        col = int(window.col_off)
        height = int(window.height)
        width = int(window.width)
        return self.arrays[product][row : row + height, col : col + width]


def test_connectivity_keeps_drainage_component_reassigns_enclosed_and_drops_exterior():
    values = np.zeros((5, 7), dtype="int32")
    valid = np.zeros_like(values, dtype=bool)
    values[1, 1:3] = 1
    valid[1, 1:3] = True
    # An undrained label-1 island is completely surrounded by label 2.
    values[2:5, 3:6] = 2
    valid[2:5, 3:6] = True
    values[3, 4] = 1
    # Another label-1 component touches the exterior grid boundary.
    values[0, 6] = 1
    valid[0, 6] = True
    drainage_values = np.zeros_like(values, dtype="uint8")
    drainage_values[1, 1] = 1
    ownership = np.ma.array(values, mask=~valid)
    drainage = np.ma.array(drainage_values, mask=~valid)
    assembler = _ArrayAssembler(ownership, drainage)
    grid = GridSpec(CRS.from_epsg(3857), from_origin(0, 5, 1, 1), 7, 5)

    correction = _plan_connectivity_correction(
        assembler,
        grid,
        Polygon([(0, 0), (7, 0), (7, 5), (0, 5)]),
        1,
        "mini",
        1_000_000,
    )

    assert correction is not None
    targets = dict(zip(correction["flat"], correction["targets"], strict=True))
    assert targets[np.ravel_multi_index((3, 4), values.shape)] == 2
    assert targets[np.ravel_multi_index((0, 6), values.shape)] == 0
    ownership.data.ravel()[correction["flat"]] = correction["targets"]
    ownership.mask.ravel()[correction["flat"]] = correction["targets"] == 0
    assert _ownership_index(assembler, grid, 2) == [
        [1, 1, 3, 3, 4], [2, 3, 0, 6, 3],
    ]


def test_connectivity_selects_by_drainage_then_size_then_first_cell():
    owned = np.zeros((4, 7), dtype=bool)
    owned[0, 0:2] = True
    owned[2, 0:3] = True
    owned[0, 5:7] = True
    drainage = np.zeros_like(owned)
    drainage[0, 0] = True
    drainage[2, 0] = True
    drainage[0, 5] = True
    _, sizes, drain_counts, first = _label_components(owned, drainage)
    candidates = np.flatnonzero(drain_counts[1:] > 0) + 1
    selected = min(
        candidates,
        key=lambda value: (
            -int(drain_counts[value]),
            -int(sizes[value]),
            int(first[value]),
        ),
    )
    assert sizes[selected] == 3

    # Equal drainage and size falls back to the row-major first cell.
    owned[2, 0:3] = False
    _, sizes, drain_counts, first = _label_components(owned, drainage)
    candidates = np.flatnonzero(drain_counts[1:] > 0) + 1
    selected = min(
        candidates,
        key=lambda value: (
            -int(drain_counts[value]),
            -int(sizes[value]),
            int(first[value]),
        ),
    )
    assert first[selected] == 0


def test_joint_drainage_recovers_matching_line_hidden_by_stable_burn_order():
    ownership = np.asarray([[1, 2]], dtype="int32")
    valid = np.ones_like(ownership, dtype=bool)
    segments = np.asarray(
        [LineString([(0, 0.5), (2, 0.5)]), LineString([(0, 0.5), (2, 0.5)])],
        dtype=object,
    )
    drainage = _rasterize_drainage_block(
        segments,
        np.asarray([1, 2], dtype="int32"),
        ownership,
        valid,
        from_origin(0, 1, 1, 1),
    )
    np.testing.assert_array_equal(drainage, [[1, 1]])


def test_streaming_connectivity_joins_diagonal_components_across_block_corner():
    tracker = _BlockConnectivity(4)
    first = np.zeros((2, 2), dtype="int32")
    first[1, 1] = 1
    first_valid = first != 0
    tracker.start_row(0)
    tracker.add_block(0, 0, first, first_valid, first_valid)

    second = np.zeros((2, 2), dtype="int32")
    second[0, 0] = 1
    second_valid = second != 0
    tracker.start_row(2)
    tracker.add_block(2, 2, second, second_valid, np.zeros_like(second_valid))

    assert tracker.disconnected_labels(np.asarray([1], dtype="int32")) == []


def test_preparation_embeds_tight_bounds_and_preserves_numeric_ids(tmp_path):
    ids = list(range(12, 0, -1))
    values = {
        "id": ids, "id_down": [-1] * 12, "sub": [1] * 12,
        "p_order": [1] * 12, "unit_length": [1.0] * 12,
        "upstream_length": [1.0] * 12, "unit_area": [1.0] * 12,
        "upstream_area": [1.0] * 12,
    }
    polygons = [
        Polygon([(i - 0.9, 0.1), (i - 0.1, 0.1), (i - 0.1, 1.9), (i - 0.9, 1.9)])
        for i in ids
    ]
    lines = [LineString([(i - 0.5, 0.1), (i - 0.5, 1.9)]) for i in ids]
    for name, geometries, kind in (
        ("catchments", polygons, "Polygon"), ("segments", lines, "LineString"),
    ):
        write_vector_table(
            VectorTable.from_pydict(values, geometries, crs="EPSG:3857", geometry_type=kind),
            tmp_path / f"{name}.fgb", driver="FlatGeobuf",
        )
    dem = tmp_path / "dem.tif"
    _write_multiblock_raster(dem, np.ones((2, 12)), "float32")
    report = prepare_dataset(PreparationSpec(
        dem=dem, mini_catchments=tmp_path / "catchments.fgb",
        mini_segments=tmp_path / "segments.fgb", output_dir=tmp_path / "prepared",
        workers=1,
    ))
    index = read_mini_index(report.mini_ownership)
    with rasterio.open(report.mini_ownership) as cells:
        assert cells.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        assert json.loads(cells.tags()["mini_index"]) == index
        np.testing.assert_array_equal(cells.read(1), [list(range(1, 13))] * 2)
    assert index == [[i, i - 1, 0, i, 2] for i in range(1, 13)]
    assert sorted(path.name for path in report.files) == ["cells.tif", "dem.tif", "drainage.tif"]
    assert not (report.output_dir / "mini_index.csv").exists()


@pytest.mark.parametrize("bad_ids", [["1"], [2], [True]])
def test_preparation_requires_dense_integer_ids(tmp_path, bad_ids):
    catchments, segments = _write_multiblock_minis(tmp_path, 2)
    for path in (catchments, segments):
        from mgb_vec_hydro.execution.vector import read_vector_table
        vector = read_vector_table(path)
        attrs = vector.table.drop([vector.geometry_column]).to_pydict()
        attrs["id"] = bad_ids
        path.unlink()
        write_vector_table(
            VectorTable.from_pydict(attrs, vector.geometries(), crs=vector.crs,
                                   geometry_type=vector.geometry_type),
            path, driver="FlatGeobuf",
        )
    with pytest.raises(PreparedDataError, match="dense integers"):
        prepare_dataset(PreparationSpec(
            dem=tmp_path / "unused-dem.tif", mini_catchments=catchments,
            mini_segments=segments, output_dir=tmp_path / "prepared", workers=1,
        ))
    assert not (tmp_path / "prepared").exists()
