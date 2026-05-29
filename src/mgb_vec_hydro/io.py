from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from mgb_vec_hydro.exceptions import UnsupportedOutputFormatError


SUPPORTED_OUTPUT_FORMATS = {
    "fgb": ("FlatGeobuf", ".fgb"),
    "gpkg": ("GPKG", ".gpkg"),
    "shp": ("ESRI Shapefile", ".shp"),
}


@dataclass(frozen=True)
class RoiOutputPaths:
    """Output paths for Stage 1 ROI files."""

    catchments: Path
    segments: Path


def read_vector(path: str | Path) -> gpd.GeoDataFrame:
    """Read a vector layer into a GeoDataFrame."""

    return gpd.read_file(Path(path))


def output_paths(output_dir: str | Path, output_format: str) -> RoiOutputPaths:
    """Return legacy ROI output names for the requested format."""

    output_format = output_format.lower()
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_OUTPUT_FORMATS))
        raise UnsupportedOutputFormatError(
            f"Unsupported output format '{output_format}'. Supported formats: {supported}"
        )

    suffix = SUPPORTED_OUTPUT_FORMATS[output_format][1]
    output_dir = Path(output_dir)
    return RoiOutputPaths(
        catchments=output_dir / f"roi_areas{suffix}",
        segments=output_dir / f"roi_trecs{suffix}",
    )


def write_vector(
    gdf: gpd.GeoDataFrame,
    path: str | Path,
    *,
    output_format: str,
) -> Path:
    """Write a GeoDataFrame using a supported vector driver."""

    output_format = output_format.lower()
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_OUTPUT_FORMATS))
        raise UnsupportedOutputFormatError(
            f"Unsupported output format '{output_format}'. Supported formats: {supported}"
        )

    driver = SUPPORTED_OUTPUT_FORMATS[output_format][0]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, driver=driver)
    return path
