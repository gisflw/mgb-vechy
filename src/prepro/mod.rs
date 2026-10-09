//! Preprocessing of hydrography, mini-basins, terrain, and model attributes.
//!
//! Stage implementations follow the frozen contracts in `docs/`.
pub mod cli;
pub mod execution;
pub mod io;
pub mod model;
pub mod sampling;

pub use sampling::{SamplingReport, SamplingSpec, sample_minibasins};

pub mod terrain;
pub use terrain::{DirectionSource, TerrainReport, TerrainSpec, create_terrain_dataset};
