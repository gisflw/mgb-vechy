"""QGIS-free vector hydrography preprocessing for MGB inputs."""

from mgb_vec_hydro.preparation import (
    NamedRaster,
    PreparationReport,
    PreparationSpec,
    PreparedDataset,
    prepare_dataset,
)
from mgb_vec_hydro.sampling import MiniSamplingResult, sample_minibasins
from mgb_vec_hydro.terrain import (
    compute_flow_directions,
    compute_hand,
    compute_ltnd,
)

__version__ = "0.1.0"

__all__ = [
    "MiniSamplingResult",
    "NamedRaster",
    "PreparationReport",
    "PreparationSpec",
    "PreparedDataset",
    "compute_flow_directions",
    "compute_hand",
    "compute_ltnd",
    "prepare_dataset",
    "sample_minibasins",
]
