pub mod compare;
pub mod runner;

use anyhow::{Context, Result, bail, ensure};
use clap::ValueEnum;
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::File,
    io::Read,
    path::{Component, Path, PathBuf},
};

#[derive(Clone, Copy, Debug, ValueEnum, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Network {
    Bhae,
    Tdxhydro,
}

impl Network {
    pub fn name(self) -> &'static str {
        match self {
            Self::Bhae => "bhae",
            Self::Tdxhydro => "tdxhydro",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub enum Stage {
    All,
    DefineRoi,
    Aggregate,
    Prepare,
    TerrainProducts,
    SampleMinis,
}

impl Stage {
    pub fn name(self) -> &'static str {
        match self {
            Self::All => "all",
            Self::DefineRoi => "define-roi",
            Self::Aggregate => "aggregate",
            Self::Prepare => "prepare",
            Self::TerrainProducts => "terrain-products",
            Self::SampleMinis => "sample-minis",
        }
    }
    pub fn stages(self) -> Vec<Self> {
        if self == Self::All {
            vec![
                Self::DefineRoi,
                Self::Aggregate,
                Self::Prepare,
                Self::TerrainProducts,
                Self::SampleMinis,
            ]
        } else {
            vec![self]
        }
    }
    pub fn products(self) -> Vec<&'static str> {
        match self {
            Self::All => self.stages().into_iter().flat_map(Self::products).collect(),
            Self::DefineRoi => vec!["roi_catchments.fgb", "roi_segments.fgb"],
            Self::Aggregate => vec![
                "mini_catchments.fgb",
                "mini_segments.fgb",
                "source_to_mini.csv",
            ],
            Self::Prepare => vec![
                "dem.tif",
                "hru.tif",
                "grid_catchments.tif",
                "grid_segments.tif",
            ],
            Self::TerrainProducts => vec!["hand.tif", "ltnd.tif", "undrained_cells.csv"],
            Self::SampleMinis => vec!["sampled_minis.csv"],
        }
    }
}

pub fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/regression/jacui")
}

fn collect_manifest_files(value: &Value, files: &mut BTreeMap<PathBuf, String>) -> Result<()> {
    if let Some(object) = value.as_object() {
        if object.contains_key("path") || object.contains_key("sha256") {
            let path = object["path"]
                .as_str()
                .context("Manifest file path missing")?;
            let checksum = object["sha256"]
                .as_str()
                .context("Manifest file checksum missing")?;
            ensure!(
                checksum.len() == 64 && checksum.bytes().all(|byte| byte.is_ascii_hexdigit()),
                "Invalid manifest checksum: {path}"
            );
            if let Some(previous) = files.insert(PathBuf::from(path), checksum.to_owned()) {
                ensure!(
                    previous == checksum,
                    "Conflicting manifest checksums: {path}"
                );
            }
        } else {
            for value in object.values() {
                collect_manifest_files(value, files)?;
            }
        }
    } else if let Some(values) = value.as_array() {
        for value in values {
            collect_manifest_files(value, files)?;
        }
    }
    Ok(())
}

fn fixture_file(root: &Path, recorded: &Path) -> Result<PathBuf> {
    let default_root = fixture_root().canonicalize()?;
    let relative = if let Ok(relative) = recorded.strip_prefix(root) {
        relative
    } else if let Ok(relative) = recorded.strip_prefix(default_root) {
        relative
    } else if recorded.is_relative() {
        recorded
    } else {
        bail!("Unsafe manifest path: {}", recorded.display());
    };
    ensure!(
        !relative.as_os_str().is_empty()
            && relative
                .components()
                .all(|component| matches!(component, Component::Normal(_))),
        "Unsafe manifest path: {}",
        recorded.display()
    );
    let path = root.join(relative).canonicalize()?;
    ensure!(
        path.starts_with(root),
        "Unsafe manifest path: {}",
        recorded.display()
    );
    Ok(path)
}

pub fn verify(root: &Path) -> Result<()> {
    let root = root.canonicalize().context("Fixture directory missing")?;
    let mut files = BTreeMap::new();
    for network in [Network::Bhae, Network::Tdxhydro] {
        for stage in Stage::All.stages() {
            let path = root
                .join("expected")
                .join(network.name())
                .join(format!("manifest-{}.json", stage.name()));
            let manifest: Value = serde_json::from_reader(
                File::open(&path)
                    .with_context(|| format!("Missing stage manifest: {}", path.display()))?,
            )?;
            ensure!(
                manifest["step"] == stage.name()
                    && manifest["inputs"].is_object()
                    && manifest["outputs"].is_object(),
                "Invalid stage manifest: {}",
                path.display()
            );
            collect_manifest_files(&manifest["inputs"], &mut files)?;
            collect_manifest_files(&manifest["outputs"], &mut files)?;
        }
    }
    ensure!(!files.is_empty(), "Stage manifests contain no files");
    let mut issues = Vec::new();
    for (recorded, expected) in &files {
        let result = (|| -> Result<()> {
            let path = fixture_file(&root, recorded)
                .with_context(|| format!("Missing or unsafe fixture {}", recorded.display()))?;
            let mut file = File::open(&path)?;
            let mut hash = Sha256::new();
            let mut buffer = vec![0; 1024 * 1024];
            loop {
                let count = file.read(&mut buffer)?;
                if count == 0 {
                    break;
                }
                hash.update(&buffer[..count]);
            }
            ensure!(
                format!("{:x}", hash.finalize()) == *expected,
                "Checksum mismatch: {}",
                recorded.display()
            );
            Ok(())
        })();
        if let Err(error) = result {
            issues.push(format!("{error:#}"));
        }
    }
    if !issues.is_empty() {
        bail!("Fixture verification failed:\n{}", issues.join("\n"));
    }
    println!("Verified {} manifest files", files.len());
    Ok(())
}
