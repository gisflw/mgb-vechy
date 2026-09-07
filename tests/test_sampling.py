import json

import numpy as np
import pandas as pd
import pytest
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import MiniSamplingError, WorkMemoryError
from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
from mgb_vec_hydro.preparation import NamedRaster, PreparationSpec, prepare_dataset
from mgb_vec_hydro.sampling import MiniSamplingSpec, sample_minibasins
from mgb_vec_hydro.terrain import TerrainSpec, create_terrain_dataset


def _sampling_inputs(tmp_path):
    transform = Affine(10, 0, 0, 0, -10, 40)
    dem_values = np.arange(32, dtype=np.float32).reshape(4, 8)
    hru_values = np.array(
        [
            [1, 1, 2, 0, 2, 3, 3, 0],
            [1, 2, 2, 0, 2, 2, 3, 0],
            [1, 1, 2, 0, 2, 3, 3, 0],
            [1, 2, 2, 0, 2, 2, 3, 0],
        ],
        dtype=np.int16,
    )
    for path, values in (
        (tmp_path / "dem.tif", dem_values),
        (tmp_path / "hru.tif", hru_values),
    ):
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=8,
            height=4,
            count=1,
            dtype=values.dtype,
            crs="EPSG:3857",
            transform=transform,
        ) as target:
            target.write(values, 1)
            target.write_mask(np.full(values.shape, 255, dtype="uint8"))

    values = {
        "id": ["a", "b"],
        "id_down": ["b", None],
        "sub": [1, 1],
        "strahler_order": [1, 1],
        "unit_length": [1.0, 1.0],
        "upstream_length": [1.0, 2.0],
        "unit_area": [1.0, 1.0],
        "upstream_area": [1.0, 2.0],
        "water_course": [1, 1],
    }
    catchments = VectorTable.from_pydict(
        values,
        [
            Polygon([(0, 0), (30, 0), (30, 40), (0, 40)]),
            Polygon([(40, 0), (70, 0), (70, 40), (40, 40)]),
        ],
        crs="EPSG:3857",
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        values,
        [
            LineString([(25, 0), (25, 40)]),
            LineString([(65, 0), (65, 40)]),
        ],
        crs="EPSG:3857",
        geometry_type="LineString",
    )

    minis = tmp_path / "minis"
    minis.mkdir()
    write_vector_table(catchments, minis / "mini_catchments.fgb", driver="FlatGeobuf")
    write_vector_table(segments, minis / "mini_segments.fgb", driver="FlatGeobuf")
    (minis / "source_to_mini.csv").write_text(
        "id,mini_id,sub,longitude,latitude\na,a,1,0,0\nb,b,1,0,0\n"
    )
    roi = tmp_path / "roi"
    (roi / "vectors").mkdir(parents=True)
    write_vector_table(
        catchments, roi / "vectors" / "roi_catchments.fgb", driver="FlatGeobuf"
    )
    (roi / "manifest.json").write_text(
        json.dumps(
            {
                "crs_wkt": CRS.from_epsg(3857).to_wkt(),
                "assets": {"catchments": {"path": "vectors/roi_catchments.fgb"}},
            }
        )
    )
    (minis / "manifest.json").write_text(
        json.dumps(
            {
                "contract": "mgb-aggregation-dataset",
                "version": 2,
                "roi": str(roi.resolve()),
                "crs_wkt": CRS.from_epsg(3857).to_wkt(),
                "assets": {
                    "catchments": {
                        "path": "mini_catchments.fgb",
                        "driver": "FlatGeobuf",
                    },
                    "segments": {
                        "path": "mini_segments.fgb",
                        "driver": "FlatGeobuf",
                    },
                    "mapping": {
                        "path": "source_to_mini.csv",
                        "driver": "CSV",
                    },
                },
            }
        )
    )

    prepared = tmp_path / "prepared"
    prepare_dataset(
        PreparationSpec(
            dem=tmp_path / "dem.tif",
            minis=minis,
            rasters=(NamedRaster("hru", tmp_path / "hru.tif", "categorical"),),
            output_dir=prepared,
            buffer_cells=0,
        )
    )
    terrain = tmp_path / "terrain"
    create_terrain_dataset(
        TerrainSpec(prepared=prepared, output_dir=terrain, workers=1)
    )
    return minis, prepared, terrain


def test_sampling_pipeline_is_exact_block_reusing_and_atomic(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    output = tmp_path / "sampled"
    report = sample_minibasins(
        MiniSamplingSpec(
            minis=minis,
            prepared=prepared,
            terrain=terrain,
            output_dir=output,
            workers=1,
            memory_limit_mb=64,
        )
    )

    frame = pd.read_csv(report.sampled_minis)
    assert frame["id"].tolist() == ["a", "b"]
    assert report.mini_count == 2
    assert report.hru_class_ids == (1, 2, 3)
    assert list(frame.filter(regex=r"^hru_").columns) == [
        "hru_1_pct",
        "hru_2_pct",
        "hru_3_pct",
    ]
    np.testing.assert_allclose(frame.filter(regex=r"^hru_").sum(axis=1), 100)
    assert report.execution.worker_diagnostics == (
        {
            "blocks_read": 6,
            "minis": 2,
            "catchment_cells": 24,
            "reach_cells": 8,
        },
    )
    assert sorted(path.name for path in output.iterdir()) == ["sampled_minis.csv"]

    with (
        rasterio.open(prepared / "rasters" / "dem.tif") as dem,
        rasterio.open(terrain / "rasters" / "mini_ownership.tif") as ownership,
        rasterio.open(terrain / "rasters" / "drainage.tif") as drainage,
        rasterio.open(terrain / "rasters" / "hand.tif") as hand,
        rasterio.open(terrain / "rasters" / "ltnd.tif") as ltnd,
    ):
        labels = ownership.read(1)
        drain = drainage.read(1) != 0
        for label, row in enumerate(frame.itertuples(index=False), start=1):
            catchment = labels == label
            reach = catchment & drain
            dem_values = dem.read(1)[reach]
            hand_values = hand.read(1)[catchment]
            ltnd_values = ltnd.read(1)[catchment]
            maximum = float(ltnd_values.max())
            expected_reach_slope = (
                np.percentile(dem_values, 85) - np.percentile(dem_values, 10)
            ) / 0.75
            expected_tributary_slope = hand_values[
                np.isclose(ltnd_values, maximum)
            ].mean() / (maximum / 1000)
            assert row.reach_slope_m_per_km == expected_reach_slope
            assert np.isclose(row.tributary_length_km, maximum / 1000)
            assert np.isclose(row.tributary_slope_m_per_km, expected_tributary_slope)

    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    expected = [transformer.transform(15, 20), transformer.transform(55, 20)]
    np.testing.assert_allclose(frame["longitude"], [value[0] for value in expected])
    np.testing.assert_allclose(frame["latitude"], [value[1] for value in expected])


def test_sampling_serial_runs_are_byte_deterministic(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    paths = []
    for name, workers in (("serial", 1), ("parallel", 2)):
        report = sample_minibasins(
            MiniSamplingSpec(
                minis=minis,
                prepared=prepared,
                terrain=terrain,
                output_dir=tmp_path / name,
                workers=workers,
                memory_limit_mb=64,
            )
        )
        paths.append(report.sampled_minis)
    assert paths[0].read_bytes() == paths[1].read_bytes()


def test_sampling_rejects_missing_hru_and_oversized_unit_without_publication(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)

    missing_output = tmp_path / "missing"
    with pytest.raises(MiniSamplingError, match="no categorical raster"):
        sample_minibasins(
            MiniSamplingSpec(
                minis=minis,
                prepared=prepared,
                terrain=terrain,
                hru_name="missing",
                output_dir=missing_output,
                workers=1,
            )
        )
    assert not missing_output.exists()

    memory_output = tmp_path / "memory"
    with pytest.raises(WorkMemoryError, match="raster unit"):
        sample_minibasins(
            MiniSamplingSpec(
                minis=minis,
                prepared=prepared,
                terrain=terrain,
                output_dir=memory_output,
                workers=1,
                memory_limit_mb=1,
            )
        )
    assert not memory_output.exists()
