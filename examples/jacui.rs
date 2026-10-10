//! Developer tooling, intentionally separate from the production CLI.
#[path = "../tests/support/jacui/mod.rs"]
mod jacui;

use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Verify, exercise, and compare frozen Jacui preprocessing references")]
struct Cli {
    /// Local capture directory (large assets are not committed).
    #[arg(long, global = true)]
    fixture: Option<PathBuf>,
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Check fixture files against SHA-256 checksums in stage manifests.
    Verify,
    /// Run a candidate preprocessing executable and write stage logs.
    Run(jacui::runner::RunOptions),
    /// Compare candidate products with a frozen reference.
    CompareProducts {
        #[arg(long)]
        reference_dir: PathBuf,
        #[arg(long)]
        output_dir: PathBuf,
        #[arg(long)]
        legacy_sub: bool,
        #[arg(long, value_enum, default_value = "all")]
        stage: jacui::Stage,
    },
    Compare {
        #[arg(long, value_enum)]
        network: jacui::Network,
        #[arg(long, value_enum, default_value = "all")]
        stage: jacui::Stage,
        #[arg(long)]
        output_dir: PathBuf,
    },
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    let fixture = cli.fixture.unwrap_or_else(jacui::fixture_root);
    match cli.command {
        Commands::Verify => jacui::verify(&fixture),
        Commands::Run(options) => jacui::runner::run(&fixture, &options),
        Commands::CompareProducts {
            reference_dir,
            output_dir,
            legacy_sub,
            stage,
        } => jacui::compare::compare_products(&reference_dir, &output_dir, stage, legacy_sub),
        Commands::Compare {
            network,
            stage,
            output_dir,
        } => jacui::compare::compare(&fixture, network, stage, &output_dir),
    }
}
