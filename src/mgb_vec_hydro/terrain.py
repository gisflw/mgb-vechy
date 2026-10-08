"""AGREE-conditioned terrain-driven basin routing and raster products.

Natural D8 drainage on the conditioned DEM is retained except on flats and
targeted shallow-breach corridors that connect trapped basins to the supplied
drainage. HAND elevations continue to use the unmodified DEM.
"""

from __future__ import annotations

import csv
import heapq
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import rasterio
from affine import Affine
from numba import njit
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.windows import Window

from mgb_vec_hydro.crs_utils import (
    geodetic_tools,
    require_metre_units,
)
from mgb_vec_hydro.exceptions import TerrainProductsError
from mgb_vec_hydro.execution.executor import (
    ExecutionConfig,
    ExecutionReport,
    LocalExecutor,
    WorkerContext,
    WorkerOutput,
    WorkItem,
)
from mgb_vec_hydro.execution.manifest import write_manifest
from mgb_vec_hydro.execution.memory import MemorySizing, raster_cache
from mgb_vec_hydro.execution.progress import ProgressCallback, StageReporter
from mgb_vec_hydro.execution.publication import AtomicOutputDirectory
from mgb_vec_hydro.execution.raster import (
    AlignedRasterReader,
    RasterAssembler,
    RasterPacket,
    RasterPatch,
    RasterProductSpec,
    RasterUnit,
    _require_grid,
    grid_from_dem,
    packet_raster_units,
    plan_raster_units,
)
from mgb_vec_hydro.preparation import (
    GridSpec, read_mini_index, validate_segment_ownership,
)

# Code, row delta, column delta. This order is also the final tie-break.
_DIRECTIONS = (
    (1, -1, 0),
    (2, -1, 1),
    (3, 0, 1),
    (4, 1, 1),
    (5, 1, 0),
    (6, 1, -1),
    (7, 0, -1),
    (8, -1, -1),
)
_DELTAS = {code: (dr, dc) for code, dr, dc in _DIRECTIONS}
_OPPOSITE = {1: 5, 2: 6, 3: 7, 4: 8, 5: 1, 6: 2, 7: 3, 8: 4}
_DR = np.array([-1, -1, 0, 1, 1, 1, 0, -1], dtype=np.int8)
_DC = np.array([0, 1, 1, 1, 0, -1, -1, -1], dtype=np.int8)


DOMAIN_BYTES_PER_CELL = 16
DEM_BYTES_PER_CELL = 128
D8_BYTES_PER_CELL = 80
# Projected distances retain one float64 edge length per cell. Coordinate
# transformations are capped at 4096 edges, fitting the task's fixed allowance.
METRIC_BYTES_PER_CELL = 8
GEODESIC_BATCH_CELLS = 4096
# EPSG methods: pseudo-Mercator, Mercator A/B, cylindrical equal-area
# spherical/ellipsoidal. Their inverse longitude is linear in x and latitude
# depends only on y, so row tables are exact on a north-up raster grid.
_SEPARABLE_PROJECTION_METHODS = {"1024", "9804", "9805", "9834", "9835"}
TASK_FIXED_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class TerrainSpec:
    dem: Path
    grid_catchments: Path
    grid_segments: Path
    output_dir: Path
    d8: Path | None = None
    direction_source: Literal["dem", "d8"] = "dem"
    write_flow_direction: bool = False
    agree_sharp: float = 80.0
    agree_smooth: float = 8.0
    agree_buffer: int = 4
    workers: int = 4
    memory_limit_mb: int = 4096
    io_slots: int = 2
    overwrite: bool = False


@dataclass(frozen=True)
class TerrainReport:
    output_dir: Path
    hand: Path
    ltnd: Path
    flow_direction: Path | None
    mini_count: int
    owned_cells: int
    drainage_cells: int
    negative_hand_cells: int
    negative_hand_min: float | None
    negative_hand_max: float | None
    direction_source: str
    domain_execution: ExecutionReport
    terrain_execution: ExecutionReport
    timings: dict[str, float]
    undrained_cells_csv: Path
    undrained_cells: int


def _pixel_sizes(transform: Affine) -> tuple[float, float]:
    if not isinstance(transform, Affine):
        transform = Affine(*transform)
    if transform.b != 0 or transform.d != 0:
        raise TerrainProductsError("A north-up, unrotated raster transform is required")
    width, height = abs(transform.a), abs(transform.e)
    if width == 0 or height == 0:
        raise TerrainProductsError("DEM pixel dimensions must be non-zero")
    return width, height


def _validate_arrays(
    elevation: np.ndarray,
    labels: np.ndarray,
    drainage: np.ndarray,
) -> None:
    if elevation.ndim != 2:
        raise TerrainProductsError("Elevation must be a two-dimensional array")
    if labels.shape != elevation.shape or drainage.shape != elevation.shape:
        raise TerrainProductsError(
            "Elevation, catchment labels, and drainage mask must have equal shapes"
        )
    if not np.issubdtype(labels.dtype, np.integer):
        raise TerrainProductsError("Catchment labels must be an integer array")


def _validate_agree_parameters(
    sharp: float,
    smooth: float,
    buffer: int,
) -> None:
    if not np.isfinite(sharp) or sharp < 0:
        raise TerrainProductsError("AGREE sharp value must be finite and non-negative")
    if not np.isfinite(smooth) or smooth < 0:
        raise TerrainProductsError("AGREE smooth value must be finite and non-negative")
    if (
        isinstance(buffer, (bool, np.bool_))
        or not np.isscalar(buffer)
        or not np.isfinite(buffer)
        or int(buffer) != buffer
        or buffer < 0
    ):
        raise TerrainProductsError("AGREE buffer must be a non-negative integer")


def _agree_condition_dem(
    elevation: np.ndarray,
    catchment_labels: np.ndarray,
    drainage_mask: np.ndarray,
    *,
    sharp: float = 80.0,
    smooth: float = 8.0,
    buffer: int = 4,
) -> np.ndarray:
    """Return a catchment-confined AGREE-conditioned copy of a DEM."""

    elevation = np.asarray(elevation)
    labels = np.asarray(catchment_labels)
    drainage = np.asarray(drainage_mask, dtype=bool)
    _validate_arrays(elevation, labels, drainage)
    _validate_agree_parameters(sharp, smooth, buffer)
    valid = (labels >= 0) & np.isfinite(elevation)
    if np.any(drainage & ~valid):
        raise TerrainProductsError("Drainage cells must be finite owned cells")
    del valid
    return _agree_condition_kernel(
        elevation.astype(np.float64, copy=False),
        labels,
        drainage,
        float(sharp),
        float(smooth),
        int(buffer),
    )


@njit(cache=True)
def _agree_condition_kernel(elevation, labels, drainage, sharp, smooth, buffer):
    """Apply the AGREE profile using Euclidean distances in raster pixels."""
    rows, cols = elevation.shape
    distance = np.full((rows, cols), np.inf, np.float64)
    for r in range(rows):
        for c in range(cols):
            if not drainage[r, c]:
                continue
            owner = labels[r, c]
            for dr in range(-buffer, buffer + 1):
                nr = r + dr
                if nr < 0 or nr >= rows:
                    continue
                for dc in range(-buffer, buffer + 1):
                    nc = c + dc
                    if nc < 0 or nc >= cols:
                        continue
                    candidate = math.sqrt(dr * dr + dc * dc)
                    if (
                        candidate <= buffer
                        and labels[nr, nc] == owner
                        and np.isfinite(elevation[nr, nc])
                        and candidate < distance[nr, nc]
                    ):
                        distance[nr, nc] = candidate
    conditioned = elevation.copy()
    for r in range(rows):
        for c in range(cols):
            if np.isfinite(distance[r, c]):
                conditioned[r, c] += smooth * (distance[r, c] - buffer)
                if drainage[r, c]:
                    conditioned[r, c] -= sharp
    return conditioned


def compute_flow_directions(
    elevation: np.ndarray,
    catchment_labels: np.ndarray,
    drainage_mask: np.ndarray,
    transform: Affine,
) -> tuple[np.ndarray, np.ndarray]:
    """Return terrain-driven D8 directions with targeted shallow breaching.

    Negative catchment labels and non-finite elevations are nodata. Rank is
    ``-1`` there; drainage cells have rank and direction zero.

    Ordinary cells use their steepest metric downhill neighbour. Flats drain to
    their lowest natural outlet, while pits and closed flats form local basins.
    Trapped basins are connected on a basin adjacency graph by corridors that
    minimize maximum cut depth, cumulative excavation, and metric length.
    Returned ranks are traversal aids only; they never constrain ordinary flow.
    """

    elevation = np.asarray(elevation)
    labels = np.asarray(catchment_labels)
    drainage = np.asarray(drainage_mask, dtype=bool)
    _validate_arrays(elevation, labels, drainage)
    pixel_width, pixel_height = _pixel_sizes(transform)
    valid = (labels >= 0) & np.isfinite(elevation)
    if np.any(drainage & ~valid):
        raise TerrainProductsError("Drainage cells must be finite owned cells")
    del valid

    direction = _natural_d8_and_flats(
        elevation.astype(np.float64, copy=False),
        labels,
        drainage,
        pixel_width,
        pixel_height,
    )
    rank, terminal = _rank_and_terminal(direction)
    basin, unique_terminals = _label_basins(terminal)
    basin_count = unique_terminals.size
    stream_basin = np.zeros(basin_count, dtype=bool)
    stream_basin[np.unique(basin[drainage])] = True
    del unique_terminals

    order = _rank_order(rank)
    cut_max, cut_sum, corridor_length = _corridor_costs(
        elevation.astype(np.float64, copy=False),
        direction,
        terminal,
        order,
        pixel_width,
        pixel_height,
    )
    edge_data = _scan_basin_boundaries(
        basin,
        labels,
        cut_max,
        cut_sum,
        corridor_length,
        pixel_width,
        pixel_height,
    )
    selected = _basin_paths_to_stream(basin_count, stream_basin, edge_data)
    trapped = (~stream_basin) & (selected < 0)
    undrained = np.zeros(basin.shape, dtype=bool)
    basin_cells = basin >= 0
    undrained[basin_cells] = trapped[basin[basin_cells]]
    del basin_cells, cut_max, cut_sum, corridor_length, order, rank, stream_basin, terminal
    direction = _reverse_selected_corridors(direction, edge_data, selected)
    direction[undrained] = -1
    del undrained
    del edge_data, selected
    rank = _rank_only(direction)
    return direction, rank


@njit(cache=True)
def _natural_d8_and_flats(elevation, labels, drainage, width, height):
    """Assign strict D8 descent, then resolve equal-elevation components."""
    rows, cols = elevation.shape
    size = rows * cols
    direction = np.full((rows, cols), -1, np.int8)
    unresolved = np.zeros((rows, cols), np.uint8)
    distances = np.array(
        [
            height,
            math.hypot(width, height),
            width,
            math.hypot(width, height),
            height,
            math.hypot(width, height),
            width,
            math.hypot(width, height),
        ]
    )
    for r in range(rows):
        for c in range(cols):
            if labels[r, c] < 0 or not np.isfinite(elevation[r, c]):
                continue
            if drainage[r, c]:
                direction[r, c] = 0
                continue
            best_slope = 0.0
            best_index = size
            best_code = -1
            for k in range(8):
                nr, nc = r + _DR[k], c + _DC[k]
                if (
                    0 <= nr < rows
                    and 0 <= nc < cols
                    and labels[nr, nc] == labels[r, c]
                    and np.isfinite(elevation[nr, nc])
                    and elevation[nr, nc] < elevation[r, c]
                ):
                    slope = (elevation[r, c] - elevation[nr, nc]) / distances[k]
                    index = nr * cols + nc
                    if slope > best_slope or (
                        slope == best_slope and index < best_index
                    ):
                        best_slope, best_index, best_code = slope, index, k + 1
            if best_code > 0:
                direction[r, c] = best_code
            else:
                unresolved[r, c] = 1

    # Work arrays are reused for every flat, keeping memory linear.
    seen = np.zeros((rows, cols), np.uint8)
    in_component = np.zeros((rows, cols), np.uint8)
    queue = np.empty(size, np.int64)
    component = np.empty(size, np.int64)
    for sr in range(rows):
        for sc in range(cols):
            if unresolved[sr, sc] == 0 or seen[sr, sc] != 0:
                continue
            z = elevation[sr, sc]
            owner = labels[sr, sc]
            head, tail, count = 0, 1, 0
            queue[0] = sr * cols + sc
            seen[sr, sc] = 1
            lowest_outlet = np.inf
            while head < tail:
                index = queue[head]
                head += 1
                r, c = index // cols, index % cols
                component[count] = index
                count += 1
                in_component[r, c] = 1
                if direction[r, c] > 0:
                    k = direction[r, c] - 1
                    lowest_outlet = min(
                        lowest_outlet, elevation[r + _DR[k], c + _DC[k]]
                    )
                elif direction[r, c] == 0:
                    lowest_outlet = min(lowest_outlet, z)
                for k in range(8):
                    nr, nc = r + _DR[k], c + _DC[k]
                    if (
                        0 <= nr < rows
                        and 0 <= nc < cols
                        and seen[nr, nc] == 0
                        and labels[nr, nc] == owner
                        and np.isfinite(elevation[nr, nc])
                        and elevation[nr, nc] == z
                    ):
                        seen[nr, nc] = 1
                        queue[tail] = nr * cols + nc
                        tail += 1

            # Seed a breadth-first routing from natural outlets at the lowest
            # downslope elevation. Closed flats use their row-major first cell.
            head, tail = 0, 0
            for i in range(count):
                index = component[i]
                r, c = index // cols, index % cols
                seed = direction[r, c] == 0
                if direction[r, c] > 0:
                    k = direction[r, c] - 1
                    seed = elevation[r + _DR[k], c + _DC[k]] == lowest_outlet
                if seed:
                    queue[tail] = index
                    tail += 1
            if tail == 0:
                root = component[0]
                direction[root // cols, root % cols] = 0
                unresolved[root // cols, root % cols] = 0
                queue[0] = root
                tail = 1
            while head < tail:
                index = queue[head]
                head += 1
                r, c = index // cols, index % cols
                for k in range(8):
                    nr, nc = r + _DR[k], c + _DC[k]
                    if (
                        0 <= nr < rows
                        and 0 <= nc < cols
                        and in_component[nr, nc] != 0
                        and unresolved[nr, nc] != 0
                    ):
                        # Point toward the already routed flat cell.
                        direction[nr, nc] = ((k + 4) % 8) + 1
                        unresolved[nr, nc] = 0
                        queue[tail] = nr * cols + nc
                        tail += 1
            # Cells with their own strict downhill direction are deliberately
            # not rewritten. Such cells can partition the unresolved part of
            # a plateau, so use every natural outlet as a fallback seed for
            # any portion the globally lowest outlet could not reach.
            head, tail = 0, 0
            for i in range(count):
                index = component[i]
                r, c = index // cols, index % cols
                if direction[r, c] >= 0:
                    queue[tail] = index
                    tail += 1
            while head < tail:
                index = queue[head]
                head += 1
                r, c = index // cols, index % cols
                for k in range(8):
                    nr, nc = r + _DR[k], c + _DC[k]
                    if (
                        0 <= nr < rows
                        and 0 <= nc < cols
                        and in_component[nr, nc] != 0
                        and unresolved[nr, nc] != 0
                    ):
                        direction[nr, nc] = ((k + 4) % 8) + 1
                        unresolved[nr, nc] = 0
                        queue[tail] = nr * cols + nc
                        tail += 1
            for i in range(count):
                index = component[i]
                in_component[index // cols, index % cols] = 0
    return direction


@njit(cache=True)
def _rank_and_terminal(direction):
    """Validate routes and return numeric traversal ranks and terminal cells."""
    rows, cols = direction.shape
    size = rows * cols
    rank = np.full(size, -1, np.int32)
    terminal = np.full(size, -1, np.int64)
    state = np.zeros(size, np.uint8)
    path = np.empty(size, np.int64)
    flat = direction.ravel()
    for start in range(size):
        if flat[start] == 0:
            rank[start], terminal[start], state[start] = 0, start, 2
        elif flat[start] < 0:
            state[start] = 2
    for start in range(size):
        if flat[start] <= 0 or state[start] == 2:
            continue
        current, count = start, 0
        while state[current] != 2:
            if state[current] == 1:
                raise ValueError("Flow-direction raster contains a cycle")
            state[current] = 1
            path[count] = current
            count += 1
            code = flat[current]
            if code < 1 or code > 8:
                raise ValueError("Invalid flow-direction code")
            r, c = current // cols, current % cols
            nr, nc = r + _DR[code - 1], c + _DC[code - 1]
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                raise ValueError("Flow direction points outside the raster")
            current = nr * cols + nc
            if flat[current] < 0:
                raise ValueError("A route points into nodata")
        value, root = rank[current], terminal[current]
        for i in range(count - 1, -1, -1):
            cell = path[i]
            value += 1
            rank[cell], terminal[cell], state[cell] = value, root, 2
    return rank.reshape((rows, cols)), terminal.reshape((rows, cols))


@njit(cache=True)
def _rank_only(direction):
    """Validate routes and return ranks without retaining terminal indices."""
    rows, cols = direction.shape
    size = rows * cols
    rank = np.full(size, -1, np.int32)
    state = np.zeros(size, np.uint8)
    path = np.empty(size, np.int64)
    flat = direction.ravel()
    for start in range(size):
        if flat[start] == 0:
            rank[start], state[start] = 0, 2
        elif flat[start] < 0:
            state[start] = 2
    for start in range(size):
        if flat[start] <= 0 or state[start] == 2:
            continue
        current, count = start, 0
        while state[current] != 2:
            if state[current] == 1:
                raise ValueError("Flow-direction raster contains a cycle")
            state[current] = 1
            path[count] = current
            count += 1
            code = flat[current]
            if code < 1 or code > 8:
                raise ValueError("Invalid flow-direction code")
            r, c = current // cols, current % cols
            nr, nc = r + _DR[code - 1], c + _DC[code - 1]
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                raise ValueError("Flow direction points outside the raster")
            current = nr * cols + nc
            if flat[current] < 0:
                raise ValueError("A route points into nodata")
        value = rank[current]
        for i in range(count - 1, -1, -1):
            cell = path[i]
            value += 1
            rank[cell], state[cell] = value, 2
    return rank.reshape((rows, cols))


@njit(cache=True)
def _label_basins(terminal):
    """Compact terminal cell indices into dense basin labels in linear time."""
    rows, cols = terminal.shape
    size = rows * cols
    root_to_basin = np.full(size, -1, np.int64)
    basin = np.full(size, -1, np.int64)
    root_count = 0
    flat_terminal = terminal.ravel()
    for cell in range(size):
        root = flat_terminal[cell]
        if root >= 0 and root_to_basin[root] < 0:
            root_to_basin[root] = root_count
            root_count += 1
    unique = np.empty(root_count, np.int64)
    for cell in range(size):
        root = flat_terminal[cell]
        if root >= 0:
            basin[cell] = root_to_basin[root]
            if cell == root:
                unique[root_to_basin[root]] = root
    return basin.reshape((rows, cols)), unique


@njit(cache=True)
def _rank_order(rank):
    """Counting-sort valid cells by traversal rank in linear time."""
    flat = rank.ravel()
    maximum = 0
    valid_count = 0
    for i in range(flat.size):
        if flat[i] >= 0:
            valid_count += 1
            maximum = max(maximum, flat[i])
    counts = np.zeros(maximum + 1, np.int64)
    for i in range(flat.size):
        if flat[i] >= 0:
            counts[flat[i]] += 1
    offsets = np.empty(maximum + 1, np.int64)
    position = 0
    for value in range(maximum + 1):
        offsets[value] = position
        position += counts[value]
    order = np.empty(valid_count, np.int64)
    for i in range(flat.size):
        value = flat[i]
        if value >= 0:
            position = offsets[value]
            order[position] = i
            offsets[value] += 1
    return order


@njit(cache=True)
def _drainage_connected_cells(owned, drainage):
    """Keep owned cells in 8-connected components containing matching drainage."""
    rows, cols = owned.shape
    connected = np.zeros((rows, cols), np.bool_)
    queue = np.empty(rows * cols, np.int64)
    tail = 0
    for row in range(rows):
        for col in range(cols):
            if owned[row, col] and drainage[row, col]:
                connected[row, col] = 1
                queue[tail] = row * cols + col
                tail += 1
    head = 0
    while head < tail:
        cell = queue[head]
        head += 1
        row, col = cell // cols, cell % cols
        for k in range(8):
            nr, nc = row + _DR[k], col + _DC[k]
            if (
                0 <= nr < rows
                and 0 <= nc < cols
                and owned[nr, nc]
                and not connected[nr, nc]
            ):
                connected[nr, nc] = 1
                queue[tail] = nr * cols + nc
                tail += 1
    return connected


@njit(cache=True)
def _corridor_costs(elevation, direction, terminal, order, width, height):
    size = direction.size
    cols = direction.shape[1]
    maximum = np.zeros(size, np.float64)
    cumulative = np.zeros(size, np.float64)
    length = np.zeros(size, np.float64)
    elev = elevation.ravel()
    dirs = direction.ravel()
    roots = terminal.ravel()
    diagonal = math.hypot(width, height)
    steps = np.array(
        [height, diagonal, width, diagonal, height, diagonal, width, diagonal]
    )
    for oi in range(order.size):
        cell = order[oi]
        code = dirs[cell]
        if code <= 0:
            continue
        r, c = cell // cols, cell % cols
        parent = (r + _DR[code - 1]) * cols + c + _DC[code - 1]
        depth = max(elev[cell] - elev[roots[cell]], 0.0)
        maximum[cell] = max(maximum[parent], depth)
        cumulative[cell] = cumulative[parent] + depth
        length[cell] = length[parent] + steps[code - 1]
    return (
        maximum.reshape(direction.shape),
        cumulative.reshape(direction.shape),
        length.reshape(direction.shape),
    )


@njit(cache=True)
def _scan_basin_boundaries(basin, labels, cut_max, cut_sum, path_length, width, height):
    """Return the best numeric corridor for every directed adjacent basin pair."""
    rows, cols = basin.shape
    occurrences = 0
    for r in range(rows):
        for c in range(cols):
            if basin[r, c] < 0:
                continue
            for k in (2, 3, 4, 5):  # E, SE, S, SW: each pair exactly once.
                nr, nc = r + _DR[k], c + _DC[k]
                if (
                    0 <= nr < rows
                    and 0 <= nc < cols
                    and basin[nr, nc] >= 0
                    and labels[nr, nc] == labels[r, c]
                    and basin[nr, nc] != basin[r, c]
                ):
                    occurrences += 2
    capacity = 1
    while capacity < max(4, occurrences * 2):
        capacity *= 2
    keys = np.full(capacity, -1, np.int64)
    origins = np.full(capacity, -1, np.int64)
    destinations = np.full(capacity, -1, np.int64)
    maxima = np.full(capacity, np.inf)
    sums = np.full(capacity, np.inf)
    lengths = np.full(capacity, np.inf)
    diagonal = math.hypot(width, height)
    steps = np.array(
        [height, diagonal, width, diagonal, height, diagonal, width, diagonal]
    )
    mask = capacity - 1
    for r in range(rows):
        for c in range(cols):
            a = basin[r, c]
            if a < 0:
                continue
            for k in (2, 3, 4, 5):
                nr, nc = r + _DR[k], c + _DC[k]
                if (
                    nr < 0
                    or nr >= rows
                    or nc < 0
                    or nc >= cols
                    or labels[nr, nc] != labels[r, c]
                ):
                    continue
                b = basin[nr, nc]
                if b < 0 or a == b:
                    continue
                for reverse in range(2):
                    source = a if reverse == 0 else b
                    target = b if reverse == 0 else a
                    origin = r * cols + c if reverse == 0 else nr * cols + nc
                    destination = nr * cols + nc if reverse == 0 else r * cols + c
                    rr, cc = origin // cols, origin % cols
                    cm, cs = cut_max[rr, cc], cut_sum[rr, cc]
                    pl = path_length[rr, cc] + steps[k]
                    key = (source << 32) | target
                    slot = (key * 1140071481932319845) & mask
                    while keys[slot] != -1 and keys[slot] != key:
                        slot = (slot + 1) & mask
                    better = (
                        cm < maxima[slot]
                        or (cm == maxima[slot] and cs < sums[slot])
                        or (
                            cm == maxima[slot]
                            and cs == sums[slot]
                            and pl < lengths[slot]
                        )
                        or (
                            cm == maxima[slot]
                            and cs == sums[slot]
                            and pl == lengths[slot]
                            and origin < origins[slot]
                        )
                    )
                    if keys[slot] == -1 or better:
                        keys[slot], origins[slot], destinations[slot] = (
                            key,
                            origin,
                            destination,
                        )
                        maxima[slot], sums[slot], lengths[slot] = cm, cs, pl
    count = np.sum(keys != -1)
    result = np.empty((count, 8), np.float64)
    out = 0
    for slot in range(capacity):
        if keys[slot] != -1:
            result[out, 0] = keys[slot] >> 32
            result[out, 1] = keys[slot] & 0xFFFFFFFF
            result[out, 2] = maxima[slot]
            result[out, 3] = sums[slot]
            result[out, 4] = lengths[slot]
            result[out, 5] = origins[slot]
            result[out, 6] = destinations[slot]
            result[out, 7] = origins[slot]
            out += 1
    return result


def _basin_paths_to_stream(
    count: int, stream: np.ndarray, edges: np.ndarray
) -> np.ndarray:
    """Run a lexicographic multi-source shortest path on the basin graph."""
    incoming: list[list[int]] = [[] for _ in range(count)]
    for edge_index, edge in enumerate(edges):
        incoming[int(edge[1])].append(edge_index)
    costs: list[tuple[float, float, float, int, int] | None] = [None] * count
    selected = np.full(count, -1, dtype=np.int64)
    settled = np.zeros(count, dtype=bool)
    queue: list[tuple[float, float, float, int, int, int]] = []
    for basin_id in np.flatnonzero(stream):
        costs[int(basin_id)] = (0.0, 0.0, 0.0, -1, -1)
        heapq.heappush(queue, (0.0, 0.0, 0.0, -1, -1, int(basin_id)))
    while queue:
        *raw, basin_id = heapq.heappop(queue)
        if costs[basin_id] != tuple(raw) or settled[basin_id]:
            continue
        settled[basin_id] = True
        for edge_index in incoming[basin_id]:
            edge = edges[edge_index]
            source = int(edge[0])
            if settled[source]:
                continue
            candidate = (
                max(float(edge[2]), raw[0]),
                float(edge[3]) + raw[1],
                float(edge[4]) + raw[2],
                int(edge[7]),
                basin_id,
            )
            if costs[source] is None or candidate < costs[source]:
                costs[source] = candidate
                selected[source] = edge_index
                heapq.heappush(queue, (*candidate, source))
    return selected


@njit(cache=True)
def _reverse_selected_corridors(direction, edges, selected):
    original = direction
    result = direction.copy()
    cols = direction.shape[1]
    for basin_id in range(selected.size):
        edge_index = selected[basin_id]
        if edge_index < 0:
            continue
        origin = int(edges[edge_index, 5])
        destination = int(edges[edge_index, 6])
        previous = destination
        current = origin
        while True:
            cr, cc = current // cols, current % cols
            pr, pc = previous // cols, previous % cols
            dr, dc = pr - cr, pc - cc
            for k in range(8):
                if _DR[k] == dr and _DC[k] == dc:
                    result[cr, cc] = k + 1
                    break
            code = original[cr, cc]
            if code == 0:
                break
            previous = current
            current = (cr + _DR[code - 1]) * cols + cc + _DC[code - 1]
    return result


def compute_hand(
    elevation: np.ndarray,
    direction: np.ndarray,
    rank: np.ndarray | None = None,
) -> np.ndarray:
    """Compute signed height above terminal drainage from a direction raster."""

    elevation = np.asarray(elevation)
    direction = np.asarray(direction)
    if elevation.shape != direction.shape or elevation.ndim != 2:
        raise TerrainProductsError("Elevation and direction must be equal 2-D arrays")
    rank = _routing_rank(direction) if rank is None else np.asarray(rank)
    if rank.shape != direction.shape:
        raise TerrainProductsError("Rank and direction must have equal shapes")
    order = _rank_order(rank)
    return _hand_kernel(elevation.astype(np.float64, copy=False), direction, order)


def compute_ltnd(
    direction: np.ndarray,
    transform: Affine,
    rank: np.ndarray | None = None,
    *,
    crs: str | CRS,
) -> np.ndarray:
    """Accumulate route distance in metres on the CRS ellipsoid."""

    direction = np.asarray(direction)
    if direction.ndim != 2:
        raise TerrainProductsError("Direction must be a two-dimensional array")
    rank = _routing_rank(direction) if rank is None else np.asarray(rank)
    if rank.shape != direction.shape:
        raise TerrainProductsError("Rank and direction must have equal shapes")
    order = _rank_order(rank)
    return _metric_ltnd(direction, order, transform, crs)


def _uses_row_steps(crs: CRS) -> bool:
    operation = crs.coordinate_operation
    return crs.is_geographic or (
        crs.is_projected
        and operation is not None
        and operation.method_code in _SEPARABLE_PROJECTION_METHODS
    )


def _metric_ltnd(direction, order, transform, crs):
    """Measure routed edges on the native ellipsoid without raster reprojection."""
    if not isinstance(transform, Affine):
        transform = Affine(*transform)
    _pixel_sizes(transform)
    source = CRS.from_user_input(crs)
    if not (source.is_geographic or source.is_projected):
        raise TerrainProductsError("LTND requires a geographic or projected raster CRS")
    transformer, geod = geodetic_tools(source.to_wkt())
    if _uses_row_steps(source):
        # Geographic and separable cylindrical grids have the same edge
        # lengths at every column. Measure three kinds of edge per row
        # and reuse symmetry for all eight route directions.
        steps = np.zeros((direction.shape[0], 8), dtype=np.float64)
        x = np.full(direction.shape[0], transform.c + 0.5 * transform.a)
        y = transform.f + (np.arange(direction.shape[0]) + 0.5) * transform.e
        lon, lat = transformer.transform(x, y)
        elon, elat = transformer.transform(x + transform.a, y)
        horizontal = geod.inv(lon, lat, elon, elat)[2]
        _require_metric_distances(horizontal)
        steps[:, 2] = horizontal
        steps[:, 6] = horizontal
        if direction.shape[0] > 1:
            vertical = geod.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])[2]
            diagonal = geod.inv(lon[:-1], lat[:-1], elon[1:], elat[1:])[2]
            _require_metric_distances(vertical)
            _require_metric_distances(diagonal)
            steps[:-1, 4] = vertical
            steps[1:, 0] = vertical
            steps[:-1, 3] = diagonal
            steps[:-1, 5] = diagonal
            steps[1:, 1] = diagonal
            steps[1:, 7] = diagonal
        return _ltnd_row_steps_kernel(direction, order, steps)

    # Transform only actual route edges, in batches with bounded temporaries.
    flat = direction.ravel()
    edges = np.zeros(direction.shape, dtype=np.float64)
    output = edges.ravel()
    for start in range(0, flat.size, GEODESIC_BATCH_CELLS):
        codes = flat[start : start + GEODESIC_BATCH_CELLS]
        cells = np.flatnonzero(codes > 0) + start
        if not cells.size:
            continue
        rows, cols = np.divmod(cells, direction.shape[1])
        indices = flat[cells] - 1
        x = transform.c + (cols + 0.5) * transform.a
        y = transform.f + (rows + 0.5) * transform.e
        px = x + _DC[indices] * transform.a
        py = y + _DR[indices] * transform.e
        # These float64 buffers are local to the batch and no longer need
        # native coordinates. Reuse them for lon/lat and inverse results.
        lon, lat = transformer.transform(x, y, inplace=True)
        plon, plat = transformer.transform(px, py, inplace=True)
        distances = geod.inv(
            lon, lat, plon, plat, inplace=True, return_back_azimuth=False
        )[2]
        _require_metric_distances(distances)
        output[cells] = distances
    return _ltnd_edge_steps_kernel(direction, order, edges)


def _require_metric_distances(distances):
    if not np.all(np.isfinite(distances)) or np.any(np.asarray(distances) <= 0):
        raise TerrainProductsError("Raster CRS produced non-positive or non-finite metric steps")


@njit(cache=True)
def _ltnd_row_steps_kernel(direction, order, steps):
    result = np.full(direction.size, np.nan)
    dirs = direction.ravel()
    cols = direction.shape[1]
    for oi in range(order.size):
        cell = order[oi]
        code = dirs[cell]
        if code < 0:
            continue
        if code == 0:
            result[cell] = 0.0
        else:
            r, c = cell // cols, cell % cols
            parent = (r + _DR[code - 1]) * cols + c + _DC[code - 1]
            result[cell] = result[parent] + steps[r, code - 1]
    return result.reshape(direction.shape)


@njit(cache=True)
def _ltnd_edge_steps_kernel(direction, order, edges):
    result = np.full(direction.size, np.nan)
    dirs = direction.ravel()
    lengths = edges.ravel()
    cols = direction.shape[1]
    for oi in range(order.size):
        cell = order[oi]
        code = dirs[cell]
        if code < 0:
            continue
        if code == 0:
            result[cell] = 0.0
        else:
            r, c = cell // cols, cell % cols
            parent = (r + _DR[code - 1]) * cols + c + _DC[code - 1]
            result[cell] = result[parent] + lengths[cell]
    return result.reshape(direction.shape)


@njit(cache=True)
def _hand_kernel(elevation, direction, order):
    result = np.full(direction.size, np.nan)
    terminal_z = np.full(direction.size, np.nan)
    dirs = direction.ravel()
    elev = elevation.ravel()
    cols = direction.shape[1]
    for oi in range(order.size):
        cell = order[oi]
        code = dirs[cell]
        if code < 0:
            continue
        if code == 0:
            terminal_z[cell] = elev[cell]
        else:
            r, c = cell // cols, cell % cols
            parent = (r + _DR[code - 1]) * cols + c + _DC[code - 1]
            terminal_z[cell] = terminal_z[parent]
        result[cell] = elev[cell] - terminal_z[cell]
    return result.reshape(direction.shape)


def _parent(row: int, col: int, code: int, shape: tuple[int, int]) -> tuple[int, int]:
    if code not in _DELTAS:
        raise TerrainProductsError(f"Invalid flow-direction code {code}")
    dr, dc = _DELTAS[code]
    nr, nc = row + dr, col + dc
    if not (0 <= nr < shape[0] and 0 <= nc < shape[1]):
        raise TerrainProductsError("Flow direction points outside the raster")
    return nr, nc


def _routing_rank(direction: np.ndarray) -> np.ndarray:
    """Derive ranks while validating that every route terminates."""
    return _rank_and_terminal(np.asarray(direction))[0]


def _warm_routing_kernels() -> None:
    """Load/compile cached kernels separately from measured production routing."""
    if _ltnd_row_steps_kernel.signatures and _agree_condition_kernel.signatures:
        return
    elevation = np.array([[1.0, 0.0]], dtype=np.float64)
    labels = np.zeros((1, 2), dtype=np.int64)
    drainage = np.array([[False, True]])
    _agree_condition_dem(elevation, labels, drainage)
    direction, rank = compute_flow_directions(
        elevation, labels, drainage, Affine(1, 0, 0, 0, -1, 0)
    )
    compute_hand(elevation, direction, rank)
    compute_ltnd(direction, Affine(1, 0, 0, 0, -1, 0), rank, crs="EPSG:3857")


@dataclass(frozen=True)
class _MiniUnit:
    raster: RasterUnit
    mini_id: int
    catchment_fid: int = -1
    segment_fid: int = -1


@dataclass(frozen=True)
class _TerrainPayload:
    grid: GridSpec
    raster_assets: dict[str, Path]
    units: tuple[_MiniUnit, ...]
    direction_source: str
    write_flow_direction: bool
    agree_sharp: float
    agree_smooth: float
    agree_buffer: int
    gdal_cache_bytes: int


@dataclass(frozen=True)
class _TerrainPatch:
    mini_id: int
    window: Window
    valid: np.ndarray
    hand: np.ndarray
    ltnd: np.ndarray
    direction: np.ndarray | None
    owned_cells: int
    undrained_cells: int


@dataclass(frozen=True)
class _PacketValue:
    patches: tuple[Any, ...]


def create_terrain_dataset(
    spec: TerrainSpec, *, progress: ProgressCallback | None = None
) -> TerrainReport:
    """Build terrain products with a bounded coordinator GDAL block cache."""
    reporter = StageReporter(progress)
    sizing = MemorySizing(spec.memory_limit_mb * 1024**2, spec.workers)
    with raster_cache(sizing.coordinator_cache_bytes):
        report = _create_terrain_dataset(spec, reporter)
    reporter.finish(report.timings)
    return report


def _create_terrain_dataset(spec: TerrainSpec, reporter: StageReporter) -> TerrainReport:
    """Build and atomically publish bounded mini-based terrain products."""

    overall_started = time.perf_counter()
    _validate_terrain_spec(spec)
    reporter.operation("Inspecting DEM grid")
    grid = grid_from_dem(spec.dem)
    raster_assets = {
        "dem": Path(spec.dem),
        "grid_catchments": Path(spec.grid_catchments),
        "grid_segments": Path(spec.grid_segments),
    }
    if spec.d8 is not None:
        raster_assets["d8"] = Path(spec.d8)
    reporter.operation("Validating raster inputs")
    _validate_terrain_inputs(raster_assets, grid)

    planning_started = time.perf_counter()
    reporter.operation("Planning minis")
    mini_units = _plan_minis(Path(spec.grid_catchments), grid)
    sizing = MemorySizing(spec.memory_limit_mb * 1024**2, spec.workers)
    memory_bytes = sizing.limit_bytes
    planning_seconds = time.perf_counter() - planning_started
    config = ExecutionConfig(
        workers=spec.workers,
        memory_limit_bytes=memory_bytes,
        max_in_flight=2 * spec.workers,
        io_slots=spec.io_slots,
    )
    domain_report = ExecutionReport(0, 0, 0, 0, 0, 0.0, {}, ())

    publisher = AtomicOutputDirectory(spec.output_dir, overwrite=spec.overwrite)
    compression_seconds = 0.0
    with publisher as staging:
        reporter.operation("Planning terrain batches")
        terrain_items = _terrain_work_items(
            mini_units,
            spec,
            grid,
            raster_assets,
            memory_bytes,
        )
        reporter.enter(
            "processing", len(terrain_items), operation="Processing terrain batches",
            unit="batches",
        )
        terrain_specs = [
            RasterProductSpec(
                "hand",
                "float32",
                Resampling.average,
                _terrain_tags(spec, "height above matching drainage"),
            ),
            RasterProductSpec(
                "ltnd",
                "float32",
                Resampling.average,
                _terrain_tags(spec, "along-route distance to matching drainage"),
            ),
        ]
        if spec.write_flow_direction:
            terrain_specs.append(
                RasterProductSpec(
                    "flow_direction",
                    "uint8",
                    Resampling.nearest,
                    _terrain_tags(spec, "canonical clockwise D8 direction")
                    | {"direction_codes": ("0 drainage, 1-8 N NE E SE S SW W NW")},
                )
            )
        with RasterAssembler(
            staging,
            grid,
            sorted(terrain_specs, key=lambda product: product.name),
            scratch_memory_bytes=sizing.limit_bytes,
            compression_threads=min(spec.workers, 4),
        ) as terrain_assembler:

            def reduce_terrain(result):
                started = time.perf_counter()
                for patch in result.value.patches:
                    terrain_assembler.write(
                        RasterPatch("hand", patch.window, patch.hand, patch.valid)
                    )
                    terrain_assembler.write(
                        RasterPatch("ltnd", patch.window, patch.ltnd, patch.valid)
                    )
                    if patch.direction is not None:
                        terrain_assembler.write(
                            RasterPatch(
                                "flow_direction",
                                patch.window,
                                patch.direction,
                                patch.valid,
                            )
                        )
                    undrained_by_mini[patch.mini_id] = (
                        patch.undrained_cells,
                        patch.owned_cells,
                    )
                return {"output_write": time.perf_counter() - started}

            undrained_by_mini = {}
            terrain_report = LocalExecutor(config).run(
                terrain_items,
                _terrain_worker,
                reduce_terrain,
                progress=reporter.execution_progress,
            )
            reporter.operation("Writing undrained-cell report")
            _write_undrained_report(staging / "undrained_cells.csv", undrained_by_mini)
            reporter.enter("finalizing")
            started = time.perf_counter()
            reporter.operation(
                "Compressing raster outputs", total=len(terrain_specs), unit="rasters"
            )
            terrain_paths = terrain_assembler.finish(
                progress=lambda name, completed, total: reporter.operation(
                    f"Compressing {name}.tif", completed=completed,
                    total=total, unit="rasters",
                )
            )
            compression_seconds += time.perf_counter() - started

        reporter.operation("Validating staged rasters")
        _validate_terrain_outputs(terrain_paths, grid)
        reporter.operation("Writing output manifest")
        manifest = write_manifest(staging, "terrain-products", spec)
        reporter.operation("Publishing outputs", total=1, unit="steps")
        publisher.publish(
            (*tuple(path.name for path in terrain_paths.values()),
             "undrained_cells.csv", manifest)
        )
        reporter.advance(1)


    diagnostics = tuple(terrain_report.worker_diagnostics)
    owned_cells = sum(int(value.get("owned_cells", 0)) for value in diagnostics)
    drainage_cells = sum(int(value.get("drainage_cells", 0)) for value in diagnostics)
    undrained_cells = sum(count for count, _ in undrained_by_mini.values())
    negative_cells = sum(
        int(value.get("negative_hand_cells", 0)) for value in diagnostics
    )
    negative_mins = [
        float(value["negative_hand_min"])
        for value in diagnostics
        if value.get("negative_hand_min") is not None
    ]
    negative_maxs = [
        float(value["negative_hand_max"])
        for value in diagnostics
        if value.get("negative_hand_max") is not None
    ]
    timings = _terrain_timings(
        planning_seconds,
        compression_seconds,
        domain_report,
        terrain_report,
        overall_started,
    )
    output_dir = Path(spec.output_dir)
    return TerrainReport(
        output_dir,
        output_dir / "hand.tif",
        output_dir / "ltnd.tif",
        output_dir / "flow_direction.tif" if spec.write_flow_direction else None,
        len(mini_units),
        owned_cells,
        drainage_cells,
        negative_cells,
        min(negative_mins) if negative_mins else None,
        max(negative_maxs) if negative_maxs else None,
        spec.direction_source,
        domain_report,
        terrain_report,
        timings,
        output_dir / "undrained_cells.csv",
        undrained_cells,
    )


def _write_undrained_report(path: Path, values: dict[int, tuple[int, int]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("mini_id", "undrained_cells", "total_cells", "percentage_undrained"))
        for mini_id, (undrained, total) in sorted(values.items()):
            if undrained:
                writer.writerow((mini_id, undrained, total, 100 * undrained / total))


def _validate_terrain_spec(spec: TerrainSpec) -> None:
    if spec.direction_source == "d8" and spec.d8 is None:
        raise TerrainProductsError("D8 input is required when direction source is 'd8'")
    if spec.direction_source not in {"dem", "d8"}:
        raise TerrainProductsError("direction source must be 'dem' or 'd8'")
    _validate_agree_parameters(spec.agree_sharp, spec.agree_smooth, spec.agree_buffer)


def _validate_terrain_inputs(assets: dict[str, Path], grid: GridSpec) -> None:
    expected_dtypes = {
        "dem": None,
        "grid_catchments": "int32",
        "grid_segments": "int32",
        "d8": "uint8",
    }
    for name, path in assets.items():
        with rasterio.open(path) as source:
            _require_grid(source, grid, name)
            if name == "dem":
                require_metre_units(source, "DEM")
            expected = expected_dtypes[name]
            if expected is not None and source.dtypes[0] != expected:
                raise TerrainProductsError(
                    f"{name} raster has dtype {source.dtypes[0]}, "
                    f"expected {expected}"
                )

    validate_segment_ownership(assets, grid, TerrainProductsError)


def _validate_terrain_outputs(paths: dict[str, Path], grid: GridSpec) -> None:
    expected = {"hand", "ltnd"}
    if paths.keys() != expected and paths.keys() != expected | {"flow_direction"}:
        raise TerrainProductsError("Terrain output file set is incomplete")
    expected_dtypes = {"hand": "float32", "ltnd": "float32", "flow_direction": "uint8"}
    for name, path in paths.items():
        with rasterio.open(path) as source:
            _require_grid(source, grid, name)
            if source.dtypes[0] != expected_dtypes[name]:
                raise TerrainProductsError(
                    f"Terrain raster {name} has unexpected dtype {source.dtypes[0]}"
                )


def _plan_minis(cells: Path, grid: GridSpec) -> tuple[_MiniUnit, ...]:
    records = read_mini_index(cells)
    ids_by_key = {f"mini-{mini_id:010d}": mini_id for mini_id, *_ in records}
    planned = plan_raster_units(
        grid,
        [(f"mini-{mini_id:010d}", tuple(bounds)) for mini_id, *bounds in records],
        bytes_per_cell=DOMAIN_BYTES_PER_CELL,
        fixed_bytes=TASK_FIXED_BYTES,
    )
    return tuple(_MiniUnit(raster, ids_by_key[raster.key]) for raster in planned)


def _reestimated_units(
    units: tuple[_MiniUnit, ...], bytes_per_cell: int, row_step_bytes: int = 0
) -> tuple[RasterUnit, ...]:
    return tuple(
        RasterUnit(
            unit.raster.key,
            unit.raster.bounds,
            unit.raster.window,
            int(unit.raster.window.width * unit.raster.window.height) * bytes_per_cell
            + int(unit.raster.window.height) * row_step_bytes
            + TASK_FIXED_BYTES,
            unit.raster.spatial_key,
        )
        for unit in units
    )


def _packet_units(
    units: tuple[_MiniUnit, ...],
    bytes_per_cell: int,
    memory_limit_bytes: int,
    row_step_bytes: int = 0,
    target_bytes: int | None = None,
) -> tuple[tuple[RasterPacket, tuple[_MiniUnit, ...]], ...]:
    packets = packet_raster_units(
        _reestimated_units(units, bytes_per_cell, row_step_bytes),
        memory_limit_bytes=memory_limit_bytes,
        target_bytes=target_bytes,
    )
    by_key = {unit.raster.key: unit for unit in units}
    return tuple(
        (packet, tuple(by_key[value.key] for value in packet.units))
        for packet in packets
    )


def _terrain_work_items(
    units: tuple[_MiniUnit, ...],
    spec: TerrainSpec,
    grid: GridSpec,
    raster_assets: dict[str, Path],
    memory_limit_bytes: int,
) -> tuple[WorkItem[_TerrainPayload], ...]:
    bytes_per_cell = (
        DEM_BYTES_PER_CELL if spec.direction_source == "dem" else D8_BYTES_PER_CELL
    )
    # Row tables plus their temporary lon/lat arrays scale with rows,
    # not raster area; avoid charging 192 bytes for every cell in wide minis.
    row_step_bytes = 192 if _uses_row_steps(grid.crs) else 0
    if not row_step_bytes:
        bytes_per_cell += METRIC_BYTES_PER_CELL
    sizing = MemorySizing(spec.memory_limit_mb * 1024**2, spec.workers)
    result = []
    for ordinal, (packet, packet_units) in enumerate(
        _packet_units(units, bytes_per_cell, memory_limit_bytes, row_step_bytes, sizing.packet_bytes)
    ):
        result.append(
            WorkItem(
                packet.key,
                ordinal,
                packet.estimated_bytes,
                _TerrainPayload(
                    grid,
                    raster_assets,
                    packet_units,
                    spec.direction_source,
                    spec.write_flow_direction,
                    spec.agree_sharp,
                    spec.agree_smooth,
                    spec.agree_buffer,
                    sizing.worker_cache_bytes,
                ),
            )
        )
    return tuple(result)


def _terrain_worker(
    payload: _TerrainPayload, context: WorkerContext
) -> WorkerOutput[_PacketValue]:
    with raster_cache(payload.gdal_cache_bytes):
        return _terrain_worker_with_cache(payload, context)


def _terrain_worker_with_cache(
    payload: _TerrainPayload, context: WorkerContext
) -> WorkerOutput[_PacketValue]:
    jit_started = time.perf_counter()
    context.resources.get(
        "terrain-numba-kernels-v2",
        lambda: _warm_worker_kernels(_uses_row_steps(payload.grid.crs)),
    )
    jit_seconds = time.perf_counter() - jit_started
    aligned_reader = context.resources.get(
        "terrain-aligned-inputs:"
        + ":".join(
            f"{name}={Path(path).resolve()}"
            for name, path in sorted(payload.raster_assets.items())
        ),
        lambda: AlignedRasterReader(
            payload.grid,
            payload.raster_assets,
            context,
        ),
    )
    timings = {
        "jit_initialization": jit_seconds,
        "raster_read": 0.0,
        "conditioning": 0.0,
        "d8_validation": 0.0,
        "routing": 0.0,
        "products": 0.0,
    }
    patches = []
    owned_count = drainage_count = undrained_count = negative_count = 0
    negative_min = negative_max = None
    for unit in payload.units:
        window = unit.raster.window
        read_started = time.perf_counter()
        ownership = aligned_reader.read("grid_catchments", window)
        drainage_values = aligned_reader.read("grid_segments", window)
        dem = aligned_reader.read("dem", window)
        d8 = (
            aligned_reader.read("d8", window)
            if payload.direction_source == "d8"
            else None
        )
        timings["raster_read"] += time.perf_counter() - read_started
        owned = (~np.ma.getmaskarray(ownership)) & (
            np.asarray(ownership.data) == unit.mini_id
        )
        if not np.any(owned):
            raise TerrainProductsError(
                f"Rasterized mini {unit.mini_id} has no owned cells"
            )
        if np.any(np.ma.getmaskarray(drainage_values)[owned]):
            raise TerrainProductsError("Drainage mask does not cover cells")
        segment_ids = np.asarray(drainage_values.data)
        drainage = owned & (segment_ids == unit.mini_id)
        if not np.any(drainage):
            raise TerrainProductsError(
                f"Rasterized mini {unit.mini_id} has no drainage"
            )
        elevation = np.asarray(dem.filled(np.nan), dtype=np.float64)
        del ownership, drainage_values, dem
        if np.any(~np.isfinite(elevation[owned])):
            raise TerrainProductsError(
                f"DEM contains nodata within mini {unit.mini_id}"
            )
        labels = np.full(owned.shape, -1, dtype="int32")
        labels[owned] = 0
        transform = rasterio.windows.transform(window, payload.grid.transform)
        if payload.direction_source == "dem":
            conditioning_started = time.perf_counter()
            routing_elevation = _agree_condition_dem(
                elevation,
                labels,
                drainage,
                sharp=payload.agree_sharp,
                smooth=payload.agree_smooth,
                buffer=payload.agree_buffer,
            )
            timings["conditioning"] += time.perf_counter() - conditioning_started
            routing_started = time.perf_counter()
            direction, rank = compute_flow_directions(
                routing_elevation, labels, drainage, transform
            )
            timings["routing"] += time.perf_counter() - routing_started
            del routing_elevation
        else:
            validation_started = time.perf_counter()
            routable = _drainage_connected_cells(owned, drainage)
            direction, rank = _validated_d8(d8, routable, drainage, unit.mini_id)
            del d8
            timings["d8_validation"] += time.perf_counter() - validation_started
        valid = owned & (rank >= 0)
        undrained = owned & ~valid
        product_started = time.perf_counter()
        hand, ltnd = _terrain_products_float32(
            elevation, direction, rank, transform, crs=payload.grid.crs
        )
        del elevation, labels, rank
        timings["products"] += time.perf_counter() - product_started
        negatives = hand[valid & (hand < 0)]
        if negatives.size:
            value_min, value_max = float(negatives.min()), float(negatives.max())
            negative_min = (
                value_min if negative_min is None else min(negative_min, value_min)
            )
            negative_max = (
                value_max if negative_max is None else max(negative_max, value_max)
            )
        direction_output = None
        if payload.write_flow_direction:
            direction_output = np.where(valid, direction, 0).astype("uint8")
        patches.append(
            _TerrainPatch(
                unit.mini_id,
                window,
                valid,
                hand,
                ltnd,
                direction_output,
                int(owned.sum()),
                int(undrained.sum()),
            )
        )
        owned_count += int(owned.sum())
        drainage_count += int(drainage.sum())
        undrained_count += int(undrained.sum())
        negative_count += int(negatives.size)
    return WorkerOutput(
        _PacketValue(tuple(patches)),
        timings,
        {
            "owned_cells": owned_count,
            "drainage_cells": drainage_count,
            "undrained_cells": undrained_count,
            "negative_hand_cells": negative_count,
            "negative_hand_min": negative_min,
            "negative_hand_max": negative_max,
        },
    )


def _warm_worker_kernels(row_steps: bool = True) -> bool:
    _warm_routing_kernels()
    direction = np.array([[3, 0]], dtype="int8")
    order = np.array([1, 0], dtype="int64")
    if row_steps:
        _ltnd_row_steps_kernel(direction, order, np.ones((1, 8), dtype=np.float64))
    else:
        _ltnd_edge_steps_kernel(direction, order, np.ones((1, 2), dtype=np.float64))
    return True


def _validated_d8(
    values: np.ma.MaskedArray | None,
    owned: np.ndarray,
    drainage: np.ndarray,
    mini_id: Any,
) -> tuple[np.ndarray, np.ndarray]:
    if values is None:
        raise TerrainProductsError("Explicit D8 values were not loaded")
    if np.any(np.ma.getmaskarray(values)[owned]):
        raise TerrainProductsError(f"D8 contains nodata within mini {mini_id}")
    raw = np.asarray(values.data)
    if np.any((raw[owned] > 8) | (raw[owned] < 0)):
        raise TerrainProductsError(f"D8 contains invalid codes within mini {mini_id}")
    if np.any(owned & ~drainage & (raw == 0)):
        raise TerrainProductsError(
            f"D8 has non-drainage terminals within mini {mini_id}"
        )
    direction = np.full(owned.shape, -1, dtype="int8")
    direction[owned] = raw[owned].astype("int8", copy=False)
    direction[drainage] = 0
    rank, _ = _rank_and_terminal(direction)
    if np.any(rank[owned] < 0):
        raise TerrainProductsError(f"D8 does not terminate within mini {mini_id}")
    return direction, rank


def _terrain_products_float32(
    elevation: np.ndarray,
    direction: np.ndarray,
    rank: np.ndarray,
    transform: Affine,
    *,
    crs: str | CRS,
) -> tuple[np.ndarray, np.ndarray]:
    order = _rank_order(rank)
    hand = _hand_kernel(elevation, direction, order).astype("float32")
    ltnd = _metric_ltnd(direction, order, transform, crs).astype("float32")
    return hand, ltnd


def _terrain_tags(spec: TerrainSpec, role: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": role,
        "routing_source": spec.direction_source,
        "ownership": "strict aggregated mini catchments; no buffer",
    }
    if role in {"height above matching drainage", "along-route distance to matching drainage"}:
        result["units"] = "m"
    if role == "along-route distance to matching drainage":
        result["distance_method"] = "geodesic"
    if spec.direction_source == "dem":
        result.update(
            agree_sharp=spec.agree_sharp,
            agree_smooth=spec.agree_smooth,
            agree_buffer_pixels=spec.agree_buffer,
        )
    return result


def _terrain_timings(
    planning_seconds: float,
    compression_seconds: float,
    domain_report: ExecutionReport,
    terrain_report: ExecutionReport,
    overall_started: float,
) -> dict[str, float]:
    return {
        "planning": planning_seconds,
        "domain_execution": domain_report.wall_seconds,
        "terrain_execution": terrain_report.wall_seconds,
        "vector_read": domain_report.timings.get("vector_read", 0.0),
        "rasterization": domain_report.timings.get("rasterization", 0.0),
        "raster_read": terrain_report.timings.get("raster_read", 0.0),
        "jit_initialization": terrain_report.timings.get("jit_initialization", 0.0),
        "conditioning": terrain_report.timings.get("conditioning", 0.0),
        "d8_validation": terrain_report.timings.get("d8_validation", 0.0),
        "routing": terrain_report.timings.get("routing", 0.0),
        "products": terrain_report.timings.get("products", 0.0),
        "coordination": domain_report.timings.get("coordination", 0.0)
        + terrain_report.timings.get("coordination", 0.0),
        "output_write": domain_report.timings.get("output_write", 0.0)
        + terrain_report.timings.get("output_write", 0.0),
        "compression": compression_seconds,
        "total": time.perf_counter() - overall_started,
    }
