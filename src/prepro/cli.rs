//! Preprocessing command parsing, separate from the root module dispatcher.
use super::{
    AggregationSpec, D8Encoding, DirectionSource, NamedRaster, PreparationSpec, RasterKind,
    RoiSpec, SamplingSpec, TerrainSpec, aggregate_roi_dataset, create_terrain_dataset,
    define_roi_dataset, prepare_dataset, sample_minibasins,
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
}

pub fn run(args: Args) -> anyhow::Result<()> {
    match args.command {
        Command::DefineRoi(args) => {
            let report = define_roi_dataset(&RoiSpec {
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
            })?;
            println!(
                "Selected {} sources using {} workers: {}",
                report.source_count,
                report.workers_used,
                report.catchments.display()
            );
            Ok(())
        }
        Command::Aggregate(args) => {
            let report = aggregate_roi_dataset(&AggregationSpec {
                roi_catchments: args.roi_catchments,
                roi_segments: args.roi_segments,
                output_dir: args.output_dir,
                uparea_min: args.uparea_min,
                lmin: args.lmin,
                workers: args.workers,
                memory_limit_mb: args.memory_limit_mb,
            })?;
            println!(
                "Aggregated {} sources into {} minis using {} workers: {}",
                report.source_count,
                report.mini_count,
                report.workers_used,
                report.catchments.display()
            );
            Ok(())
        }
        Command::Prepare(args) => {
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
            let report = prepare_dataset(&PreparationSpec {
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
            })?;
            println!(
                "Prepared {} minis using {} workers: {}",
                report.mini_count,
                report.workers_used,
                report.dem.display()
            );
            Ok(())
        }
        Command::TerrainProducts(args) => {
            let report = create_terrain_dataset(&TerrainSpec {
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
            })?;
            println!(
                "Routed {} minis using {} workers ({} undrained cells): {}",
                report.mini_count,
                report.workers_used,
                report.undrained_count,
                report.hand.display()
            );
            Ok(())
        }
        Command::SampleMinis(args) => {
            let report = sample_minibasins(&SamplingSpec {
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
            })?;
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
