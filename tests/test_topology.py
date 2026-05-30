import pandas as pd
import pytest

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    MissingColumnsError,
    OutletNotFoundError,
    TopologyCycleError,
)
from mgb_vec_hydro.topology import (
    find_upstream_segments,
)


def test_find_upstream_segments_with_generic_columns():
    segments = pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "id_down": [None, 1, 2, 2, 4],
        }
    )

    result = find_upstream_segments(segments, [2])

    assert result == {2, 3, 4, 5}


def test_find_upstream_segments_with_custom_columns():
    segments = pd.DataFrame(
        {
            "cotrecho": [10, 20, 30, 40],
            "nutrjus": [None, 10, 20, 20],
        }
    )

    result = find_upstream_segments(
        segments,
        [20],
        id_col="cotrecho",
        id_down_col="nutrjus",
    )

    assert result == {20, 30, 40}


def test_multiple_outlets_return_union():
    segments = pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5, 6],
            "id_down": [None, 1, 2, 1, 4, None],
        }
    )

    result = find_upstream_segments(segments, [2, 4])

    assert result == {2, 3, 4, 5}


def test_empty_string_downstream_is_sink():
    segments = pd.DataFrame(
        {
            "id": ["a", "b", "c"],
            "id_down": ["", "a", "b"],
        }
    )

    result = find_upstream_segments(segments, ["a"])

    assert result == {"a", "b", "c"}


def test_missing_columns_raise_package_error():
    segments = pd.DataFrame({"id": [1]})

    with pytest.raises(MissingColumnsError, match="id_down"):
        find_upstream_segments(segments, [1])


def test_duplicate_segment_ids_raise_package_error():
    segments = pd.DataFrame(
        {
            "id": [1, 1],
            "id_down": [None, None],
        }
    )

    with pytest.raises(DuplicateSegmentIdError, match="duplicate"):
        find_upstream_segments(segments, [1])


def test_missing_outlet_raises_package_error():
    segments = pd.DataFrame(
        {
            "id": [1],
            "id_down": [None],
        }
    )

    with pytest.raises(OutletNotFoundError, match="99"):
        find_upstream_segments(segments, [99])


def test_cycle_detection_raises_package_error():
    segments = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "id_down": [3, 1, 2],
        }
    )

    with pytest.raises(TopologyCycleError, match="cycle"):
        find_upstream_segments(segments, [1])
