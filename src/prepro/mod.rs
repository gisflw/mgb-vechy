//! Preprocessing of hydrography, mini-basins, terrain, and model attributes.
//!
//! Stage implementations follow the frozen contracts in `docs/`.
pub mod aggregation;
pub mod cli;
pub mod roi;
pub use aggregation::{AggregationReport, AggregationSpec, aggregate_roi_dataset};
pub use roi::{RoiReport, RoiSpec, define_roi_dataset};
pub mod execution;
pub mod io;
pub mod model;
pub mod sampling;

pub use sampling::{SamplingReport, SamplingSpec, sample_minibasins};

pub mod terrain;
pub use terrain::{DirectionSource, TerrainReport, TerrainSpec, create_terrain_dataset};

pub mod preparation;
pub use preparation::{
    D8Encoding, NamedRaster, PreparationReport, PreparationSpec, RasterKind, prepare_dataset,
};
