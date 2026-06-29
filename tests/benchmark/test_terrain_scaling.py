"""Opt-in timing checks for the terrain-routing core.

Run synthetic scaling checks with ``RUN_TERRAIN_BENCHMARKS=1 pytest
tests/benchmark``.  Set ``BHAE_ROUTING_INPUT`` to an ``.npz`` containing
``elevation``, ``labels``, ``drainage``, and six Affine ``transform`` values to
exercise the full BHAE routing raster without making its one-minute target a
release gate.
"""

import os
from pathlib import Path
import time

from affine import Affine
import numpy as np
import pytest

from mgb_vec_hydro.terrain import compute_flow_directions, compute_hand, compute_ltnd


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TERRAIN_BENCHMARKS") != "1",
    reason="terrain scaling benchmarks are opt-in",
)


@pytest.mark.parametrize("side", [512, 1024, 2048])
def test_synthetic_routing_scaling(side, record_property):
    rows = np.arange(side, dtype=np.float64)[:, None]
    cols = np.arange(side, dtype=np.float64)[None, :]
    elevation = rows + cols
    labels = np.zeros((side, side), dtype=np.int32)
    drainage = np.zeros((side, side), dtype=bool)
    drainage[0, :] = True
    transform = Affine(90, 0, 0, 0, -90, 0)

    started = time.perf_counter()
    direction, rank = compute_flow_directions(elevation, labels, drainage, transform)
    routing_seconds = time.perf_counter() - started
    started = time.perf_counter()
    compute_hand(elevation, direction, rank)
    compute_ltnd(direction, transform, rank)
    products_seconds = time.perf_counter() - started

    record_property("cells", side * side)
    record_property("routing_seconds", routing_seconds)
    record_property("hand_ltnd_seconds", products_seconds)


def test_full_bhae_performance_target(record_property):
    source = os.environ.get("BHAE_ROUTING_INPUT")
    if not source:
        pytest.skip("BHAE_ROUTING_INPUT is not configured")
    with np.load(Path(source)) as data:
        elevation = data["elevation"]
        labels = data["labels"]
        drainage = data["drainage"]
        transform = Affine(*data["transform"])

    started = time.perf_counter()
    direction, rank = compute_flow_directions(elevation, labels, drainage, transform)
    routing_seconds = time.perf_counter() - started
    started = time.perf_counter()
    compute_hand(elevation, direction, rank)
    compute_ltnd(direction, transform, rank)
    products_seconds = time.perf_counter() - started

    record_property("cells", elevation.size)
    record_property("routing_seconds", routing_seconds)
    record_property("hand_ltnd_seconds", products_seconds)
    record_property("one_minute_target_met", routing_seconds + products_seconds <= 60)
