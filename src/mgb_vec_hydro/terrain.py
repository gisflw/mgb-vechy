"""AGREE-conditioned terrain-driven basin routing and raster products.

Natural D8 drainage on the conditioned DEM is retained except on flats and
targeted shallow-breach corridors that connect trapped basins to the supplied
drainage. HAND elevations continue to use the unmodified DEM.
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
import os
from pathlib import Path
import tempfile
import time

import geopandas as gpd
import numpy as np
from numba import njit
import rasterio
from affine import Affine
from rasterio.features import rasterize
from rasterio.windows import Window, from_bounds
from shapely.geometry.base import BaseGeometry

from mgb_vec_hydro.exceptions import TerrainProductsError


# Code, row delta, column delta. This order is also the final tie-break.
_DIRECTIONS = (
    (1, -1, 0), (2, -1, 1), (3, 0, 1), (4, 1, 1),
    (5, 1, 0), (6, 1, -1), (7, 0, -1), (8, -1, -1),
)
_DELTAS = {code: (dr, dc) for code, dr, dc in _DIRECTIONS}
_OPPOSITE = {1: 5, 2: 6, 3: 7, 4: 8, 5: 1, 6: 2, 7: 3, 8: 4}
_DR = np.array([-1, -1, 0, 1, 1, 1, 0, -1], dtype=np.int8)
_DC = np.array([0, 1, 1, 1, 0, -1, -1, -1], dtype=np.int8)


@dataclass(frozen=True)
class TerrainProductPaths:
    hand: Path
    ltnd: Path
    flow_direction: Path | None


@dataclass(frozen=True)
class TerrainProductReport:
    paths: TerrainProductPaths
    owned_cells: int
    drainage_cells: int
    unreachable_components: int
    negative_hand_cells: int
    negative_hand_min: float | None
    negative_hand_max: float | None
    routing_seconds: float = 0.0
    jit_compilation_seconds: float = 0.0
    raster_io_seconds: float = 0.0
    conditioning_seconds: float = 0.0


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
    return _agree_condition_kernel(
        elevation.astype(np.float64, copy=False),
        labels.astype(np.int64, copy=False),
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
                    if (candidate <= buffer and labels[nr, nc] == owner
                            and np.isfinite(elevation[nr, nc])
                            and candidate < distance[nr, nc]):
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

    direction = _natural_d8_and_flats(
        elevation.astype(np.float64, copy=False),
        labels.astype(np.int64, copy=False),
        drainage,
        pixel_width,
        pixel_height,
    )
    rank, terminal = _rank_and_terminal(direction)
    basin, unique_terminals = _label_basins(terminal)
    stream_basin = np.zeros(unique_terminals.size, dtype=bool)
    stream_basin[np.unique(basin[drainage])] = True

    order = _rank_order(rank)
    cut_max, cut_sum, corridor_length = _corridor_costs(
        elevation.astype(np.float64, copy=False), direction, terminal, order,
        pixel_width, pixel_height,
    )
    edge_data = _scan_basin_boundaries(
        basin, labels.astype(np.int64, copy=False), cut_max, cut_sum,
        corridor_length, pixel_width, pixel_height,
    )
    selected = _basin_paths_to_stream(
        unique_terminals.size, stream_basin, edge_data
    )
    if np.any((~stream_basin) & (selected < 0)):
        trapped = (~stream_basin) & (selected < 0)
        cells = int(np.isin(basin, np.flatnonzero(trapped)).sum())
        raise TerrainProductsError(
            f"{cells} owned cells in trapped basin(s) cannot connect to matching drainage"
        )
    direction = _reverse_selected_corridors(direction, edge_data, selected)
    rank, _ = _rank_and_terminal(direction)
    return direction, rank


@njit(cache=True)
def _natural_d8_and_flats(elevation, labels, drainage, width, height):
    """Assign strict D8 descent, then resolve equal-elevation components."""
    rows, cols = elevation.shape
    size = rows * cols
    direction = np.full((rows, cols), -1, np.int8)
    unresolved = np.zeros((rows, cols), np.uint8)
    distances = np.array(
        [height, math.hypot(width, height), width, math.hypot(width, height),
         height, math.hypot(width, height), width, math.hypot(width, height)]
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
                if (0 <= nr < rows and 0 <= nc < cols
                        and labels[nr, nc] == labels[r, c]
                        and np.isfinite(elevation[nr, nc])
                        and elevation[nr, nc] < elevation[r, c]):
                    slope = (elevation[r, c] - elevation[nr, nc]) / distances[k]
                    index = nr * cols + nc
                    if slope > best_slope or (slope == best_slope and index < best_index):
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
                    lowest_outlet = min(lowest_outlet, elevation[r + _DR[k], c + _DC[k]])
                elif direction[r, c] == 0:
                    lowest_outlet = min(lowest_outlet, z)
                for k in range(8):
                    nr, nc = r + _DR[k], c + _DC[k]
                    if (0 <= nr < rows and 0 <= nc < cols and seen[nr, nc] == 0
                            and labels[nr, nc] == owner
                            and np.isfinite(elevation[nr, nc])
                            and elevation[nr, nc] == z):
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
                    if (0 <= nr < rows and 0 <= nc < cols
                            and in_component[nr, nc] != 0
                            and unresolved[nr, nc] != 0):
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
                    if (0 <= nr < rows and 0 <= nc < cols
                            and in_component[nr, nc] != 0
                            and unresolved[nr, nc] != 0):
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
    steps = np.array([height, diagonal, width, diagonal, height, diagonal, width, diagonal])
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
    return maximum.reshape(direction.shape), cumulative.reshape(direction.shape), length.reshape(direction.shape)


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
                if (0 <= nr < rows and 0 <= nc < cols and basin[nr, nc] >= 0
                        and labels[nr, nc] == labels[r, c]
                        and basin[nr, nc] != basin[r, c]):
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
    steps = np.array([height, diagonal, width, diagonal, height, diagonal, width, diagonal])
    mask = capacity - 1
    for r in range(rows):
        for c in range(cols):
            a = basin[r, c]
            if a < 0:
                continue
            for k in (2, 3, 4, 5):
                nr, nc = r + _DR[k], c + _DC[k]
                if (nr < 0 or nr >= rows or nc < 0 or nc >= cols
                        or labels[nr, nc] != labels[r, c]):
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
                    better = (cm < maxima[slot]
                              or (cm == maxima[slot] and cs < sums[slot])
                              or (cm == maxima[slot] and cs == sums[slot] and pl < lengths[slot])
                              or (cm == maxima[slot] and cs == sums[slot] and pl == lengths[slot]
                                  and origin < origins[slot]))
                    if keys[slot] == -1 or better:
                        keys[slot], origins[slot], destinations[slot] = key, origin, destination
                        maxima[slot], sums[slot], lengths[slot] = cm, cs, pl
    count = np.sum(keys != -1)
    result = np.empty((count, 8), np.float64)
    out = 0
    for slot in range(capacity):
        if keys[slot] != -1:
            result[out, 0] = keys[slot] >> 32
            result[out, 1] = keys[slot] & 0xffffffff
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
    original = direction.copy()
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
    return _hand_kernel(
        elevation.astype(np.float64, copy=False), direction, order
    )


def compute_ltnd(
    direction: np.ndarray,
    transform: Affine,
    rank: np.ndarray | None = None,
) -> np.ndarray:
    """Accumulate floating-point parent-chain distance in raster CRS units."""

    direction = np.asarray(direction)
    if direction.ndim != 2:
        raise TerrainProductsError("Direction must be a two-dimensional array")
    rank = _routing_rank(direction) if rank is None else np.asarray(rank)
    if rank.shape != direction.shape:
        raise TerrainProductsError("Rank and direction must have equal shapes")
    width, height = _pixel_sizes(transform)
    order = _rank_order(rank)
    return _ltnd_kernel(direction, order, width, height)


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


@njit(cache=True)
def _ltnd_kernel(direction, order, width, height):
    result = np.full(direction.size, np.nan)
    dirs = direction.ravel()
    cols = direction.shape[1]
    diagonal = math.hypot(width, height)
    steps = np.array([height, diagonal, width, diagonal, height, diagonal, width, diagonal])
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
            result[cell] = result[parent] + steps[code - 1]
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
    try:
        return _rank_and_terminal(np.asarray(direction))[0]
    except ValueError as exc:
        raise TerrainProductsError(str(exc)) from exc


def _rasterize_drainage(
    ordered_catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    id_col: str,
    shape: tuple[int, int],
    transform: Affine,
    labels: np.ndarray,
) -> np.ndarray:
    """Rasterize matching segments as catchment-confined, all-touched cells."""
    drainage = np.zeros(shape, dtype=bool)
    segment_by_id = segments.set_index(id_col)
    for index, (_, feature) in enumerate(ordered_catchments.iterrows()):
        geometry = segment_by_id.loc[feature[id_col]].geometry
        drainage |= rasterize(
            [(geometry, 1)],
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype="uint8",
            all_touched=True,
        ).astype(bool) & (labels == index)
    return drainage


def create_terrain_products(
    dem_path: str | Path,
    catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    output_dir: str | Path,
    *,
    id_col: str = "id",
    write_flow_direction: bool = False,
    agree_sharp: float = 80.0,
    agree_smooth: float = 8.0,
    agree_buffer: int = 4,
) -> TerrainProductReport:
    """Validate vectors, route them on the DEM grid, and publish tiled GeoTIFFs."""

    _validate_vectors(catchments, segments, id_col)
    _validate_agree_parameters(agree_sharp, agree_smooth, agree_buffer)
    dem_path, output_dir = Path(dem_path), Path(output_dir)
    io_started = time.perf_counter()
    with rasterio.open(dem_path) as src:
        _validate_dem(src)
        catchments = catchments.to_crs(src.crs)
        segments = segments.to_crs(src.crs)
        bounds = catchments.total_bounds
        db = src.bounds
        if bounds[0] < db.left or bounds[1] < db.bottom or bounds[2] > db.right or bounds[3] > db.top:
            raise TerrainProductsError("DEM does not cover the complete catchment ROI")
        raw = from_bounds(*bounds, transform=src.transform)
        col0, row0 = max(0, math.floor(raw.col_off)), max(0, math.floor(raw.row_off))
        col1 = min(src.width, math.ceil(raw.col_off + raw.width))
        row1 = min(src.height, math.ceil(raw.row_off + raw.height))
        window = Window(col0, row0, col1 - col0, row1 - row0)
        transform = src.window_transform(window)
        elevation = src.read(1, window=window, masked=True).astype(np.float64).filled(np.nan)
        shape = elevation.shape

        # Internal sequential labels avoid assumptions about the user ID dtype.
        ordered = catchments.sort_values(id_col, key=lambda s: s.astype(str))
        labels = rasterize(
            ((geom, index) for index, geom in enumerate(ordered.geometry)),
            out_shape=shape, transform=transform, fill=-1, dtype="int32",
        )
        drainage = _rasterize_drainage(
            ordered, segments, id_col, shape, transform, labels
        )

        raster_io_seconds = time.perf_counter() - io_started
        jit_started = time.perf_counter()
        _warm_routing_kernels()
        jit_compilation_seconds = time.perf_counter() - jit_started
        conditioning_started = time.perf_counter()
        routing_elevation = _agree_condition_dem(
            elevation,
            labels,
            drainage,
            sharp=agree_sharp,
            smooth=agree_smooth,
            buffer=agree_buffer,
        )
        conditioning_seconds = time.perf_counter() - conditioning_started
        routing_started = time.perf_counter()
        direction, rank = compute_flow_directions(
            routing_elevation, labels, drainage, transform
        )
        hand = compute_hand(elevation, direction, rank).astype("float32")
        ltnd = compute_ltnd(direction, transform, rank).astype("float32")
        routing_seconds = time.perf_counter() - routing_started
        profile = src.profile.copy()
        profile.update(
            width=shape[1], height=shape[0], transform=transform, count=1,
            driver="GTiff", tiled=True, blockxsize=256, blockysize=256,
            compress="deflate", BIGTIFF="IF_SAFER",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    final = TerrainProductPaths(
        output_dir / "hand.tif", output_dir / "ltnd.tif",
        output_dir / "flow_direction.tif" if write_flow_direction else None,
    )
    staged: list[tuple[Path, Path]] = []
    io_started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix=".terrain-", dir=output_dir) as tmp:
            tmpdir = Path(tmp)
            for name, data, dtype, nodata in (
                ("hand.tif", hand, "float32", np.nan),
                ("ltnd.tif", ltnd, "float32", np.nan),
                *((("flow_direction.tif", direction, "int8", -1),) if write_flow_direction else ()),
            ):
                path = tmpdir / name
                out_profile = profile | {"dtype": dtype, "nodata": nodata}
                with rasterio.open(path, "w", **out_profile) as dst:
                    dst.write(data, 1)
                    dst.update_tags(
                        routing=(
                            "terrain-driven basin routing with catchment-confined "
                            "AGREE conditioning and targeted shallow breaching"
                        ),
                        agree_sharp=agree_sharp,
                        agree_smooth=agree_smooth,
                        agree_buffer_pixels=agree_buffer,
                        direction_codes="-1 nodata, 0 drainage, 1-8 N NE E SE S SW W NW",
                    )
                staged.append((path, output_dir / name))
            for source, target in staged:
                os.replace(source, target)
    except Exception:
        # Files remain private in the temporary directory until all are complete.
        raise
    raster_io_seconds += time.perf_counter() - io_started

    negatives = hand[np.isfinite(hand) & (hand < 0)]
    return TerrainProductReport(
        final, int((labels >= 0).sum()), int(drainage.sum()), 0,
        int(negatives.size),
        float(negatives.min()) if negatives.size else None,
        float(negatives.max()) if negatives.size else None,
        routing_seconds,
        jit_compilation_seconds,
        raster_io_seconds,
        conditioning_seconds,
    )


def _warm_routing_kernels() -> None:
    """Load/compile cached kernels separately from measured production routing."""
    if _ltnd_kernel.signatures and _agree_condition_kernel.signatures:
        return
    elevation = np.array([[1.0, 0.0]], dtype=np.float64)
    labels = np.zeros((1, 2), dtype=np.int64)
    drainage = np.array([[False, True]])
    _agree_condition_dem(elevation, labels, drainage)
    direction, rank = compute_flow_directions(
        elevation, labels, drainage, Affine(1, 0, 0, 0, -1, 0)
    )
    compute_hand(elevation, direction, rank)
    compute_ltnd(direction, Affine(1, 0, 0, 0, -1, 0), rank)


def _validate_dem(src: rasterio.io.DatasetReader) -> None:
    if src.crs is None:
        raise TerrainProductsError("DEM has no CRS")
    if src.crs.is_geographic or not src.crs.is_projected:
        raise TerrainProductsError("DEM CRS must be projected")
    unit = src.crs.linear_units.lower()
    _, factor = src.crs.linear_units_factor
    if unit not in {"metre", "meter", "metres", "meters"} or not math.isclose(factor, 1.0):
        raise TerrainProductsError("DEM CRS horizontal units must be metres")
    _pixel_sizes(src.transform)


def _validate_vectors(
    catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    id_col: str,
) -> None:
    for name, frame, allowed in (
        ("catchments", catchments, {"Polygon", "MultiPolygon"}),
        ("segments", segments, {"LineString", "MultiLineString"}),
    ):
        if id_col not in frame:
            raise TerrainProductsError(f"{name} has no '{id_col}' column")
        if frame.crs is None:
            raise TerrainProductsError(f"{name} has no CRS")
        if frame.empty or frame.geometry.is_empty.any() or frame.geometry.isna().any():
            raise TerrainProductsError(f"{name} contains missing or empty geometry")
        invalid = set(frame.geom_type) - allowed
        if invalid:
            raise TerrainProductsError(f"{name} has invalid geometry type(s): {sorted(invalid)}")
        if frame[id_col].isna().any() or frame[id_col].duplicated().any():
            raise TerrainProductsError(f"{name} IDs must be non-null and unique")
    if set(catchments[id_col]) != set(segments[id_col]):
        raise TerrainProductsError("Catchment and segment IDs do not match")
    spatial = catchments.geometry
    for i, geometry in enumerate(spatial):
        for j in catchments.sindex.query(geometry, predicate="intersects"):
            if j > i and geometry.intersection(spatial.iloc[j]).area > 0:
                raise TerrainProductsError("Catchments have positive-area overlaps")
