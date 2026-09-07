import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.aggregation import AggregationSpec, aggregate_roi_dataset
from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
from mgb_vec_hydro.preparation import (
    NamedRaster,
    PreparationSpec,
    PreparedDataset,
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
    aggregate_roi_dataset(
        AggregationSpec(
            roi=tmp_path / "roi",
            uparea_min=0,
            lmin=0,
            output_dir=tmp_path / "minis",
            workers=1,
        )
    )

    report = prepare_dataset(
        PreparationSpec(
            dem=dem,
            minis=tmp_path / "minis",
            rasters=(NamedRaster("land", land, "categorical"),),
            output_dir=tmp_path / "prepared",
            memory_limit_mb=16,
            buffer_cells=0,
        )
    )

    assert report.raster_count == 2
    dataset = PreparedDataset.open(report.output_dir)
    dataset.validate()
    assert dataset.manifest["version"] == 4
    assert set(dataset.manifest["assets"]) == {
        "rasters",
        "mini_ownership",
        "drainage",
        "mini_index",
    }
    for name in ("dem", "land"):
        with rasterio.open(report.output_dir / f"rasters/{name}.tif") as source:
            assert source.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
