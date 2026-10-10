use super::{Network, Stage};
use anyhow::{Context, Result, bail, ensure};
use clap::Args;
use serde_json::Value;
use std::{
    ffi::OsString,
    fs::{self, File},
    path::{Component, Path, PathBuf},
    process::Command,
};

#[derive(Args, Debug)]
pub struct RunOptions {
    #[arg(long, value_enum)]
    pub network: Network,
    #[arg(long, value_enum, default_value = "all")]
    pub stage: Stage,
    #[arg(long)]
    pub output_dir: PathBuf,
    /// Candidate executable; must implement `prepro <stage>`.
    #[arg(long)]
    pub command: PathBuf,
    /// Repeat for each executable-prefix argument, e.g. --command-arg=--release.
    #[arg(long, allow_hyphen_values = true)]
    pub command_arg: Vec<OsString>,
    #[arg(long, default_value = "4", value_parser = clap::value_parser!(u32).range(1..))]
    pub workers: u32,
    #[arg(long, default_value = "4096", value_parser = clap::value_parser!(u32).range(1..))]
    pub memory_limit_mb: u32,
    #[arg(long, value_parser = clap::value_parser!(u32).range(1..))]
    pub io_slots: Option<u32>,
}

pub struct Invocation {
    pub args: Vec<OsString>,
    pub inputs: Vec<PathBuf>,
}

/// All stage adapters match the Rust CLI.
pub fn invocation(root: &Path, options: &RunOptions, stage: Stage) -> Result<Invocation> {
    let manifest_path = root
        .join("expected")
        .join(options.network.name())
        .join(format!("manifest-{}.json", stage.name()));
    let manifest: Value = serde_json::from_reader(
        File::open(&manifest_path)
            .with_context(|| format!("Stage manifest missing: {}", manifest_path.display()))?,
    )?;
    ensure!(manifest["step"] == stage.name(), "Invalid stage manifest");
    let parameters = manifest["parameters"]
        .as_object()
        .context("Stage manifest parameters missing")?;
    let input = root.join("input");
    let upstream = if options.stage == Stage::All {
        options.output_dir.clone()
    } else {
        root.join("expected").join(options.network.name())
    };
    let mut args = vec![OsString::from("prepro"), OsString::from(stage.name())];
    let mut inputs = Vec::new();
    let mut file = |flag: &str, path: PathBuf| {
        args.extend([OsString::from(flag), path.clone().into_os_string()]);
        inputs.push(path);
    };
    match stage {
        Stage::DefineRoi => {
            file(
                "--catchments",
                input.join(options.network.name()).join("catchments.fgb"),
            );
            file(
                "--segments",
                input.join(options.network.name()).join("segments.fgb"),
            );
        }
        Stage::Aggregate => {
            file("--roi-catchments", upstream.join("roi_catchments.fgb"));
            file("--roi-segments", upstream.join("roi_segments.fgb"));
        }
        Stage::Prepare => {
            file("--dem", input.join("dem.tif"));
            file("--mini-catchments", upstream.join("mini_catchments.fgb"));
            file("--mini-segments", upstream.join("mini_segments.fgb"));
            inputs.push(input.join("hru.tif"));
            args.extend([
                OsString::from("--categorical-raster"),
                OsString::from("hru"),
                input.join("hru.tif").into_os_string(),
            ]);
        }
        Stage::TerrainProducts => {
            for name in ["dem", "grid_catchments", "grid_segments"] {
                file(
                    &format!("--{}", name.replace('_', "-")),
                    upstream.join(format!("{name}.tif")),
                );
            }
        }
        Stage::SampleMinis => {
            file("--mini-catchments", upstream.join("mini_catchments.fgb"));
            file("--mini-segments", upstream.join("mini_segments.fgb"));
            for name in [
                "dem",
                "grid_catchments",
                "grid_segments",
                "hand",
                "ltnd",
                "hru",
            ] {
                file(
                    &format!("--{}", name.replace('_', "-")),
                    upstream.join(format!("{name}.tif")),
                );
            }
        }
        Stage::All => bail!("Build one stage invocation at a time"),
    }
    let mut value =
        |flag: &str, val: String| args.extend([OsString::from(flag), OsString::from(val)]);
    match stage {
        Stage::DefineRoi => {
            for (flag, val) in [
                ("--crs", "crs"),
                ("--id-col", "id_col"),
                ("--id-down-col", "id_down_col"),
                ("--strahler-order-col", "strahler_order_col"),
            ] {
                value(
                    flag,
                    parameters[val]
                        .as_str()
                        .with_context(|| format!("Manifest parameter {val} missing"))?
                        .to_owned(),
                );
            }
            for outlet in parameters["outlet_ids"]
                .as_array()
                .context("Manifest outlet IDs missing")?
            {
                value(
                    "--outlet-id",
                    outlet
                        .as_str()
                        .context("Manifest outlet ID is not a string")?
                        .to_owned(),
                );
            }
            for (flag, val) in [
                ("--catchments-source-crs", "catchments_source_crs"),
                ("--segments-source-crs", "segments_source_crs"),
            ] {
                if let Some(val) = parameters[val].as_str() {
                    value(flag, val.to_owned());
                }
            }
        }
        Stage::Aggregate => {
            value(
                "--uparea-min",
                parameters["uparea_min"]
                    .as_f64()
                    .context("Manifest uparea_min missing")?
                    .to_string(),
            );
            value(
                "--lmin",
                parameters["lmin"]
                    .as_f64()
                    .context("Manifest lmin missing")?
                    .to_string(),
            );
        }
        Stage::Prepare => value(
            "--dem-scale",
            parameters["dem_scale"]
                .as_f64()
                .context("Manifest dem_scale missing")?
                .to_string(),
        ),
        Stage::TerrainProducts => {
            value(
                "--direction-source",
                parameters["direction_source"]
                    .as_str()
                    .context("Manifest direction source missing")?
                    .to_owned(),
            );
            value(
                "--agree-sharp",
                parameters["agree_sharp"]
                    .as_f64()
                    .context("Manifest agree_sharp missing")?
                    .to_string(),
            );
            value(
                "--agree-smooth",
                parameters["agree_smooth"]
                    .as_f64()
                    .context("Manifest agree_smooth missing")?
                    .to_string(),
            );
            value(
                "--agree-buffer",
                parameters["agree_buffer"]
                    .as_u64()
                    .context("Manifest agree_buffer missing")?
                    .to_string(),
            );
        }
        _ => {}
    }
    value("--workers", options.workers.to_string());
    value("--memory-limit-mb", options.memory_limit_mb.to_string());
    if let Some(io_slots) = options.io_slots {
        value("--io-slots", io_slots.to_string());
    }
    args.extend([
        OsString::from("--output-dir"),
        options.output_dir.clone().into_os_string(),
    ]);
    Ok(Invocation { args, inputs })
}

/// Resolve symlinks in existing ancestors even when the final path does not exist.
pub fn resolved(path: &Path) -> Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut result = PathBuf::new();
    for part in absolute.components() {
        match part {
            Component::ParentDir => {
                result.pop();
            }
            Component::CurDir => {}
            other => {
                result.push(other.as_os_str());
                if result.symlink_metadata().is_ok() {
                    result = result.canonicalize()?;
                }
            }
        }
    }
    Ok(result)
}

pub fn protect_output(root: &Path, output: &Path) -> Result<PathBuf> {
    let output = resolved(output)?;
    for name in ["input", "expected"] {
        let protected = resolved(&root.join(name))?;
        ensure!(
            !output.starts_with(&protected) && !protected.starts_with(&output),
            "Candidate output overlaps protected fixtures: {}",
            output.display()
        );
    }
    if output.exists() {
        ensure!(
            output.is_dir() && fs::read_dir(&output)?.next().is_none(),
            "Candidate output must be a fresh, empty directory: {}",
            output.display()
        );
    }
    Ok(output)
}

pub fn run(root: &Path, options: &RunOptions) -> Result<()> {
    ensure!(
        options.workers > 0 && options.memory_limit_mb > 0,
        "Resource settings must be positive"
    );
    let root = root
        .canonicalize()
        .context("Local fixture directory missing")?;
    let output = protect_output(&root, &options.output_dir)?;
    let command = if options.command.components().count() > 1 {
        options
            .command
            .canonicalize()
            .context("Candidate executable missing")?
    } else {
        options.command.clone()
    };
    let options = RunOptions {
        output_dir: output.clone(),
        command,
        command_arg: options.command_arg.clone(),
        ..*options
    };
    // Validate the first stage before creating outputs. Later full-pipeline inputs
    // are produced by earlier candidate stages.
    let first = invocation(&root, &options, options.stage.stages()[0])?;
    for path in first.inputs {
        ensure!(path.is_file(), "Local fixture missing: {}", path.display());
    }
    fs::create_dir_all(&output)?;
    for stage in options.stage.stages() {
        let result = || -> Result<i32> {
            let invocation = invocation(&root, &options, stage)?;
            for path in &invocation.inputs {
                ensure!(path.is_file(), "Stage input missing: {}", path.display());
            }
            let log = File::create(output.join(format!("{}.log", stage.name())))?;
            let mut child = Command::new(&options.command)
                .args(&options.command_arg)
                .args(&invocation.args)
                .current_dir(env!("CARGO_MANIFEST_DIR"))
                .stdout(log.try_clone()?)
                .stderr(log)
                .spawn()
                .with_context(|| format!("Cannot start candidate {}", options.command.display()))?;
            Ok(child.wait()?.code().unwrap_or(-1))
        };
        let code = result()?;
        ensure!(
            code == 0,
            "{} failed with exit code {code}; see {}",
            stage.name(),
            output.join(format!("{}.log", stage.name())).display()
        );
        println!("{}/{} completed", options.network.name(), stage.name());
    }
    Ok(())
}
