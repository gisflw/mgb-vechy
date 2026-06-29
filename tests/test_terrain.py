import numpy as np
import pytest
from affine import Affine
import geopandas as gpd
import rasterio
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.exceptions import TerrainProductsError
from mgb_vec_hydro.terrain import (
    _agree_condition_dem,
    _rasterize_drainage,
    compute_flow_directions,
    compute_hand,
    compute_ltnd,
    create_terrain_products,
)


TRANSFORM = Affine(10, 0, 0, 0, -10, 0)


def _route(direction, start):
    deltas = {
        1: (-1, 0), 2: (-1, 1), 3: (0, 1), 4: (1, 1),
        5: (1, 0), 6: (1, -1), 7: (0, -1), 8: (-1, -1),
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


def test_drainage_rasterization_uses_all_touched_and_matching_catchment():
    catchments = gpd.GeoDataFrame(
        {
            "id": [1, 2],
            "geometry": [
                Polygon([(0, 0), (3, 0), (3, 3), (0, 3)]),
                Polygon([(3, 0), (4, 0), (4, 3), (3, 3)]),
            ],
        },
        crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {
            "id": [1, 2],
            "geometry": [
                LineString([(0, 0), (3, 3)]),
                LineString([(3, 0), (3, 3)]),
            ],
        },
        crs=catchments.crs,
    )
    labels = np.array([[0, 0, 0, 1]] * 3)

    drainage = _rasterize_drainage(
        catchments, segments, "id", labels.shape,
        Affine(1, 0, 0, 0, -1, 3), labels,
    )

    assert drainage[:, :3].sum() == 5
    assert drainage[:, 3].all()


def test_hand_uses_raw_dem_after_agree_controls_routing():
    raw = np.array([[0.0, 5.0, 10.0]])
    labels = np.zeros_like(raw, dtype=int)
    drainage = np.array([[False, False, True]])
    conditioned = _agree_condition_dem(
        raw, labels, drainage, sharp=20, smooth=0, buffer=0
    )

    direction, rank = compute_flow_directions(
        conditioned, labels, drainage, TRANSFORM
    )
    hand = compute_hand(raw, direction, rank)

    np.testing.assert_array_equal(direction, [[3, 3, 0]])
    np.testing.assert_array_equal(hand, [[-10, -5, 0]])


def test_terrain_products_records_custom_agree_profile(tmp_path):
    dem_path = tmp_path / "dem.tif"
    output_dir = tmp_path / "out"
    transform = Affine(10, 0, 0, 0, -10, 60)
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        width=6,
        height=6,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=transform,
        nodata=np.nan,
    ) as target:
        target.write(np.arange(36, dtype=np.float32).reshape(6, 6), 1)
    catchments = gpd.GeoDataFrame(
        {"id": [1], "geometry": [Polygon([(0, 0), (60, 0), (60, 60), (0, 60)])]},
        crs="EPSG:3857",
    )
    segments = gpd.GeoDataFrame(
        {"id": [1], "geometry": [LineString([(0, 0), (60, 60)])]},
        crs="EPSG:3857",
    )

    report = create_terrain_products(
        dem_path,
        catchments,
        segments,
        output_dir,
        agree_sharp=12,
        agree_smooth=3,
        agree_buffer=2,
    )

    assert report.conditioning_seconds >= 0
    with rasterio.open(report.paths.hand) as result:
        tags = result.tags()
        hand = result.read(1)
    assert tags["agree_sharp"] == "12"
    assert tags["agree_smooth"] == "3"
    assert tags["agree_buffer_pixels"] == "2"
    assert np.count_nonzero(hand == 0) >= report.drainage_cells


def test_longer_valley_route_wins_over_short_ridge_breach():
    elevation = np.array(
        [
            [20,  6, 10, 20, 20],
            [ 4, 20, 20, 20, 20],
            [20,  2,  0, 20, 20],
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

    direction, rank = compute_flow_directions(
        elevation, labels, drainage, TRANSFORM
    )

    np.testing.assert_array_equal(direction[:, 1], [-1, -1])
    np.testing.assert_array_equal(rank[:, 1], [-1, -1])

    cyclic = np.array([[3, 7]], dtype=np.int8)
    with pytest.raises(TerrainProductsError, match="cycle"):
        compute_hand(np.array([[1.0, 1.0]]), cyclic)
