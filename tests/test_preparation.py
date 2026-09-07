import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from pyproj import CRS
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.aggregation import AggregationSpec, aggregate_roi_dataset
from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
from mgb_vec_hydro.exceptions import PreparedDataError
from mgb_vec_hydro.preparation import (
    GridSpec,
    NamedRaster,
    PreparationSpec,
    _BlockConnectivity,
    _label_components,
    _plan_connectivity_correction,
    _rasterize_drainage_block,
    _rasterize_ownership_block,
    prepare_dataset,
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
        "mini_catchments.fgb",
        "mini_segments.fgb",
        "source_to_mini.csv",
    ]
    assert not (aggregation.output_dir / "manifest.json").exists()
    assert not any(path.is_dir() for path in aggregation.output_dir.iterdir())

    report = prepare_dataset(
        PreparationSpec(
            dem=dem,
            mini_catchments=tmp_path / "minis" / "mini_catchments.fgb",
            mini_segments=tmp_path / "minis" / "mini_segments.fgb",
            rasters=(NamedRaster("land", land, "categorical"),),
            output_dir=tmp_path / "prepared",
            memory_limit_mb=16,
            buffer_cells=0,
        )
    )

    assert report.raster_count == 2
    assert sorted(path.name for path in report.output_dir.iterdir()) == [
        "dem.tif",
        "drainage.tif",
        "land.tif",
        "mini_index.parquet",
        "mini_ownership.tif",
    ]
    assert not (report.output_dir / "manifest.json").exists()
    assert not any(path.is_dir() for path in report.output_dir.iterdir())
    index = pd.read_parquet(report.mini_index)
    assert list(index.columns) == [
        "mini_label",
        "mini_id",
        "minx",
        "miny",
        "maxx",
        "maxy",
    ]
    for name in ("dem", "land"):
        with rasterio.open(report.output_dir / f"{name}.tif") as source:
            assert source.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"


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
        self.arrays = {"mini_ownership": ownership, "drainage": drainage}

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


def test_connectivity_selects_by_drainage_then_size_then_first_cell():
    owned = np.zeros((4, 7), dtype=bool)
    owned[0, 0:2] = True
    owned[2, 0:3] = True
    owned[0, 5:7] = True
    drainage = np.zeros_like(owned)
    drainage[0, 0] = True
    drainage[2, 0] = True
    drainage[0, 5] = True
    components, sizes, drain_counts, first = _label_components(owned, drainage)
    candidates = np.flatnonzero(drain_counts[1:] > 0) + 1
    selected = min(
        candidates,
        key=lambda value: (
            -int(drain_counts[value]), -int(sizes[value]), int(first[value])
        ),
    )
    assert sizes[selected] == 3

    # Equal drainage and size falls back to the row-major first cell.
    owned[2, 0:3] = False
    components, sizes, drain_counts, first = _label_components(owned, drainage)
    candidates = np.flatnonzero(drain_counts[1:] > 0) + 1
    selected = min(
        candidates,
        key=lambda value: (
            -int(drain_counts[value]), -int(sizes[value]), int(first[value])
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
        np.asarray([1, 0]),
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
