//! Mini-basin sampling: inputs, validation, native statistics, and stage coordination.
use super::{
    execution::{CACHE_BYTES, CacheBudget, MIB, publish},
    io::{self, read, spatial_ref, windows},
    model::{ATTRIBUTES, Attributes, Grid, Window},
};
use anyhow::{Context, Result, bail, ensure};
use gdal::{
    Dataset, Metadata,
    spatial_ref::{AxisMappingStrategy, CoordTransform, SpatialRef},
    vector::{LayerAccess, OGRFieldType},
};
use geos::{Geom, Geometry, GeometryTypes};
use serde::Serialize;
use std::{
    collections::{BTreeMap, VecDeque},
    fs::{self, File},
    path::{Path, PathBuf},
    sync::{Condvar, Mutex, mpsc},
    thread,
};
/// Explicit inputs to mini sampling. Raster values are already in metres.
#[derive(Debug, Clone, Serialize)]
pub struct SamplingSpec {
    pub mini_catchments: PathBuf,
    pub mini_segments: PathBuf,
    pub dem: PathBuf,
    pub grid_catchments: PathBuf,
    pub grid_segments: PathBuf,
    pub hand: PathBuf,
    pub ltnd: PathBuf,
    pub hru: PathBuf,
    pub output_dir: PathBuf,
    /// Maximum concurrent workers; actual concurrency may be lower to fit memory.
    pub workers: usize,
    /// Application allocation budget in MiB, not a hard process RSS ceiling.
    pub memory_limit_mb: usize,
    pub overwrite: bool,
    pub io_slots: usize,
}

impl SamplingSpec {
    pub(crate) fn rasters(&self) -> [&Path; 6] {
        [
            &self.dem,
            &self.grid_catchments,
            &self.grid_segments,
            &self.hand,
            &self.ltnd,
            &self.hru,
        ]
    }
}

/// Paths and counts produced by a successful sampling run.
#[derive(Debug)]
pub struct SamplingReport {
    pub sampled_minis: PathBuf,
    pub manifest: PathBuf,
    pub nodata_reports: Vec<PathBuf>,
    pub mini_count: usize,
    /// Peak number of concurrently admitted mini jobs.
    pub workers_used: usize,
    pub timings: super::execution::StageTimings,
}

pub(crate) const NODATA_NAMES: [&str; 5] = ["dem", "grid_segments", "hand", "ltnd", "hru"];
const WORKER_BYTES: usize = 32 * MIB;

#[derive(Debug)]
pub(crate) struct Mini {
    pub attributes: Attributes,
    pub longitude: f64,
    pub latitude: f64,
    pub reach_length: f64,
    pub window: Window,
    pub owned_cells: usize,
}

impl Mini {
    pub fn id(&self) -> i64 {
        self.attributes.integers[0]
    }
}

#[derive(Clone)]
pub(crate) struct Statistics {
    pub reach_slope: f64,
    pub reach_elevation: f64,
    pub tributary_length: f64,
    pub tributary_slope: f64,
    pub hru_counts: [u64; 100],
    pub flooded_area: [f64; 100],
}

#[derive(Clone)]
pub(crate) struct MiniResult {
    pub id: i64,
    pub total_cells: usize,
    pub nodata: [usize; 5],
    pub statistics: Option<Statistics>,
    pub failures: Vec<String>,
}
/// Sample explicit mini, terrain, and HRU inputs into deterministic CSV products.
///
/// Replacements require `overwrite`. A completed scan missing
/// required statistics publishes only nodata reports and returns an error.
/// The application budget is conservative, not a hard RSS limit. During the
/// run GDAL's process-wide block-cache limit is capped and restored on exit.
pub fn sample_minibasins(spec: &SamplingSpec) -> Result<SamplingReport> {
    sample_minibasins_with_progress(spec, &|_| {})
}

pub fn sample_minibasins_with_progress(
    spec: &SamplingSpec,
    progress: super::execution::ProgressCallback<'_>,
) -> Result<SamplingReport> {
    let mut reporter = super::execution::Reporter::new(progress);
    let io_slots = super::execution::IoSlots::new(spec.io_slots)?;

    ensure!(
        spec.workers > 0 && spec.memory_limit_mb > 0,
        "Workers and memory limit must be positive"
    );
    let budget = spec
        .memory_limit_mb
        .checked_mul(MIB)
        .context("Memory budget overflow")?;
    ensure!(
        budget >= CACHE_BYTES + WORKER_BYTES,
        "Memory budget needs at least 48 MiB for GIS cache and raster windows"
    );
    check_collisions(&spec.output_dir, spec.overwrite)?;
    let mut spec = spec.clone();
    for path in [
        &mut spec.mini_catchments,
        &mut spec.mini_segments,
        &mut spec.dem,
        &mut spec.grid_catchments,
        &mut spec.grid_segments,
        &mut spec.hand,
        &mut spec.ltnd,
        &mut spec.hru,
    ] {
        *path = fs::canonicalize(&*path)
            .with_context(|| format!("Input unavailable: {}", path.display()))?;
    }
    spec.output_dir = std::path::absolute(&spec.output_dir)?;
    let mut inputs: Vec<_> = spec.rasters().into_iter().map(Path::to_owned).collect();
    inputs.extend([spec.mini_catchments.clone(), spec.mini_segments.clone()]);
    let _output = super::execution::OutputDirectory::new(&spec.output_dir)?;
    spec.output_dir = fs::canonicalize(&spec.output_dir)?;
    check_collisions(&spec.output_dir, spec.overwrite)?;
    let input_refs: Vec<_> = inputs.iter().map(PathBuf::as_path).collect();
    super::execution::protect_inputs(&spec.output_dir, &sampling_names(), &input_refs)?;
    let manifest_inputs = super::execution::manifest_files(&[
        ("mini_catchments", &spec.mini_catchments),
        ("mini_segments", &spec.mini_segments),
        ("dem", &spec.dem),
        ("grid_catchments", &spec.grid_catchments),
        ("grid_segments", &spec.grid_segments),
        ("hand", &spec.hand),
        ("ltnd", &spec.ltnd),
        ("hru", &spec.hru),
    ])?;
    let staging = tempfile::tempdir_in(&spec.output_dir)?;
    let cache_bytes = (budget / 4).clamp(CACHE_BYTES, 8 * 1024 * MIB);
    let _cache = CacheBudget::with_limit(cache_bytes)?;
    let (grid, minis) = inspect(&spec)?;
    let coordinator = minis
        .len()
        .checked_mul(8192)
        .and_then(|v| v.checked_add(cache_bytes))
        .context("Coordinator allocation overflow")?;
    let available = budget
        .checked_sub(coordinator)
        .context("Memory budget cannot hold mini metadata")?;
    ensure!(
        available >= WORKER_BYTES + 128 * 1024,
        "Memory budget cannot hold sampling windows"
    );
    let block_size = if available >= 256 * MIB {
        1024
    } else {
        io::BLOCK
    };
    let worker_bytes = if block_size == 1024 {
        80 * MIB
    } else {
        WORKER_BYTES
    };
    ensure!(
        available >= worker_bytes,
        "Memory budget cannot hold one sampling worker"
    );
    let workers = spec.workers.min(minis.len()).min(available / worker_bytes);
    let rasters = SamplingRasters {
        datasets: open_rasters_threaded(&spec, (workers / spec.io_slots).clamp(1, 4))?
            .into_iter()
            .map(std::sync::Mutex::new)
            .collect(),
        slots: &io_slots,
        cpus: super::execution::IoSlots::new(workers)?,
        decode_threads: (workers / spec.io_slots).clamp(1, 4),
        block_size,
    };
    reporter.enter("processing", "Sampling mini basins");
    let mut output = SampleOutput::new(staging.path())?;
    let workers_used = sample_minis_bounded(
        &grid,
        &minis,
        available,
        worker_bytes,
        workers,
        &reporter,
        &mut output,
        |mini, areas| sample_mini(mini, &rasters, areas),
    )?;
    reporter.enter("finalizing", "Writing samples and diagnostics");
    let (mut files, failure) = output.finish(staging.path(), minis.len())?;
    if let Some(message) = failure {
        publish(
            staging.path(),
            &spec.output_dir,
            &files,
            spec.overwrite,
            &sampling_names()
                .into_iter()
                .filter(|name| !files.contains(name))
                .collect::<Vec<_>>(),
        )?;
        if files.is_empty() {
            bail!("{message}");
        }
        bail!(
            "{message}. Nodata reports saved in {}",
            spec.output_dir.display()
        );
    }
    let nodata_reports = files
        .iter()
        .map(|name| spec.output_dir.join(name))
        .collect();
    let mut product_files = vec![("sampled_minis".to_owned(), "sampled_minis.csv".to_owned())];
    product_files.extend(files.iter().map(|name| {
        (
            format!(
                "diagnostics/{}",
                name.trim_start_matches("nodata_").trim_end_matches(".csv")
            ),
            name.clone(),
        )
    }));
    files.push("sampled_minis.csv".into());
    let remove: Vec<_> = sampling_names()
        .into_iter()
        .filter(|name| name != "manifest-sample-minis.json" && !files.contains(name))
        .collect();
    let (manifest, timings) = super::io::vector::finish(
        staging.path(),
        &spec.output_dir,
        super::io::vector::ManifestSpec {
            stage: "sample-minis",
            parameters: super::execution::manifest_parameters(
                &spec,
                &[
                    "mini_catchments",
                    "mini_segments",
                    "dem",
                    "grid_catchments",
                    "grid_segments",
                    "hand",
                    "ltnd",
                    "hru",
                ],
            )?,
            inputs: manifest_inputs,
            products: product_files,
            workers_used,
            overwrite: spec.overwrite,
            remove,
        },
        &mut reporter,
    )?;
    Ok(SamplingReport {
        timings,
        sampled_minis: spec.output_dir.join("sampled_minis.csv"),
        manifest,
        nodata_reports,
        mini_count: minis.len(),
        workers_used,
    })
}

struct SampleAdmission {
    pending: VecDeque<usize>,
    reservations: BTreeMap<usize, usize>,
    bytes: usize,
    active: usize,
    peak: usize,
    stopped: bool,
}

fn sample_reservation(mini: &Mini, worker_bytes: usize) -> Result<usize> {
    let accumulator_buffers = mini
        .owned_cells
        .checked_mul(3 * std::mem::size_of::<f64>())
        .context("Mini sampling accumulator allocation overflow")?;
    accumulator_buffers
        .checked_add(worker_bytes)
        .and_then(|bytes| bytes.checked_add(2048))
        .context("Mini sampling allocation overflow")
}

fn ensure_sample_fits(mini: &Mini, available: usize, worker_bytes: usize) -> Result<usize> {
    let required = sample_reservation(mini, worker_bytes)?;
    ensure!(
        required <= available,
        "Mini {} requires about {} MiB for sampling; increase --memory-limit-mb",
        mini.id(),
        required.div_ceil(MIB)
    );
    Ok(required)
}

#[allow(clippy::too_many_arguments)]
fn sample_minis_bounded(
    grid: &Grid,
    minis: &[Mini],
    available: usize,
    worker_bytes: usize,
    workers: usize,
    reporter: &super::execution::Reporter<'_>,
    output: &mut SampleOutput,
    job: impl Fn(&Mini, &io::CellAreas) -> Result<MiniResult> + Sync,
) -> Result<usize> {
    let reservation = |ordinal: usize| sample_reservation(&minis[ordinal], worker_bytes);
    for mini in minis {
        ensure_sample_fits(mini, available, worker_bytes)?;
    }
    // Reuse terrain's tile order so nearby minis share decoded GDAL blocks.
    let mut order: Vec<_> = (0..minis.len()).collect();
    order.sort_unstable_by_key(|&i| {
        (
            minis[i].window.y / io::BLOCK,
            minis[i].window.x / io::BLOCK,
            minis[i].id(),
        )
    });
    let state = Mutex::new(SampleAdmission {
        pending: order.into(),
        reservations: BTreeMap::new(),
        bytes: 0,
        active: 0,
        peak: 0,
        stopped: false,
    });
    let ready = Condvar::new();
    thread::scope(|scope| -> Result<usize> {
        let (sender, receiver) = mpsc::sync_channel(0);
        let mut handles = Vec::new();
        for _ in 0..workers {
            let sender = sender.clone();
            let state = &state;
            let ready = &ready;
            let job = &job;
            handles.push(scope.spawn(move || {
                let run = || -> Result<()> {
                    let areas = io::CellAreas::new(grid)?;
                    loop {
                        let (ordinal, bytes) = {
                            let mut admission = state.lock().map_err(|_| {
                                anyhow::anyhow!("Sampling admission lock poisoned")
                            })?;
                            loop {
                                if admission.stopped || admission.pending.is_empty() {
                                    return Ok(());
                                }
                                let ordinal = *admission.pending.front().unwrap();
                                let bytes = reservation(ordinal)?;
                                if bytes <= available.saturating_sub(admission.bytes) {
                                    ensure!(
                                        !admission.reservations.contains_key(&ordinal),
                                        "Sampling mini {ordinal} already has a reservation"
                                    );
                                    admission.reservations.insert(ordinal, bytes);
                                    admission.pending.pop_front();
                                    admission.bytes += bytes;
                                    admission.active += 1;
                                    admission.peak = admission.peak.max(admission.active.min(workers));
                                    break (ordinal, bytes);
                                }
                                if admission.active == 0 {
                                    ensure_sample_fits(&minis[ordinal], available, worker_bytes)?;
                                    bail!(
                                        "Cannot admit mini {} under the current sampling memory reservations",
                                        minis[ordinal].id()
                                    );
                                }
                                admission = ready.wait(admission).map_err(|_| {
                                    anyhow::anyhow!("Sampling admission lock poisoned")
                                })?;
                            }
                        };
                        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            job(&minis[ordinal], &areas)
                        }))
                            .map_err(|_| anyhow::anyhow!("Sampling worker panicked"))
                            .and_then(|result| result)
                            .with_context(|| format!("Sample mini {}", minis[ordinal].id()));
                        let failed = result.is_err();
                        if failed {
                            if let Ok(mut admission) = state.lock() {
                                admission.stopped = true;
                            }
                            ready.notify_all();
                        }
                        let disconnected = sender.send((ordinal, bytes, result)).is_err();
                        if disconnected && let Ok(mut admission) = state.lock() {
                            admission.stopped = true;
                        }
                        ready.notify_all();
                        if failed || disconnected {
                            return Ok(());
                        }
                    }
                };
                if let Err(error) = run() {
                    if let Ok(mut admission) = state.lock() {
                        admission.stopped = true;
                    }
                    let _ = sender.send((usize::MAX, 0, Err(error)));
                    ready.notify_all();
                }
            }));
        }
        drop(sender);
        let mut error = None;
        let mut completed = 0;
        let mut processed = 0;
        // Compact results fit the existing 8192-byte-per-mini coordinator reserve.
        let mut pending = BTreeMap::<usize, MiniResult>::new();
        for (ordinal, bytes, result) in receiver {
            release_sample(&state, &ready, ordinal, bytes)?;
            if error.is_some() {
                continue;
            }
            match result {
                Ok(result) => {
                    processed += 1;
                    reporter.advance(processed, Some(minis.len()));
                    pending.insert(ordinal, result);
                    while let Some(result) = pending.remove(&completed) {
                        if let Err(e) = output.add(&minis[completed], result) {
                            error = Some(e);
                            break;
                        }
                        completed += 1;
                    }
                }
                Err(e) => error = Some(e),
            }
            if error.is_some() {
                state
                    .lock()
                    .map_err(|_| anyhow::anyhow!("Sampling admission lock poisoned"))?
                    .stopped = true;
                ready.notify_all();
                pending.clear();
            }
        }
        for handle in handles {
            if handle.join().is_err() && error.is_none() {
                error = Some(anyhow::anyhow!("Sampling worker panicked"));
            }
        }
        let outstanding = state
            .lock()
            .map_err(|_| anyhow::anyhow!("Sampling admission lock poisoned"))?
            .reservations
            .clone();
        if !outstanding.is_empty() {
            if error.is_none() {
                error = Some(anyhow::anyhow!(
                    "Sampling workers ended with outstanding mini reservations"
                ));
            }
            for (ordinal, bytes) in outstanding {
                release_sample(&state, &ready, ordinal, bytes)?;
            }
        }
        let admission = state
            .lock()
            .map_err(|_| anyhow::anyhow!("Sampling admission lock poisoned"))?;
        let peak = admission.peak;
        ensure!(
            admission.bytes == 0 && admission.active == 0 && admission.reservations.is_empty(),
            "Sampling results left memory reservations outstanding"
        );
        drop(admission);
        if let Some(error) = error {
            return Err(error);
        }
        ensure!(completed == minis.len(), "Incomplete sampling results");
        Ok(peak)
    })
}

fn release_sample_reservation(
    admission: &mut SampleAdmission,
    ordinal: usize,
    bytes: usize,
) -> Result<()> {
    if bytes == 0 {
        return Ok(());
    }
    ensure!(
        admission.reservations.remove(&ordinal) == Some(bytes),
        "Sampling mini {ordinal} reservation was not held"
    );
    admission.bytes = admission
        .bytes
        .checked_sub(bytes)
        .context("Sampling reservation accounting underflow")?;
    admission.active = admission
        .active
        .checked_sub(1)
        .context("Sampling active job accounting underflow")?;
    Ok(())
}

fn release_sample(
    state: &Mutex<SampleAdmission>,
    ready: &Condvar,
    ordinal: usize,
    bytes: usize,
) -> Result<()> {
    if bytes == 0 {
        return Ok(());
    }
    let mut admission = state
        .lock()
        .map_err(|_| anyhow::anyhow!("Sampling admission lock poisoned"))?;
    release_sample_reservation(&mut admission, ordinal, bytes)?;
    ready.notify_all();
    Ok(())
}

struct SamplingRasters<'a> {
    datasets: Vec<std::sync::Mutex<Dataset>>,
    slots: &'a super::execution::IoSlots,
    cpus: super::execution::IoSlots,
    decode_threads: usize,
    block_size: usize,
}
impl SamplingRasters<'_> {
    fn read_one(&self, index: usize, window: Window) -> Result<io::RasterBlock> {
        let dataset = self.datasets[index]
            .lock()
            .map_err(|_| anyhow::anyhow!("Raster read lock poisoned"))?;
        let _io = self.slots.acquire()?;
        let _cpu = self.cpus.acquire_many(self.decode_threads)?;
        read(&dataset, window)
    }
    fn read(&self, window: Window, id: i64) -> Result<Option<Vec<io::RasterBlock>>> {
        let owners = self.read_one(1, window)?;
        if !(0..window.width * window.height).any(|cell| owners.value(cell) == Some(id as f64)) {
            return Ok(None);
        }
        let mut owners = Some(owners);
        (0..self.datasets.len())
            .map(|index| {
                if index == 1 {
                    Ok(owners.take().unwrap())
                } else {
                    self.read_one(index, window)
                }
            })
            .collect::<Result<Vec<_>>>()
            .map(Some)
    }
}

enum MiniLookup {
    Dense(Vec<usize>),
    Sparse(BTreeMap<i64, usize>),
}
impl MiniLookup {
    fn new(minis: &[Mini]) -> Self {
        let maximum = minis.iter().map(Mini::id).max().unwrap_or(0);
        if maximum <= minis.len().saturating_mul(4) as i64 {
            let mut ids = vec![usize::MAX; maximum as usize + 1];
            for (i, mini) in minis.iter().enumerate() {
                ids[mini.id() as usize] = i;
            }
            Self::Dense(ids)
        } else {
            Self::Sparse(
                minis
                    .iter()
                    .enumerate()
                    .map(|(i, mini)| (mini.id(), i))
                    .collect(),
            )
        }
    }
    fn get(&self, id: i64) -> Option<usize> {
        match self {
            Self::Dense(ids) => usize::try_from(id)
                .ok()
                .and_then(|id| ids.get(id).copied())
                .filter(|&id| id != usize::MAX),
            Self::Sparse(ids) => ids.get(&id).copied(),
        }
    }
}

fn sample_mini(
    mini: &Mini,
    rasters: &SamplingRasters<'_>,
    areas: &io::CellAreas,
) -> Result<MiniResult> {
    let mut accumulator = Accumulator::new(mini.owned_cells);
    for window in mini_windows(mini, rasters.block_size) {
        let Some(blocks) = rasters.read(window, mini.id())? else {
            continue;
        };
        let _cpu = rasters.cpus.acquire()?;
        let mut row_area = None;
        for cell in 0..window.width * window.height {
            if blocks[1].value(cell) != Some(mini.id() as f64) {
                continue;
            }
            let values = [0, 2, 3, 4, 5].map(|i| blocks[i].value(cell));
            let row = window.y + cell / window.width;
            let area = if values[2].is_some() {
                if areas.is_geographic() {
                    if row_area.is_none_or(|(r, _)| r != row) {
                        row_area = Some((row, areas.area(window.x, row)?));
                    }
                    row_area.unwrap().1
                } else {
                    areas.area(window.x + cell % window.width, row)?
                }
            } else {
                0.
            };
            accumulator.add(mini.id(), values, area)?;
        }
    }
    ensure!(
        accumulator.total_cells == mini.owned_cells,
        "Mini {} ownership changed during sampling",
        mini.id()
    );
    accumulator.finish(mini.id(), mini.reach_length)
}

fn mini_windows(mini: &Mini, size: usize) -> impl Iterator<Item = Window> {
    io::windows_sized(mini.window, size)
}

fn check_collisions(output: &Path, overwrite: bool) -> Result<()> {
    super::execution::check_outputs(output, &sampling_names(), overwrite)
}
fn sampling_names() -> Vec<String> {
    [
        "sampled_minis.csv".into(),
        "manifest-sample-minis.json".into(),
    ]
    .into_iter()
    .chain(NODATA_NAMES.map(|n| format!("nodata_{n}.csv")))
    .collect()
}

// Keep floating columns recognizably floating even when every value is integral.
fn float(value: f64) -> String {
    format!("{value:?}")
}

struct SampleOutput {
    raw: csv::Writer<File>,
    nodata: Vec<csv::Writer<File>>,
    nodata_present: [bool; 5],
    classes: [bool; 100],
    failure: Option<String>,
    failure_count: usize,
    results: usize,
}

impl SampleOutput {
    fn new(directory: &Path) -> Result<Self> {
        let headers = sample_headers(0..100);
        let mut raw = csv::Writer::from_path(directory.join(".sampled_minis.raw.csv"))?;
        raw.write_record(headers)?;
        let mut nodata = Vec::with_capacity(NODATA_NAMES.len());
        for name in NODATA_NAMES {
            let mut writer = csv::Writer::from_path(directory.join(format!("nodata_{name}.csv")))?;
            writer.write_record([
                "mini_id",
                "nodata_cells",
                "total_cells",
                "percentage_nodata",
            ])?;
            nodata.push(writer);
        }
        Ok(Self {
            raw,
            nodata,
            nodata_present: [false; 5],
            classes: [false; 100],
            failure: None,
            failure_count: 0,
            results: 0,
        })
    }

    fn add(&mut self, mini: &Mini, result: MiniResult) -> Result<()> {
        ensure!(mini.id() == result.id, "Sampling result ID mismatch");
        self.results += 1;
        for (i, &count) in result.nodata.iter().enumerate() {
            if count == 0 {
                continue;
            }
            self.nodata_present[i] = true;
            self.nodata[i].write_record([
                result.id.to_string(),
                count.to_string(),
                result.total_cells.to_string(),
                float(100. * count as f64 / result.total_cells as f64),
            ])?;
        }
        let Some(statistics) = result.statistics else {
            self.failure_count += 1;
            if self.failure.is_none() {
                self.failure = result
                    .failures
                    .into_iter()
                    .next()
                    .or_else(|| Some(format!("Mini {} has incomplete statistics", mini.id())));
            }
            return Ok(());
        };
        for (present, &count) in self.classes.iter_mut().zip(&statistics.hru_counts) {
            *present |= count > 0;
        }
        self.raw
            .write_record(sample_row(mini, &statistics, 0..100))?;
        Ok(())
    }

    fn finish(
        mut self,
        directory: &Path,
        mini_count: usize,
    ) -> Result<(Vec<String>, Option<String>)> {
        self.raw.flush()?;
        drop(self.raw);
        for writer in &mut self.nodata {
            writer.flush()?;
        }
        drop(self.nodata);
        let files: Vec<_> = NODATA_NAMES
            .iter()
            .enumerate()
            .filter(|(i, _)| self.nodata_present[*i])
            .map(|(_, name)| format!("nodata_{name}.csv"))
            .collect();
        for (i, name) in NODATA_NAMES.iter().enumerate() {
            if !self.nodata_present[i] {
                fs::remove_file(directory.join(format!("nodata_{name}.csv")))?;
            }
        }
        if self.failure_count == 0 {
            ensure!(self.results == mini_count, "Incomplete sampling results");
            write_sampled(directory, &self.classes)?;
        } else {
            fs::remove_file(directory.join(".sampled_minis.raw.csv"))?;
        }
        let failure = self.failure.map(|message| {
            if self.failure_count > 1 {
                format!(
                    "{message} ({} additional mini failures)",
                    self.failure_count - 1
                )
            } else {
                message
            }
        });
        Ok((files, failure))
    }
}

fn sample_headers(classes: impl Iterator<Item = usize>) -> Vec<String> {
    let mut headers: Vec<_> = ATTRIBUTES.iter().map(|name| (*name).to_owned()).collect();
    headers.extend(
        [
            "longitude",
            "latitude",
            "reach_slope",
            "reach_elevation",
            "tributary_length",
            "tributary_slope",
        ]
        .map(str::to_owned),
    );
    headers.extend(classes.map(|id| format!("hru_{}", id + 1)));
    headers.extend((1..=100).map(|stage| format!("flooded_area_{stage}")));
    headers
}

fn sample_row(
    mini: &Mini,
    stats: &Statistics,
    classes: impl Iterator<Item = usize>,
) -> Vec<String> {
    let mut row: Vec<_> = mini
        .attributes
        .integers
        .iter()
        .map(i64::to_string)
        .collect();
    row.extend(mini.attributes.metrics.iter().map(|v| float(*v)));
    row.extend(
        [
            mini.longitude,
            mini.latitude,
            stats.reach_slope,
            stats.reach_elevation,
            stats.tributary_length,
            stats.tributary_slope,
        ]
        .map(float),
    );
    let total = stats.hru_counts.iter().sum::<u64>() as f64;
    row.extend(classes.map(|i| float(100. * stats.hru_counts[i] as f64 / total)));
    row.extend(stats.flooded_area.map(float));
    row
}

fn write_sampled(directory: &Path, classes: &[bool; 100]) -> Result<()> {
    let raw_path = directory.join(".sampled_minis.raw.csv");
    let mut reader = csv::Reader::from_path(&raw_path)?;
    let headers = reader.headers()?.clone();
    let keep: Vec<_> = headers
        .iter()
        .enumerate()
        .filter_map(|(i, header)| {
            let class = header
                .strip_prefix("hru_")
                .and_then(|value| value.parse::<usize>().ok());
            class
                .is_none_or(|class| (1..=100).contains(&class) && classes[class - 1])
                .then_some(i)
        })
        .collect();
    let mut writer = csv::Writer::from_path(directory.join("sampled_minis.csv"))?;
    writer.write_record(keep.iter().map(|&i| &headers[i]))?;
    for row in reader.records() {
        let row = row?;
        writer.write_record(keep.iter().map(|&i| &row[i]))?;
    }
    writer.flush()?;
    fs::remove_file(raw_path)?;
    Ok(())
}

fn inspect(spec: &SamplingSpec) -> Result<(Grid, Vec<Mini>)> {
    let datasets = open_rasters(spec)?;
    let dem = &datasets[0];
    let grid = io::canonical_grid(dem)?;
    for (i, source) in datasets.iter().enumerate() {
        io::validate_raster(
            source,
            spec.rasters()[i],
            &grid,
            if matches!(i, 0 | 3 | 4) {
                "Float32"
            } else {
                "Int32"
            },
            matches!(i, 0 | 3 | 4),
        )?;
    }
    ensure!(
        datasets[4].metadata_item("distance_method", "").as_deref() == Some("geodesic"),
        "LTND must declare distance_method=geodesic"
    );
    let index = io::mini_windows(&datasets[1], &grid)?;
    let catchments = read_vectors(&spec.mini_catchments, &grid, false)?;
    let segments = read_vectors(&spec.mini_segments, &grid, true)?;
    ensure!(
        catchments.len() == index.len() && segments.len() == index.len(),
        "Vector IDs and mini_index differ"
    );
    let mut minis = Vec::with_capacity(index.len());
    for (id, window) in index {
        let catchment = catchments
            .get(&id)
            .with_context(|| format!("Missing catchment mini {id}"))?;
        let segment = segments
            .get(&id)
            .with_context(|| format!("Missing segment mini {id}"))?;
        ensure!(
            catchment.0.integers == segment.0.integers
                && catchment.0.metrics[1..] == segment.0.metrics[1..],
            "Mini {id} catchment and segment attributes differ"
        );
        minis.push(Mini {
            attributes: catchment.0.clone(),
            longitude: segment.1[0],
            latitude: segment.1[1],
            reach_length: segment.0.metrics[0],
            window,
            owned_cells: 0,
        });
    }
    validate_ownership(&datasets, &grid, &mut minis)?;
    Ok((grid, minis))
}

fn open_rasters(spec: &SamplingSpec) -> Result<Vec<Dataset>> {
    open_rasters_threaded(spec, 1)
}
fn open_rasters_threaded(spec: &SamplingSpec, threads: usize) -> Result<Vec<Dataset>> {
    let option = format!("NUM_THREADS={threads}");
    spec.rasters()
        .into_iter()
        .map(|path| {
            Ok(Dataset::open_ex(
                path,
                gdal::DatasetOptions {
                    open_options: Some(&[option.as_str()]),
                    ..Default::default()
                },
            )?)
        })
        .collect()
}

fn read_vectors(
    path: &Path,
    grid: &Grid,
    segments: bool,
) -> Result<BTreeMap<i64, (Attributes, [f64; 2])>> {
    let dataset = Dataset::open(path).with_context(|| format!("Open vector {}", path.display()))?;
    ensure!(
        dataset.layer_count() == 1,
        "Mini vector must contain exactly one layer"
    );
    let mut layer = dataset.layer(0)?;
    let source = spatial_ref(&grid.wkt)?;
    let mut vector_crs = layer
        .spatial_ref()
        .context("Mini vector must declare a CRS")?;
    vector_crs.set_axis_mapping_strategy(AxisMappingStrategy::TraditionalGisOrder);
    ensure!(
        vector_crs == source,
        "Vector CRS differs from DEM: {}",
        path.display()
    );
    let schema: Vec<_> = layer
        .defn()
        .fields()
        .map(|f| (f.name(), f.field_type()))
        .collect();
    ensure!(
        schema.len() == ATTRIBUTES.len()
            && schema
                .iter()
                .enumerate()
                .all(|(i, (name, ty))| name == ATTRIBUTES[i]
                    && *ty
                        == if i < 4 {
                            OGRFieldType::OFTInteger64
                        } else {
                            OGRFieldType::OFTReal
                        }),
        "Invalid mini vector schema: {} ({schema:?})",
        path.display()
    );
    let mut target = SpatialRef::from_epsg(4326)?;
    target.set_axis_mapping_strategy(AxisMappingStrategy::TraditionalGisOrder);
    let transform = CoordTransform::new(&source, &target)?;
    let mut result = BTreeMap::new();
    for feature in layer.features() {
        let mut integers = [0; 4];
        let mut metrics = [0.; 4];
        for (i, integer) in integers.iter_mut().enumerate() {
            *integer = feature
                .field_as_integer64(i)?
                .context("Null mini integer attribute")?;
        }
        for (i, metric) in metrics.iter_mut().enumerate() {
            *metric = feature
                .field_as_double(i + 4)?
                .context("Null mini metric attribute")?;
        }
        let id = integers[0];
        ensure!(
            id > 0 && metrics.iter().all(|v| v.is_finite()),
            "Mini {id} has invalid attributes"
        );
        let ogr = feature.geometry().context("Mini has no geometry")?;
        let geometry = Geometry::new_from_wkb(&ogr.wkb()?)?;
        let allowed = if segments {
            [GeometryTypes::LineString, GeometryTypes::MultiLineString]
        } else {
            [GeometryTypes::Polygon, GeometryTypes::MultiPolygon]
        };
        ensure!(
            !geometry.is_empty()?
                && geometry.is_valid()?
                && allowed.contains(&geometry.geometry_type()?),
            "Mini {id} has invalid geometry"
        );
        let mut xy = [0.; 2];
        if segments {
            ensure!(
                metrics[0] > 0. && geometry.length()? > 0.,
                "Mini {id} has zero or invalid reach length"
            );
            let point = geometry.point_on_surface()?;
            let mut x = [point.get_x()?];
            let mut y = [point.get_y()?];
            transform.transform_coords(&mut x, &mut y, &mut [])?;
            xy = [x[0], y[0]];
            ensure!(
                xy.iter().all(|v| v.is_finite()),
                "Mini {id} has invalid representative coordinates"
            );
        }
        ensure!(
            result
                .insert(id, (Attributes { integers, metrics }, xy))
                .is_none(),
            "Duplicate mini ID {id}"
        );
    }
    ensure!(!result.is_empty(), "Mini vector is empty");
    Ok(result)
}

fn validate_ownership(datasets: &[Dataset], grid: &Grid, minis: &mut [Mini]) -> Result<()> {
    let ids = MiniLookup::new(minis);
    let mut bounds = vec![(usize::MAX, usize::MAX, 0, 0); minis.len()];
    for window in windows(Window {
        x: 0,
        y: 0,
        width: grid.width,
        height: grid.height,
    }) {
        let owners = read(&datasets[1], window)?;
        let segments = read(&datasets[2], window)?;
        for cell in 0..window.width * window.height {
            // Partial segment coverage is sampled and reported rather than rejected.
            let Some(owner) = owners.value(cell) else {
                ensure!(
                    segments.value(cell).is_none(),
                    "Segment validity exists outside catchment ownership"
                );
                continue;
            };
            let id = owner as i64;
            let index = ids
                .get(id)
                .with_context(|| format!("Ownership contains unknown mini {id}"))?;
            if let Some(segment) = segments.value(cell) {
                ensure!(
                    segment == 0. || segment == owner,
                    "Positive segment ownership mismatch for mini {id}"
                );
            }
            let x = window.x + cell % window.width;
            let y = window.y + cell / window.width;
            let mini = &mut minis[index];
            ensure!(
                x >= mini.window.x
                    && x < mini.window.x + mini.window.width
                    && y >= mini.window.y
                    && y < mini.window.y + mini.window.height,
                "Ownership lies outside mini {id} index bounds"
            );
            mini.owned_cells += 1;
            let b = &mut bounds[index];
            b.0 = b.0.min(x);
            b.1 = b.1.min(y);
            b.2 = b.2.max(x + 1);
            b.3 = b.3.max(y + 1);
        }
    }
    for (mini, bounds) in minis.iter().zip(bounds) {
        let w = mini.window;
        ensure!(
            mini.owned_cells > 0 && bounds == (w.x, w.y, w.x + w.width, w.y + w.height),
            "Mini {} index bounds are not tight ownership bounds",
            mini.id()
        );
    }
    Ok(())
}

struct Accumulator {
    pub total_cells: usize,
    pub nodata: [usize; 5],
    dem: Vec<f64>,
    paired: Vec<(f64, f64)>,
    hru: [u64; 100],
    floods: [f64; 101],
    hand_count: usize,
}

impl Accumulator {
    pub fn new(cells: usize) -> Self {
        Self {
            total_cells: 0,
            nodata: [0; 5],
            dem: Vec::with_capacity(cells),
            paired: Vec::with_capacity(cells),
            hru: [0; 100],
            floods: [0.; 101],
            hand_count: 0,
        }
    }

    /// Values are DEM, segment ID, HAND, LTND, HRU; absent means masked or NaN.
    pub fn add(&mut self, id: i64, values: [Option<f64>; 5], area: f64) -> Result<()> {
        self.total_cells += 1;
        for (i, value) in values.iter().enumerate() {
            if value.is_none() {
                self.nodata[i] += 1;
            }
            ensure!(
                value.is_none_or(f64::is_finite),
                "Mini {id} contains infinite sampled values"
            );
        }
        let [dem, segment, hand, ltnd, hru] = values;
        if let Some(segment) = segment {
            ensure!(
                segment == 0. || segment == id as f64,
                "Mini {id} has mismatched positive segment ownership"
            );
            if segment == id as f64
                && let Some(dem) = dem
            {
                self.dem.push(dem);
            }
        }
        if let (Some(hand), Some(ltnd)) = (hand, ltnd) {
            self.paired.push((hand, ltnd));
        }
        if let Some(hru) = hru {
            ensure!(
                (1. ..=100.).contains(&hru) && hru.fract() == 0.,
                "Mini {id} contains invalid HRU class {hru}"
            );
            self.hru[hru as usize - 1] += 1;
        }
        if let Some(hand) = hand {
            ensure!(
                area.is_finite() && area > 0.,
                "Mini {id} has invalid geodesic pixel area"
            );
            self.hand_count += 1;
            let bin = if hand <= 1. {
                0
            } else if hand > 100. {
                100
            } else {
                hand.ceil() as usize - 1
            };
            self.floods[bin] += area;
        }
        Ok(())
    }

    pub fn finish(mut self, id: i64, reach_length: f64) -> Result<MiniResult> {
        let mut failures = Vec::new();
        for (valid, name) in [
            (self.hru.iter().sum::<u64>() > 0, "HRU percentages"),
            (self.hand_count > 0, "HAND flooded areas"),
            (!self.dem.is_empty(), "DEM reach statistics"),
            (!self.paired.is_empty(), "paired HAND/LTND statistics"),
        ] {
            if !valid {
                failures.push(format!("Mini {id} has no valid data for {name}"));
            }
        }
        let maximum = self
            .paired
            .iter()
            .map(|(_, ltnd)| *ltnd)
            .fold(f64::NEG_INFINITY, f64::max);
        if !self.paired.is_empty() && maximum <= 0. {
            failures.push(format!("Mini {id} has non-positive maximum LTND"));
        }
        let mut statistics = if failures.is_empty() {
            self.dem.sort_unstable_by(f64::total_cmp);
            let mut sum = 0.;
            let mut count = 0;
            for (hand, ltnd) in self.paired {
                if (ltnd - maximum).abs() <= 1e-8 + 1e-5 * maximum.abs() {
                    sum += hand;
                    count += 1;
                }
            }
            let mut cumulative = 0.;
            let flooded_area = std::array::from_fn(|i| {
                cumulative += self.floods[i];
                cumulative
            });
            Some(Statistics {
                reach_elevation: percentile(&self.dem, 0.5),
                reach_slope: (percentile(&self.dem, 0.85) - percentile(&self.dem, 0.1))
                    / (0.75 * reach_length),
                tributary_length: maximum / 1000.,
                tributary_slope: (sum / count as f64) / (maximum / 1000.),
                hru_counts: self.hru,
                flooded_area,
            })
        } else {
            None
        };
        if statistics.as_ref().is_some_and(|s| {
            ![
                s.reach_slope,
                s.reach_elevation,
                s.tributary_length,
                s.tributary_slope,
            ]
            .into_iter()
            .chain(s.flooded_area)
            .all(f64::is_finite)
        }) {
            failures.push(format!("Mini {id} produced non-finite statistics"));
            statistics = None;
        }
        Ok(MiniResult {
            id,
            total_cells: self.total_cells,
            nodata: self.nodata,
            statistics,
            failures,
        })
    }
}

fn percentile(sorted: &[f64], fraction: f64) -> f64 {
    let position = (sorted.len() - 1) as f64 * fraction;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower as f64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Barrier};

    fn mini(id: i64) -> Mini {
        Mini {
            attributes: Attributes {
                integers: [id, -1, 0, 1],
                metrics: [1.; 4],
            },
            longitude: 0.,
            latitude: 0.,
            reach_length: 1.,
            window: Window {
                x: id as usize,
                y: 0,
                width: 1,
                height: 1,
            },
            owned_cells: 1,
        }
    }

    #[test]
    fn spatial_sampling_releases_working_memory_and_keeps_output_order() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let minis: Vec<_> = (1..=6)
            .map(|id| {
                let mut mini = mini(id);
                mini.window.x = (6 - id) as usize * io::BLOCK;
                mini
            })
            .collect();
        let grid = Grid {
            transform: [0., 0.01, 0., 0., 0., -0.01],
            width: 6 * io::BLOCK,
            height: 1,
            wkt: SpatialRef::from_epsg(4326)?.to_wkt()?,
        };
        let bytes = sample_reservation(&minis[0], 1024)?;
        let mut output = SampleOutput::new(directory.path())?;
        let visited = Mutex::new(Vec::new());
        let progress = Mutex::new(Vec::new());
        let record_progress = |event: super::super::execution::StageProgress| {
            if event.phase == "processing" && event.total.is_some() {
                progress.lock().unwrap().push(event.completed);
            }
        };
        let mut reporter = super::super::execution::Reporter::new(&record_progress);
        reporter.enter("processing", "Sampling mini basins");
        assert_eq!(
            sample_minis_bounded(
                &grid,
                &minis,
                bytes,
                1024,
                1,
                &reporter,
                &mut output,
                |mini, _| {
                    visited.lock().unwrap().push(mini.id());
                    Ok(MiniResult {
                        id: mini.id(),
                        total_cells: 1,
                        nodata: [1; 5],
                        statistics: None,
                        failures: Vec::new(),
                    })
                },
            )?,
            1
        );
        assert_eq!(*visited.lock().unwrap(), [6, 5, 4, 3, 2, 1]);
        assert_eq!(*progress.lock().unwrap(), [1, 2, 3, 4, 5, 6]);
        output.finish(directory.path(), minis.len())?;
        let mut reader = csv::Reader::from_path(directory.path().join("nodata_dem.csv"))?;
        let ids = reader
            .records()
            .map(|row| Ok(row?[0].parse::<i64>()?))
            .collect::<Result<Vec<_>>>()?;
        assert_eq!(ids, [1, 2, 3, 4, 5, 6]);
        Ok(())
    }

    fn injected_job_failure(failure_id: i64, panic: bool) {
        let directory = tempfile::tempdir().unwrap();
        let minis: Vec<_> = (0..4).map(mini).collect();
        let grid = Grid {
            transform: [0., 0.01, 0., 0., 0., -0.01],
            width: 8,
            height: 8,
            wkt: SpatialRef::from_epsg(4326).unwrap().to_wkt().unwrap(),
        };
        let bytes = sample_reservation(&minis[0], 1024).unwrap();
        let mut output = SampleOutput::new(directory.path()).unwrap();
        let no_progress = |_| {};
        let reporter = super::super::execution::Reporter::new(&no_progress);
        let barrier = Arc::new(Barrier::new(minis.len()));
        let gate = Arc::new((Mutex::new(false), Condvar::new()));
        let result = sample_minis_bounded(
            &grid,
            &minis,
            bytes * minis.len(),
            1024,
            minis.len(),
            &reporter,
            &mut output,
            |mini, _areas| {
                barrier.wait();
                if mini.id() == 0 && failure_id != 0 {
                    let (lock, ready) = &*gate;
                    let mut failed = lock.lock().unwrap();
                    while !*failed {
                        failed = ready.wait(failed).unwrap();
                    }
                }
                if mini.id() == failure_id {
                    let (lock, ready) = &*gate;
                    *lock.lock().unwrap() = true;
                    ready.notify_all();
                    if panic {
                        panic!("injected sampling panic");
                    }
                    bail!("injected sampling error");
                }
                Ok(MiniResult {
                    id: mini.id(),
                    total_cells: 1,
                    nodata: [0; 5],
                    statistics: None,
                    failures: Vec::new(),
                })
            },
        );
        let error = result.unwrap_err();
        let error = format!("{error:#}");
        assert!(
            error.contains(&format!("Sample mini {failure_id}")),
            "{error}"
        );
        assert!(
            error.contains(if panic {
                "Sampling worker panicked"
            } else {
                "injected sampling error"
            }),
            "{error}"
        );
    }

    #[test]
    fn admitted_sampling_panics_stop_workers_and_release_reservations() {
        injected_job_failure(0, true);
        injected_job_failure(2, true);
    }

    #[test]
    fn admitted_sampling_errors_stop_workers_and_release_reservations() {
        injected_job_failure(2, false);
    }

    #[test]
    fn exact_percentiles_ties_and_flood_thresholds() {
        assert_eq!(percentile(&[0., 10.], 0.85), 8.5);
        assert_eq!(percentile(&[7.], 0.1), 7.);
        let mut acc = Accumulator::new(4);
        for (dem, hand, ltnd, hru) in [
            (0., -1., 1000., 1.),
            (10., 1.1, 999.995, 2.),
            (20., 100., 2., 2.),
            (30., 101., 1., 2.),
        ] {
            acc.add(
                12,
                [Some(dem), Some(12.), Some(hand), Some(ltnd), Some(hru)],
                0.1,
            )
            .unwrap();
        }
        let stats = acc.finish(12, 2.).unwrap().statistics.unwrap();
        assert_eq!(stats.reach_elevation, 15.);
        assert_eq!(stats.reach_slope, 15.);
        assert!((stats.tributary_slope - 0.05).abs() < 1e-15);
        assert_eq!(stats.hru_counts[..2], [1, 3]);
        assert_eq!(stats.flooded_area[0], 0.1);
        assert_eq!(stats.flooded_area[1], 0.2);
        assert!((stats.flooded_area[99] - 0.3).abs() < 1e-15);
    }
    #[test]
    fn oversized_mini_reservation_is_reported_without_overflow() {
        let mini = Mini {
            attributes: Attributes {
                integers: [1; 4],
                metrics: [1.; 4],
            },
            longitude: 0.,
            latitude: 0.,
            reach_length: 1.,
            window: Window {
                x: 0,
                y: 0,
                width: 20,
                height: 30,
            },
            owned_cells: 1_000,
        };
        assert_eq!(
            sample_reservation(&mini, 32 * MIB).unwrap(),
            32 * MIB + 24_000 + 2048
        );
        let required = sample_reservation(&mini, 32 * MIB).unwrap();
        let error = ensure_sample_fits(&mini, required - 1, 32 * MIB).unwrap_err();
        assert!(format!("{error:#}").contains("Mini 1 requires about 33 MiB"));

        let mut sparse = mini;
        sparse.window.width = usize::MAX;
        sparse.window.height = 2;
        sparse.owned_cells = 1;
        assert_eq!(
            sample_reservation(&sparse, 32 * MIB).unwrap(),
            32 * MIB + 24 + 2048
        );
        assert!(sample_reservation(&sparse, usize::MAX).is_err());

        let mut overflow = sparse;
        overflow.owned_cells = usize::MAX / 24 + 1;
        assert!(sample_reservation(&overflow, 32 * MIB).is_err());
    }

    #[test]
    fn mini_tiles_cover_only_unaligned_mini_bounds() {
        let mini = Mini {
            attributes: Attributes {
                integers: [1; 4],
                metrics: [1.; 4],
            },
            longitude: 0.,
            latitude: 0.,
            reach_length: 1.,
            window: Window {
                x: 5,
                y: 3,
                width: 7,
                height: 6,
            },
            owned_cells: 42,
        };
        let mut coverage = vec![0; mini.window.width * mini.window.height];
        let tiles: Vec<_> = mini_windows(&mini, 4).collect();
        for tile in &tiles {
            assert!(tile.x >= mini.window.x && tile.y >= mini.window.y);
            assert!(tile.x + tile.width <= mini.window.x + mini.window.width);
            assert!(tile.y + tile.height <= mini.window.y + mini.window.height);
            for y in tile.y..tile.y + tile.height {
                for x in tile.x..tile.x + tile.width {
                    coverage[(y - mini.window.y) * mini.window.width + (x - mini.window.x)] += 1;
                }
            }
        }
        assert_eq!(tiles.len(), 4);
        assert!(coverage.into_iter().all(|count| count == 1));
    }

    #[test]
    fn missing_pairs_and_invalid_values() {
        let mut acc = Accumulator::new(2);
        acc.add(1, [Some(1.), Some(1.), None, Some(1.), Some(1.)], 1.)
            .unwrap();
        acc.add(1, [Some(1.), Some(1.), Some(2.), None, Some(1.)], 1.)
            .unwrap();
        let result = acc.finish(1, 1.).unwrap();
        assert!(result.statistics.is_none());
        assert_eq!(result.nodata, [0, 0, 1, 1, 0]);
        assert!(result.failures[0].contains("paired"));
        for values in [
            [Some(f64::INFINITY), None, None, None, None],
            [None, None, None, None, Some(101.)],
            [None, Some(2.), None, None, None],
        ] {
            assert!(Accumulator::new(1).add(1, values, 1.).is_err());
        }
        let mut acc = Accumulator::new(2);
        for dem in [0., 10.] {
            acc.add(1, [Some(dem), Some(1.), Some(1.), Some(1.), Some(1.)], 1.)
                .unwrap();
        }
        let result = acc.finish(1, f64::MIN_POSITIVE).unwrap();
        assert!(result.statistics.is_none());
        assert!(result.failures[0].contains("non-finite"));
    }
}
