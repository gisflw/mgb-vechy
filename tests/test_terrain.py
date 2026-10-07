import json

import numpy as np
import pytest
import rasterio
from pyproj import CRS, Transformer
from affine import Affine
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import RasterGridError, TerrainProductsError
from mgb_vec_hydro.execution.vector import (
    VectorTable,
    write_vector_table,
)
from mgb_vec_hydro.preparation import PreparationSpec, prepare_dataset, read_mini_index
from mgb_vec_hydro.terrain import (
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
    # Stage 4 must not inspect this user-facing provenance file.
    (minis / "source_to_mini.csv").write_text("deliberately,invalid\n")
    prepared = tmp_path / "prepared"
    preparation = prepare_dataset(
        PreparationSpec(
            dem=dem_path,
            mini_catchments=minis / "mini_catchments.fgb",
            mini_segments=minis / "mini_segments.fgb",
            output_dir=prepared,
            d8=d8_path,
            d8_encoding="canonical" if with_d8 else None,
        )
    )
    return preparation, minis


def test_terrain_outputs_custom_agree_profile_and_strict_domain(tmp_path):
    prepared, _minis = _terrain_inputs(tmp_path)
    output_dir = tmp_path / "out"

    report = create_terrain_dataset(
        TerrainSpec(
            dem=prepared.dem,
            mini_ownership=prepared.mini_ownership,
            drainage=prepared.drainage,
            output_dir=output_dir,
            agree_sharp=12,
            agree_smooth=3,
            agree_buffer=2,
            workers=1,

        )
    )
    phase_times = [report.timings[f"{phase}_wall"] for phase in ("preparing", "processing", "finalizing")]
    assert all(seconds >= 0 for seconds in phase_times)
    assert sum(phase_times) == pytest.approx(report.timings["total"])

    assert report.mini_count == 2
    assert report.timings["conditioning"] >= 0
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "hand.tif", "ltnd.tif", "manifest-terrain-products.json"
    ]
    manifest = json.loads((output_dir / "manifest-terrain-products.json").read_text())
    assert manifest["parameters"]["agree_sharp"] == 12
    assert not any(path.is_dir() for path in output_dir.iterdir())
    with rasterio.open(output_dir / "hand.tif") as result:
        tags = result.tags()
        mask = result.dataset_mask()
    assert tags["agree_sharp"] == "12"
    assert tags["agree_smooth"] == "3"
    assert tags["agree_buffer_pixels"] == "2"
    assert np.all(mask[:, 3] == 0)
    with (
        rasterio.open(prepared.mini_ownership) as ownership,
        rasterio.open(prepared.drainage) as drainage,
    ):
        np.testing.assert_array_equal(ownership.dataset_mask(), drainage.dataset_mask())
        assert np.all(drainage.read(1)[ownership.dataset_mask() == 0] == 0)
    assert [row[0] for row in read_mini_index(prepared.mini_ownership)] == [1, 2]


def test_terrain_validates_direct_grid_inputs(tmp_path):
    prepared, _minis = _terrain_inputs(tmp_path)
    mismatched = tmp_path / "mismatched-ownership.tif"
    with rasterio.open(prepared.mini_ownership) as source:
        profile = source.profile.copy()
        data = source.read(1)
        mask = source.dataset_mask()
    profile.update(driver="COG", crs="EPSG:4326")
    with rasterio.open(mismatched, "w", **profile) as target:
        target.write(data, 1)
        target.write_mask(mask)

    grid_output = tmp_path / "grid-error"
    with pytest.raises(RasterGridError, match="canonical grid"):
        create_terrain_dataset(
            TerrainSpec(
                dem=prepared.dem,
                mini_ownership=mismatched,
                drainage=prepared.drainage,
                output_dir=grid_output,
                workers=1,
            )
        )
    assert not grid_output.exists()


def test_terrain_d8_mode_consumes_explicit_d8_and_publishes_only_products(tmp_path):
    prepared, _minis = _terrain_inputs(tmp_path, with_d8=True)
    output = tmp_path / "d8-terrain"
    report = create_terrain_dataset(
        TerrainSpec(
            dem=prepared.dem,
            mini_ownership=prepared.mini_ownership,
            drainage=prepared.drainage,
            d8=prepared.d8,
            direction_source="d8",
            write_flow_direction=True,
            output_dir=output,
            workers=1,
        )
    )
    assert report.flow_direction == output / "flow_direction.tif"
    assert sorted(path.name for path in output.iterdir()) == [
        "flow_direction.tif",
        "hand.tif",
        "ltnd.tif",
        "manifest-terrain-products.json",
    ]
    manifest = json.loads((output / "manifest-terrain-products.json").read_text())
    assert manifest["parameters"]["agree_buffer"] == 4
    assert manifest["parameters"]["direction_source"] == "d8"
    assert not (output / "mini_index.csv").exists()

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
    with pytest.raises(ValueError, match="outside"):
        _validated_d8(
            np.ma.array([[7, 3, 0]], mask=False, dtype="uint8"),
            owned,
            drainage,
            "mini",
        )
    with pytest.raises(ValueError, match="cycle"):
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
    ltnd = compute_ltnd(direction, transform, rank, crs="EPSG:3857")

    np.testing.assert_array_equal(hand, elevation - 1)
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    geod = CRS.from_epsg(3857).get_geod()
    terminal = transformer.transform(*rasterio.transform.xy(transform, 1, 1))
    for row, col in ((0, 0), (0, 1), (1, 0)):
        start = transformer.transform(*rasterio.transform.xy(transform, row, col))
        assert ltnd[row, col] == pytest.approx(geod.inv(*start, *terminal)[2])


@pytest.mark.parametrize(
    "crs,transform",
    [
        ("EPSG:4326", Affine(0.001, 0, -45, 0, -0.002, -13)),
        ("EPSG:4326", Affine(0.001, 0, 10, 0, -0.002, 60)),
        ("EPSG:4807", Affine(0.001, 0, 0, 0, -0.002, 54)),
        ("EPSG:3857", Affine(100, 0, 1000, 0, -200, 8399737)),
        ("EPSG:3395", Affine(100, 0, 1000, 0, -200, 8399737)),
        ("EPSG:6933", Affine(100, 0, 1000, 0, -200, 6000000)),
        ("EPSG:2263", Affine(100, 0, 980000, 0, -200, 200000)),
    ],
)
@pytest.mark.parametrize(
    "direction",
    [
        np.array([[4, 5, 6], [3, 0, 7], [2, 1, 8]], dtype="int8"),
        np.array([[4, 5, 6], [3, 4, 5], [2, 3, 0]], dtype="int8"),
    ],
)
def test_ltnd_with_crs_matches_geodesic_route_sums(crs, transform, direction):
    source = CRS.from_user_input(crs)
    # Geod takes degrees, while EPSG:4807 stores grads. This independent
    # reference converts the geodetic coordinates using their declared units.
    transformer = Transformer.from_crs(source, source.geodetic_crs, always_xy=True)
    angular_factor = source.geodetic_crs.axis_info[0].unit_conversion_factor * 180 / np.pi
    geod = source.get_geod()
    expected = np.zeros(direction.shape)
    for start in np.ndindex(direction.shape):
        route = _route(direction, start)
        for cell, parent in zip(route, route[1:]):
            x, y = rasterio.transform.xy(transform, *cell)
            px, py = rasterio.transform.xy(transform, *parent)
            lon, lat = transformer.transform(x, y)
            plon, plat = transformer.transform(px, py)
            lon, lat, plon, plat = (v * angular_factor for v in (lon, lat, plon, plat))
            expected[start] += geod.inv(lon, lat, plon, plat)[2]
    actual = compute_ltnd(direction, transform, crs=source)
    np.testing.assert_allclose(actual, expected, rtol=1e-10, atol=1e-7)


def test_agree_defaults_are_unchanged(tmp_path):
    spec = TerrainSpec(
        dem=tmp_path / "dem", mini_ownership=tmp_path / "ownership",
        drainage=tmp_path / "drainage",
        output_dir=tmp_path / "output",
    )
    assert (spec.agree_sharp, spec.agree_smooth, spec.agree_buffer) == (80.0, 8.0, 4)


@pytest.mark.parametrize("metadata,error", [(None, KeyError), ("broken", json.JSONDecodeError)])
def test_terrain_missing_or_malformed_index_fails_natively(tmp_path, metadata, error):
    from rasterio.shutil import copy as copy_raster

    prepared, _ = _terrain_inputs(tmp_path)
    working = tmp_path / "working.tif"
    cells = tmp_path / "cells.tif"
    with rasterio.open(prepared.mini_ownership) as source:
        profile = source.profile.copy()
        profile.update(driver="GTiff")
        with rasterio.open(working, "w", **profile) as target:
            target.write(source.read(1), 1)
            target.write_mask(source.dataset_mask())
            if metadata is not None:
                target.update_tags(mini_index=metadata)
    copy_raster(working, cells, driver="COG")
    output = tmp_path / "terrain"
    with pytest.raises(error):
        create_terrain_dataset(TerrainSpec(
            dem=prepared.dem, mini_ownership=cells, drainage=prepared.drainage,
            output_dir=output, workers=1,
        ))
    assert not output.exists()
