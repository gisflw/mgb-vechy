//! Preprocessing command parsing, separate from the root module dispatcher.
use super::{SamplingSpec, sample_minibasins};
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

pub fn run(args: Args) -> anyhow::Result<()> {
    match args.command {
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
