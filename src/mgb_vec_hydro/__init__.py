"""QGIS-free vector hydrography preprocessing for MGB inputs."""

from mgb_vec_hydro.execution.vector import VectorTable
from mgb_vec_hydro.preparation import (
    NamedRaster,
    PreparationReport,
    PreparationSpec,
    PreparedDataset,
    prepare_dataset,
)
from mgb_vec_hydro.roi import RoiDataset, RoiReport, RoiSpec, define_roi_dataset
from mgb_vec_hydro.sampling import (
    MiniSamplingReport,
    MiniSamplingSpec,
    sample_minibasins,
)
from mgb_vec_hydro.terrain import (
    TerrainDataset,
    TerrainReport,
    TerrainSpec,
    compute_flow_directions,
    compute_hand,
    compute_ltnd,
    create_terrain_dataset,
)

__version__ = "0.1.0"

__all__ = [
    "MiniSamplingReport",
    "MiniSamplingSpec",
    "NamedRaster",
    "PreparationReport",
    "PreparationSpec",
    "PreparedDataset",
    "RoiDataset",
    "RoiReport",
    "RoiSpec",
    "TerrainDataset",
    "TerrainReport",
    "TerrainSpec",
    "VectorTable",
    "compute_flow_directions",
    "compute_hand",
    "compute_ltnd",
    "create_terrain_dataset",
    "define_roi_dataset",
    "prepare_dataset",
    "sample_minibasins",
]
