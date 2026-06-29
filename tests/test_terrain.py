import numpy as np
import pytest
from affine import Affine

from mgb_vec_hydro.exceptions import TerrainProductsError
from mgb_vec_hydro.terrain import (
    compute_flow_directions,
    compute_hand,
    compute_ltnd,
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
