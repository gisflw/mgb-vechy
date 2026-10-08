import json
import warnings
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
import rasterio
from affine import Affine
from pyproj import CRS, Transformer
from rasterio.shutil import copy as copy_raster
from shapely import point_on_surface
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.crs_utils import CrsError
from mgb_vec_hydro.exceptions import (
    MiniSamplingError,
    WorkerExecutionError,
    WorkMemoryError,
)
from mgb_vec_hydro.execution.vector import VectorTable, write_vector_table
from mgb_vec_hydro.preparation import (
    NamedRaster,
    PreparationSpec,
    prepare_dataset,
)
from mgb_vec_hydro.sampling import (
    MiniSamplingSpec,
    sample_minibasins,
)
from mgb_vec_hydro.terrain import TerrainSpec, create_terrain_dataset


def _sampling_inputs(
    tmp_path, *, crs="EPSG:3857", dem_scale=1.0, transform=None, explicit_d8=False,
    mini_count=2,
):
    transform = transform or Affine(10, 0, 0, 0, -10, 40)
    dem_values = np.arange(16 * mini_count, dtype=np.float32).reshape(4, 4 * mini_count) / dem_scale
    hru_values = np.array(
        [
            [1, 1, 2, 0, 2, 3, 3, 0],
            [1, 2, 2, 0, 2, 2, 3, 0],
            [1, 1, 2, 0, 2, 3, 3, 0],
            [1, 2, 2, 0, 2, 2, 3, 0],
        ],
        dtype=np.int16,
    )
    hru_values = np.tile(hru_values, (1, (mini_count + 1) // 2))[:, :4 * mini_count]
    for path, values in (
        (tmp_path / "dem.tif", dem_values),
        (tmp_path / "hru.tif", hru_values),
        *([(tmp_path / "d8.tif", np.full((4, 4 * mini_count), 3, dtype="uint8"))] if explicit_d8 else []),
    ):
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=4 * mini_count,
            height=4,
            count=1,
            dtype=values.dtype,
            crs=crs,
            transform=transform,
        ) as target:
            target.write(values, 1)
            target.write_mask(np.full(values.shape, 255, dtype="uint8"))

    values = {
        "id": list(range(1, mini_count + 1)),
        "id_down": list(range(2, mini_count + 1)) + [-1],
        "sub": [1] * mini_count,
        "p_order": list(range(1, mini_count + 1)),
        "unit_length": [1.0] * mini_count,
        "upstream_length": [float(i) for i in range(1, mini_count + 1)],
        "unit_area": [1.0] * mini_count,
        "upstream_area": [float(i) for i in range(1, mini_count + 1)],
    }
    def point(col, row):
        return (transform.c + col * transform.a, transform.f + row * transform.e)

    catchments = VectorTable.from_pydict(
        values,
        [
            Polygon([point(4*i, 4), point(4*i+3, 4), point(4*i+3, 0), point(4*i, 0)])
            for i in range(mini_count)
        ],
        crs=crs,
        geometry_type="Polygon",
    )
    segments = VectorTable.from_pydict(
        values,
        [
            LineString([point(4*i+2.5, 4), point(4*i+2.5, 0)])
            for i in range(mini_count)
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
    updates = []
    report = sample_minibasins(
        _sampling_spec(minis, prepared, terrain, tmp_path / "sampled"),
        progress=updates.append,
    )
    operations = {update.operation for update in updates}
    assert "Validating raster inputs" in operations
    assert "Planning sampling packets" in operations
    assert "Assembling sampled CSV" in operations
    assert "Validating sampled output" in operations
    assert "Publishing outputs" in operations
    frame = pd.read_csv(report.sampled_minis)
    np.testing.assert_allclose(frame.reach_slope, (22.4 - 4.4) / 0.75)
    np.testing.assert_allclose(frame.tributary_length, maximum_m / 1000, rtol=1e-6)
    np.testing.assert_allclose(frame.tributary_slope, -2 / (maximum_m / 1000), rtol=1e-6)
    assert report.execution.worker_diagnostics[0]["blocks_read"] == 6
    for path in (prepared.dem, terrain.hand, terrain.ltnd):
        with rasterio.open(path) as source:
            assert source.tags()["units"] == "m"
            assert source.units == ("m",)


@pytest.mark.parametrize("name", ["dem", "hand", "ltnd"])
def test_sampling_rejects_wrong_units_without_publication(tmp_path, name):
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
    with pytest.raises(CrsError, match=f"{name.upper()}.*units=m"):
        sample_minibasins(replace(spec, **{name: legacy}))
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


def _rewrite_raster(path, output, edit, mask_edit=None):
    with rasterio.open(path) as source:
        values = source.read(1)
        mask = source.dataset_mask()
        profile = source.profile.copy()
        tags = source.tags()
    edit(values)
    if mask_edit is not None:
        mask_edit(mask)
    profile["driver"] = "GTiff"
    working = output.with_name(output.stem + "-working.tif")
    with rasterio.open(working, "w", **profile) as target:
        target.write(values, 1)
        target.write_mask(mask)
        target.update_tags(**tags)
    copy_raster(working, output, driver="COG")
    return output


def test_sampling_pipeline_is_exact_block_reusing_and_atomic(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    output = tmp_path / "sampled"
    spec = _sampling_spec(minis, prepared, terrain, output)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = sample_minibasins(spec)
        phase_times = [report.timings[f"{phase}_wall"] for phase in ("preparing", "processing", "finalizing")]
        assert all(seconds >= 0 for seconds in phase_times)
        assert sum(phase_times) == pytest.approx(report.timings["total"])
    assert not caught  # Masked cells outside mini ownership are not findings.

    frame = pd.read_csv(report.sampled_minis)
    assert frame["id"].tolist() == [1, 2]
    assert report.mini_count == 2
    assert report.hru_class_ids == (1, 2, 3)
    assert list(frame.filter(regex=r"^hru_").columns) == [
        "hru_1",
        "hru_2",
        "hru_3",
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
    assert sorted(path.name for path in output.iterdir()) == [
        "manifest-sample-minis.json",
        "sampled_minis.csv",
    ]
    manifest = json.loads((output / "manifest-sample-minis.json").read_text())
    assert manifest["parameters"]["hru"] == str(spec.hru.resolve())
    assert manifest["parameters"]["batch_size"] == 10_000

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
            assert row.reach_elevation == np.percentile(dem_values, 50)
            expected_tributary_slope = hand_values[
                np.isclose(ltnd_values, maximum)
            ].mean() / (maximum / 1000)
            assert row.reach_slope == expected_reach_slope
            assert np.isclose(row.tributary_length, maximum / 1000)
            assert np.isclose(row.tributary_slope, expected_tributary_slope)

    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    points = [point_on_surface(LineString([(x, 0), (x, 40)])) for x in (25, 65)]
    expected = [transformer.transform(point.x, point.y) for point in points]
    np.testing.assert_allclose(frame["longitude"], [value[0] for value in expected])
    np.testing.assert_allclose(frame["latitude"], [value[1] for value in expected])


def test_sampling_flooded_areas_use_hand_thresholds_and_catchment_area(tmp_path):
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
    area_columns = [f"flooded_area_{stage}" for stage in range(1, 101)]
    assert frame.columns.get_loc("hru_1") < frame.columns.get_loc(area_columns[0])
    assert list(frame.columns[frame.columns.get_loc(area_columns[0]) :]) == area_columns
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
            assert row.reach_elevation == p50
            assert row.reach_slope == (p85 - p10) / 0.75

            catchment_hand = hand_values[labels == label]
            expected = [
                row.unit_area * np.count_nonzero(catchment_hand <= stage) / len(catchment_hand)
                for stage in range(1, 101)
            ]
            np.testing.assert_allclose(
                frame.loc[label - 1, area_columns].to_numpy(dtype=float), expected
            )



def test_fully_flooded_area_equals_vector_catchment_area(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    with rasterio.open(prepared.mini_ownership) as source:
        labels = source.read(1)
    hand_path = _rewrite_raster(
        terrain.hand,
        tmp_path / "fully-flooded-hand.tif",
        lambda values: values.__setitem__(labels > 0, 50),
    )
    report = sample_minibasins(
        replace(
            _sampling_spec(minis, prepared, terrain, tmp_path / "sampled"),
            hand=hand_path,
        )
    )
    frame = pd.read_csv(report.sampled_minis)
    for row in frame.itertuples(index=False):
        assert row.flooded_area_100 == row.unit_area


def test_sampling_warns_for_partial_nodata_and_uses_valid_cells(tmp_path, monkeypatch):
    from mgb_vec_hydro.execution.memory import MemorySizing

    monkeypatch.setattr(MemorySizing, "packet_bytes", property(lambda self: 1))
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    with rasterio.open(prepared.mini_ownership) as source:
        labels = source.read(1)
    mini_one = np.argwhere(labels == 1)
    mini_two = np.argwhere(labels == 2)
    nodata_paths = {
        "dem": _rewrite_raster(
            prepared.dem,
            tmp_path / "dem-with-nan.tif",
            lambda values: values.__setitem__(tuple(mini_one[0]), np.nan),
        ),
        "hand": _rewrite_raster(
            terrain.hand,
            tmp_path / "hand-with-nan.tif",
            lambda values: values.__setitem__(tuple(mini_one[1]), np.nan),
        ),
        "ltnd": _rewrite_raster(
            terrain.ltnd,
            tmp_path / "ltnd-with-mask.tif",
            lambda values: None,
            lambda mask: mask.__setitem__(tuple(mini_one[2]), 0),
        ),
        "hru": _rewrite_raster(
            prepared.rasters["hru"],
            tmp_path / "hru-with-mask.tif",
            lambda values: None,
            lambda mask: (
                mask.__setitem__(tuple(mini_one[3]), 0),
                mask.__setitem__(tuple(mini_two[0]), 0),
            ),
        ),
        "drainage": _rewrite_raster(
            prepared.drainage,
            tmp_path / "drainage-with-mask.tif",
            lambda values: None,
            lambda mask: mask.__setitem__(tuple(mini_one[4]), 0),
        ),
    }
    spec = replace(
        _sampling_spec(minis, prepared, terrain, tmp_path / "sampled", workers=2),
        **nodata_paths,
    )
    with pytest.warns(RuntimeWarning) as caught:
        report = sample_minibasins(spec)

    messages = [str(value.message) for value in caught]
    assert report.execution.task_count > 1
    assert messages == [
        (
            "Nodata cells were found within the domain for raster(s): "
            "--dem, --drainage, --hand, --hru, --ltnd. Statistics exclude these cells; "
            "substantial missing coverage can produce unrealistic results. Please verify "
            "whether the affected results are suitable."
        )
    ]
    assert {path.name for path in report.nodata_reports} == {
        f"nodata_{name}.csv" for name in nodata_paths
    }
    for name in nodata_paths:
        frame = pd.read_csv(report.output_dir / f"nodata_{name}.csv")
        assert frame.columns.tolist() == [
            "mini_id",
            "nodata_cells",
            "total_cells",
            "percentage_nodata",
        ]
        expected_minis = [1, 2] if name == "hru" else [1]
        assert frame["mini_id"].tolist() == expected_minis
        assert frame["nodata_cells"].tolist() == [1] * len(expected_minis)
        assert frame["total_cells"].tolist() == [12] * len(expected_minis)
        np.testing.assert_allclose(frame["percentage_nodata"], 100 / 12)
    frame = pd.read_csv(report.sampled_minis)
    np.testing.assert_allclose(frame.filter(regex=r"^hru_").sum(axis=1), 100)
    with (
        rasterio.open(spec.hand) as hand_source,
        rasterio.open(spec.ltnd) as ltnd_source,
    ):
        hand = hand_source.read(1, masked=True)
        ltnd = ltnd_source.read(1, masked=True)
        valid_hand = (
            (labels == 1)
            & ~np.ma.getmaskarray(hand)
            & ~np.isnan(hand.data)
        )
        expected_areas = [
            frame.loc[0, "unit_area"]
            * np.count_nonzero(valid_hand & (hand.data <= stage))
            / np.count_nonzero(valid_hand)
            for stage in range(1, 101)
        ]
        paired = (
            (labels == 1)
            & ~np.ma.getmaskarray(hand)
            & ~np.ma.getmaskarray(ltnd)
            & ~np.isnan(hand.data)
            & ~np.isnan(ltnd.data)
        )
        maximum = float(ltnd.data[paired].max())
        expected_slope = hand.data[paired & np.isclose(ltnd.data, maximum)].mean() / (
            maximum / 1000
        )
    assert frame.loc[0, "tributary_length"] == pytest.approx(maximum / 1000)
    assert frame.loc[0, "tributary_slope"] == pytest.approx(expected_slope)
    np.testing.assert_allclose(
        frame.loc[0, [f"flooded_area_{stage}" for stage in range(1, 101)]],
        expected_areas,
    )


@pytest.mark.parametrize(
    ("dataset", "statistic"),
    (
        ("hru", "HRU percentages"),
        ("hand", "flooded area"),
        ("dem", "DEM reach statistics"),
        ("ltnd", "paired HAND/LTND statistics"),
    ),
)
def test_sampling_fails_when_required_statistic_has_only_nodata(
    tmp_path, dataset, statistic
):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    with rasterio.open(prepared.mini_ownership) as source:
        labels = source.read(1)
    missing = labels == 1
    path = {
        "hru": prepared.rasters["hru"],
        "hand": terrain.hand,
        "dem": prepared.dem,
        "ltnd": terrain.ltnd,
    }[dataset]
    nodata_path = _rewrite_raster(
        path,
        tmp_path / f"{dataset}-all-nodata.tif",
        lambda values: values.__setitem__(missing, np.nan)
        if dataset == "hand"
        else None,
        None
        if dataset == "hand"
        else lambda mask: mask.__setitem__(missing, 0),
    )
    spec = replace(
        _sampling_spec(minis, prepared, terrain, tmp_path / "sampled"),
        **{dataset: nodata_path},
    )
    spec.output_dir.mkdir()
    previous_sample = spec.output_dir / "sampled_minis.csv"
    previous_sample.write_text("previous successful sample\n")
    with (
        pytest.warns(RuntimeWarning, match="Nodata cells were found"),
        pytest.raises(
            MiniSamplingError,
            match=f"Mini 1.*{statistic}.*Nodata reports saved in",
        ),
    ):
        sample_minibasins(spec)
    assert (spec.output_dir / f"nodata_{dataset}.csv").is_file()
    assert previous_sample.read_text() == "previous successful sample\n"
    assert not (spec.output_dir / "manifest-sample-minis.json").exists()


def test_sampling_rejects_infinite_values_within_the_mini_domain(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path)
    with rasterio.open(prepared.mini_ownership) as source:
        ownership = source.read(1)
    cell = tuple(np.argwhere(ownership == 1)[0])
    dem_path = _rewrite_raster(
        prepared.dem,
        tmp_path / "dem-with-infinity.tif",
        lambda values: values.__setitem__(cell, np.inf),
    )
    spec = replace(
        _sampling_spec(minis, prepared, terrain, tmp_path / "sampled"),
        dem=dem_path,
    )
    with pytest.raises(WorkerExecutionError, match="infinite DEM values"):
        sample_minibasins(spec)
    assert not spec.output_dir.exists()


def test_sampling_serial_and_parallel_runs_are_byte_deterministic(tmp_path, monkeypatch):
    from mgb_vec_hydro.execution.memory import MemorySizing

    monkeypatch.setattr(MemorySizing, "packet_bytes", property(lambda self: 1))
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
    with pytest.raises(rasterio.errors.RasterioIOError, match="missing.tif"):
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


def test_sampling_preserves_mini_ids_above_nine_through_the_pipeline(tmp_path):
    minis, prepared, terrain = _sampling_inputs(tmp_path, mini_count=12, explicit_d8=True)
    report = sample_minibasins(_sampling_spec(minis, prepared, terrain, tmp_path / "sampled"))
    frame = pd.read_csv(report.sampled_minis)
    assert frame["id"].tolist() == list(range(1, 13))
    assert report.mini_count == terrain.mini_count == 12
    assert report.catchment_cells == 12 * 12
    assert report.reach_cells == 12 * 4
    with rasterio.open(prepared.dem) as dem, rasterio.open(prepared.mini_ownership) as cells:
        owners = cells.read(1)
        elevations = dem.read(1)
        for mini_id, row in zip(range(1, 13), frame.itertuples(index=False), strict=True):
            reach = (owners == mini_id) & (np.indices(owners.shape)[1] % 4 == 2)
            assert row.reach_elevation == np.median(elevations[reach])
    np.testing.assert_allclose(frame.filter(regex=r"^hru_").sum(axis=1), 100)
