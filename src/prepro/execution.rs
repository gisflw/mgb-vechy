//! Shared GIS cache and temporary-output helpers.
use anyhow::{Context, Result};
use std::{
    fs,
    path::Path,
    sync::{Mutex, MutexGuard},
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
        let lock = CACHE_LOCK
            .lock()
            .map_err(|_| anyhow::anyhow!("Sampling cache lock poisoned"))?;
        // GDAL's block cache is process-wide; serialize our changes and restore it on exit.
        let previous = unsafe { gdal_sys::GDALGetCacheMax64() };
        unsafe { gdal_sys::GDALSetCacheMax64(CACHE_BYTES as i64) };
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

pub(crate) fn publish(staging: &Path, output: &Path, names: &[String]) -> Result<()> {
    let mut published = Vec::new();
    for name in names {
        let target = output.join(name);
        if let Err(error) = fs::hard_link(staging.join(name), &target) {
            for path in published {
                fs::remove_file(path).context("Rollback newly published sampling file")?;
            }
            return Err(error).with_context(|| {
                format!(
                    "Publish {} without overwriting existing files",
                    target.display()
                )
            });
        }
        published.push(target);
    }
    Ok(())
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
                &["first.csv".into(), "second.csv".into()]
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
}
