// Shared GIS cache and temporary-output helpers.
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use std::{
    fs,
    path::Path,
    sync::{Condvar, Mutex, MutexGuard},
    time::Instant,
};
pub(crate) const MIB: usize = 1024 * 1024;
pub(crate) const CACHE_BYTES: usize = 16 * MIB;
static CACHE_LOCK: Mutex<()> = Mutex::new(());

pub(crate) struct CacheBudget {
    previous: i64,
    _lock: MutexGuard<'static, ()>,
}
impl CacheBudget {
    pub(crate) fn new() -> Result<Self> {
        Self::with_limit(CACHE_BYTES)
    }
    pub(crate) fn with_limit(bytes: usize) -> Result<Self> {
        // Unwinding restores the GDAL limit through Drop, so a later run can recover.
        let lock = CACHE_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // GDAL's block cache is process-wide; serialize our changes and restore it on exit.
        let previous = unsafe { gdal_sys::GDALGetCacheMax64() };
        unsafe { gdal_sys::GDALSetCacheMax64(bytes as i64) };
        Ok(Self {
            previous,
            _lock: lock,
        })
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
    let parameters = &manifest["parameters"];
    let mut names = Vec::new();
    if !parameters["d8"].is_null() {
        names.push("d8.tif".to_owned());
    }
    if let Some(rasters) = parameters["rasters"].as_array() {
        for raster in rasters {
            let name = raster["name"]
                .as_str()
                .context("Invalid preparation raster manifest")?;
            ensure!(
                name.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
                    && name.bytes().all(|b| b.is_ascii_lowercase()
                        || b.is_ascii_digit()
                        || b == b'_'
                        || b == b'-'),
                "Invalid preparation raster name"
            );
            names.push(format!("{name}.tif"));
        }
    }
    Ok(names)
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
    let affected: Vec<_> = names.iter().chain(remove).cloned().collect();
    check_outputs(output, &affected, overwrite)?;
    for name in names {
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
        for name in names {
            let target = output.join(name);
            fs::hard_link(staging.join(name), &target)
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

pub(crate) struct Reporter<'a> {
    callback: ProgressCallback<'a>,
    started: Instant,
    boundary: Instant,
    phase: &'static str,
    operation: &'static str,
    timings: StageTimings,
}

impl<'a> Reporter<'a> {
    pub fn new(callback: ProgressCallback<'a>) -> Self {
        let now = Instant::now();
        let reporter = Self {
            callback,
            started: now,
            boundary: now,
            phase: "preparing",
            operation: "Preparing inputs",
            timings: StageTimings::default(),
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
    pub fn finish(mut self) -> StageTimings {
        self.record();
        self.timings.total = self.started.elapsed().as_secs_f64();
        self.timings
    }
    pub fn elapsed_seconds(&self) -> f64 {
        self.started.elapsed().as_secs_f64()
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
