pub mod compare;
pub mod runner;

use anyhow::{Context, Result, bail, ensure};
use clap::ValueEnum;
use serde::{Deserialize, Serialize};
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

#[derive(Deserialize)]
struct Inventory {
    files: BTreeMap<String, Fingerprint>,
}
#[derive(Deserialize)]
struct Fingerprint {
    bytes: u64,
    sha256: String,
}

pub fn verify(root: &Path) -> Result<()> {
    let inventory: Inventory = serde_json::from_reader(File::open(root.join("inventory.json"))?)?;
    ensure!(!inventory.files.is_empty(), "Fixture inventory is empty");
    let mut issues = Vec::new();
    for (name, expected) in &inventory.files {
        ensure!(
            Path::new(name)
                .components()
                .all(|c| matches!(c, Component::Normal(_))),
            "Unsafe inventory path: {name}"
        );
        let path = root.join(name);
        let result = (|| -> Result<()> {
            let mut file = File::open(&path).with_context(|| format!("Missing fixture {name}"))?;
            ensure!(
                file.metadata()?.len() == expected.bytes,
                "Size mismatch: {name}"
            );
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
                format!("{:x}", hash.finalize()) == expected.sha256,
                "Checksum mismatch: {name}"
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
    println!("Verified {} captured files", inventory.files.len());
    Ok(())
}
