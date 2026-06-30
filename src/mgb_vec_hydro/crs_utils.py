"""Shared CRS validation and in-memory raster reprojection."""

from __future__ import annotations

from dataclasses import dataclass
import math

import geopandas as gpd
from pyproj import CRS
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform

from mgb_vec_hydro.exceptions import MgbVecHydroError


DEFAULT_CRS = "EPSG:6933"


class CrsError(MgbVecHydroError):
    """Raised when spatial reference metadata cannot be resolved safely."""


def parse_metric_crs(value: str | CRS = DEFAULT_CRS) -> CRS:
    """Return a projected CRS whose horizontal unit is exactly one metre."""
    try:
        crs = CRS.from_user_input(value)
    except Exception as exc:
        raise CrsError(f"Invalid target CRS: {value!r}") from exc
    if not crs.is_projected:
        raise CrsError("Target CRS must be projected")
    axes = crs.axis_info
    if not axes or any(
        axis.unit_name.lower() not in {"metre", "meter", "metres", "meters"}
        or not math.isclose(axis.unit_conversion_factor, 1.0)
        for axis in axes[:2]
    ):
        raise CrsError("Target CRS linear units must be metres")
    return crs


def transform_vector(
    frame: gpd.GeoDataFrame,
    target_crs: str | CRS = DEFAULT_CRS,
    *,
    name: str = "vector",
    source_crs: str | CRS | None = None,
) -> gpd.GeoDataFrame:
    """Validate CRS metadata and return a frame expressed in target CRS."""
    target = parse_metric_crs(target_crs)
    if source_crs is not None:
        frame = frame.set_crs(source_crs, allow_override=True)
    elif frame.crs is None:
        raise CrsError(f"{name} has no CRS")
    return frame.to_crs(target)


@dataclass(frozen=True)
class RasterGrid:
    crs: CRS
    transform: object
    width: int
    height: int


def raster_grid(dataset, target_crs: str | CRS = DEFAULT_CRS) -> RasterGrid:
    """Calculate the virtual destination grid while retaining nominal resolution."""
    target = parse_metric_crs(target_crs)
    if dataset.crs is None:
        raise CrsError("Raster has no CRS")
    transform, width, height = calculate_default_transform(
        dataset.crs, target, dataset.width, dataset.height, *dataset.bounds
    )
    return RasterGrid(target, transform, width, height)


def warped_vrt(
    dataset,
    target_crs: str | CRS = DEFAULT_CRS,
    *,
    resampling: Resampling = Resampling.bilinear,
    grid: RasterGrid | None = None,
) -> WarpedVRT:
    """Expose a raster in a metric CRS without creating an intermediate file."""
    if dataset.crs is None:
        raise CrsError("Raster has no CRS")
    destination = grid or raster_grid(dataset, target_crs)
    target = parse_metric_crs(target_crs)
    if destination.crs != target:
        raise CrsError("Raster destination grid CRS does not match target CRS")
    return WarpedVRT(
        dataset,
        crs=target,
        transform=destination.transform,
        width=destination.width,
        height=destination.height,
        resampling=resampling,
    )


def require_aligned_sources(first, second, first_name="HAND", second_name="LTND") -> None:
    """Require two source rasters to share one grid before virtual transformation."""
    if first.crs is None or second.crs is None:
        missing = first_name if first.crs is None else second_name
        raise CrsError(f"{missing} raster has no CRS")
    if (
        first.crs != second.crs
        or first.shape != second.shape
        or not first.transform.almost_equals(second.transform)
    ):
        raise CrsError(f"{first_name} and {second_name} source rasters are not aligned")
