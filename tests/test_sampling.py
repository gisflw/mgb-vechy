from dataclasses import replace
import importlib

import numpy as np
import pandas as pd
import pytest
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.shutil import copy as copy_raster
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import CheckpointError, MiniSamplingError, WorkMemoryError
from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
from mgb_vec_hydro.preparation import (
    GridSpec,
    NamedRaster,
    PreparationSpec,
    prepare_dataset,
)
from mgb_vec_hydro.sampling import (
    MiniSamplingSpec,
    _cell_areas_km2,
    sample_minibasins,
)
from mgb_vec_hydro.terrain import TerrainSpec, create_terrain_dataset


def _sampling_inputs(
    tmp_path, *, crs="EPSG:3857", dem_scale=1.0, transform=None, explicit_d8=False,
):
    transform = transform or Affine(10, 0, 0, 0, -10, 40)
    dem_values = np.arange(32, dtype=np.float32).reshape(4, 8) / dem_scale
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
        *([(tmp_path / "d8.tif", np.full((4, 8), 3, dtype="uint8"))] if explicit_d8 else []),
    ):
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=8,
            height=4,
            count=1,
            dtype=values.dtype,
            crs=crs,
            transform=transform,
        ) as target:
            target.write(values, 1)
            target.write_mask(np.full(values.shape, 255, dtype="uint8"))

    values = {
        "id": [1, 2],
        "id_down": [2, -1],
        "sub": [1, 1],
        "p_order": [1, 2],
        "unit_length": [1.0, 1.0],
        "upstream_length": [1.0, 2.0],
        "unit_area": [1.0, 1.0],
        "upstream_area": [1.0, 2.0],
    }
    def point(col, row):
        return (transform.c + col * transform.a, transform.f + row * transform.e)

    catchments = VectorTable.from_pydict(
        values,
        [
            Polygon([point(0, 4), point(3, 4), point(3, 0), point(0, 0)]),
            Polygon([point(4, 4), point(7, 4), point(7, 0), point(4, 0)]),
        ],
        crs=crs,
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        values,
        [
            LineString([point(2.5, 4), point(2.5, 0)]),
            LineString([point(6.5, 4), point(6.5, 0)]),
        ],
        crs=crs,
        geometry_type="LineString",
    )

    minis = tmp_path / "minis"
    minis.mkdir()
    write_vector_table(catchments, minis / "mini_catchments.fgb", driver="FlatGeobuf")
    write_vector_table(segments, minis / "mini_segments.fgb", driver="FlatGeobuf")
    (minis / "source_to_mini.csv").write_text(
        "id,mini_id,sub,longitude,latitude\n1,1,1,0,0\n2,2,1,0,0\n"
    )
    prepared = tmp_path / "prepared"
    preparation = prepare_dataset(
        PreparationSpec(
            dem=tmp_path / "dem.tif",
            mini_catchments=minis / "mini_catchments.fgb",
            mini_segments=minis / "mini_segments.fgb",
            rasters=(NamedRaster("hru", tmp_path / "hru.tif", "categorical"),),
            output_dir=prepared,
            dem_scale=dem_scale,
            d8=tmp_path / "d8.tif" if explicit_d8 else None,
            d8_encoding="canonical" if explicit_d8 else None,
        )
    )
    terrain = tmp_path / "terrain"
    terrain_report = create_terrain_dataset(
        TerrainSpec(
            dem=preparation.dem,
            mini_ownership=preparation.mini_ownership,
            drainage=preparation.drainage,
            mini_index=preparation.mini_index,
            output_dir=terrain,
            workers=1,
            d8=preparation.d8,
            direction_source="d8" if explicit_d8 else "dem",
        )
    )
    return minis, preparation, terrain_report


def test_sampling_geographic_centimetre_dem_has_metric_slopes(tmp_path):
    transform = Affine(1 / 3600, 0, -45, 0, -1 / 3600, -13)
    minis, prepared, terrain = _sampling_inputs(
        tmp_path, crs="EPSG:4326", dem_scale=0.01, transform=transform,
        explicit_d8=True,
    )
    # Explicit D8 makes every owned cell flow east to drainage.
    # The farthest column is two horizontal geodesic steps from the reach.
    geod = CRS.from_epsg(4326).get_geod()
    expected_distances = []
    for row in range(4):
        lat = transform.f + (row + 0.5) * transform.e
        lon = transform.c + 0.5 * transform.a
        expected_distances.append(2 * geod.inv(lon, lat, lon + transform.a, lat)[2])
    maximum_m = max(expected_distances)
    # Existing unit_length=1 km attributes are the authoritative reach metric.
    report = sample_minibasins(_sampling_spec(minis, prepared, terrain, tmp_path / "sampled"))
    frame = pd.read_csv(report.sampled_minis)
    np.testing.assert_allclose(frame.reach_slope_m_per_km, (22.4 - 4.4) / 0.75)
    np.testing.assert_allclose(frame.tributary_length_km, maximum_m / 1000, rtol=1e-6)
    np.testing.assert_allclose(frame.tributary_slope_m_per_km, -2 / (maximum_m / 1000), rtol=1e-6)
    assert report.execution.worker_diagnostics[0]["blocks_read"] == 6
    for path in (prepared.dem, terrain.hand, terrain.ltnd):
        with rasterio.open(path) as source:
            assert source.tags()["units"] == "m"
            assert source.units == ("m",)


@pytest.mark.parametrize("name", ["dem", "hand", "ltnd"])
def test_sampling_rejects_legacy_units_without_publication(tmp_path, name):
    from rasterio.shutil import copy as copy_raster

    minis, prepared, terrain = _sampling_inputs(tmp_path)
    spec = _sampling_spec(minis, prepared, terrain, tmp_path / "sampled")
    legacy = tmp_path / f"legacy-{name}.tif"
    with rasterio.open(getattr(spec, name)) as source:
        tags = source.tags()
        tags["units"] = "degrees" if name == "ltnd" else ""
        # Copy through a working GTiff before recreating the required COG.
        working = tmp_path / "working.tif"
        copy_raster(source, working, driver="GTiff")
    with rasterio.open(working, "r+") as source:
        source.update_tags(**tags)
    copy_raster(working, legacy, driver="COG")
    with pytest.raises(MiniSamplingError, match=f"{name.upper()}.*regenerate"):
        sample_minibasins(replace(spec, **{name: legacy}))
    assert not spec.output_dir.exists()


@pytest.mark.parametrize(
    "stage,legacy_version", [("terrain", "1"), ("sampling", "3")]
)
def test_metric_stages_reject_legacy_checkpoints(
    tmp_path, monkeypatch, stage, legacy_version
):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    module = importlib.import_module(f"mgb_vec_hydro.{stage}")
    if stage == "sampling":
        run = sample_minibasins
        spec = _sampling_spec(minis, prepared, terrain, tmp_path / "output")
    else:
        run = create_terrain_dataset
        spec = TerrainSpec(
            dem=prepared.dem, mini_ownership=prepared.mini_ownership,
            drainage=prepared.drainage, mini_index=prepared.mini_index,
            output_dir=tmp_path / "output", workers=1,
        )
    spec = replace(spec, checkpoint_dir=tmp_path / "checkpoint")
    fingerprint = module.execution_fingerprint

    def old_fingerprint(**kwargs):
        kwargs["version"] = legacy_version
        return fingerprint(**kwargs)

    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupt after checkpoint creation")

    with monkeypatch.context() as patch:
        patch.setattr(module, "execution_fingerprint", old_fingerprint)
        patch.setattr(module.LocalExecutor, "run", interrupt)
        with pytest.raises(RuntimeError, match="interrupt"):
            run(spec)
    with pytest.raises(CheckpointError, match="incompatible"):
        run(spec)
    assert not spec.output_dir.exists()


def _sampling_spec(
    minis,
    prepared,
    terrain,
    output,
    *,
    hru=None,
    workers=1,
    memory_limit_mb=64,
):
    return MiniSamplingSpec(
        mini_catchments=minis / "mini_catchments.fgb",
        mini_segments=minis / "mini_segments.fgb",
        mini_index=prepared.mini_index,
        dem=prepared.dem,
        mini_ownership=prepared.mini_ownership,
        drainage=prepared.drainage,
        hand=terrain.hand,
        ltnd=terrain.ltnd,
        hru=prepared.rasters["hru"] if hru is None else hru,
        output_dir=output,
        workers=workers,
        memory_limit_mb=memory_limit_mb,
    )


def _rewrite_raster(path, output, edit):
    with rasterio.open(path) as source:
        values = source.read(1)
        mask = source.dataset_mask()
        profile = source.profile.copy()
        tags = source.tags()
    edit(values)
    profile["driver"] = "GTiff"
    working = output.with_name(output.stem + "-working.tif")
    with rasterio.open(working, "w", **profile) as target:
        target.write(values, 1)
        target.write_mask(mask)
        target.update_tags(**tags)
    copy_raster(working, output, driver="COG")
    return output


def _direct_cell_areas_km2(crs, transform, rows, cols):
    source_crs = CRS.from_user_input(crs)
    geodetic = source_crs.geodetic_crs
    transformer = Transformer.from_crs(source_crs, geodetic, always_xy=True)
    geod = source_crs.get_geod()
    areas = []
    for row, col in zip(rows, cols, strict=True):
        x0 = transform.c + col * transform.a
        x1 = x0 + transform.a
        y1 = transform.f + row * transform.e
        y0 = y1 + transform.e
        lon, lat = transformer.transform([x0, x1, x1, x0], [y0, y0, y1, y1])
        areas.append(abs(geod.polygon_area_perimeter(lon, lat)[0]) / 1e6)
    return np.asarray(areas)


def test_sampling_pipeline_is_exact_block_reusing_and_atomic(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    output = tmp_path / "sampled"
    report = sample_minibasins(
        _sampling_spec(minis, prepared, terrain, output)
    )

    frame = pd.read_csv(report.sampled_minis)
    assert frame["id"].tolist() == [1, 2]
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
        rasterio.open(prepared.dem) as dem,
        rasterio.open(prepared.mini_ownership) as ownership,
        rasterio.open(prepared.drainage) as drainage,
        rasterio.open(terrain.hand) as hand,
        rasterio.open(terrain.ltnd) as ltnd,
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
            assert row.reach_reference_elevation == np.percentile(dem_values, 50)
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


def test_sampling_flooded_areas_use_hand_thresholds_and_cell_areas(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    with rasterio.open(prepared.mini_ownership) as source:
        labels = source.read(1)
    with rasterio.open(prepared.drainage) as source:
        drainage = source.read(1)
    reach_cells = np.argwhere((labels == 1) & (drainage != 0))
    custom_hand = np.array(
        [-2, 0, 1, 1.01, 2, 99.5, 100, 100.01, 200, 3, 0.5, 10.2],
        dtype=np.float32,
    )
    assert custom_hand.size == np.count_nonzero(labels == 1)

    def remove_reach_cell(values):
        values[tuple(reach_cells[0])] = 0

    def set_hand_values(values):
        values[labels == 1] = custom_hand

    drainage_path = _rewrite_raster(
        prepared.drainage, tmp_path / "drainage-odd-reach.tif", remove_reach_cell
    )
    hand_path = _rewrite_raster(
        terrain.hand, tmp_path / "hand-thresholds.tif", set_hand_values
    )
    spec = replace(
        _sampling_spec(minis, prepared, terrain, tmp_path / "sampled"),
        drainage=drainage_path,
        hand=hand_path,
    )
    report = sample_minibasins(spec)
    frame = pd.read_csv(report.sampled_minis)
    area_columns = [f"flooded_area_{stage}m_km2" for stage in range(1, 101)]
    assert list(
        frame.columns[
            frame.columns.get_loc(area_columns[0]) : frame.columns.get_loc("hru_1_pct")
        ]
    ) == area_columns
    assert np.all(np.diff(frame[area_columns].to_numpy(), axis=1) >= 0)

    with (
        rasterio.open(spec.dem) as dem,
        rasterio.open(spec.mini_ownership) as ownership,
        rasterio.open(spec.drainage) as drain,
        rasterio.open(spec.hand) as hand,
    ):
        dem_values = dem.read(1)
        labels = ownership.read(1)
        drainage = drain.read(1) != 0
        hand_values = hand.read(1)
        assert report.reach_cells == 7  # Three cells for mini 1, four for mini 2.
        assert np.count_nonzero((labels == 1) & drainage) == 3
        assert np.count_nonzero((labels == 2) & drainage) == 4
        for label, row in enumerate(frame.itertuples(index=False), start=1):
            reach = (labels == label) & drainage
            values = dem_values[reach]
            p10, p50, p85 = np.percentile(values, (10, 50, 85))
            assert row.reach_reference_elevation == p50
            assert row.reach_slope_m_per_km == (p85 - p10) / 0.75

            catchment = labels == label
            cell_rows, cell_cols = np.nonzero(catchment)
            areas = _direct_cell_areas_km2(
                dem.crs, dem.transform, cell_rows, cell_cols
            )
            catchment_hand = hand_values[catchment]
            expected = [
                areas[catchment_hand <= stage].sum() for stage in range(1, 101)
            ]
            np.testing.assert_allclose(
                frame.loc[label - 1, area_columns].to_numpy(dtype=float), expected
            )


def test_cell_area_calculation_handles_geographic_rows_and_projected_cells():
    cases = (
        ("EPSG:4326", Affine(0.01, 0, -45, 0, -0.01, -10)),
        ("EPSG:3857", Affine(1000, 0, 0, 0, -1000, 1_000_000)),
    )
    for crs, transform in cases:
        grid = GridSpec(CRS.from_user_input(crs), transform, 2, 2)
        selected = np.ones((2, 2), dtype=bool)
        actual = _cell_areas_km2(grid, rasterio.windows.Window(0, 0, 2, 2), selected)
        expected = _direct_cell_areas_km2(
            grid.crs,
            transform,
            np.array([0, 0, 1, 1]),
            np.array([0, 1, 0, 1]),
        )
        np.testing.assert_allclose(actual, expected)
        if crs == "EPSG:4326":
            assert actual[0] > actual[2]
        else:
            assert actual[0] != actual[2]


def test_sampling_serial_runs_are_byte_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr("mgb_vec_hydro.sampling.MAX_PACKET_UNITS", 1)
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    paths = []
    for name, workers in (("serial", 1), ("parallel", 2)):
        report = sample_minibasins(
            _sampling_spec(minis, prepared, terrain, tmp_path / name, workers=workers)
        )
        paths.append(report.sampled_minis)
    assert paths[0].read_bytes() == paths[1].read_bytes()


def test_sampling_rejects_missing_hru_and_oversized_unit_without_publication(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)

    missing_output = tmp_path / "missing"
    with pytest.raises(MiniSamplingError, match="HRU input is not a local file"):
        sample_minibasins(
            _sampling_spec(
                minis,
                prepared,
                terrain,
                missing_output,
                hru=tmp_path / "missing.tif",
            )
        )
    assert not missing_output.exists()

    memory_output = tmp_path / "memory"
    with pytest.raises(WorkMemoryError, match="raster unit"):
        sample_minibasins(
            _sampling_spec(
                minis,
                prepared,
                terrain,
                memory_output,
                workers=1,
                memory_limit_mb=1,
            )
        )
    assert not memory_output.exists()
