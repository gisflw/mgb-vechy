use super::{Network, Stage};
use anyhow::{Context, Result, bail, ensure};
use clap::Args;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    ffi::OsString,
    fs::{self, File},
    path::{Component, Path, PathBuf},
    process::Command,
    time::Instant,
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
}

#[derive(Deserialize)]
pub struct Config {
    crs: String,
    uparea_min: f64,
    lmin: f64,
    dem_scale: f64,
    agree_sharp: f64,
    agree_smooth: f64,
    agree_buffer: u32,
    networks: BTreeMap<String, Fields>,
}
#[derive(Deserialize)]
struct Fields {
    outlet_ids: Vec<String>,
    id_col: String,
    id_down_col: String,
    strahler_order_col: String,
    catchments_source_crs: Option<String>,
    segments_source_crs: Option<String>,
}

pub struct Invocation {
    pub args: Vec<OsString>,
    pub inputs: Vec<PathBuf>,
}

/// Sampling and terrain match the Rust CLI; pending stage flags remain provisional.
pub fn invocation(root: &Path, options: &RunOptions, stage: Stage) -> Result<Invocation> {
    let config: Config = serde_json::from_reader(File::open(root.join("config.json"))?)?;
    let fields = config
        .networks
        .get(options.network.name())
        .context("Network configuration missing")?;
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
                ("--crs", &config.crs),
                ("--id-col", &fields.id_col),
                ("--id-down-col", &fields.id_down_col),
                ("--strahler-order-col", &fields.strahler_order_col),
            ] {
                value(flag, val.clone());
            }
            for outlet in &fields.outlet_ids {
                value("--outlet-id", outlet.clone());
            }
            for (flag, val) in [
                ("--catchments-source-crs", &fields.catchments_source_crs),
                ("--segments-source-crs", &fields.segments_source_crs),
            ] {
                if let Some(val) = val {
                    value(flag, val.clone());
                }
            }
        }
        Stage::Aggregate => {
            value("--uparea-min", config.uparea_min.to_string());
            value("--lmin", config.lmin.to_string());
        }
        Stage::Prepare => value("--dem-scale", config.dem_scale.to_string()),
        Stage::TerrainProducts => {
            value("--direction-source", "dem".into());
            value("--agree-sharp", config.agree_sharp.to_string());
            value("--agree-smooth", config.agree_smooth.to_string());
            value("--agree-buffer", config.agree_buffer.to_string());
        }
        _ => {}
    }
    value("--workers", options.workers.to_string());
    value("--memory-limit-mb", options.memory_limit_mb.to_string());
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

pub fn run(root: &Path, options: &RunOptions) -> Result<Value> {
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
    let revision = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .output()?;
    let mut report = json!({
        "network": options.network.name(), "command": [options.command.as_os_str().to_string_lossy()],
        "command_args": options.command_arg.iter().map(|s| s.to_string_lossy()).collect::<Vec<_>>(),
        "workers": options.workers, "memory_limit_mb": options.memory_limit_mb,
        "platform": format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH),
        "cpu_count": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1),
        "recorded_at": chrono::Utc::now().to_rfc3339(),
        "revision": String::from_utf8_lossy(&revision.stdout).trim(), "measurements": []
    });
    for stage in options.stage.stages() {
        let result = || -> Result<(i32, Option<i64>)> {
            let invocation = invocation(&root, &options, stage)?;
            for path in &invocation.inputs {
                ensure!(path.is_file(), "Stage input missing: {}", path.display());
            }
            let log = File::create(output.join(format!("{}.log", stage.name())))?;
            let child = Command::new(&options.command)
                .args(&options.command_arg)
                .args(&invocation.args)
                .current_dir(env!("CARGO_MANIFEST_DIR"))
                .stdout(log.try_clone()?)
                .stderr(log)
                .spawn()
                .with_context(|| format!("Cannot start candidate {}", options.command.display()))?;
            wait_with_usage(child)
        };
        let started = Instant::now();
        let result = result();
        let (code, rss) = result.as_ref().copied().unwrap_or((-1, None));
        let measurement = json!({"stage": stage.name(), "wall_seconds": started.elapsed().as_secs_f64(),
            "max_process_rss_kib": rss, "exit_code": code,
            "error": result.as_ref().err().map(|error| format!("{error:#}"))});
        report["measurements"]
            .as_array_mut()
            .unwrap()
            .push(measurement);
        fs::write(
            output.join("benchmark.json"),
            serde_json::to_string_pretty(&report)? + "\n",
        )?;
        result?;
        ensure!(
            code == 0,
            "{} failed with exit code {code}; see {}",
            stage.name(),
            output.join(format!("{}.log", stage.name())).display()
        );
        println!("{}/{} completed", options.network.name(), stage.name());
    }
    Ok(report)
}

#[cfg(target_os = "linux")]
fn wait_with_usage(child: std::process::Child) -> Result<(i32, Option<i64>)> {
    let mut status = 0;
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
    loop {
        // SAFETY: status and usage point to writable storage; wait4 initializes
        // both on success. Wait only for this owned child's PID.
        let result = unsafe { libc::wait4(child.id() as i32, &mut status, 0, usage.as_mut_ptr()) };
        if result >= 0 {
            break;
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(error.into());
        }
    }
    // SAFETY: successful wait4 initialized the rusage structure.
    let usage = unsafe { usage.assume_init() };
    let code = if libc::WIFEXITED(status) {
        libc::WEXITSTATUS(status)
    } else {
        -libc::WTERMSIG(status)
    };
    Ok((code, Some(usage.ru_maxrss)))
}

#[cfg(not(target_os = "linux"))]
fn wait_with_usage(mut child: std::process::Child) -> Result<(i32, Option<i64>)> {
    Ok((child.wait()?.code().unwrap_or(-1), None))
}
