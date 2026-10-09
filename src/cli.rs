use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "mgb", version, about = "MGB hydrography and model-input tools")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Preprocess hydrography and model inputs.
    Prepro(mgb::prepro::cli::Args),
}

pub fn run() -> anyhow::Result<()> {
    match Cli::parse().command {
        Command::Prepro(args) => mgb::prepro::cli::run(args),
    }
}
