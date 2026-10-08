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
    _ownership_index,
    _rasterize_ownership_block,
    _rasterize_segments_block,
    _segment_graph,
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
    updates = []
    report = prepare_dataset(prepare_spec, progress=updates.append)
    phase_times = [report.timings[f"{phase}_wall"] for phase in ("preparing", "processing", "finalizing")]
    assert all(seconds >= 0 for seconds in phase_times)
    assert sum(phase_times) == pytest.approx(report.timings["total"])
    phases = list(dict.fromkeys(update.phase for update in updates))
    assert phases == ["preparing", "processing", "finalizing"]
    operations = {update.operation for update in updates}
    assert "Reading mini inputs" in operations
    assert "Building mini ownership index" in operations
    assert "Compressing grid_catchments.tif" in operations
    assert "Publishing outputs" in operations
    processing = [update for update in updates if update.phase == "processing"]
    assert processing[-1].completed == processing[-1].total == report.execution.reduced

    assert report.raster_count == 2
    assert sorted(path.name for path in report.output_dir.iterdir()) == [
        "dem.tif",
        "grid_catchments.tif",
        "grid_segments.tif",
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
    assert [row[0] for row in read_mini_index(report.grid_catchments)] == [1]
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
    for name in ("dem", "land", "d8", "grid_catchments", "grid_segments"):
        with (
            rasterio.open(reports[1].output_dir / f"{name}.tif") as serial,
            rasterio.open(reports[2].output_dir / f"{name}.tif") as parallel,
        ):
            assert serial.profile == parallel.profile
            np.testing.assert_array_equal(serial.read(1), parallel.read(1))
            np.testing.assert_array_equal(
                serial.dataset_mask(), parallel.dataset_mask()
            )
    assert read_mini_index(reports[1].grid_catchments) == read_mini_index(reports[2].grid_catchments)
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



@pytest.mark.parametrize("order", [[0, 1, 2], [2, 1, 0], [1, 0, 2]])
@pytest.mark.parametrize("graph, winner", [
    ({1: 2, 2: 3, 3: None}, 3),
    ({1: None, 2: None, 3: None}, 1),
    ({1: 3, 2: None, 3: None}, 2),
])
def test_segment_collisions_follow_ancestry_then_lowest_id(order, graph, winner):
    lines = np.asarray([LineString([(0, .5), (2, .5)])] * 3, dtype=object)
    labels = np.asarray([1, 2, 3], dtype="int32")
    result = _rasterize_segments_block(lines[order], labels[order], (1, 2),
                                       from_origin(0, 1, 1, 1), graph)
    np.testing.assert_array_equal(result, [[winner, winner]])
    assert result.dtype == np.int32


@pytest.mark.parametrize("downstream, error", [
    (None, "require id_down"), ([2, 7], "missing downstream"),
    ([2, 1], "cycle"),
])
def test_segment_graph_rejects_invalid_inputs(downstream, error):
    attrs = {"id": [1, 2]}
    if downstream is not None:
        attrs["id_down"] = downstream
    vector = VectorTable.from_pydict(attrs, [LineString([(0, 0), (1, 1)])] * 2,
                                     crs="EPSG:3857", geometry_type="LineString")
    with pytest.raises(PreparedDataError, match=error):
        _segment_graph(vector)


def test_segment_graph_accepts_null_sinks():
    vector = VectorTable.from_pydict({"id": [1, 2], "id_down": [2, None]},
        [LineString([(0, 0), (1, 1)])] * 2, crs="EPSG:3857", geometry_type="LineString")
    assert _segment_graph(vector) == {1: 2, 2: None}

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
    index = read_mini_index(report.grid_catchments)
    with rasterio.open(report.grid_catchments) as cells:
        assert cells.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
        assert json.loads(cells.tags()["mini_index"]) == index
        np.testing.assert_array_equal(cells.read(1), [list(range(1, 13))] * 2)
    assert index == [[i, i - 1, 0, i, 2] for i in range(1, 13)]
    assert sorted(path.name for path in report.files) == ["dem.tif", "grid_catchments.tif", "grid_segments.tif"]
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


def _write_overlay_minis(tmp_path, polygons, lines, downstream):
    attrs = {"id": list(range(1, len(polygons) + 1)), "id_down": downstream}
    paths = []
    for name, geometries, kind in (
        ("catchments", polygons, "MultiPolygon" if any(g.geom_type == "MultiPolygon" for g in polygons) else "Polygon"),
        ("segments", lines, "MultiLineString" if any(g.geom_type == "MultiLineString" for g in lines) else "LineString"),
    ):
        path = tmp_path / f"{name}.fgb"
        write_vector_table(VectorTable.from_pydict(attrs, geometries,
            crs="EPSG:3857", geometry_type=kind), path, driver="FlatGeobuf")
        paths.append(path)
    return paths


@pytest.mark.parametrize("workers", [1, 2])
def test_overlay_preserves_polygons_and_expands_source_masks(tmp_path, workers):
    from shapely.geometry import box
    # Streams cross a polygon gap, neighboring labels and exterior cells.
    polygons = [box(2, 2, 5, 6), box(7, 2, 10, 6)]
    lines = [LineString([(3.5, 4.5), (11.5, 4.5)]),
             LineString([(8.5, 3.5), (8.5, 5.5)])]
    catchments, segments = _write_overlay_minis(tmp_path, polygons, lines, [2, -1])
    dem = tmp_path / "dem.tif"
    hru = tmp_path / "hru.tif"
    _write_multiblock_raster(dem, np.ones((8, 14)), "float32")
    _write_multiblock_raster(hru, np.ones((8, 14)), "int32")
    report = prepare_dataset(PreparationSpec(dem=dem, mini_catchments=catchments,
        mini_segments=segments, output_dir=tmp_path / "prepared", workers=workers,
        rasters=(NamedRaster("hru", hru, "categorical"),)))
    with rasterio.open(report.grid_catchments) as owner, rasterio.open(report.grid_segments) as stream:
        assert owner.bounds == (1, 1, 13, 7)
        assert owner.dtypes == stream.dtypes == ("int32",)
        expected, polygon_valid = _rasterize_ownership_block(np.asarray(polygons, dtype=object),
            np.array([1, 2]), owner.shape, owner.transform)
        streams = _rasterize_segments_block(np.asarray(lines, dtype=object), np.array([1, 2]),
            owner.shape, owner.transform, {1: 2, 2: None})
        expected[streams > 0] = streams[streams > 0]
        valid = polygon_valid | (streams > 0)
        np.testing.assert_array_equal(owner.read(1), expected)
        np.testing.assert_array_equal(owner.dataset_mask(), valid * 255)
        np.testing.assert_array_equal(stream.read(1), streams)
        np.testing.assert_array_equal(stream.dataset_mask(), owner.dataset_mask())
        assert np.any((streams > 0) & ~polygon_valid)
        assert np.all(stream.read(1)[~valid] == 0)
        for path in (report.dem, report.rasters["hru"]):
            with rasterio.open(path) as source:
                np.testing.assert_array_equal(source.dataset_mask(), owner.dataset_mask())


def test_segment_only_blocks_are_masked_and_deterministic(tmp_path):
    from shapely.geometry import box
    # Exercise real block boundaries with a short raster.
    polygons = [box(1, 1, 3, 3)]
    lines = [LineString([(1.5, 1.5), (1028.5, 1.5)])]
    catchments, segments = _write_overlay_minis(tmp_path, polygons, lines, [-1])
    dem = tmp_path / "dem.tif"
    _write_multiblock_raster(dem, np.ones((4, 1030)), "float32")
    outputs = []
    for workers in (1, 2):
        report = prepare_dataset(PreparationSpec(dem=dem, mini_catchments=catchments,
            mini_segments=segments, output_dir=tmp_path / f"prepared-{workers}", workers=workers))
        with rasterio.open(report.grid_segments) as source:
            expected = _rasterize_segments_block(np.asarray(lines, dtype=object),
                np.array([1]), source.shape, source.transform, {1: None})
            np.testing.assert_array_equal(source.read(1), expected)
        arrays = []
        for path in (report.dem, report.grid_catchments, report.grid_segments):
            with rasterio.open(path) as source:
                arrays.extend((source.read(1), source.dataset_mask()))
        outputs.append(arrays)
        assert arrays[3][2, 1028] == 255
        assert arrays[0][2, 1028] == 1
    for first, second in zip(*outputs, strict=True):
        np.testing.assert_array_equal(first, second)


def test_disconnected_components_survive_production_and_route(tmp_path):
    from shapely.geometry import box, MultiPolygon, MultiLineString
    from mgb_vec_hydro.terrain import TerrainSpec, create_terrain_dataset
    polygons = [MultiPolygon([box(1, 1, 4, 4), box(6, 1, 9, 4)])]
    lines = [MultiLineString([[(3.5, 1), (3.5, 4)], [(8.5, 1), (8.5, 4)]])]
    catchments, segments = _write_overlay_minis(tmp_path, polygons, lines, [-1])
    dem = tmp_path / "dem.tif"
    _write_multiblock_raster(dem, np.tile(np.arange(10, 0, -1), (5, 1)), "float32")
    prepared = prepare_dataset(PreparationSpec(dem=dem, mini_catchments=catchments,
        mini_segments=segments, output_dir=tmp_path / "prepared", workers=1))
    with rasterio.open(prepared.grid_catchments) as source:
        assert np.count_nonzero(source.read(1)) == 18  # Both nine-cell polygon components are retained.
    terrain = create_terrain_dataset(TerrainSpec(dem=prepared.dem,
        grid_catchments=prepared.grid_catchments, grid_segments=prepared.grid_segments,
        output_dir=tmp_path / "terrain", workers=1))
    with rasterio.open(terrain.ltnd) as source:
        assert source.read(1).max() > 0


def test_legacy_outputs_retired_atomically(tmp_path, monkeypatch):
    from mgb_vec_hydro.exceptions import PublicationError
    import mgb_vec_hydro.execution.publication as publication
    catchments, segments = _write_multiblock_minis(tmp_path, 2)
    dem = tmp_path / "dem.tif"
    _write_multiblock_raster(dem, np.ones((2, 2)), "float32")
    output = tmp_path / "prepared"
    output.mkdir()
    for name in ("cells.tif", "drainage.tif"):
        (output / name).write_bytes(b"old")
    spec = PreparationSpec(dem=dem, mini_catchments=catchments, mini_segments=segments,
                           output_dir=output, workers=1)
    with pytest.raises(PublicationError, match="already exist"):
        prepare_dataset(spec)
    from dataclasses import replace
    original = publication.os.replace
    def fail(source, destination):
        if Path(destination) == output / "grid_segments.tif":
            raise OSError("injected failure")
        return original(source, destination)
    from pathlib import Path
    monkeypatch.setattr(publication.os, "replace", fail)
    with pytest.raises(PublicationError, match="injected failure"):
        prepare_dataset(replace(spec, overwrite=True))
    assert sorted(p.name for p in output.iterdir()) == ["cells.tif", "drainage.tif"]
    assert all(p.read_bytes() == b"old" for p in output.iterdir())
    monkeypatch.setattr(publication.os, "replace", original)
    prepare_dataset(replace(spec, overwrite=True))
    assert not (output / "cells.tif").exists()
    assert not (output / "drainage.tif").exists()


@pytest.mark.parametrize("outside_dem", [False, True])
def test_overlay_rejects_insufficient_source_coverage(tmp_path, outside_dem):
    from shapely.geometry import box
    polygons = [box(1, 1, 3, 3)]
    lines = [LineString([(1.5, 1.5), (6.5 if outside_dem else 4.5, 1.5)])]
    catchments, segments = _write_overlay_minis(tmp_path, polygons, lines, [-1])
    dem = tmp_path / "dem.tif"
    hru = tmp_path / "hru.tif"
    _write_multiblock_raster(dem, np.ones((5, 6)), "float32")
    _write_multiblock_raster(hru, np.ones((5, 4)), "int32")
    output = tmp_path / "prepared"
    with pytest.raises(PreparedDataError, match="does not cover"):
        prepare_dataset(PreparationSpec(dem=dem, mini_catchments=catchments,
            mini_segments=segments, output_dir=output, workers=1,
            rasters=(NamedRaster("hru", hru, "categorical"),)))
    assert not output.exists()


def test_undrained_disconnected_component_is_preserved_and_masked_in_terrain(tmp_path):
    from shapely.geometry import box, MultiPolygon
    from mgb_vec_hydro.terrain import TerrainSpec, create_terrain_dataset
    catchments, segments = _write_overlay_minis(tmp_path,
        [MultiPolygon([box(1, 1, 4, 4), box(6, 1, 9, 4)])],
        [LineString([(3.5, 1), (3.5, 4)])], [-1])
    dem = tmp_path / "dem.tif"
    _write_multiblock_raster(dem, np.ones((5, 10)), "float32")
    prepared = prepare_dataset(PreparationSpec(dem=dem, mini_catchments=catchments,
        mini_segments=segments, output_dir=tmp_path / "prepared", workers=1))
    with rasterio.open(prepared.grid_catchments) as source:
        assert np.count_nonzero(source.read(1)) == 18
    output = tmp_path / "terrain"
    report = create_terrain_dataset(
        TerrainSpec(
            dem=prepared.dem,
            grid_catchments=prepared.grid_catchments,
            grid_segments=prepared.grid_segments,
            output_dir=output,
            workers=1,
        )
    )
    assert report.undrained_cells == 9
    assert (output / "undrained_cells.csv").read_text().splitlines() == [
        "mini_id,undrained_cells,total_cells,percentage_undrained",
        "1,9,18,50.0",
    ]
    with (
        rasterio.open(prepared.grid_catchments) as ownership,
        rasterio.open(output / "hand.tif") as hand,
    ):
        owned = ownership.read(1) == 1
        assert np.count_nonzero((hand.dataset_mask() == 0) & owned) == 9


def test_collisions_follow_noncontending_intermediate_segments():
    lines = np.asarray([LineString([(0, .5), (2, .5)])] * 2, dtype=object)
    result = _rasterize_segments_block(lines, np.array([1, 3]), (1, 2),
        from_origin(0, 1, 1, 1), {1: 2, 2: 3, 3: None})
    np.testing.assert_array_equal(result, [[3, 3]])


@pytest.mark.parametrize("name", ["grid_catchments", "grid_segments", "cells", "drainage"])
def test_preparation_rejects_reserved_named_rasters(tmp_path, name):
    with pytest.raises(PreparedDataError, match="non-reserved"):
        prepare_dataset(PreparationSpec(dem=tmp_path / "dem",
            mini_catchments=tmp_path / "catchments", mini_segments=tmp_path / "segments",
            output_dir=tmp_path / "out", rasters=(NamedRaster(name, tmp_path / "raster", "categorical"),)))


def test_segment_junctions_at_pixel_and_block_boundaries_match_global_burn():
    from rasterio.windows import Window, transform as window_transform
    transform = from_origin(0, 12, 1, 1)
    lines = np.asarray([
        LineString([(0.25, .25), (512, 6), (1029.75, 11.75)]),
        LineString([(512, 0), (512, 12)]),
        LineString([(0, 6), (1030, 6)]),
    ], dtype=object)
    graph = {1: 2, 2: None, 3: None}
    expected = _rasterize_segments_block(lines, np.array([1, 2, 3]), (12, 1030), transform, graph)
    result = np.zeros_like(expected)
    for col in range(0, 1030, 512):
        window = Window(col, 0, min(512, 1030 - col), 12)
        result[:, col:col + int(window.width)] = _rasterize_segments_block(
            lines, np.array([1, 2, 3]), (12, int(window.width)),
            window_transform(window, transform), graph)
    np.testing.assert_array_equal(result, expected)
