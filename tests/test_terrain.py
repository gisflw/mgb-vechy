import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from affine import Affine
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import (
    TerrainProductsError,
    WorkerExecutionError,
    WorkMemoryError,
)
from mgb_vec_hydro.execution.raster import RasterAssembler
from mgb_vec_hydro.preparation import PreparationSpec, prepare_dataset
from mgb_vec_hydro.terrain import (
    TerrainDataset,
    TerrainSpec,
    _agree_condition_dem,
    _validated_d8,
    compute_flow_directions,
    compute_hand,
    compute_ltnd,
    create_terrain_dataset,
)

TRANSFORM = Affine(10, 0, 0, 0, -10, 0)


def _route(direction, start):
    deltas = {
        1: (-1, 0),
        2: (-1, 1),
        3: (0, 1),
        4: (1, 1),
        5: (1, 0),
        6: (1, -1),
        7: (0, -1),
        8: (-1, -1),
    }
    route = [start]
    while direction[route[-1]] != 0:
        row, col = route[-1]
        dr, dc = deltas[int(direction[row, col])]
        route.append((row + dr, col + dc))
    return route


def test_agree_profile_uses_pixel_distance_and_preserves_input():
    elevation = np.full((1, 7), 100.0)
    original = elevation.copy()
    labels = np.zeros_like(elevation, dtype=int)
    drainage = np.zeros_like(elevation, dtype=bool)
    drainage[0, 3] = True

    conditioned = _agree_condition_dem(
        elevation, labels, drainage, sharp=10, smooth=2, buffer=2
    )

    np.testing.assert_array_equal(elevation, original)
    np.testing.assert_allclose(conditioned, [[100, 100, 98, 86, 98, 100, 100]])
    assert conditioned[0, 2] == 98
    assert conditioned[0, 4] == 98


def test_agree_is_catchment_confined_and_preserves_nodata():
    elevation = np.array([[100.0, 100.0, 100.0, np.nan]])
    labels = np.array([[0, 0, 1, 1]])
    drainage = np.array([[True, False, False, False]])

    conditioned = _agree_condition_dem(
        elevation, labels, drainage, sharp=10, smooth=2, buffer=3
    )

    np.testing.assert_allclose(conditioned[0, :3], [84, 96, 100])
    assert np.isnan(conditioned[0, 3])


def test_zero_agree_buffer_applies_only_stream_incision():
    elevation = np.full((1, 3), 10.0)
    labels = np.zeros_like(elevation, dtype=int)
    drainage = np.array([[False, True, False]])

    conditioned = _agree_condition_dem(
        elevation, labels, drainage, sharp=4, smooth=9, buffer=0
    )

    np.testing.assert_array_equal(conditioned, [[10, 6, 10]])


@pytest.mark.parametrize(
    ("sharp", "smooth", "buffer", "message"),
    [(-1, 8, 4, "sharp"), (80, -1, 4, "smooth"), (80, 8, -1, "buffer")],
)
def test_agree_rejects_negative_parameters(sharp, smooth, buffer, message):
    with pytest.raises(TerrainProductsError, match=message):
        _agree_condition_dem(
            np.array([[1.0]]),
            np.array([[0]]),
            np.array([[True]]),
            sharp=sharp,
            smooth=smooth,
            buffer=buffer,
        )


def test_hand_uses_raw_dem_after_agree_controls_routing():
    raw = np.array([[0.0, 5.0, 10.0]])
    labels = np.zeros_like(raw, dtype=int)
    drainage = np.array([[False, False, True]])
    conditioned = _agree_condition_dem(
        raw, labels, drainage, sharp=20, smooth=0, buffer=0
    )

    direction, rank = compute_flow_directions(conditioned, labels, drainage, TRANSFORM)
    hand = compute_hand(raw, direction, rank)

    np.testing.assert_array_equal(direction, [[3, 3, 0]])
    np.testing.assert_array_equal(hand, [[-10, -5, 0]])


def _terrain_inputs(tmp_path, *, with_d8=False):
    dem_path = tmp_path / "dem.tif"
    transform = Affine(10, 0, 0, 0, -10, 40)
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        width=8,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=transform,
    ) as target:
        target.write(np.arange(32, dtype=np.float32).reshape(4, 8), 1)
        target.write_mask(np.full((4, 8), 255, dtype="uint8"))
    d8_path = None
    if with_d8:
        d8_path = tmp_path / "d8.tif"
        d8 = np.zeros((4, 8), dtype="uint8")
        d8[:, 0:2] = 3
        d8[:, 4:6] = 3
        with rasterio.open(
            d8_path,
            "w",
            driver="GTiff",
            width=8,
            height=4,
            count=1,
            dtype="uint8",
            crs="EPSG:3857",
            transform=transform,
        ) as target:
            target.write(d8, 1)
            target.write_mask(np.full((4, 8), 255, dtype="uint8"))
    prepared = tmp_path / "prepared"
    prepare_dataset(
        PreparationSpec(
            dem=dem_path,
            crs="EPSG:3857",
            resolution=10,
            output_dir=prepared,
            d8=d8_path,
            d8_encoding="canonical" if with_d8 else None,
        )
    )
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
    catchments = gpd.GeoDataFrame(
        values,
        geometry=[
            Polygon([(0, 0), (30, 0), (30, 40), (0, 40)]),
            Polygon([(40, 0), (70, 0), (70, 40), (40, 40)]),
        ],
        crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        values,
        geometry=[
            LineString([(25, 0), (25, 40)]),
            LineString([(65, 0), (65, 40)]),
        ],
        crs="EPSG:3857",
    )
    minis = tmp_path / "minis"
    minis.mkdir()
    catchments.to_file(minis / "mini_catchments.fgb", driver="FlatGeobuf", index=False)
    segments.to_file(minis / "mini_segments.fgb", driver="FlatGeobuf", index=False)
    # Stage 4 must not inspect this user-facing provenance file.
    (minis / "source_to_mini.csv").write_text("deliberately,invalid\n")
    return prepared, minis


def test_terrain_dataset_records_custom_agree_profile_and_strict_domain(tmp_path):
    prepared, minis = _terrain_inputs(tmp_path)
    output_dir = tmp_path / "out"
    checkpoint_dir = tmp_path / "checkpoints"

    report = create_terrain_dataset(
        TerrainSpec(
            prepared=prepared,
            minis=minis,
            output_dir=output_dir,
            agree_sharp=12,
            agree_smooth=3,
            agree_buffer=2,
            workers=1,
            checkpoint_dir=checkpoint_dir,
        )
    )

    TerrainDataset.open(output_dir).validate()
    assert report.mini_count == 2
    assert report.timings["conditioning"] >= 0
    with rasterio.open(output_dir / "rasters" / "hand.tif") as result:
        tags = result.tags()
        mask = result.dataset_mask()
    assert tags["agree_sharp"] == "12"
    assert tags["agree_smooth"] == "3"
    assert tags["agree_buffer_pixels"] == "2"
    assert np.all(mask[:, 3] == 0)
    assert np.all(mask[:, 7] == 0)
    with (
        rasterio.open(output_dir / "rasters" / "mini_ownership.tif") as ownership,
        rasterio.open(output_dir / "rasters" / "drainage.tif") as drainage,
    ):
        np.testing.assert_array_equal(ownership.dataset_mask(), drainage.dataset_mask())
        assert np.all(drainage.read(1)[ownership.dataset_mask() == 0] == 0)
    index = pd.read_parquet(output_dir / "mini_index.parquet")
    assert index.to_dict("list") == {"mini_label": [1, 2], "mini_id": ["a", "b"]}
    assert not checkpoint_dir.exists()

    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] += 1
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(TerrainProductsError, match="contract or version"):
        TerrainDataset.open(output_dir).validate()


def test_d8_validation_terminalizes_drainage_and_rejects_invalid_paths():
    owned = np.ones((1, 3), dtype=bool)
    drainage = np.array([[False, False, True]])
    values = np.ma.array([[3, 3, 7]], mask=False, dtype="uint8")

    direction, rank = _validated_d8(values, owned, drainage, "mini")

    np.testing.assert_array_equal(direction, [[3, 3, 0]])
    np.testing.assert_array_equal(rank, [[2, 1, 0]])
    with pytest.raises(TerrainProductsError, match="non-drainage terminals"):
        _validated_d8(
            np.ma.array([[0, 3, 0]], mask=False, dtype="uint8"),
            owned,
            drainage,
            "mini",
        )
    with pytest.raises(TerrainProductsError, match="outside"):
        _validated_d8(
            np.ma.array([[7, 3, 0]], mask=False, dtype="uint8"),
            owned,
            drainage,
            "mini",
        )
    with pytest.raises(TerrainProductsError, match="cycle"):
        _validated_d8(
            np.ma.array([[3, 7, 0]], mask=False, dtype="uint8"),
            owned,
            drainage,
            "mini",
        )
    with pytest.raises(TerrainProductsError, match="nodata"):
        _validated_d8(
            np.ma.array([[3, 3, 0]], mask=[[True, False, False]], dtype="uint8"),
            owned,
            drainage,
            "mini",
        )


def test_terrain_rejects_missing_d8_and_oversized_complete_mini(tmp_path):
    prepared, minis = _terrain_inputs(tmp_path)
    missing_output = tmp_path / "missing-d8"

    with pytest.raises(TerrainProductsError, match="no D8"):
        create_terrain_dataset(
            TerrainSpec(
                prepared=prepared,
                minis=minis,
                output_dir=missing_output,
                direction_source="d8",
            )
        )
    assert not missing_output.exists()

    oversized_output = tmp_path / "oversized"
    with pytest.raises(WorkMemoryError, match="packet budget"):
        create_terrain_dataset(
            TerrainSpec(
                prepared=prepared,
                minis=minis,
                output_dir=oversized_output,
                memory_limit_mb=1,
            )
        )
    assert not oversized_output.exists()


def test_prepared_d8_uses_same_dataset_interface_and_is_deterministic(tmp_path):
    prepared, minis = _terrain_inputs(tmp_path, with_d8=True)
    serial = tmp_path / "serial"
    parallel = tmp_path / "parallel"

    create_terrain_dataset(
        TerrainSpec(
            prepared=prepared,
            minis=minis,
            output_dir=serial,
            direction_source="d8",
            write_flow_direction=True,
            workers=1,
        )
    )
    create_terrain_dataset(
        TerrainSpec(
            prepared=prepared,
            minis=minis,
            output_dir=parallel,
            direction_source="d8",
            write_flow_direction=True,
            workers=2,
        )
    )

    for name in (
        "mini_ownership",
        "drainage",
        "hand",
        "ltnd",
        "flow_direction",
    ):
        with (
            rasterio.open(serial / "rasters" / f"{name}.tif") as first,
            rasterio.open(parallel / "rasters" / f"{name}.tif") as second,
        ):
            np.testing.assert_array_equal(first.read(1), second.read(1))
            np.testing.assert_array_equal(first.dataset_mask(), second.dataset_mask())
    with rasterio.open(serial / "rasters" / "flow_direction.tif") as source:
        direction = source.read(1, masked=True)
    assert set(np.unique(direction.compressed())) <= set(range(9))


def test_domain_and_terrain_checkpoints_resume_after_failed_publication(
    tmp_path, monkeypatch
):
    prepared, minis = _terrain_inputs(tmp_path)
    output = tmp_path / "terrain"
    checkpoint = tmp_path / "checkpoint"
    finish = RasterAssembler.finish

    finish_calls = 0

    def fail_second_finish(assembler):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 2:
            raise RuntimeError("injected failure")
        return finish(assembler)

    with monkeypatch.context() as context:
        context.setattr(RasterAssembler, "finish", fail_second_finish)
        with pytest.raises(RuntimeError, match="injected failure"):
            create_terrain_dataset(
                TerrainSpec(
                    prepared=prepared,
                    minis=minis,
                    output_dir=output,
                    checkpoint_dir=checkpoint,
                    workers=1,
                )
            )

    assert not output.exists()
    assert (checkpoint / "domain" / "checkpoint.json").is_file()
    assert (checkpoint / "terrain" / "checkpoint.json").is_file()
    assert RasterAssembler.finish is finish

    report = create_terrain_dataset(
        TerrainSpec(
            prepared=prepared,
            minis=minis,
            output_dir=output,
            checkpoint_dir=checkpoint,
            workers=1,
        )
    )

    assert report.domain_execution.resumed > 0
    assert report.terrain_execution.resumed > 0
    assert not checkpoint.exists()


def test_domain_worker_failure_publishes_nothing(tmp_path):
    prepared, minis = _terrain_inputs(tmp_path)
    segment_path = minis / "mini_segments.fgb"
    segments = gpd.read_file(segment_path)
    segments.loc[segments["id"] == "a", "geometry"] = LineString([(70, 0), (70, 40)])
    segment_path.unlink()
    segments.to_file(segment_path, driver="FlatGeobuf", index=False)
    output = tmp_path / "terrain"

    with pytest.raises(WorkerExecutionError, match="no matching drainage cells"):
        create_terrain_dataset(
            TerrainSpec(
                prepared=prepared,
                minis=minis,
                output_dir=output,
                workers=1,
            )
        )

    assert not output.exists()


def test_longer_valley_route_wins_over_short_ridge_breach():
    elevation = np.array(
        [
            [20, 6, 10, 20, 20],
            [4, 20, 20, 20, 20],
            [20, 2, 0, 20, 20],
            [20, 20, 20, 20, 20],
            [20, 20, 20, 20, 20],
        ],
        dtype=float,
    )
    labels = np.zeros_like(elevation, dtype=int)
    drainage = np.zeros_like(elevation, dtype=bool)
    drainage[2, 2] = True

    direction, _ = compute_flow_directions(elevation, labels, drainage, TRANSFORM)
    route = _route(direction, (0, 2))

    assert len(route) > 3
    assert route == [(0, 2), (0, 1), (1, 0), (2, 1), (2, 2)]


def test_global_geometry_cannot_override_valid_steepest_downhill_route():
    elevation = np.array(
        [
            [10, 9, 0],
            [8, 7, 6],
        ],
        dtype=float,
    )
    labels = np.zeros_like(elevation, dtype=int)
    drainage = np.zeros_like(elevation, dtype=bool)
    drainage[0, 2] = True

    direction, _ = compute_flow_directions(elevation, labels, drainage, TRANSFORM)

    # SE is steeper than E after metric D8 distance is accounted for.
    assert direction[0, 0] == 4
    assert _route(direction, (0, 0)) == [(0, 0), (1, 1), (0, 2)]


def test_drainable_flat_reaches_its_lowest_natural_outlet():
    elevation = np.array(
        [
            [5, 5, 5, 4],
            [5, 5, 5, 2],
        ],
        dtype=float,
    )
    labels = np.zeros_like(elevation, dtype=int)
    drainage = np.zeros_like(elevation, dtype=bool)
    drainage[1, 3] = True

    direction, _ = compute_flow_directions(elevation, labels, drainage, TRANSFORM)

    for cell in ((0, 0), (0, 1), (1, 0), (1, 1)):
        assert _route(direction, cell)[-1] == (1, 3)


def test_natural_outlet_partition_does_not_leave_flat_cell_unrouted():
    elevation = np.full((3, 4), np.nan)
    labels = np.full((3, 4), -1, dtype=int)
    for cell, value in {
        (0, 0): 5,  # unresolved flat cell
        (1, 1): 5,  # natural outlet toward elevation 4
        (2, 2): 5,  # globally lowest natural outlet
        (0, 2): 4,
        (2, 3): 2,
    }.items():
        elevation[cell] = value
        labels[cell] = 0
    drainage = np.zeros_like(elevation, dtype=bool)
    drainage[2, 3] = True

    direction, rank = compute_flow_directions(elevation, labels, drainage, TRANSFORM)

    assert direction[0, 0] >= 0
    assert rank[0, 0] >= 0
    assert _route(direction, (0, 0))[-1] == (2, 3)


def test_natural_slope_takes_direct_downhill_route():
    elevation = np.array([[3, 2, 1, 0]], dtype=float)
    labels = np.zeros_like(elevation, dtype=int)
    drainage = np.array([[False, False, False, True]])

    direction, rank = compute_flow_directions(elevation, labels, drainage, TRANSFORM)

    np.testing.assert_array_equal(direction, [[3, 3, 3, 0]])
    np.testing.assert_array_equal(rank, [[3, 2, 1, 0]])


def test_multiple_streams_are_deterministic_and_respect_owners():
    elevation = np.full((3, 5), 10.0)
    labels = np.array([[0, 0, 0, 1, 1]] * 3)
    drainage = np.zeros_like(elevation, dtype=bool)
    drainage[1, 0] = True
    drainage[1, 2] = True
    drainage[1, 4] = True

    first, _ = compute_flow_directions(elevation, labels, drainage, TRANSFORM)
    second, _ = compute_flow_directions(elevation, labels, drainage, TRANSFORM)

    np.testing.assert_array_equal(first, second)
    for start in np.argwhere(first > 0):
        route = _route(first, tuple(start))
        assert all(labels[cell] == labels[tuple(start)] for cell in route)


def test_hand_and_ltnd_follow_selected_tree_with_rectangular_pixels():
    elevation = np.array([[3, 2], [4, 1]], dtype=float)
    labels = np.zeros_like(elevation, dtype=int)
    drainage = np.array([[False, False], [False, True]])
    transform = Affine(3, 0, 0, 0, -4, 0)

    direction, rank = compute_flow_directions(elevation, labels, drainage, transform)
    hand = compute_hand(elevation, direction, rank)
    ltnd = compute_ltnd(direction, transform, rank)

    np.testing.assert_array_equal(hand, elevation - 1)
    assert ltnd[0, 0] == pytest.approx(5)
    assert ltnd[0, 1] == pytest.approx(4)
    assert ltnd[1, 0] == pytest.approx(3)


def test_disconnected_owned_component_raises_clear_error():
    elevation = np.array([[1, np.nan, 1]], dtype=float)
    labels = np.zeros_like(elevation, dtype=int)
    drainage = np.array([[True, False, False]])

    with pytest.raises(TerrainProductsError, match="cannot connect"):
        compute_flow_directions(elevation, labels, drainage, TRANSFORM)


def test_nodata_is_preserved_and_cycle_validation_still_applies():
    elevation = np.array([[2, np.nan, 1], [3, np.nan, 0]], dtype=float)
    labels = np.array([[0, -1, 1], [0, -1, 1]])
    drainage = np.array([[True, False, False], [False, False, True]])

    direction, rank = compute_flow_directions(elevation, labels, drainage, TRANSFORM)

    np.testing.assert_array_equal(direction[:, 1], [-1, -1])
    np.testing.assert_array_equal(rank[:, 1], [-1, -1])

    cyclic = np.array([[3, 7]], dtype=np.int8)
    with pytest.raises(TerrainProductsError, match="cycle"):
        compute_hand(np.array([[1.0, 1.0]]), cyclic)
