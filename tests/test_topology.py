import pandas as pd
import pytest

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    MissingColumnsError,
    OutletNotFoundError,
    TopologyCycleError,
)
from mgb_vec_hydro.topology import (
    find_upstream_catchments,
    find_upstream_segments,
)


def test_find_upstream_segments_with_generic_columns():
    segments = pd.DataFrame(
        {
            "seg_id": [1, 2, 3, 4, 5],
            "seg_id_down": [None, 1, 2, 2, 4],
            "catch_id": ["a", "b", "c", "d", "e"],
        }
    )

    result = find_upstream_segments(segments, [2])

    assert result == {2, 3, 4, 5}


def test_find_upstream_catchments_with_bho_columns():
    segments = pd.DataFrame(
        {
            "cotrecho": [10, 20, 30, 40],
            "nutrjus": [None, 10, 20, 20],
            "cobacia": [100, 200, 300, 400],
        }
    )

    result = find_upstream_catchments(
        segments,
        [20],
        seg_id_col="cotrecho",
        seg_id_down_col="nutrjus",
        catch_id_col="cobacia",
    )

    assert result == {200, 300, 400}


def test_multiple_outlets_return_union():
    segments = pd.DataFrame(
        {
            "seg_id": [1, 2, 3, 4, 5, 6],
            "seg_id_down": [None, 1, 2, 1, 4, None],
            "catch_id": ["a", "b", "c", "d", "e", "f"],
        }
    )

    result = find_upstream_segments(segments, [2, 4])

    assert result == {2, 3, 4, 5}


def test_empty_string_downstream_is_sink():
    segments = pd.DataFrame(
        {
            "seg_id": ["a", "b", "c"],
            "seg_id_down": ["", "a", "b"],
            "catch_id": [1, 2, 3],
        }
    )

    result = find_upstream_segments(segments, ["a"])

    assert result == {"a", "b", "c"}


def test_missing_columns_raise_package_error():
    segments = pd.DataFrame({"seg_id": [1], "catch_id": [1]})

    with pytest.raises(MissingColumnsError, match="seg_id_down"):
        find_upstream_segments(segments, [1])


def test_duplicate_segment_ids_raise_package_error():
    segments = pd.DataFrame(
        {
            "seg_id": [1, 1],
            "seg_id_down": [None, None],
            "catch_id": [10, 20],
        }
    )

    with pytest.raises(DuplicateSegmentIdError, match="duplicate"):
        find_upstream_segments(segments, [1])


def test_missing_outlet_raises_package_error():
    segments = pd.DataFrame(
        {
            "seg_id": [1],
            "seg_id_down": [None],
            "catch_id": [10],
        }
    )

    with pytest.raises(OutletNotFoundError, match="99"):
        find_upstream_segments(segments, [99])


def test_cycle_detection_raises_package_error():
    segments = pd.DataFrame(
        {
            "seg_id": [1, 2, 3],
            "seg_id_down": [3, 1, 2],
            "catch_id": [10, 20, 30],
        }
    )

    with pytest.raises(TopologyCycleError, match="cycle"):
        find_upstream_segments(segments, [1])
