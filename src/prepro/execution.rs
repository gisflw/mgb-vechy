// Shared GIS cache and temporary-output helpers.
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::{Condvar, Mutex, MutexGuard},
    time::{Duration, Instant},
};
pub(crate) const MIB: usize = 1024 * 1024;
pub(crate) const CACHE_BYTES: usize = 16 * MIB;
static CACHE_LOCK: Mutex<()> = Mutex::new(());

pub(crate) fn cache_allocation(
    budget: usize,
    coordinator: usize,
    work_item: usize,
) -> Result<usize> {
    let available = budget
        .checked_sub(coordinator)
        .and_then(|bytes| bytes.checked_sub(work_item))
        .context("Memory budget cannot hold coordinator and one complete work item")?;
    ensure!(
        available >= CACHE_BYTES,
        "Memory budget cannot hold coordinator, one complete work item, and 16 MiB GDAL cache"
    );
    Ok((budget / 4)
        .clamp(CACHE_BYTES, 8 * 1024 * MIB)
        .min(available))
}

pub(crate) struct CacheBudget {
    previous: i64,
    _lock: MutexGuard<'static, ()>,
}
impl CacheBudget {
    pub(crate) fn new() -> Result<Self> {
        Self::with_limit(CACHE_BYTES)
    }
    pub(crate) fn with_limit(bytes: usize) -> Result<Self> {
        let limit = bytes.try_into().context("GDAL cache limit exceeds int64")?;
        // Unwinding restores the GDAL limit through Drop, so a later run can recover.
        let lock = CACHE_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // GDAL's block cache is process-wide; serialize our changes and restore it on exit.
        let previous = unsafe { gdal_sys::GDALGetCacheMax64() };
        unsafe { gdal_sys::GDALSetCacheMax64(limit) };
        Ok(Self {
            previous,
            _lock: lock,
        })
    }
    pub(crate) fn resize(&mut self, bytes: usize) -> Result<()> {
        let limit = bytes.try_into().context("GDAL cache limit exceeds int64")?;
        unsafe { gdal_sys::GDALSetCacheMax64(limit) };
        Ok(())
    }
}

impl Drop for CacheBudget {
    fn drop(&mut self) {
        unsafe { gdal_sys::GDALSetCacheMax64(self.previous) };
    }
}

/// Remove a directory created by this run if no products were published.
pub(crate) struct OutputDirectory {
    path: std::path::PathBuf,
    created: bool,
}
impl OutputDirectory {
    pub fn new(path: &Path) -> Result<Self> {
        let created = !path.try_exists()?;
        fs::create_dir_all(path)?;
        Ok(Self {
            path: path.to_owned(),
            created,
        })
    }
}
impl Drop for OutputDirectory {
    fn drop(&mut self) {
        if self.created {
            let _ = fs::remove_dir(&self.path);
        }
    }
}

pub(crate) fn check_outputs(output: &Path, names: &[String], overwrite: bool) -> Result<()> {
    for name in names {
        let path = output.join(name);
        match path.symlink_metadata() {
            Ok(metadata) => {
                ensure!(
                    !metadata.is_dir(),
                    "Output path is a directory: {}",
                    path.display()
                );
                ensure!(
                    overwrite,
                    "Output already exists: {}; use --overwrite to replace stage outputs",
                    path.display()
                );
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

/// Optional rasters belong to preparation only when recorded by its manifest.
pub(crate) fn preparation_optional(output: &Path) -> Result<Vec<String>> {
    let path = output.join("manifest-prepare.json");
    match path.symlink_metadata() {
        Ok(metadata) if metadata.file_type().is_symlink() => return Ok(Vec::new()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error.into()),
        _ => {}
    }
    let manifest: serde_json::Value = serde_json::from_reader(fs::File::open(path)?)?;
    ensure!(
        manifest["step"] == "prepare",
        "Invalid preparation manifest stage"
    );
    let mut names = Vec::new();
    if let Some(outputs) = manifest["outputs"].as_object() {
        if let Some(record) = outputs.get("d8") {
            validate_manifest_output(record, "d8.tif", output)?;
            names.push("d8.tif".to_owned());
        }
        if let Some(rasters) = outputs.get("rasters").and_then(Value::as_object) {
            for (name, record) in rasters {
                validate_raster_name(name)?;
                let filename = format!("{name}.tif");
                validate_manifest_output(record, &filename, output)?;
                names.push(filename);
            }
        }
    } else {
        let parameters = &manifest["parameters"];
        if !parameters["d8"].is_null() {
            names.push("d8.tif".to_owned());
        }
        if let Some(rasters) = parameters["rasters"].as_array() {
            for raster in rasters {
                let name = raster["name"]
                    .as_str()
                    .context("Invalid preparation raster manifest")?;
                validate_raster_name(name)?;
                names.push(format!("{name}.tif"));
            }
        }
    }
    Ok(names)
}

fn validate_raster_name(name: &str) -> Result<()> {
    ensure!(
        name.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
            && name.bytes().all(|b| {
                b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-'
            }),
        "Invalid preparation raster name"
    );
    Ok(())
}

fn validate_manifest_output(record: &Value, filename: &str, output: &Path) -> Result<()> {
    let path = Path::new(
        record["path"]
            .as_str()
            .context("Invalid preparation output manifest path")?,
    );
    ensure!(
        path.is_absolute()
            && path.file_name().and_then(|name| name.to_str()) == Some(filename)
            && path.parent() == Some(output),
        "Invalid preparation output filename: {}",
        path.display()
    );
    Ok(())
}

pub(crate) fn protect_inputs(output: &Path, names: &[String], inputs: &[&Path]) -> Result<()> {
    for name in names {
        let target = output.join(name);
        if target
            .symlink_metadata()
            .is_ok_and(|m| !m.file_type().is_symlink())
        {
            let canonical = fs::canonicalize(&target)?;
            ensure!(
                !inputs.iter().any(|input| **input == canonical),
                "Output would replace an input: {}",
                target.display()
            );
        }
    }
    Ok(())
}

pub(crate) fn publish(
    staging: &Path,
    output: &Path,
    names: &[String],
    overwrite: bool,
    remove: &[String],
) -> Result<()> {
    publish_transaction(staging, output, names, None, overwrite, remove, || Ok(()))
}

pub(crate) fn publish_with_manifest(
    staging: &Path,
    output: &Path,
    products: &[String],
    manifest: &str,
    overwrite: bool,
    remove: &[String],
    write_manifest: impl FnOnce() -> Result<()>,
) -> Result<()> {
    publish_transaction(
        staging,
        output,
        products,
        Some(manifest),
        overwrite,
        remove,
        write_manifest,
    )
}

fn publish_transaction(
    staging: &Path,
    output: &Path,
    products: &[String],
    manifest: Option<&str>,
    overwrite: bool,
    remove: &[String],
    write_manifest: impl FnOnce() -> Result<()>,
) -> Result<()> {
    let names: Vec<_> = products
        .iter()
        .cloned()
        .chain(manifest.map(str::to_owned))
        .collect();
    let affected: Vec<_> = names.iter().chain(remove).cloned().collect();
    check_outputs(output, &affected, overwrite)?;
    for name in products {
        ensure!(
            staging.join(name).is_file(),
            "Missing staged output: {name}"
        );
    }
    let backup = tempfile::tempdir_in(output)?;
    let mut moved = Vec::new();
    let mut published = Vec::new();
    let result = (|| -> Result<()> {
        if overwrite {
            for name in &affected {
                let target = output.join(name);
                if target.symlink_metadata().is_ok() {
                    fs::rename(&target, backup.path().join(name))?;
                    moved.push(name);
                }
            }
        }
        for name in products {
            let target = output.join(name);
            fs::hard_link(staging.join(name), &target)
                .with_context(|| format!("Publish {}", target.display()))?;
            published.push(target);
        }
        if let Some(manifest) = manifest {
            write_manifest()?;
            let target = output.join(manifest);
            fs::hard_link(staging.join(manifest), &target)
                .with_context(|| format!("Publish {}", target.display()))?;
            published.push(target);
        }
        Ok(())
    })();
    if let Err(error) = result {
        let mut rollback_errors = Vec::new();
        for path in published {
            if let Err(error) = fs::remove_file(&path) {
                rollback_errors.push(format!("Remove {}: {error}", path.display()));
            }
        }
        for name in moved {
            if let Err(error) = fs::rename(backup.path().join(name), output.join(name)) {
                rollback_errors.push(format!("Restore {name}: {error}"));
            }
        }
        if !rollback_errors.is_empty() {
            let recovery = backup.keep();
            return Err(error).context(format!(
                "Rollback incomplete ({}); recovery files retained in {}",
                rollback_errors.join("; "),
                recovery.display()
            ));
        }
        return Err(error);
    }
    Ok(())
}

pub(crate) fn manifest_files(entries: &[(&str, &Path)]) -> Result<Value> {
    let mut files = Map::new();
    for (key, path) in entries {
        let path = std::path::absolute(path)?;
        insert_file(
            &mut files,
            key,
            serde_json::json!({
                "path": path.to_string_lossy(),
                "sha256": checksum_path(&path)?
            }),
        )?;
    }
    Ok(Value::Object(files))
}

pub(crate) fn manifest_outputs(
    staging: &Path,
    output: &Path,
    entries: &[(&str, &str)],
) -> Result<Value> {
    let mut files = Map::new();
    for (key, filename) in entries {
        let staged = staging.join(filename);
        let path = output.join(filename);
        insert_file(
            &mut files,
            key,
            serde_json::json!({
                "path": std::path::absolute(&path)?.to_string_lossy(),
                "sha256": checksum_path(&staged)?
            }),
        )?;
    }
    Ok(Value::Object(files))
}

fn insert_file(map: &mut Map<String, Value>, key: &str, value: Value) -> Result<()> {
    if let Some((parent, leaf)) = key.rsplit_once('/') {
        ensure!(
            !parent.is_empty() && !leaf.is_empty(),
            "Invalid manifest file key"
        );
        let entry = map
            .entry(parent.to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        return insert_file(
            entry
                .as_object_mut()
                .context("Conflicting manifest file keys")?,
            leaf,
            value,
        );
    }
    ensure!(!key.is_empty(), "Empty manifest file key");
    ensure!(!map.contains_key(key), "Duplicate manifest file key: {key}");
    map.insert(key.to_owned(), value);
    Ok(())
}

pub(crate) fn manifest_parameters<T: Serialize>(spec: &T, paths: &[&str]) -> Result<Value> {
    let mut parameters = serde_json::to_value(spec)?;
    let object = std::mem::take(
        parameters
            .as_object_mut()
            .context("Stage parameters must serialize as an object")?,
    );
    let removed: std::collections::BTreeSet<_> = paths
        .iter()
        .copied()
        .chain(["output_dir", "overwrite"])
        .collect();
    let mut ordered = Map::new();
    for (key, mut value) in object {
        if removed.contains(key.as_str()) {
            continue;
        }
        if key == "rasters"
            && let Some(rasters) = value.as_array_mut()
        {
            for raster in rasters {
                if let Some(fields) = raster.as_object_mut() {
                    *fields = std::mem::take(fields)
                        .into_iter()
                        .filter(|(field, _)| field != "path")
                        .collect();
                }
            }
        }
        ordered.insert(key, value);
    }
    parameters = Value::Object(ordered);
    Ok(parameters)
}

fn checksum_path(path: &Path) -> Result<String> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        anyhow::bail!("Cannot checksum a symlink: {}", path.display());
    }
    if metadata.is_file() {
        return checksum_file(path).map(|digest| hex_digest(&digest));
    }
    ensure!(
        metadata.is_dir(),
        "Unsupported input type: {}",
        path.display()
    );
    let mut files = Vec::new();
    collect_directory_files(path, path, &mut files)?;
    files.sort_by(|a, b| a.0.cmp(&b.0));
    let mut hash = Sha256::new();
    for (relative, file) in files {
        let bytes = relative.as_bytes();
        hash.update((bytes.len() as u64).to_be_bytes());
        hash.update(bytes);
        hash.update(checksum_file(&file)?);
    }
    Ok(hex_digest(&hash.finalize()))
}

fn hex_digest(digest: &[u8]) -> String {
    use std::fmt::Write;
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

fn collect_directory_files(
    root: &Path,
    current: &Path,
    files: &mut Vec<(String, PathBuf)>,
) -> Result<()> {
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            anyhow::bail!(
                "Cannot checksum a directory containing a symlink: {}",
                path.display()
            );
        } else if file_type.is_dir() {
            collect_directory_files(root, &path, files)?;
        } else if file_type.is_file() {
            let relative = path
                .strip_prefix(root)?
                .to_str()
                .context("Directory input has a non-UTF-8 filename")?
                .replace('\\', "/");
            files.push((relative, path));
        } else {
            anyhow::bail!("Unsupported directory entry: {}", path.display());
        }
    }
    Ok(())
}

fn checksum_file(path: &Path) -> Result<[u8; 32]> {
    let mut file = fs::File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hash.update(&buffer[..read]);
    }
    Ok(hash.finalize().into())
}

/// A coordinator progress update. Totals are absent during open-ended scans.
#[derive(Clone, Debug)]
pub struct StageProgress {
    pub phase: &'static str,
    pub operation: &'static str,
    pub completed: usize,
    pub total: Option<usize>,
    pub elapsed_seconds: f64,
    pub timings: StageTimings,
}

pub type ProgressCallback<'a> = &'a (dyn Fn(StageProgress) + Sync);

/// Non-overlapping wall-clock phase timings, in seconds.
#[derive(Clone, Debug, Default, Serialize)]
pub struct StageTimings {
    pub preparing: f64,
    pub processing: f64,
    pub finalizing: f64,
    pub total: f64,
}

#[derive(Clone, Debug)]
pub(crate) struct RuntimeSnapshot {
    pub ran_at: String,
    pub timezone: String,
    pub timings: StageTimings,
    pub workers_used: usize,
    pub peak_ram_usage_mib: Option<f64>,
    pub peak_cpu_usage_percent: Option<f64>,
    pub cpu_time_seconds: Option<f64>,
}

#[derive(Default)]
struct ResourceMetrics {
    peak_ram_bytes: Option<u64>,
    peak_cpu_percent: Option<f32>,
    latest_cpu_ms: Option<u64>,
}

struct ResourceMonitor {
    metrics: std::sync::Arc<Mutex<ResourceMetrics>>,
    start_cpu_ms: Option<u64>,
    stop: Option<std::sync::mpsc::Sender<()>>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl ResourceMonitor {
    fn new() -> Self {
        use sysinfo::{Pid, ProcessRefreshKind, ProcessesToUpdate, System};
        let pid = Pid::from_u32(std::process::id());
        let mut system = System::new();
        system.refresh_processes_specifics(
            ProcessesToUpdate::Some(&[pid]),
            true,
            ProcessRefreshKind::nothing().with_cpu().with_memory(),
        );
        let start_cpu_ms = system
            .process(pid)
            .map(|process| process.accumulated_cpu_time());
        let mut initial = ResourceMetrics::default();
        if let Some(process) = system.process(pid) {
            initial.peak_ram_bytes = Some(process.memory());
            initial.latest_cpu_ms = Some(process.accumulated_cpu_time());
        }
        let metrics = std::sync::Arc::new(Mutex::new(initial));
        let (stop, receiver) = std::sync::mpsc::channel();
        let shared = metrics.clone();
        let thread = std::thread::Builder::new()
            .name("mgb-resource-monitor".into())
            .spawn(move || {
                let mut samples = 1;
                let mut sampled = Instant::now();
                loop {
                    let stopping = receiver.recv_timeout(Duration::from_millis(250)).is_ok();
                    let cpu_due = !stopping || sampled.elapsed() >= Duration::from_millis(200);
                    system.refresh_processes_specifics(
                        ProcessesToUpdate::Some(&[pid]),
                        true,
                        ProcessRefreshKind::nothing().with_cpu().with_memory(),
                    );
                    if let Some(process) = system.process(pid)
                        && let Ok(mut metrics) = shared.lock()
                    {
                        metrics.peak_ram_bytes = Some(
                            metrics
                                .peak_ram_bytes
                                .unwrap_or_default()
                                .max(process.memory()),
                        );
                        metrics.latest_cpu_ms = Some(process.accumulated_cpu_time());
                        if cpu_due {
                            samples += 1;
                            if samples >= 2 {
                                metrics.peak_cpu_percent = Some(
                                    metrics
                                        .peak_cpu_percent
                                        .unwrap_or_default()
                                        .max(process.cpu_usage()),
                                );
                            }
                        }
                    }
                    if stopping {
                        break;
                    }
                    sampled = Instant::now();
                }
            })
            .ok();
        Self {
            metrics,
            start_cpu_ms,
            stop: thread.as_ref().map(|_| stop),
            thread,
        }
    }

    fn finish(&mut self) -> (Option<f64>, Option<f64>, Option<f64>) {
        let Some(thread) = self.thread.take() else {
            return (None, None, None);
        };
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if thread.join().is_err() {
            return (None, None, None);
        }
        let Ok(metrics) = self.metrics.lock() else {
            return (None, None, None);
        };
        (
            metrics
                .peak_ram_bytes
                .map(|bytes| bytes as f64 / (1024. * 1024.)),
            metrics.peak_cpu_percent.map(f64::from),
            self.start_cpu_ms
                .zip(metrics.latest_cpu_ms)
                .map(|(start, end)| end.saturating_sub(start) as f64 / 1000.),
        )
    }
}

impl Drop for ResourceMonitor {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

pub(crate) struct Reporter<'a> {
    callback: ProgressCallback<'a>,
    started: Instant,
    boundary: Instant,
    phase: &'static str,
    operation: &'static str,
    timings: StageTimings,
    ran_at: String,
    timezone: String,
    resources: ResourceMonitor,
    finished: bool,
}

impl<'a> Reporter<'a> {
    pub fn new(callback: ProgressCallback<'a>) -> Self {
        let now = Instant::now();
        let date_time = chrono::Local::now();
        let reporter = Self {
            callback,
            started: now,
            boundary: now,
            phase: "preparing",
            operation: "Preparing inputs",
            timings: StageTimings::default(),
            ran_at: date_time.format("%Y-%m-%d %H:%M").to_string(),
            timezone: date_time.format("%:z").to_string(),
            resources: ResourceMonitor::new(),
            finished: false,
        };
        reporter.advance(0, None);
        reporter
    }
    pub fn enter(&mut self, phase: &'static str, operation: &'static str) {
        self.record();
        self.phase = phase;
        self.operation = operation;
        self.advance(0, None);
    }
    pub fn advance(&self, completed: usize, total: Option<usize>) {
        let now = Instant::now();
        let mut timings = self.timings.clone();
        let seconds = now.duration_since(self.boundary).as_secs_f64();
        match self.phase {
            "preparing" => timings.preparing += seconds,
            "processing" => timings.processing += seconds,
            "finalizing" => timings.finalizing += seconds,
            _ => unreachable!(),
        }
        timings.total = now.duration_since(self.started).as_secs_f64();
        (self.callback)(StageProgress {
            phase: self.phase,
            operation: self.operation,
            completed,
            total,
            elapsed_seconds: timings.total,
            timings,
        });
    }
    fn record(&mut self) {
        let seconds = self.boundary.elapsed().as_secs_f64();
        match self.phase {
            "preparing" => self.timings.preparing += seconds,
            "processing" => self.timings.processing += seconds,
            "finalizing" => self.timings.finalizing += seconds,
            _ => unreachable!(),
        }
        self.boundary = Instant::now();
    }
    pub fn finish(&mut self, workers_used: usize) -> RuntimeSnapshot {
        let (ram, cpu, cpu_time) = self.resources.finish();
        if !self.finished {
            self.record();
            self.timings.total = self.started.elapsed().as_secs_f64();
            self.finished = true;
        }
        RuntimeSnapshot {
            ran_at: self.ran_at.clone(),
            timezone: self.timezone.clone(),
            timings: self.timings.clone(),
            workers_used,
            peak_ram_usage_mib: ram,
            peak_cpu_usage_percent: cpu,
            cpu_time_seconds: cpu_time,
        }
    }
}

/// Restrict native I/O calls without holding a slot while computing or waiting.
pub(crate) struct IoSlots {
    available: Mutex<usize>,
    ready: Condvar,
    capacity: usize,
}
pub(crate) struct IoPermit<'a>(&'a IoSlots, usize);
impl IoSlots {
    pub fn new(slots: usize) -> Result<Self> {
        ensure!(slots > 0, "I/O slots must be positive");
        Ok(Self {
            available: Mutex::new(slots),
            ready: Condvar::new(),
            capacity: slots,
        })
    }
    pub fn acquire(&self) -> Result<IoPermit<'_>> {
        self.acquire_many(1)
    }
    pub fn acquire_many(&self, count: usize) -> Result<IoPermit<'_>> {
        ensure!(
            count > 0 && count <= self.capacity,
            "Requested slots exceed configured capacity"
        );
        let mut available = self
            .available
            .lock()
            .map_err(|_| anyhow::anyhow!("I/O lock poisoned"))?;
        while *available < count {
            available = self
                .ready
                .wait(available)
                .map_err(|_| anyhow::anyhow!("I/O lock poisoned"))?;
        }
        *available -= count;
        Ok(IoPermit(self, count))
    }
}
impl Drop for IoPermit<'_> {
    fn drop(&mut self) {
        if let Ok(mut available) = self.0.available.lock() {
            *available += self.1;
            self.0.ready.notify_all();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_allocation_preserves_complete_work_items() -> Result<()> {
        for (budget, coordinator, work, expected) in [
            (16, 0, 0, 16),
            (64, 16, 32, 16),
            (4096, 32, 64, 1024),
            (65536, 32, 64, 8192),
            (256, 32, 200, 24),
        ] {
            assert_eq!(
                cache_allocation(budget * MIB, coordinator * MIB, work * MIB)?,
                expected * MIB
            );
        }
        for (budget, coordinator, work) in [
            (15 * MIB, 0, 0),
            (64 * MIB, 17 * MIB, 32 * MIB),
            (0, usize::MAX, 1),
            (usize::MAX, usize::MAX, 1),
            (usize::MAX, 1, usize::MAX),
        ] {
            assert!(cache_allocation(budget, coordinator, work).is_err());
        }
        Ok(())
    }

    #[test]
    fn resized_cache_restores_original_on_success_error_and_panic() -> Result<()> {
        for exit in 0..3 {
            let mut original = 0;
            let outcome =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
                    let mut cache = CacheBudget::new()?;
                    original = cache.previous;
                    assert_eq!(unsafe { gdal_sys::GDALGetCacheMax64() }, CACHE_BYTES as i64);
                    cache.resize(64 * MIB)?;
                    assert_eq!(unsafe { gdal_sys::GDALGetCacheMax64() }, (64 * MIB) as i64);
                    cache.resize(CACHE_BYTES)?;
                    assert_eq!(unsafe { gdal_sys::GDALGetCacheMax64() }, CACHE_BYTES as i64);
                    cache.resize(32 * MIB)?;
                    match exit {
                        1 => anyhow::bail!("guarded failure"),
                        2 => panic!("guarded panic"),
                        _ => Ok(()),
                    }
                }));
            match exit {
                0 => outcome.unwrap()?,
                1 => assert!(outcome.unwrap().is_err()),
                _ => assert!(outcome.is_err()),
            }
            let _lock = CACHE_LOCK
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            assert_eq!(unsafe { gdal_sys::GDALGetCacheMax64() }, original);
        }
        Ok(())
    }

    #[test]
    fn publication_collision_preserves_existing_files_and_rolls_back() -> Result<()> {
        let staging = tempfile::tempdir()?;
        let output = tempfile::tempdir()?;
        for name in ["first.csv", "second.csv"] {
            fs::write(staging.path().join(name), "new")?;
        }
        fs::write(output.path().join("second.csv"), "previous")?;
        assert!(
            publish(
                staging.path(),
                output.path(),
                &["first.csv".into(), "second.csv".into()],
                false,
                &[]
            )
            .is_err()
        );
        assert!(!output.path().join("first.csv").exists());
        assert_eq!(
            fs::read_to_string(output.path().join("second.csv"))?,
            "previous"
        );
        Ok(())
    }

    #[test]
    fn publication_failure_after_replacement_restores_backup() -> Result<()> {
        let staging = tempfile::tempdir()?;
        let output = tempfile::tempdir()?;
        fs::create_dir(staging.path().join("missing"))?;
        fs::write(staging.path().join("first.txt"), "new")?;
        fs::write(staging.path().join("missing/second.txt"), "new")?;
        fs::write(output.path().join("first.txt"), "previous")?;
        assert!(
            publish(
                staging.path(),
                output.path(),
                &["first.txt".into(), "missing/second.txt".into()],
                true,
                &[]
            )
            .is_err()
        );
        assert_eq!(
            fs::read_to_string(output.path().join("first.txt"))?,
            "previous"
        );
        assert_eq!(fs::read_dir(output.path())?.count(), 1);
        Ok(())
    }

    #[test]
    fn manifest_failure_restores_products_manifest_and_stale_outputs() -> Result<()> {
        let staging = tempfile::tempdir()?;
        let output = tempfile::tempdir()?;
        fs::write(staging.path().join("product.csv"), "new product")?;
        fs::write(output.path().join("product.csv"), "old product")?;
        fs::write(output.path().join("manifest-stage.json"), "old manifest")?;
        fs::write(output.path().join("stale.csv"), "old optional")?;

        let result = publish_with_manifest(
            staging.path(),
            output.path(),
            &["product.csv".into()],
            "manifest-stage.json",
            true,
            &["stale.csv".into()],
            || {
                fs::write(staging.path().join("manifest-stage.json"), "partial")?;
                anyhow::bail!("manifest serialization failed")
            },
        );

        assert!(result.is_err());
        assert_eq!(
            fs::read_to_string(output.path().join("product.csv"))?,
            "old product"
        );
        assert_eq!(
            fs::read_to_string(output.path().join("manifest-stage.json"))?,
            "old manifest"
        );
        assert_eq!(
            fs::read_to_string(output.path().join("stale.csv"))?,
            "old optional"
        );
        Ok(())
    }

    #[test]
    fn directory_checksums_ignore_creation_order_and_include_names() -> Result<()> {
        let first = tempfile::tempdir()?;
        let second = tempfile::tempdir()?;
        fs::create_dir_all(first.path().join("nested"))?;
        fs::write(first.path().join("z.txt"), "two")?;
        fs::write(first.path().join("nested/a.txt"), "one")?;
        fs::create_dir_all(second.path().join("nested"))?;
        fs::write(second.path().join("nested/a.txt"), "one")?;
        fs::write(second.path().join("z.txt"), "two")?;

        assert_eq!(checksum_path(first.path())?, checksum_path(second.path())?);
        fs::rename(
            second.path().join("z.txt"),
            second.path().join("renamed.txt"),
        )?;
        assert_ne!(checksum_path(first.path())?, checksum_path(second.path())?);
        Ok(())
    }

    #[test]
    fn resource_monitor_finishes_and_drop_stops_sampling() {
        let started = Instant::now();
        let mut monitor = ResourceMonitor::new();
        std::thread::sleep(Duration::from_millis(275));
        let (ram, cpu, cpu_time) = monitor.finish();
        assert!(started.elapsed() < Duration::from_secs(2));
        for value in [ram, cpu, cpu_time].into_iter().flatten() {
            assert!(value.is_finite() && value >= 0.);
        }
        let started = Instant::now();
        drop(ResourceMonitor::new());
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn preparation_optional_validates_new_manifest_filenames() -> Result<()> {
        let output = tempfile::tempdir()?;
        fs::write(
            output.path().join("manifest-prepare.json"),
            serde_json::json!({
                "step": "prepare",
                "outputs": {
                    "d8": {"path": output.path().join("elsewhere/d8.tif"), "sha256": ""},
                    "rasters": {}
                }
            })
            .to_string(),
        )?;
        assert!(preparation_optional(output.path()).is_err());
        Ok(())
    }

    #[test]
    fn preparation_optional_reads_legacy_manifest_parameters() -> Result<()> {
        let output = tempfile::tempdir()?;
        fs::write(
            output.path().join("manifest-prepare.json"),
            r#"{"step":"prepare","parameters":{"d8":"old-path.tif","rasters":[{"name":"hru","path":"old-hru.tif"}]}}"#,
        )?;
        assert_eq!(preparation_optional(output.path())?, ["d8.tif", "hru.tif"]);
        Ok(())
    }

    #[test]
    fn replacement_removes_stale_products_and_preserves_unrelated_files() -> Result<()> {
        let staging = tempfile::tempdir()?;
        let output = tempfile::tempdir()?;
        fs::write(staging.path().join("result.csv"), "new")?;
        fs::write(output.path().join("result.csv"), "old")?;
        fs::write(output.path().join("stale.csv"), "stale")?;
        fs::write(output.path().join("other.csv"), "keep")?;
        publish(
            staging.path(),
            output.path(),
            &["result.csv".into()],
            true,
            &["stale.csv".into()],
        )?;
        assert_eq!(fs::read_to_string(output.path().join("result.csv"))?, "new");
        assert!(!output.path().join("stale.csv").exists());
        assert_eq!(fs::read_to_string(output.path().join("other.csv"))?, "keep");
        Ok(())
    }

    #[test]
    fn missing_staged_file_and_directory_collision_preserve_previous_products() -> Result<()> {
        let staging = tempfile::tempdir()?;
        let output = tempfile::tempdir()?;
        fs::write(staging.path().join("first.csv"), "new")?;
        fs::write(output.path().join("first.csv"), "old")?;
        assert!(
            publish(
                staging.path(),
                output.path(),
                &["first.csv".into(), "missing.csv".into()],
                true,
                &[]
            )
            .is_err()
        );
        fs::create_dir(output.path().join("missing.csv"))?;
        assert!(
            publish(
                staging.path(),
                output.path(),
                &["first.csv".into(), "missing.csv".into()],
                true,
                &[]
            )
            .is_err()
        );
        assert_eq!(fs::read_to_string(output.path().join("first.csv"))?, "old");
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn symlink_manifest_cannot_claim_optional_products() -> Result<()> {
        let output = tempfile::tempdir()?;
        let outside = tempfile::tempdir()?;
        let manifest = outside.path().join("manifest.json");
        fs::write(
            &manifest,
            r#"{"step":"prepare","parameters":{"rasters":[{"name":"unrelated"}]}}"#,
        )?;
        std::os::unix::fs::symlink(&manifest, output.path().join("manifest-prepare.json"))?;
        assert!(preparation_optional(output.path())?.is_empty());
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn replacement_does_not_follow_output_symlinks() -> Result<()> {
        let staging = tempfile::tempdir()?;
        let output = tempfile::tempdir()?;
        let outside = tempfile::tempdir()?;
        fs::write(outside.path().join("source"), "keep")?;
        std::os::unix::fs::symlink(
            outside.path().join("source"),
            output.path().join("result.csv"),
        )?;
        fs::write(staging.path().join("result.csv"), "new")?;
        publish(
            staging.path(),
            output.path(),
            &["result.csv".into()],
            true,
            &[],
        )?;
        assert_eq!(fs::read_to_string(outside.path().join("source"))?, "keep");
        assert_eq!(fs::read_to_string(output.path().join("result.csv"))?, "new");
        Ok(())
    }
}
