//! Confined mini-basin routing and raw-DEM HAND/geodesic LTND products.
use super::{
    execution::{CacheBudget, MIB, cache_allocation},
    io::{self, attach_mask, read, staging_raster, windows},
    model::{Grid, Window},
};
use anyhow::{Context, Result, ensure};
use gdal::{Dataset, Metadata, raster::GdalType};
use geographiclib_rs::{Geodesic, InverseGeodesic};
use serde::Serialize;
use std::{
    cmp::{Ordering, Reverse},
    collections::{BTreeMap, BinaryHeap, HashMap},
    fs,
    path::{Path, PathBuf},
    sync::{Condvar, Mutex, mpsc},
    thread,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, clap::ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum DirectionSource {
    Dem,
    D8,
}

/// Explicit canonical-grid inputs; DEM elevations are already in metres.
#[derive(Debug, Clone, Serialize)]
pub struct TerrainSpec {
    pub dem: PathBuf,
    pub grid_catchments: PathBuf,
    pub grid_segments: PathBuf,
    pub output_dir: PathBuf,
    pub direction_source: DirectionSource,
    pub d8: Option<PathBuf>,
    pub write_flow_direction: bool,
    pub agree_sharp: f64,
    pub agree_smooth: f64,
    pub agree_buffer: usize,
    pub workers: usize,
    /// Application allocation budget in MiB, not a hard RSS ceiling.
    pub memory_limit_mb: usize,
    /// Reserved routing bytes per cell in a mini's bounding window.
    pub routing_bytes_per_cell: usize,
    pub overwrite: bool,
    pub io_slots: usize,
}

#[derive(Debug)]
pub struct TerrainReport {
    pub hand: PathBuf,
    pub ltnd: PathBuf,
    pub flow_direction: Option<PathBuf>,
    pub undrained_cells: PathBuf,
    pub manifest: PathBuf,
    pub mini_count: usize,
    /// Peak number of concurrent admitted mini workers.
    pub workers_used: usize,
    pub timings: super::execution::StageTimings,
    pub undrained_count: usize,
}

pub(super) const DEFAULT_ROUTING_BYTES_PER_CELL: usize = 128;
const WORKER_BYTES: usize = 32 * MIB;

const DR: [isize; 8] = [-1, -1, 0, 1, 1, 1, 0, -1];
const DC: [isize; 8] = [0, 1, 1, 1, 0, -1, -1, -1];

fn neighbor(cell: usize, k: usize, width: usize, height: usize) -> Option<usize> {
    let r = (cell / width) as isize + DR[k];
    let c = (cell % width) as isize + DC[k];
    if r < 0 || c < 0 || r >= height as isize || c >= width as isize {
        None
    } else {
        Some(r as usize * width + c as usize)
    }
}
fn steps(dx: f64, dy: f64) -> [f64; 8] {
    let d = dx.hypot(dy);
    [dy, d, dx, d, dy, d, dx, d]
}

fn agree(
    z: &[f64],
    labels: &[i32],
    drainage: &[bool],
    width: usize,
    sharp: f64,
    smooth: f64,
    buffer: usize,
) -> Result<Vec<f64>> {
    ensure!(
        sharp.is_finite() && sharp >= 0. && smooth.is_finite() && smooth >= 0.,
        "AGREE depths must be finite and non-negative"
    );
    let height = z.len() / width;
    let mut distance = vec![f64::INFINITY; z.len()];
    for cell in 0..z.len() {
        if !drainage[cell] {
            continue;
        }
        let r = cell / width;
        let c = cell % width;
        for nr in r.saturating_sub(buffer)..=r.saturating_add(buffer).min(height - 1) {
            for nc in c.saturating_sub(buffer)..=c.saturating_add(buffer).min(width - 1) {
                let other = nr * width + nc;
                let dr = nr.abs_diff(r) as f64;
                let dc = nc.abs_diff(c) as f64;
                let d = (dr * dr + dc * dc).sqrt();
                if d <= buffer as f64 && labels[other] == labels[cell] && z[other].is_finite() {
                    distance[other] = distance[other].min(d);
                }
            }
        }
    }
    let mut result = z.to_vec();
    for i in 0..z.len() {
        if distance[i].is_finite() {
            result[i] += smooth * (distance[i] - buffer as f64);
            if drainage[i] {
                result[i] -= sharp;
            }
            ensure!(result[i].is_finite(), "AGREE produced non-finite elevation");
        }
    }
    Ok(result)
}

fn natural_d8(
    z: &[f64],
    labels: &[i32],
    drainage: &[bool],
    width: usize,
    pixel_steps: &[f64; 8],
) -> Vec<i8> {
    let n = z.len();
    let height = n / width;
    let mut direction = vec![-1; n];
    let mut unresolved = vec![false; n];
    for i in 0..n {
        if labels[i] < 0 || !z[i].is_finite() {
            continue;
        }
        if drainage[i] {
            direction[i] = 0;
            continue;
        }
        let mut best_slope = 0.;
        let mut best_index = n;
        for (k, step) in pixel_steps.iter().enumerate() {
            if let Some(j) = neighbor(i, k, width, height)
                && labels[j] == labels[i]
                && z[j].is_finite()
                && z[j] < z[i]
            {
                let slope = (z[i] - z[j]) / step;
                if slope > best_slope || (slope == best_slope && j < best_index) {
                    best_slope = slope;
                    best_index = j;
                    direction[i] = k as i8 + 1;
                }
            }
        }
        unresolved[i] = direction[i] < 0;
    }
    let mut seen = vec![false; n];
    let mut in_component = vec![false; n];
    let mut queue = Vec::with_capacity(n);
    let mut component = Vec::with_capacity(n);
    for start in 0..n {
        if !unresolved[start] || seen[start] {
            continue;
        }
        queue.clear();
        component.clear();
        queue.push(start);
        seen[start] = true;
        let mut head = 0;
        let mut lowest = f64::INFINITY;
        while head < queue.len() {
            let i = queue[head];
            head += 1;
            component.push(i);
            in_component[i] = true;
            if direction[i] > 0 {
                let j = neighbor(i, direction[i] as usize - 1, width, height).unwrap();
                lowest = lowest.min(z[j]);
            } else if direction[i] == 0 {
                lowest = lowest.min(z[i]);
            }
            for k in 0..8 {
                if let Some(j) = neighbor(i, k, width, height)
                    && !seen[j]
                    && labels[j] == labels[start]
                    && z[j].is_finite()
                    && z[j] == z[start]
                {
                    seen[j] = true;
                    queue.push(j);
                }
            }
        }
        queue.clear();
        for &i in &component {
            if direction[i] == 0
                || (direction[i] > 0
                    && z[neighbor(i, direction[i] as usize - 1, width, height).unwrap()] == lowest)
            {
                queue.push(i);
            }
        }
        if queue.is_empty() {
            let root = component[0];
            direction[root] = 0;
            unresolved[root] = false;
            queue.push(root);
        }
        for pass in 0..2 {
            if pass == 1 {
                queue.clear();
                queue.extend(component.iter().copied().filter(|&i| direction[i] >= 0));
            }
            head = 0;
            while head < queue.len() {
                let i = queue[head];
                head += 1;
                for k in 0..8 {
                    if let Some(j) = neighbor(i, k, width, height)
                        && in_component[j]
                        && unresolved[j]
                    {
                        direction[j] = ((k + 4) % 8) as i8 + 1;
                        unresolved[j] = false;
                        queue.push(j);
                    }
                }
            }
        }
        for &i in &component {
            in_component[i] = false;
        }
    }
    direction
}

fn ranks(direction: &[i8], width: usize) -> Result<(Vec<i32>, Vec<usize>)> {
    let n = direction.len();
    let height = n / width;
    let mut rank = vec![-1i32; n];
    let mut terminal = vec![usize::MAX; n];
    let mut state = vec![0u8; n];
    let mut path = Vec::with_capacity(n);
    for i in 0..n {
        if direction[i] == 0 {
            rank[i] = 0;
            terminal[i] = i;
            state[i] = 2;
        } else if direction[i] < 0 {
            state[i] = 2;
        }
    }
    for start in 0..n {
        if direction[start] <= 0 || state[start] == 2 {
            continue;
        }
        path.clear();
        let mut current = start;
        while state[current] != 2 {
            ensure!(
                state[current] != 1,
                "Flow-direction raster contains a cycle"
            );
            state[current] = 1;
            path.push(current);
            ensure!(
                (1..=8).contains(&direction[current]),
                "Invalid flow-direction code"
            );
            current = neighbor(current, direction[current] as usize - 1, width, height)
                .context("Flow direction points outside the raster")?;
            ensure!(direction[current] >= 0, "A route points into nodata");
        }
        let mut value = rank[current];
        let root = terminal[current];
        for &i in path.iter().rev() {
            value = value.checked_add(1).context("Route rank overflow")?;
            rank[i] = value;
            terminal[i] = root;
            state[i] = 2;
        }
    }
    Ok((rank, terminal))
}
fn rank_order(rank: &[i32]) -> Vec<usize> {
    let maximum = rank.iter().copied().max().unwrap_or(0).max(0) as usize;
    let mut offsets = vec![0; maximum + 1];
    for &r in rank {
        if r >= 0 {
            offsets[r as usize] += 1;
        }
    }
    let mut count = 0;
    for v in &mut offsets {
        let c = *v;
        *v = count;
        count += c;
    }
    let mut result = vec![0; count];
    for (i, &r) in rank.iter().enumerate() {
        if r >= 0 {
            let r = r as usize;
            result[offsets[r]] = i;
            offsets[r] += 1;
        }
    }
    result
}

#[derive(Debug, Clone, Copy)]
struct Cost {
    max: f64,
    sum: f64,
    length: f64,
    origin: usize,
    target: usize,
}
impl PartialEq for Cost {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Cost {}
impl PartialOrd for Cost {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Cost {
    fn cmp(&self, other: &Self) -> Ordering {
        self.max
            .total_cmp(&other.max)
            .then(self.sum.total_cmp(&other.sum))
            .then(self.length.total_cmp(&other.length))
            .then(self.origin.cmp(&other.origin))
            .then(self.target.cmp(&other.target))
    }
}
#[derive(Clone, Copy)]
struct Edge {
    source: usize,
    destination: usize,
    cost: Cost,
}

fn routing(
    z: &[f64],
    labels: &[i32],
    drainage: &[bool],
    width: usize,
    dx: f64,
    dy: f64,
) -> Result<(Vec<i8>, Vec<i32>)> {
    let n = z.len();
    let height = n / width;
    let pixel_steps = steps(dx, dy);
    let original = natural_d8(z, labels, drainage, width, &pixel_steps);
    let (rank, terminal) = ranks(&original, width)?;
    let mut root_to_basin = vec![usize::MAX; n];
    let mut count = 0;
    for &root in &terminal {
        if root != usize::MAX && root_to_basin[root] == usize::MAX {
            root_to_basin[root] = count;
            count += 1;
        }
    }
    let basin: Vec<_> = terminal
        .iter()
        .map(|&r| {
            if r == usize::MAX {
                usize::MAX
            } else {
                root_to_basin[r]
            }
        })
        .collect();
    let mut stream = vec![false; count];
    for i in 0..n {
        if drainage[i] {
            stream[basin[i]] = true;
        }
    }
    let mut cut_max = vec![0f64; n];
    let mut cut_sum = vec![0f64; n];
    let mut length = vec![0f64; n];
    for i in rank_order(&rank) {
        if original[i] > 0 {
            let k = original[i] as usize - 1;
            let p = neighbor(i, k, width, height).unwrap();
            let depth = (z[i] - z[terminal[i]]).max(0.);
            cut_max[i] = cut_max[p].max(depth);
            cut_sum[i] = cut_sum[p] + depth;
            length[i] = length[p] + pixel_steps[k];
        }
    }
    let mut edges = HashMap::<(usize, usize), Edge>::new();
    for i in 0..n {
        if basin[i] == usize::MAX {
            continue;
        }
        for (k, step) in pixel_steps.iter().enumerate().take(6).skip(2) {
            let Some(j) = neighbor(i, k, width, height) else {
                continue;
            };
            if labels[j] != labels[i] || basin[j] == usize::MAX || basin[i] == basin[j] {
                continue;
            }
            for (origin, destination) in [(i, j), (j, i)] {
                let source = basin[origin];
                let target = basin[destination];
                let cost = Cost {
                    max: cut_max[origin],
                    sum: cut_sum[origin],
                    length: length[origin] + step,
                    origin,
                    target,
                };
                let edge = Edge {
                    source,
                    destination,
                    cost,
                };
                let best = edges.entry((source, target)).or_insert(edge);
                if cost < best.cost {
                    *best = edge;
                }
            }
        }
    }
    drop(cut_max);
    drop(cut_sum);
    drop(length);
    drop(terminal);
    drop(rank);
    drop(root_to_basin);
    let mut edges: Vec<_> = edges.into_values().collect();
    edges.sort_unstable_by_key(|e| (e.source, e.cost.target));
    let mut incoming = vec![Vec::new(); count];
    for (i, e) in edges.iter().enumerate() {
        incoming[e.cost.target].push(i);
    }
    let mut costs = vec![None; count];
    let mut selected = vec![None; count];
    let mut settled = vec![false; count];
    let mut queue = BinaryHeap::new();
    for b in 0..count {
        if stream[b] {
            let cost = Cost {
                max: 0.,
                sum: 0.,
                length: 0.,
                origin: 0,
                target: 0,
            };
            costs[b] = Some(cost);
            queue.push(Reverse((cost, b)));
        }
    }
    while let Some(Reverse((cost, b))) = queue.pop() {
        if settled[b] || costs[b] != Some(cost) {
            continue;
        }
        settled[b] = true;
        for &e in &incoming[b] {
            let edge = edges[e];
            let source = edge.source;
            if settled[source] {
                continue;
            }
            let candidate = Cost {
                max: edge.cost.max.max(cost.max),
                sum: edge.cost.sum + cost.sum,
                length: edge.cost.length + cost.length,
                origin: edge.cost.origin,
                target: b,
            };
            if costs[source].is_none_or(|old| candidate < old) {
                costs[source] = Some(candidate);
                selected[source] = Some(e);
                queue.push(Reverse((candidate, source)));
            }
        }
    }
    let mut direction = original.clone();
    for edge in selected.iter().flatten().map(|&e| edges[e]) {
        let mut previous = edge.destination;
        let mut current = edge.cost.origin;
        loop {
            let k = (0..8)
                .find(|&k| neighbor(current, k, width, height) == Some(previous))
                .context("Invalid breach corridor")?;
            direction[current] = k as i8 + 1;
            if original[current] == 0 {
                break;
            }
            previous = current;
            current = neighbor(current, original[current] as usize - 1, width, height).unwrap();
        }
    }
    for i in 0..n {
        if basin[i] != usize::MAX && !stream[basin[i]] && selected[basin[i]].is_none() {
            direction[i] = -1;
        }
    }
    let (rank, _) = ranks(&direction, width)?;
    Ok((direction, rank))
}

fn connected(owned: &[bool], drainage: &[bool], width: usize) -> Vec<bool> {
    let mut result = vec![false; owned.len()];
    let mut queue = Vec::with_capacity(owned.len());
    for i in 0..owned.len() {
        if owned[i] && drainage[i] {
            result[i] = true;
            queue.push(i);
        }
    }
    let mut head = 0;
    while head < queue.len() {
        let i = queue[head];
        head += 1;
        for k in 0..8 {
            if let Some(j) = neighbor(i, k, width, owned.len() / width)
                && owned[j]
                && !result[j]
            {
                result[j] = true;
                queue.push(j);
            }
        }
    }
    result
}
fn validate_d8(
    raw: &[f64],
    mask: &[u8],
    owned: &[bool],
    drainage: &[bool],
    width: usize,
) -> Result<(Vec<i8>, Vec<i32>)> {
    let mut direction = vec![-1; raw.len()];
    for i in 0..raw.len() {
        if owned[i] {
            ensure!(mask[i] != 0, "D8 contains nodata");
            ensure!(
                raw[i].is_finite() && raw[i].fract() == 0. && (0. ..=8.).contains(&raw[i]),
                "D8 contains invalid codes"
            );
            ensure!(drainage[i] || raw[i] != 0., "D8 has non-drainage terminal");
            direction[i] = if drainage[i] { 0 } else { raw[i] as i8 };
        }
    }
    let (rank, _) = ranks(&direction, width)?;
    Ok((direction, rank))
}

fn products(
    z: &[f64],
    direction: &[i8],
    rank: &[i32],
    width: usize,
    transform: [f64; 6],
    wkt: &str,
) -> Result<(Vec<f64>, Vec<f64>)> {
    let source = io::spatial_ref(wkt)?;
    ensure!(
        source.is_geographic() || source.is_projected(),
        "LTND requires a geographic or projected CRS"
    );
    let mut target = source.geog_cs()?;
    ensure!(
        unsafe {
            gdal_sys::OSRSetAngularUnits(
                target.to_c_hsrs(),
                c"degree".as_ptr(),
                std::f64::consts::PI / 180.,
            )
        } == 0,
        "Cannot normalize geographic CRS to degrees"
    );
    target.set_axis_mapping_strategy(gdal::spatial_ref::AxisMappingStrategy::TraditionalGisOrder);
    let a = source.semi_major()?;
    let b = source.semi_minor()?;
    ensure!(
        a.is_finite() && b.is_finite() && a > 0. && b > 0.,
        "Source CRS lacks usable ellipsoid"
    );
    let geodesic = Geodesic::new(a, (a - b) / a);
    let crs_transform = gdal::spatial_ref::CoordTransform::new(&source, &target)?;
    let n = z.len();
    let height = n / width;
    // Geographic grids share three edge lengths per row; compute each only once.
    let row_steps = if source.is_geographic() {
        let mut x = Vec::with_capacity(height * 2);
        let mut y = Vec::with_capacity(height * 2);
        for row in 0..height {
            let x0 = transform[0] + 0.5 * transform[1];
            let y0 = transform[3] + (row as f64 + 0.5) * transform[5];
            x.extend([x0, x0 + transform[1]]);
            y.extend([y0, y0]);
        }
        crs_transform.transform_coords(&mut x, &mut y, &mut [])?;
        let mut rows = vec![[0.; 8]; height];
        for row in 0..height {
            let i = row * 2;
            let horizontal: f64 = geodesic.inverse(y[i], x[i], y[i + 1], x[i + 1]);
            ensure!(
                horizontal.is_finite() && horizontal > 0.,
                "Raster CRS produced invalid metric steps"
            );
            rows[row][2] = horizontal;
            rows[row][6] = horizontal;
            if row + 1 < height {
                let vertical: f64 = geodesic.inverse(y[i], x[i], y[i + 2], x[i + 2]);
                let diagonal: f64 = geodesic.inverse(y[i], x[i], y[i + 3], x[i + 3]);
                ensure!(
                    vertical.is_finite() && vertical > 0. && diagonal.is_finite() && diagonal > 0.,
                    "Raster CRS produced invalid metric steps"
                );
                rows[row][4] = vertical;
                rows[row + 1][0] = vertical;
                rows[row][3] = diagonal;
                rows[row][5] = diagonal;
                rows[row + 1][1] = diagonal;
                rows[row + 1][7] = diagonal;
            }
        }
        Some(rows)
    } else {
        None
    };
    let mut edge_steps = if row_steps.is_none() {
        vec![0.; n]
    } else {
        Vec::new()
    };
    if row_steps.is_none() {
        for start in (0..n).step_by(16384) {
            let mut cells = Vec::new();
            let mut x = Vec::new();
            let mut y = Vec::new();
            for (i, &code) in direction
                .iter()
                .enumerate()
                .take((start + 16384).min(n))
                .skip(start)
            {
                if code > 0 {
                    let parent = neighbor(i, code as usize - 1, width, height)
                        .context("Route outside raster")?;
                    cells.push(i);
                    for cell in [i, parent] {
                        x.push(
                            transform[0]
                                + (cell % width) as f64 * transform[1]
                                + 0.5 * transform[1],
                        );
                        y.push(
                            transform[3]
                                + (cell / width) as f64 * transform[5]
                                + 0.5 * transform[5],
                        );
                    }
                }
            }
            if cells.is_empty() {
                continue;
            }
            crs_transform.transform_coords(&mut x, &mut y, &mut [])?;
            for (j, &i) in cells.iter().enumerate() {
                let distance: f64 =
                    geodesic.inverse(y[2 * j], x[2 * j], y[2 * j + 1], x[2 * j + 1]);
                ensure!(
                    distance.is_finite() && distance > 0.,
                    "Raster CRS produced invalid metric steps"
                );
                edge_steps[i] = distance;
            }
        }
    }
    let mut hand = vec![f64::NAN; n];
    let mut ltnd = vec![f64::NAN; n];
    let mut terminal_z = vec![f64::NAN; n];
    for i in rank_order(rank) {
        if direction[i] == 0 {
            terminal_z[i] = z[i];
            ltnd[i] = 0.;
        } else {
            let p = neighbor(i, direction[i] as usize - 1, width, n / width).unwrap();
            terminal_z[i] = terminal_z[p];
            ltnd[i] = ltnd[p]
                + row_steps.as_ref().map_or_else(
                    || edge_steps[i],
                    |rows| rows[i / width][direction[i] as usize - 1],
                );
        }
        hand[i] = z[i] - terminal_z[i];
    }
    Ok((hand, ltnd))
}

fn routing_reservation(window: Window, bytes_per_cell: usize) -> Result<usize> {
    let cells = window
        .width
        .checked_mul(window.height)
        .context("Mini cell count overflow")?;
    cells
        .checked_mul(bytes_per_cell)
        .and_then(|bytes| bytes.checked_add(WORKER_BYTES))
        .context("Mini working-memory estimate overflow")
}

struct Admission {
    pending: std::collections::VecDeque<usize>,
    bytes: usize,
    stopped: bool,
    active: usize,
    peak: usize,
}

struct CancelWorkers<'a> {
    state: &'a Mutex<Admission>,
    ready: &'a Condvar,
}
impl Drop for CancelWorkers<'_> {
    fn drop(&mut self) {
        if let Ok(mut state) = self.state.lock() {
            state.stopped = true;
        }
        self.ready.notify_all();
    }
}

struct Patch {
    id: i64,
    window: Window,
    owned: Vec<bool>,
    valid: Vec<u8>,
    hand: Vec<f32>,
    ltnd: Vec<f32>,
    direction: Vec<u8>,
    undrained: usize,
    total: usize,
}

fn input_paths(spec: &TerrainSpec) -> Vec<&Path> {
    let mut paths = vec![
        spec.dem.as_path(),
        spec.grid_catchments.as_path(),
        spec.grid_segments.as_path(),
    ];
    if spec.direction_source == DirectionSource::D8
        && let Some(d8) = &spec.d8
    {
        paths.push(d8);
    }
    paths
}
fn open_inputs(spec: &TerrainSpec) -> Result<Vec<Dataset>> {
    input_paths(spec)
        .into_iter()
        .map(|p| Ok(Dataset::open(p)?))
        .collect()
}
#[derive(Clone, Copy)]
struct Mini {
    id: i64,
    window: Window,
    owned_cells: usize,
}

fn mini_spatial_order(minis: &[Mini]) -> Vec<usize> {
    let mut order: Vec<_> = (0..minis.len()).collect();
    order.sort_unstable_by_key(|&ordinal| {
        let mini = minis[ordinal];
        (
            mini.window.y / io::BLOCK,
            mini.window.x / io::BLOCK,
            mini.id,
        )
    });
    order
}

fn inspect(spec: &TerrainSpec, datasets: &[Dataset]) -> Result<(Grid, Vec<Mini>)> {
    let grid = io::canonical_grid(&datasets[0])?;
    for (i, path) in input_paths(spec).into_iter().enumerate() {
        io::validate_raster(
            &datasets[i],
            path,
            &grid,
            match i {
                0 => "Float32",
                3 => "Byte",
                _ => "Int32",
            },
            i == 0,
        )?;
    }
    let minis = io::mini_windows(&datasets[1], &grid)?;
    let ids: BTreeMap<_, _> = minis
        .iter()
        .enumerate()
        .map(|(i, (id, _))| (*id, i))
        .collect();
    let mut bounds = vec![(usize::MAX, usize::MAX, 0, 0); minis.len()];
    let mut drainage = vec![0usize; minis.len()];
    let mut owned_cells = vec![0usize; minis.len()];
    let mut last_owner = None;
    for window in windows(Window {
        x: 0,
        y: 0,
        width: grid.width,
        height: grid.height,
    }) {
        let owners = read(&datasets[1], window)?;
        let segments = read(&datasets[2], window)?;
        for i in 0..window.width * window.height {
            let Some(owner) = owners.value(i) else {
                ensure!(
                    segments.value(i).is_none(),
                    "Segment validity exists outside catchment ownership"
                );
                continue;
            };
            let id = owner as i64;
            let index = if let Some((last, index)) = last_owner
                && last == id
            {
                index
            } else {
                let index = *ids.get(&id).context("Ownership contains unknown mini")?;
                last_owner = Some((id, index));
                index
            };
            let segment = segments
                .value(i)
                .context("Drainage mask does not cover owned cells")?;
            ensure!(
                segment == 0. || segment == owner,
                "Segment ownership mismatch for mini {owner}"
            );
            if segment == owner {
                drainage[index] += 1;
            }
            owned_cells[index] += 1;
            let x = window.x + i % window.width;
            let y = window.y + i / window.width;
            let b = &mut bounds[index];
            b.0 = b.0.min(x);
            b.1 = b.1.min(y);
            b.2 = b.2.max(x + 1);
            b.3 = b.3.max(y + 1);
        }
    }
    for (i, (id, w)) in minis.iter().enumerate() {
        ensure!(
            bounds[i] == (w.x, w.y, w.x + w.width, w.y + w.height),
            "Mini {id} index bounds are not tight ownership bounds"
        );
        ensure!(drainage[i] > 0, "Mini {id} has no matching drainage");
    }
    let minis = minis
        .into_iter()
        .zip(owned_cells)
        .map(|((id, window), owned_cells)| Mini {
            id,
            window,
            owned_cells,
        })
        .collect();
    Ok((grid, minis))
}
fn process_mini(
    spec: &TerrainSpec,
    datasets: &[Dataset],
    grid: &Grid,
    id: i64,
    window: Window,
    expected_owned: usize,
    slots: &super::execution::IoSlots,
) -> Result<Patch> {
    let (dem, owners, segments) = {
        let _permit = slots.acquire()?;
        (
            read(&datasets[0], window)?,
            read(&datasets[1], window)?,
            read(&datasets[2], window)?,
        )
    };
    let owned: Vec<_> = (0..window.width * window.height)
        .map(|i| owners.value(i) == Some(id as f64))
        .collect();
    ensure!(
        owned.iter().filter(|&&value| value).count() == expected_owned,
        "Mini {id} ownership changed during routing"
    );
    let drainage: Vec<_> = owned
        .iter()
        .enumerate()
        .map(|(i, &o)| o && segments.value(i) == Some(id as f64))
        .collect();
    let z: Vec<_> = (0..owned.len())
        .map(|i| dem.value(i).unwrap_or(f64::NAN))
        .collect();
    for i in 0..owned.len() {
        if owned[i] {
            ensure!(
                z[i].is_finite() && segments.value(i).is_some_and(|v| v == 0. || v == id as f64),
                "Mini {id} inputs changed during routing"
            );
        }
    }
    drop(dem);
    drop(owners);
    drop(segments);
    let (direction, rank) = match spec.direction_source {
        DirectionSource::Dem => {
            let labels: Vec<_> = owned.iter().map(|&o| if o { 0 } else { -1 }).collect();
            let conditioned = agree(
                &z,
                &labels,
                &drainage,
                window.width,
                spec.agree_sharp,
                spec.agree_smooth,
                spec.agree_buffer,
            )?;
            routing(
                &conditioned,
                &labels,
                &drainage,
                window.width,
                grid.transform[1],
                -grid.transform[5],
            )?
        }
        DirectionSource::D8 => {
            let routable = connected(&owned, &drainage, window.width);
            let d8 = {
                let _permit = slots.acquire()?;
                read(&datasets[3], window)?
            };
            validate_d8(
                d8.values.data(),
                d8.mask.data(),
                &routable,
                &drainage,
                window.width,
            )?
        }
    };
    let mut affine = grid.transform;
    affine[0] += window.x as f64 * affine[1];
    affine[3] += window.y as f64 * affine[5];
    let (hand, ltnd) = products(&z, &direction, &rank, window.width, affine, &grid.wkt)?;
    let valid: Vec<_> = owned
        .iter()
        .enumerate()
        .map(|(i, &o)| if o && rank[i] >= 0 { 255 } else { 0 })
        .collect();
    for i in 0..owned.len() {
        if valid[i] != 0 {
            ensure!(
                (hand[i] as f32).is_finite() && (ltnd[i] as f32).is_finite(),
                "Mini {id} products exceed float32 range"
            );
        }
    }
    let undrained = owned
        .iter()
        .enumerate()
        .filter(|&(i, &o)| o && valid[i] == 0)
        .count();
    let total = owned.iter().filter(|&&o| o).count();
    Ok(Patch {
        id,
        window,
        owned,
        valid,
        hand: hand.into_iter().map(|v| v as f32).collect(),
        ltnd: ltnd.into_iter().map(|v| v as f32).collect(),
        direction: if spec.write_flow_direction {
            direction.into_iter().map(|v| v.max(0) as u8).collect()
        } else {
            Vec::new()
        },
        undrained,
        total,
    })
}

fn output_names(flow: bool) -> Vec<String> {
    let mut names = vec![
        "hand.tif".into(),
        "ltnd.tif".into(),
        "undrained_cells.csv".into(),
        "manifest-terrain-products.json".into(),
    ];
    if flow {
        names.push("flow_direction.tif".into());
    }
    names
}
fn check_collisions(spec: &TerrainSpec) -> Result<()> {
    super::execution::check_outputs(&spec.output_dir, &output_names(true), spec.overwrite)
}

fn output_raster<T: GdalType>(
    directory: &Path,
    name: &str,
    grid: &Grid,
    spec: &TerrainSpec,
    role: &str,
) -> Result<Dataset> {
    let mut ds = staging_raster::<T>(directory, name, grid)?;
    ds.set_metadata_item("role", role, "")?;
    ds.set_metadata_item(
        "routing_source",
        if spec.direction_source == DirectionSource::Dem {
            "dem"
        } else {
            "d8"
        },
        "",
    )?;
    ds.set_metadata_item(
        "ownership",
        "strict aggregated mini catchments; no buffer",
        "",
    )?;
    if name != "flow_direction.tif" {
        ds.set_metadata_item("units", "m", "")?;
        ensure!(
            unsafe {
                gdal_sys::GDALSetRasterUnitType(ds.rasterband(1)?.c_rasterband(), c"m".as_ptr())
            } == 0,
            "Cannot set raster units"
        );
    }
    if name == "ltnd.tif" {
        ds.set_metadata_item("distance_method", "geodesic", "")?;
    }
    if name == "flow_direction.tif" {
        ds.set_metadata_item("direction_codes", "0 drainage, 1-8 N NE E SE S SW W NW", "")?;
    }
    if spec.direction_source == DirectionSource::Dem {
        for (key, value) in [
            ("agree_sharp", format!("{:?}", spec.agree_sharp)),
            ("agree_smooth", format!("{:?}", spec.agree_smooth)),
            ("agree_buffer_pixels", spec.agree_buffer.to_string()),
        ] {
            ds.set_metadata_item(key, &value, "")?;
        }
    }
    Ok(ds)
}

fn write_patch<T: GdalType + Copy>(ds: &mut Dataset, patch: &Patch, values: &[T]) -> Result<()> {
    let w = patch.window;
    let position = (w.x as isize, w.y as isize);
    let size = (w.width, w.height);
    let mut band = ds.rasterband(1)?;
    let mut previous = band.read_as::<T>(position, size, size, None)?;
    for (i, &value) in values.iter().enumerate() {
        if patch.owned[i] {
            previous.data_mut()[i] = value;
        }
    }
    band.write(position, size, &mut previous)?;
    Ok(())
}

/// Create confined terrain products with complete-mini resident routing.
/// Replacements require `overwrite`; GDAL's shared cache is restored on exit.
pub fn create_terrain_dataset(spec: &TerrainSpec) -> Result<TerrainReport> {
    create_terrain_dataset_with_progress(spec, &|_| {})
}

pub fn create_terrain_dataset_with_progress(
    spec: &TerrainSpec,
    progress: super::execution::ProgressCallback<'_>,
) -> Result<TerrainReport> {
    let mut reporter = super::execution::Reporter::new(progress);
    let io_slots = super::execution::IoSlots::new(spec.io_slots)?;

    ensure!(
        spec.workers > 0 && spec.memory_limit_mb > 0,
        "Workers and memory limit must be positive"
    );
    ensure!(
        spec.routing_bytes_per_cell > 0,
        "--routing-bytes-per-cell must be positive"
    );
    ensure!(
        spec.agree_sharp.is_finite()
            && spec.agree_sharp >= 0.
            && spec.agree_smooth.is_finite()
            && spec.agree_smooth >= 0.,
        "AGREE depths must be finite and non-negative"
    );
    ensure!(
        spec.direction_source != DirectionSource::D8 || spec.d8.is_some(),
        "Explicit D8 raster required in D8 mode"
    );
    ensure!(
        spec.direction_source != DirectionSource::Dem || spec.d8.is_none(),
        "D8 raster requires D8 direction source"
    );
    check_collisions(spec)?;
    let mut spec = spec.clone();
    for path in [
        &mut spec.dem,
        &mut spec.grid_catchments,
        &mut spec.grid_segments,
    ] {
        *path = fs::canonicalize(&*path)
            .with_context(|| format!("Input unavailable: {}", path.display()))?;
    }
    if let Some(path) = &mut spec.d8 {
        *path = fs::canonicalize(&*path)?;
    }
    spec.output_dir = std::path::absolute(&spec.output_dir)?;
    let budget = spec
        .memory_limit_mb
        .checked_mul(MIB)
        .context("Memory budget overflow")?;
    let mut cache = CacheBudget::new()?;
    let datasets = open_inputs(&spec)?;
    let (grid, minis) = inspect(&spec, &datasets)?;
    drop(datasets);
    let coordinator = minis
        .len()
        .checked_mul(8192)
        .and_then(|bytes| bytes.checked_add(32 * MIB))
        .context("Coordinator allocation overflow")?;
    let largest = minis
        .iter()
        .try_fold((0, 0), |largest, mini| -> Result<_> {
            let bytes = routing_reservation(mini.window, spec.routing_bytes_per_cell)?;
            Ok(if bytes > largest.0 {
                (bytes, mini.id)
            } else {
                largest
            })
        })?;
    let cache_bytes = cache_allocation(budget, coordinator, largest.0).with_context(|| {
        format!(
            "Mini {} requires about {} MiB; increase --memory-limit-mb",
            largest.1,
            largest.0.div_ceil(MIB)
        )
    })?;
    cache.resize(cache_bytes)?;
    let available = budget - coordinator - cache_bytes;
    let workers = spec.workers.min(minis.len()).min(available / WORKER_BYTES);
    fs::create_dir_all(&spec.output_dir)?;
    spec.output_dir = fs::canonicalize(&spec.output_dir)?;
    check_collisions(&spec)?;
    let mut inputs = vec![
        spec.dem.as_path(),
        spec.grid_catchments.as_path(),
        spec.grid_segments.as_path(),
    ];
    inputs.extend(spec.d8.as_deref());
    super::execution::protect_inputs(&spec.output_dir, &output_names(true), &inputs)?;
    let mut input_files = vec![
        ("dem", spec.dem.as_path()),
        ("grid_catchments", spec.grid_catchments.as_path()),
        ("grid_segments", spec.grid_segments.as_path()),
    ];
    input_files.extend(spec.d8.as_deref().map(|path| ("d8", path)));
    let manifest_inputs = super::execution::manifest_files(&input_files)?;
    let staging = tempfile::tempdir_in(&spec.output_dir)?;
    let mut hand = output_raster::<f32>(
        staging.path(),
        "hand.tif",
        &grid,
        &spec,
        "height above matching drainage",
    )?;
    let mut ltnd = output_raster::<f32>(
        staging.path(),
        "ltnd.tif",
        &grid,
        &spec,
        "along-route distance to matching drainage",
    )?;
    let mut flow = if spec.write_flow_direction {
        Some(output_raster::<u8>(
            staging.path(),
            "flow_direction.tif",
            &grid,
            &spec,
            "canonical clockwise D8 direction",
        )?)
    } else {
        None
    };
    let mut validity = staging_raster::<u8>(staging.path(), "validity.tif", &grid)?;
    let mut reports = Vec::with_capacity(minis.len());
    // Nearby windows reuse GDAL cache tiles during the shared-raster patch writes.
    let state = Mutex::new(Admission {
        pending: mini_spatial_order(&minis).into(),
        bytes: 0,
        stopped: false,
        active: 0,
        peak: 0,
    });
    let ready = Condvar::new();
    reporter.enter("processing", "Routing mini basins");
    thread::scope(|scope| -> Result<()> {
        let _cancel = CancelWorkers {
            state: &state,
            ready: &ready,
        };
        let (sender, receiver) = mpsc::sync_channel(0);
        let mut handles = Vec::new();
        for _ in 0..workers {
            let sender = sender.clone();
            let spec = &spec;
            let grid = &grid;
            let minis = &minis;
            let state = &state;
            let ready = &ready;
            let io_slots = &io_slots;
            handles.push(scope.spawn(move || -> Result<()> {
                let datasets = match open_inputs(spec) {
                    Ok(d) => d,
                    Err(e) => {
                        let _ = sender.send((0, Err(e)));
                        return Ok(());
                    }
                };
                loop {
                    let (id, window, owned, bytes) = {
                        let mut admission = state
                            .lock()
                            .map_err(|_| anyhow::anyhow!("Terrain admission lock poisoned"))?;
                        loop {
                            if admission.stopped || admission.pending.is_empty() {
                                return Ok(());
                            }
                            let fitting = admission.pending.iter().position(|&ordinal| {
                                let mini = minis[ordinal];
                                let bytes =
                                    routing_reservation(mini.window, spec.routing_bytes_per_cell)
                                        .unwrap_or(usize::MAX);
                                bytes <= available - admission.bytes
                            });
                            if let Some(position) = fitting {
                                let ordinal = admission.pending.remove(position).unwrap();
                                let mini = minis[ordinal];
                                let bytes =
                                    routing_reservation(mini.window, spec.routing_bytes_per_cell)?;
                                admission.bytes += bytes;
                                admission.active += 1;
                                admission.peak = admission.peak.max(admission.active.min(workers));
                                break (mini.id, mini.window, mini.owned_cells, bytes);
                            }
                            if admission.active == 0 {
                                let mini = minis[admission.pending[0]];
                                let required =
                                    routing_reservation(mini.window, spec.routing_bytes_per_cell)?;
                                anyhow::bail!(
                                    "Mini {} requires about {} MiB; increase --memory-limit-mb",
                                    mini.id,
                                    required.div_ceil(MIB)
                                );
                            }
                            admission = ready
                                .wait(admission)
                                .map_err(|_| anyhow::anyhow!("Terrain admission lock poisoned"))?;
                        }
                    };
                    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        process_mini(spec, &datasets, grid, id, window, owned, io_slots)
                    }))
                    .map_err(|_| anyhow::anyhow!("Terrain worker panicked"))
                    .and_then(|r| r)
                    .with_context(|| format!("Route mini {id}"));
                    let failed = result.is_err();
                    let disconnected = sender.send((bytes, result)).is_err();
                    let mut admission = state
                        .lock()
                        .map_err(|_| anyhow::anyhow!("Terrain admission lock poisoned"))?;
                    if failed || disconnected {
                        admission.stopped = true;
                    }
                    ready.notify_all();
                    if failed || disconnected {
                        break;
                    }
                }
                Ok(())
            }));
        }
        drop(sender);
        let mut failure = None;
        for (bytes, result) in receiver {
            if failure.is_some() {
                drop(result);
                let mut admission = state
                    .lock()
                    .map_err(|_| anyhow::anyhow!("Terrain admission lock poisoned"))?;
                admission.bytes -= bytes;
                admission.active -= usize::from(bytes > 0);
                ready.notify_all();
                continue;
            }
            match result.and_then(|patch| {
                write_patch(&mut validity, &patch, &patch.valid)?;
                write_patch(&mut hand, &patch, &patch.hand)?;
                write_patch(&mut ltnd, &patch, &patch.ltnd)?;
                if let Some(flow) = &mut flow {
                    write_patch(flow, &patch, &patch.direction)?;
                }
                let report = (patch.id, patch.undrained, patch.total);
                reports.push(report);
                reporter.advance(reports.len(), Some(minis.len()));
                Ok(())
            }) {
                Ok(()) => {}
                Err(e) => {
                    failure = Some(e);
                    state
                        .lock()
                        .map_err(|_| anyhow::anyhow!("Terrain admission lock poisoned"))?
                        .stopped = true;
                    ready.notify_all();
                }
            }
            let mut admission = state
                .lock()
                .map_err(|_| anyhow::anyhow!("Terrain admission lock poisoned"))?;
            admission.bytes -= bytes;
            admission.active -= usize::from(bytes > 0);
            ready.notify_all();
        }
        for handle in handles {
            if let Err(e) = handle
                .join()
                .map_err(|_| anyhow::anyhow!("Terrain worker panicked"))
                .and_then(|r| r)
                && failure.is_none()
            {
                failure = Some(e);
            }
        }
        if let Some(e) = failure {
            return Err(e);
        }
        ensure!(reports.len() == minis.len(), "Incomplete terrain results");
        Ok(())
    })?;
    reporter.enter("finalizing", "Compressing terrain COGs");
    let workers_used = state
        .lock()
        .map_err(|_| anyhow::anyhow!("Terrain admission lock poisoned"))?
        .peak;
    reports.sort_unstable_by_key(|r| r.0);
    let mut csv = csv::Writer::from_path(staging.path().join("undrained_cells.csv"))?;
    csv.write_record([
        "mini_id",
        "undrained_cells",
        "total_cells",
        "percentage_undrained",
    ])?;
    for &(id, count, total) in &reports {
        if count > 0 {
            csv.write_record([
                id.to_string(),
                count.to_string(),
                total.to_string(),
                format!("{:?}", 100. * count as f64 / total as f64),
            ])?;
        }
    }
    csv.flush()?;
    validity.flush_cache()?;
    for (name, mut ds) in [
        ("hand.tif", Some(hand)),
        ("ltnd.tif", Some(ltnd)),
        ("flow_direction.tif", flow),
    ] {
        if let Some(ds) = ds.as_mut() {
            attach_mask(ds, &validity, &grid)?;
            ds.flush_cache()?;
            let resampling = (name != "flow_direction.tif").then_some("AVERAGE");
            io::finish_cog(ds, &staging.path().join(name), workers, resampling)?;
            let output = Dataset::open(staging.path().join(name))?;
            io::validate_raster(
                &output,
                &staging.path().join(name),
                &grid,
                if name == "flow_direction.tif" {
                    "Byte"
                } else {
                    "Float32"
                },
                name != "flow_direction.tif",
            )?;
        }
    }
    let mut product_files = vec![
        ("hand", "hand.tif"),
        ("ltnd", "ltnd.tif"),
        ("undrained_cells", "undrained_cells.csv"),
    ];
    if spec.write_flow_direction {
        product_files.push(("flow_direction", "flow_direction.tif"));
    }
    let (manifest, timings) = super::io::vector::finish(
        staging.path(),
        &spec.output_dir,
        super::io::vector::ManifestSpec {
            stage: "terrain-products",
            parameters: super::execution::manifest_parameters(
                &spec,
                &["dem", "grid_catchments", "grid_segments", "d8"],
            )?,
            inputs: manifest_inputs,
            products: product_files
                .iter()
                .map(|(key, filename)| ((*key).to_owned(), (*filename).to_owned()))
                .collect(),
            workers_used,
            overwrite: spec.overwrite,
            remove: if spec.write_flow_direction {
                vec![]
            } else {
                vec!["flow_direction.tif".into()]
            },
        },
        &mut reporter,
    )?;
    Ok(TerrainReport {
        timings,
        hand: spec.output_dir.join("hand.tif"),
        ltnd: spec.output_dir.join("ltnd.tif"),
        flow_direction: spec
            .write_flow_direction
            .then(|| spec.output_dir.join("flow_direction.tif")),
        undrained_cells: spec.output_dir.join("undrained_cells.csv"),
        manifest,
        mini_count: minis.len(),
        workers_used,
        undrained_count: reports.iter().map(|r| r.1).sum(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn admission_estimates_complete_mini_working_set() -> Result<()> {
        let window = Window {
            x: 0,
            y: 0,
            width: 1024,
            height: 1024,
        };
        let bytes = routing_reservation(window, DEFAULT_ROUTING_BYTES_PER_CELL)?;
        assert_eq!(
            bytes,
            1024 * 1024 * DEFAULT_ROUTING_BYTES_PER_CELL + WORKER_BYTES
        );
        assert_eq!(
            routing_reservation(window, 256)?,
            1024 * 1024 * 256 + WORKER_BYTES
        );
        assert!(bytes > 128 * MIB);
        let enormous = Window {
            width: usize::MAX,
            height: usize::MAX,
            ..window
        };
        assert!(routing_reservation(enormous, DEFAULT_ROUTING_BYTES_PER_CELL).is_err());
        Ok(())
    }

    #[test]
    fn mini_jobs_are_ordered_by_top_left_raster_tile() {
        let minis = [
            Mini {
                id: 2,
                window: Window {
                    x: 900,
                    y: 600,
                    width: 10,
                    height: 10,
                },
                owned_cells: 1,
            },
            Mini {
                id: 3,
                window: Window {
                    x: 600,
                    y: 600,
                    width: 10,
                    height: 10,
                },
                owned_cells: 1,
            },
            Mini {
                id: 1,
                window: Window {
                    x: 900,
                    y: 100,
                    width: 10,
                    height: 10,
                },
                owned_cells: 1,
            },
        ];

        assert_eq!(mini_spatial_order(&minis), [2, 0, 1]);
    }

    use serde_json::Value;
    pub(super) fn floats(value: &Value) -> Vec<f64> {
        value
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|r| {
                r.as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_f64().unwrap_or(f64::NAN))
            })
            .collect()
    }
    pub(super) fn bools(value: &Value) -> Vec<bool> {
        value
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|r| r.as_array().unwrap().iter().map(|v| v.as_bool().unwrap()))
            .collect()
    }
    fn compare(actual: &[f64], expected: &Value, rtol: f64, atol: f64, name: &str) {
        let expected = floats(expected);
        assert_eq!(actual.len(), expected.len(), "{name}");
        for (i, (&a, &b)) in actual.iter().zip(&expected).enumerate() {
            assert!(
                (a.is_nan() && b.is_nan()) || (a - b).abs() <= atol + rtol * b.abs(),
                "{name} cell {i}: {a} != {b}"
            );
        }
    }
    #[test]
    fn overlapping_windows_preserve_masks_after_cache_eviction() -> Result<()> {
        let _cache = CacheBudget::new()?;
        let directory = tempfile::tempdir()?;
        let grid = Grid {
            width: 3072,
            height: 2048,
            transform: [0., 1., 0., 2048., 0., -1.],
            wkt: gdal::spatial_ref::SpatialRef::from_epsg(3857)?.to_wkt()?,
        };
        let spec = TerrainSpec {
            overwrite: false,
            io_slots: 2,
            dem: PathBuf::new(),
            grid_catchments: PathBuf::new(),
            grid_segments: PathBuf::new(),
            output_dir: directory.path().to_owned(),
            direction_source: DirectionSource::Dem,
            d8: None,
            write_flow_direction: false,
            agree_sharp: 80.,
            agree_smooth: 8.,
            agree_buffer: 4,
            workers: 1,
            memory_limit_mb: 256,
            routing_bytes_per_cell: DEFAULT_ROUTING_BYTES_PER_CELL,
        };
        let mut ds = output_raster::<f32>(
            directory.path(),
            "hand.tif",
            &grid,
            &spec,
            "height above matching drainage",
        )?;
        let mut validity = staging_raster::<u8>(directory.path(), "validity.tif", &grid)?;
        let first = Window {
            x: 0,
            y: 0,
            width: 2688,
            height: 1900,
        };
        let second = Window {
            x: 300,
            y: 200,
            width: 2300,
            height: 1700,
        };
        for (window, divisor) in [(first, 13), (second, 17)] {
            let n = window.width * window.height;
            let patch = Patch {
                id: 1,
                window,
                owned: (0..n).map(|i| i % divisor == 0).collect(),
                valid: vec![255; n],
                hand: vec![1.; n],
                ltnd: Vec::new(),
                direction: Vec::new(),
                undrained: 0,
                total: 0,
            };
            write_patch(&mut validity, &patch, &patch.valid)?;
            write_patch(&mut ds, &patch, &patch.hand)?;
        }
        validity.flush_cache()?;
        attach_mask(&mut ds, &validity, &grid)?;
        let cog = directory.path().join("hand.tif");
        io::finish_cog(&ds, &cog, 1, None)?;
        let ds = Dataset::open(cog)?;
        for window in windows(Window {
            x: 0,
            y: 0,
            width: grid.width,
            height: grid.height,
        }) {
            let block = read(&ds, window)?;
            for i in 0..window.width * window.height {
                let x = window.x + i % window.width;
                let y = window.y + i / window.width;
                let expected = [(first, 13), (second, 17)].iter().any(|(w, d)| {
                    x >= w.x
                        && x < w.x + w.width
                        && y >= w.y
                        && y < w.y + w.height
                        && ((y - w.y) * w.width + x - w.x) % d == 0
                });
                assert_eq!(block.mask.data()[i] != 0, expected, "mask at {x},{y}");
                if expected {
                    assert_eq!(block.values.data()[i], 1., "value at {x},{y}");
                }
            }
        }
        Ok(())
    }

    #[test]
    fn frozen_synthetic_cases() -> Result<()> {
        let capture: Value = serde_json::from_str(include_str!(
            "../../tests/regression/synthetic/terrain.json"
        ))?;
        let cases = capture["cases"].as_array().unwrap();
        assert_eq!(cases.len(), 26);
        for case in cases {
            let name = case["name"].as_str().unwrap();
            match case["operation"].as_str().unwrap() {
                "agree" => {
                    let width = case["dem"][0].as_array().unwrap().len();
                    let z = floats(&case["dem"]);
                    let labels = floats(&case["labels"])
                        .iter()
                        .map(|&v| v as i32)
                        .collect::<Vec<_>>();
                    let p = &case["parameters"];
                    let actual = agree(
                        &z,
                        &labels,
                        &bools(&case["drainage"]),
                        width,
                        p["sharp"].as_f64().unwrap(),
                        p["smooth"].as_f64().unwrap(),
                        p["buffer"].as_u64().unwrap() as usize,
                    )?;
                    compare(&actual, &case["expected"], 0., 0., name);
                }
                "routing" | "ltnd" => {
                    let affine = floats(&serde_json::json!([case["transform"]]));
                    let t = [
                        affine[2], affine[0], affine[1], affine[5], affine[3], affine[4],
                    ];
                    let wkt = gdal::spatial_ref::SpatialRef::from_definition(
                        case["crs"].as_str().unwrap(),
                    )?
                    .to_wkt()?;
                    if case["operation"] == "routing" {
                        let width = case["dem"][0].as_array().unwrap().len();
                        let z = floats(&case["dem"]);
                        let labels = floats(&case["labels"])
                            .iter()
                            .map(|&v| v as i32)
                            .collect::<Vec<_>>();
                        let drainage = bools(&case["drainage"]);
                        let p = &case["agree"];
                        let conditioned = if p.is_null() {
                            z.clone()
                        } else {
                            agree(
                                &z,
                                &labels,
                                &drainage,
                                width,
                                p["sharp"].as_f64().unwrap(),
                                p["smooth"].as_f64().unwrap(),
                                p["buffer"].as_u64().unwrap() as usize,
                            )?
                        };
                        let (direction, rank) =
                            routing(&conditioned, &labels, &drainage, width, t[1], -t[5])
                                .with_context(|| name.to_owned())?;
                        compare(
                            &direction.iter().map(|&v| v as f64).collect::<Vec<_>>(),
                            &case["expected"]["direction"],
                            0.,
                            0.,
                            name,
                        );
                        compare(
                            &rank.iter().map(|&v| v as f64).collect::<Vec<_>>(),
                            &case["expected"]["rank"],
                            0.,
                            0.,
                            name,
                        );
                        let (hand, ltnd) = products(&z, &direction, &rank, width, t, &wkt)?;
                        compare(&hand, &case["expected"]["hand"], 0., 0., name);
                        compare(&ltnd, &case["expected"]["ltnd"], 1e-10, 1e-7, name);
                    } else {
                        let width = case["direction"][0].as_array().unwrap().len();
                        let direction = floats(&case["direction"])
                            .iter()
                            .map(|&v| v as i8)
                            .collect::<Vec<_>>();
                        let (rank, _) = ranks(&direction, width)?;
                        let (_, ltnd) = products(
                            &vec![0.; direction.len()],
                            &direction,
                            &rank,
                            width,
                            t,
                            &wkt,
                        )?;
                        compare(
                            &ltnd,
                            &case["expected"],
                            case["rtol"].as_f64().unwrap(),
                            case["atol"].as_f64().unwrap(),
                            name,
                        );
                    }
                }
                "validate-d8" => {
                    let width = case["direction"][0].as_array().unwrap().len();
                    let raw = floats(&case["direction"]);
                    let mask = bools(&case["mask"])
                        .iter()
                        .map(|&v| if v { 0 } else { 255 })
                        .collect::<Vec<_>>();
                    let result = validate_d8(
                        &raw,
                        &mask,
                        &bools(&case["owned"]),
                        &bools(&case["drainage"]),
                        width,
                    );
                    if let Some(expected) = case["expected_error"].as_str() {
                        assert!(
                            format!("{:#}", result.unwrap_err()).contains(expected),
                            "{name}"
                        );
                    } else {
                        let (direction, rank) = result?;
                        compare(
                            &direction.iter().map(|&v| v as f64).collect::<Vec<_>>(),
                            &case["expected"]["direction"],
                            0.,
                            0.,
                            name,
                        );
                        compare(
                            &rank.iter().map(|&v| v as f64).collect::<Vec<_>>(),
                            &case["expected"]["rank"],
                            0.,
                            0.,
                            name,
                        );
                    }
                }
                op => panic!("Unhandled operation {op}"),
            }
        }
        Ok(())
    }
}
