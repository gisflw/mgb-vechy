"""QGIS-free vector hydrography preprocessing for MGB inputs."""

from mgb_vec_hydro.terrain import (
    compute_flow_directions,
    compute_hand,
    compute_ltnd,
)
from mgb_vec_hydro.sampling import MiniSamplingResult, sample_minibasins

__version__ = "0.1.0"

__all__ = [
    "compute_flow_directions",
    "compute_hand",
    "compute_ltnd",
    "MiniSamplingResult",
    "sample_minibasins",
]
