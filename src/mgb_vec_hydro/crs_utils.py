"""Shared CRS validation and in-memory raster reprojection."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math

import pyarrow as pa
from pyproj import CRS, Geod
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform

from mgb_vec_hydro.exceptions import MgbVecHydroError
from mgb_vec_hydro.execution.vector import VectorTable
import shapely
from pyproj import Transformer

DEFAULT_CRS = "EPSG:6933"


class CrsError(MgbVecHydroError):
    """Raised when spatial reference metadata cannot be resolved safely."""


@lru_cache(maxsize=16)
def geodetic_tools(crs_text: str) -> tuple[Transformer, Geod]:
    """Cache a source-to-degree lon/lat transformer and its native ellipsoid."""
    crs = parse_crs(crs_text)
    geodetic = crs.geodetic_crs
    if geodetic is None or crs.ellipsoid.semi_major_metre is None:
        raise CrsError("Source CRS has no usable geodetic ellipsoid")
    # Geod consumes degrees, including when the source geographic CRS uses grads.
    geographic = geodetic.to_2d().to_json_dict()
    for axis in geographic["coordinate_system"]["axis"]:
        axis["unit"] = "degree"
    target = CRS.from_json_dict(geographic)
    return Transformer.from_crs(crs, target, always_xy=True), crs.get_geod()


def require_metre_units(dataset, name: str) -> None:
    """Require elevations and route distances stored in metres."""
    if dataset.tags().get("units") != "m":
        raise CrsError(
            f"{name} must declare stored values in metres (units=m)"
        )


def parse_metric_crs(value: str | CRS = DEFAULT_CRS) -> CRS:
    """Return a projected CRS whose horizontal unit is exactly one metre."""
    crs = CRS.from_user_input(value)
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


def parse_crs(value: str | CRS) -> CRS:
    """Parse any valid CRS without imposing projected metric units."""
    return CRS.from_user_input(value)


def transform_vector(
    frame: VectorTable,
    target_crs: str | CRS = DEFAULT_CRS,
    *,
    name: str = "vector",
    source_crs: str | CRS | None = None,
) -> VectorTable:
    """Validate CRS metadata and return an Arrow vector in the target CRS."""
    target = parse_metric_crs(target_crs)
    if source_crs is not None:
        source = parse_crs(source_crs)
    else:
        source = frame.crs
    if source == target:
        return VectorTable(
            frame.table, target, frame.geometry_column, frame.geometry_type
        )
    transformer = Transformer.from_crs(source, target, always_xy=True)
    geometries = shapely.transform(
        frame.geometries(), transformer.transform, interleaved=False
    )
    wkb = pa.array(shapely.to_wkb(geometries), type=pa.binary())
    index = frame.table.schema.get_field_index(frame.geometry_column)
    table = frame.table.set_column(index, frame.geometry_column, wkb)
    return VectorTable(table, target, frame.geometry_column, frame.geometry_type)


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


def require_aligned_sources(
    first, second, first_name="HAND", second_name="LTND"
) -> None:
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
