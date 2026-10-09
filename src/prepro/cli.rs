//! Preprocessing command parsing, separate from the root module dispatcher.
use clap::{Args as ClapArgs, CommandFactory};

#[derive(ClapArgs)]
#[command(about = "Preprocessing tools; scientific stages are not implemented yet")]
pub struct Args {}

#[derive(clap::Parser)]
#[command(
    name = "mgb prepro",
    about = "Preprocessing tools; scientific stages are not implemented yet"
)]
struct Help {}

/// Present the preprocessing namespace until scientific commands are available.
pub fn run(_args: Args) {
    Help::command()
        .print_help()
        .expect("write preprocessing help");
    println!();
}
