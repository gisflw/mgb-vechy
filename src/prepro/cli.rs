//! Preprocessing command parsing, separate from the root module dispatcher.
use super::{
    AggregationSpec, D8Encoding, DirectionSource, NamedRaster, PreparationSpec, RasterKind,
    RoiSpec, SamplingSpec, TerrainSpec, aggregate_roi_dataset_with_progress,
    create_terrain_dataset_with_progress, define_roi_dataset_with_progress,
    prepare_dataset_with_progress, sample_minibasins_with_progress,
};
use clap::{Args as ClapArgs, Subcommand};
use std::path::PathBuf;

#[derive(ClapArgs)]
#[command(about = "Preprocessing tools")]
pub struct Args {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Select and normalize the network upstream of ordered outlets.
    DefineRoi(RoiArgs),
    /// Aggregate normalized ROI units into ordered mini-basins.
    Aggregate(AggregationArgs),
    /// Prepare aligned rasters and canonical mini ownership.
    Prepare(PreparationArgs),
    /// Sample terrain and HRU attributes for each mini-basin.
    SampleMinis(SampleArgs),
    /// Create confined HAND and geodesic terrain-to-drainage distance.
    TerrainProducts(TerrainArgs),
}

#[derive(ClapArgs)]
struct RoiArgs {
    #[arg(long)]
    catchments: PathBuf,
    #[arg(long)]
    segments: PathBuf,
    #[arg(long)]
    output_dir: PathBuf,
    #[arg(long)]
    crs: String,
    #[arg(long = "outlet-id", required = true)]
    outlet_ids: Vec<String>,
    #[arg(long, default_value = "id")]
    id_col: String,
    #[arg(long, default_value = "id_down")]
    id_down_col: String,
    #[arg(long, default_value = "strahler_order")]
    strahler_order_col: String,
    #[arg(long)]
    catchments_layer: Option<String>,
    #[arg(long)]
    segments_layer: Option<String>,
    #[arg(long)]
    catchments_source_crs: Option<String>,
    #[arg(long)]
    segments_source_crs: Option<String>,
    #[arg(long, default_value_t = 4)]
    workers: usize,
    #[arg(long, default_value_t = 4096)]
    memory_limit_mb: usize,
    #[arg(long)]
    overwrite: bool,
    #[arg(long, default_value_t = 2)]
    io_slots: usize,
    #[arg(long, default_value_t = 10000)]
    batch_size: usize,
}

#[derive(ClapArgs)]
struct AggregationArgs {
    #[arg(long)]
    roi_catchments: PathBuf,
    #[arg(long)]
    roi_segments: PathBuf,
    #[arg(long)]
    output_dir: PathBuf,
    #[arg(long)]
    uparea_min: f64,
    #[arg(long)]
    lmin: f64,
    #[arg(long, default_value_t = 4)]
    workers: usize,
    #[arg(long, default_value_t = 4096)]
    memory_limit_mb: usize,
    #[arg(long)]
    overwrite: bool,
    #[arg(long, default_value_t = 2)]
    io_slots: usize,
    #[arg(long, default_value_t = 10000)]
    batch_size: usize,
}

#[derive(ClapArgs)]
struct PreparationArgs {
    #[arg(long)]
    dem: PathBuf,
    #[arg(long)]
    mini_catchments: PathBuf,
    #[arg(long)]
    mini_segments: PathBuf,
    #[arg(long)]
    output_dir: PathBuf,
    #[arg(long, num_args = 2, action = clap::ArgAction::Append, value_names = ["NAME", "PATH"])]
    continuous_raster: Vec<String>,
    #[arg(long, num_args = 2, action = clap::ArgAction::Append, value_names = ["NAME", "PATH"])]
    categorical_raster: Vec<String>,
    #[arg(long, requires = "d8_encoding")]
    d8: Option<PathBuf>,
    #[arg(long, value_enum, requires = "d8")]
    d8_encoding: Option<D8Encoding>,
    #[arg(long, default_value_t = 1.)]
    dem_scale: f64,
    #[arg(long, default_value_t = 4)]
    workers: usize,
    #[arg(long, default_value_t = 4096)]
    memory_limit_mb: usize,
    #[arg(long)]
    overwrite: bool,
    #[arg(long, default_value_t = 2)]
    io_slots: usize,
}

#[derive(ClapArgs)]
struct SampleArgs {
    #[arg(long)]
    mini_catchments: PathBuf,
    #[arg(long)]
    mini_segments: PathBuf,
    #[arg(long)]
    dem: PathBuf,
    #[arg(long)]
    grid_catchments: PathBuf,
    #[arg(long)]
    grid_segments: PathBuf,
    #[arg(long)]
    hand: PathBuf,
    #[arg(long)]
    ltnd: PathBuf,
    #[arg(long)]
    hru: PathBuf,
    #[arg(long)]
    output_dir: PathBuf,
    #[arg(long, default_value_t = 4)]
    workers: usize,
    #[arg(long, default_value_t = 4096)]
    memory_limit_mb: usize,
    #[arg(long)]
    overwrite: bool,
    #[arg(long, default_value_t = 2)]
    io_slots: usize,
}

#[derive(ClapArgs)]
struct TerrainArgs {
    #[arg(long)]
    dem: PathBuf,
    #[arg(long)]
    grid_catchments: PathBuf,
    #[arg(long)]
    grid_segments: PathBuf,
    #[arg(long)]
    output_dir: PathBuf,
    #[arg(long, value_enum, default_value = "dem")]
    direction_source: DirectionSource,
    #[arg(long)]
    d8: Option<PathBuf>,
    #[arg(long)]
    write_flow_direction: bool,
    #[arg(long, default_value_t = 80.)]
    agree_sharp: f64,
    #[arg(long, default_value_t = 8.)]
    agree_smooth: f64,
    #[arg(long, default_value_t = 4)]
    agree_buffer: usize,
    #[arg(long, default_value_t = 4)]
    workers: usize,
    #[arg(long, default_value_t = 4096)]
    memory_limit_mb: usize,
    #[arg(
        long,
        default_value_t = super::terrain::DEFAULT_ROUTING_BYTES_PER_CELL
    )]
    routing_bytes_per_cell: usize,
    #[arg(long)]
    overwrite: bool,
    #[arg(long, default_value_t = 2)]
    io_slots: usize,
}

pub fn run(args: Args) -> anyhow::Result<()> {
    let display = Display::new();
    let progress = |event| display.update(event);
    match args.command {
        Command::DefineRoi(mut args) => {
            args.overwrite = replacement(
                &args.output_dir,
                &[
                    "roi_catchments.fgb".into(),
                    "roi_segments.fgb".into(),
                    "manifest-define-roi.json".into(),
                ],
                args.overwrite,
            )?;
            let report = define_roi_dataset_with_progress(
                &RoiSpec {
                    overwrite: args.overwrite,
                    io_slots: args.io_slots,
                    batch_size: args.batch_size,
                    catchments: args.catchments,
                    segments: args.segments,
                    output_dir: args.output_dir,
                    crs: args.crs,
                    outlet_ids: args.outlet_ids,
                    id_col: args.id_col,
                    id_down_col: args.id_down_col,
                    strahler_order_col: args.strahler_order_col,
                    catchments_layer: args.catchments_layer,
                    segments_layer: args.segments_layer,
                    catchments_source_crs: args.catchments_source_crs,
                    segments_source_crs: args.segments_source_crs,
                    workers: args.workers,
                    memory_limit_mb: args.memory_limit_mb,
                },
                &progress,
            )?;
            display.elapsed(&report.timings);
            println!(
                "Selected {} sources using {} workers: {}",
                report.source_count,
                report.workers_used,
                report.catchments.display()
            );
            Ok(())
        }
        Command::Aggregate(mut args) => {
            args.overwrite = replacement(
                &args.output_dir,
                &[
                    "mini_catchments.fgb".into(),
                    "mini_segments.fgb".into(),
                    "source_to_mini.csv".into(),
                    "manifest-aggregate.json".into(),
                ],
                args.overwrite,
            )?;
            let report = aggregate_roi_dataset_with_progress(
                &AggregationSpec {
                    overwrite: args.overwrite,
                    io_slots: args.io_slots,
                    batch_size: args.batch_size,
                    roi_catchments: args.roi_catchments,
                    roi_segments: args.roi_segments,
                    output_dir: args.output_dir,
                    uparea_min: args.uparea_min,
                    lmin: args.lmin,
                    workers: args.workers,
                    memory_limit_mb: args.memory_limit_mb,
                },
                &progress,
            )?;
            display.elapsed(&report.timings);
            println!(
                "Aggregated {} sources into {} minis using {} workers: {}",
                report.source_count,
                report.mini_count,
                report.workers_used,
                report.catchments.display()
            );
            Ok(())
        }
        Command::Prepare(mut args) => {
            let mut names: Vec<String> = [
                "dem.tif",
                "grid_catchments.tif",
                "grid_segments.tif",
                "d8.tif",
                "manifest-prepare.json",
            ]
            .into_iter()
            .map(str::to_owned)
            .chain(
                args.continuous_raster
                    .as_chunks::<2>()
                    .0
                    .iter()
                    .chain(args.categorical_raster.as_chunks::<2>().0)
                    .map(|pair| format!("{}.tif", pair[0])),
            )
            .collect();
            names.extend(super::execution::preparation_optional(&args.output_dir)?);
            args.overwrite = replacement(&args.output_dir, &names, args.overwrite)?;
            let mut rasters = Vec::new();
            for (values, kind) in [
                (args.continuous_raster, RasterKind::Continuous),
                (args.categorical_raster, RasterKind::Categorical),
            ] {
                for pair in values.as_chunks::<2>().0 {
                    rasters.push(NamedRaster {
                        name: pair[0].clone(),
                        path: pair[1].clone().into(),
                        kind,
                    });
                }
            }
            let report = prepare_dataset_with_progress(
                &PreparationSpec {
                    overwrite: args.overwrite,
                    io_slots: args.io_slots,
                    dem: args.dem,
                    mini_catchments: args.mini_catchments,
                    mini_segments: args.mini_segments,
                    output_dir: args.output_dir,
                    rasters,
                    d8: args.d8,
                    d8_encoding: args.d8_encoding,
                    dem_scale: args.dem_scale,
                    workers: args.workers,
                    memory_limit_mb: args.memory_limit_mb,
                },
                &progress,
            )?;
            display.elapsed(&report.timings);
            println!(
                "Prepared {} minis using {} workers: {}",
                report.mini_count,
                report.workers_used,
                report.dem.display()
            );
            Ok(())
        }
        Command::TerrainProducts(mut args) => {
            args.overwrite = replacement(
                &args.output_dir,
                &[
                    "hand.tif",
                    "ltnd.tif",
                    "flow_direction.tif",
                    "undrained_cells.csv",
                    "manifest-terrain-products.json",
                ]
                .map(str::to_owned),
                args.overwrite,
            )?;
            let report = create_terrain_dataset_with_progress(
                &TerrainSpec {
                    overwrite: args.overwrite,
                    io_slots: args.io_slots,
                    dem: args.dem,
                    grid_catchments: args.grid_catchments,
                    grid_segments: args.grid_segments,
                    output_dir: args.output_dir,
                    direction_source: args.direction_source,
                    d8: args.d8,
                    write_flow_direction: args.write_flow_direction,
                    agree_sharp: args.agree_sharp,
                    agree_smooth: args.agree_smooth,
                    agree_buffer: args.agree_buffer,
                    workers: args.workers,
                    memory_limit_mb: args.memory_limit_mb,
                    routing_bytes_per_cell: args.routing_bytes_per_cell,
                },
                &progress,
            )?;
            display.elapsed(&report.timings);
            println!(
                "Routed {} minis using {} workers ({} undrained cells): {}",
                report.mini_count,
                report.workers_used,
                report.undrained_count,
                report.hand.display()
            );
            Ok(())
        }
        Command::SampleMinis(mut args) => {
            let names: Vec<String> = [
                "sampled_minis.csv".into(),
                "manifest-sample-minis.json".into(),
            ]
            .into_iter()
            .chain(super::sampling::NODATA_NAMES.map(|n| format!("nodata_{n}.csv")))
            .collect();
            args.overwrite = replacement(&args.output_dir, &names, args.overwrite)?;
            let report = sample_minibasins_with_progress(
                &SamplingSpec {
                    overwrite: args.overwrite,
                    io_slots: args.io_slots,
                    mini_catchments: args.mini_catchments,
                    mini_segments: args.mini_segments,
                    dem: args.dem,
                    grid_catchments: args.grid_catchments,
                    grid_segments: args.grid_segments,
                    hand: args.hand,
                    ltnd: args.ltnd,
                    hru: args.hru,
                    output_dir: args.output_dir,
                    workers: args.workers,
                    memory_limit_mb: args.memory_limit_mb,
                },
                &progress,
            )?;
            display.elapsed(&report.timings);
            println!(
                "Sampled {} minis using {} workers: {}",
                report.mini_count,
                report.workers_used,
                report.sampled_minis.display()
            );
            Ok(())
        }
    }
}

fn replacement(
    output: &std::path::Path,
    names: &[String],
    overwrite: bool,
) -> anyhow::Result<bool> {
    use std::io::{IsTerminal, Write};
    let mut existing = Vec::new();
    for name in names {
        let path = output.join(name);
        match path.symlink_metadata() {
            Ok(metadata) => {
                anyhow::ensure!(
                    !metadata.is_dir(),
                    "Output path is a directory: {}",
                    path.display()
                );
                existing.push(path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    if existing.is_empty() || overwrite {
        return Ok(overwrite);
    }
    anyhow::ensure!(
        std::io::stdin().is_terminal() && std::io::stderr().is_terminal(),
        "A stage output already exists; use --overwrite to replace stage outputs"
    );
    eprintln!("Replace existing output files?");
    for path in existing {
        eprintln!("{}", path.display());
    }
    eprint!("[y/N] ");
    std::io::stderr().flush()?;
    let mut answer = String::new();
    std::io::stdin().read_line(&mut answer)?;
    anyhow::ensure!(
        matches!(answer.trim().to_ascii_lowercase().as_str(), "y" | "yes"),
        "Replacement declined"
    );
    Ok(true)
}

struct Display {
    state: std::sync::Arc<std::sync::Mutex<Option<(super::StageProgress, std::time::Instant)>>>,
    stop: std::sync::mpsc::Sender<()>,
    thread: Option<std::thread::JoinHandle<()>>,
    terminal: bool,
}
impl Display {
    fn new() -> Self {
        use std::io::IsTerminal;
        let terminal = std::io::stderr().is_terminal();
        let state = std::sync::Arc::new(std::sync::Mutex::new(
            None::<(super::StageProgress, std::time::Instant)>,
        ));
        let (stop, receiver) = std::sync::mpsc::channel();
        let shared = state.clone();
        let thread = terminal.then(|| {
            std::thread::spawn(move || {
                use std::io::Write;
                while receiver
                    .recv_timeout(std::time::Duration::from_millis(250))
                    .is_err()
                {
                    if let Ok(state) = shared.lock()
                        && let Some((event, updated)) = state.as_ref()
                    {
                        let total = event.total.map(|n| format!("/{n}")).unwrap_or_default();
                        eprint!(
                            "\r\x1b[2K{}  {}{}  {:.1}s",
                            event.operation,
                            event.completed,
                            total,
                            event.elapsed_seconds + updated.elapsed().as_secs_f64()
                        );
                        let _ = std::io::stderr().flush();
                    }
                }
            })
        });
        Self {
            state,
            stop,
            thread,
            terminal,
        }
    }
    fn update(&self, event: super::StageProgress) {
        if let Ok(mut state) = self.state.lock() {
            if !self.terminal
                && state
                    .as_ref()
                    .is_none_or(|(previous, _)| previous.operation != event.operation)
            {
                eprintln!("{}", event.operation);
            }
            *state = Some((event, std::time::Instant::now()));
        }
    }
    fn elapsed(&self, timings: &super::StageTimings) {
        if let Ok(mut state) = self.state.lock() {
            *state = None;
        }
        if self.terminal {
            eprint!("\r\x1b[2K");
        }
        eprintln!(
            "Elapsed: preparing {:.1}s, processing {:.1}s, finalizing {:.1}s, total {:.1}s",
            timings.preparing, timings.processing, timings.finalizing, timings.total
        );
    }
}
impl Drop for Display {
    fn drop(&mut self) {
        let _ = self.stop.send(());
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        if self.terminal {
            eprintln!();
        }
        if let Ok(state) = self.state.lock()
            && let Some((event, updated)) = state.as_ref()
        {
            let mut timings = event.timings.clone();
            let seconds = updated.elapsed().as_secs_f64();
            match event.phase {
                "preparing" => timings.preparing += seconds,
                "processing" => timings.processing += seconds,
                "finalizing" => timings.finalizing += seconds,
                _ => unreachable!(),
            }
            timings.total += seconds;
            eprintln!(
                "Elapsed before failure: preparing {:.1}s, processing {:.1}s, finalizing {:.1}s, total {:.1}s",
                timings.preparing, timings.processing, timings.finalizing, timings.total
            );
        }
    }
}
