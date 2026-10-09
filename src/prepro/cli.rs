//! Preprocessing command parsing, separate from the root module dispatcher.
use super::{
    DirectionSource, SamplingSpec, TerrainSpec, create_terrain_dataset, sample_minibasins,
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
    /// Sample terrain and HRU attributes for each mini-basin.
    SampleMinis(SampleArgs),
    /// Create confined HAND and geodesic terrain-to-drainage distance.
    TerrainProducts(TerrainArgs),
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
