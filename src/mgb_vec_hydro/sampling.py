from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask, geometry_window
from rasterio.enums import Resampling
from shapely.geometry import box, mapping
import shapely
from pyproj import Transformer

from mgb_vec_hydro.aggregation import INPUT_COLUMNS
from mgb_vec_hydro.exceptions import MiniSamplingError
from mgb_vec_hydro.execution.vector import VectorTable
from mgb_vec_hydro.crs_utils import (
    DEFAULT_CRS,
    raster_grid,
    require_aligned_sources,
    transform_vector,
    warped_vrt,
)


@dataclass(frozen=True)
class MiniSamplingResult:
    """Sampled mini-basin table and in-memory diagnostics."""

    sampled_minis: pd.DataFrame
    diagnostics: dict[str, object]

    @property
    def catchments(self) -> pd.DataFrame:
        """Alias for the attributed catchment rows."""
        return self.sampled_minis


def sample_minibasins(
    catchments: VectorTable,
    segments: VectorTable,
    dem: str | Path,
    hand: str | Path,
    ltnd: str | Path,
    hru: str | Path,
    *,
    crs: str = DEFAULT_CRS,
) -> MiniSamplingResult:
    """Sample terrain and categorical HRU attributes for Stage 2 mini-basins."""

    catchments = transform_vector(catchments, crs, name="catchments")
    segments = transform_vector(segments, crs, name="segments")
    _validate_vectors(catchments, segments)
    catchment_attrs = _attributes(catchments)
    segment_attrs = _attributes(segments)
    catchment_geometries = catchments.geometries()
    segment_geometries = segments.geometries()
    reaches = {
        value: (row, geometry)
        for value, (_, row), geometry in zip(
            segment_attrs["id"],
            segment_attrs.iterrows(),
            segment_geometries,
            strict=True,
        )
    }
    centroids = shapely.centroid(catchment_geometries)
    transformer = Transformer.from_crs(catchments.crs, "EPSG:4326", always_xy=True)
    lonlat = shapely.transform(centroids, transformer.transform, interleaved=False)

    rows: list[dict] = []
    sampled_counts = {
        "minis": len(catchment_attrs),
        "catchment_cells": 0,
        "reach_cells": 0,
    }
    with ExitStack() as stack:
        sources = {
            name: stack.enter_context(rasterio.open(path))
            for name, path in {
                "dem": dem,
                "hand": hand,
                "ltnd": ltnd,
                "hru": hru,
            }.items()
        }
        require_aligned_sources(sources["hand"], sources["ltnd"])
        shared_grid = raster_grid(sources["hand"], crs)
        rasters = {
            "dem": stack.enter_context(
                warped_vrt(sources["dem"], crs, resampling=Resampling.bilinear)
            ),
            "hand": stack.enter_context(
                warped_vrt(
                    sources["hand"],
                    crs,
                    resampling=Resampling.bilinear,
                    grid=shared_grid,
                )
            ),
            "ltnd": stack.enter_context(
                warped_vrt(
                    sources["ltnd"],
                    crs,
                    resampling=Resampling.bilinear,
                    grid=shared_grid,
                )
            ),
            "hru": stack.enter_context(
                warped_vrt(sources["hru"], crs, resampling=Resampling.nearest)
            ),
        }
        _validate_rasters(rasters, catchments)
        classes = _discover_hru_classes(rasters["hru"])
        sampled_counts["hru_class_count"] = len(classes)
        sampled_counts["hru_class_ids"] = classes

        for position, (_, catchment) in enumerate(catchment_attrs.iterrows()):
            mini_id = catchment["id"]
            reach, reach_geometry = reaches[mini_id]
            catchment_geometry = catchment_geometries[position]
            dem_values = _sample(rasters["dem"], reach_geometry, mini_id, "DEM", True)
            hand_values, catchment_mask = _sample(
                rasters["hand"],
                catchment_geometry,
                mini_id,
                "HAND",
                False,
                return_mask=True,
            )
            ltnd_values, ltnd_mask = _sample(
                rasters["ltnd"],
                catchment_geometry,
                mini_id,
                "LTND",
                False,
                return_mask=True,
            )
            if not np.array_equal(catchment_mask, ltnd_mask):
                raise MiniSamplingError(
                    "HAND and LTND sampled-cell masks are not aligned"
                )
            hru_values = _sample(
                rasters["hru"], catchment_geometry, mini_id, "HRU", False
            )

            length_km = float(reach["unit_length"])
            if (
                not np.isfinite(length_km)
                or length_km <= 0
                or reach_geometry.length <= 0
            ):
                raise MiniSamplingError(
                    f"Mini {mini_id} has a zero or invalid reach length"
                )
            maximum_ltnd = float(np.max(ltnd_values))
            if maximum_ltnd <= 0:
                raise MiniSamplingError(f"Mini {mini_id} has non-positive maximum LTND")
            at_maximum = np.isclose(ltnd_values, maximum_ltnd)

            row = {
                column: catchment[column]
                for column in INPUT_COLUMNS
                if column != "geometry"
            }
            row.update(
                longitude=float(shapely.get_x(lonlat[position])),
                latitude=float(shapely.get_y(lonlat[position])),
                reach_slope_m_per_km=float(
                    (np.percentile(dem_values, 85) - np.percentile(dem_values, 10))
                    / (0.75 * length_km)
                ),
                tributary_length_km=maximum_ltnd / 1000.0,
                tributary_slope_m_per_km=float(
                    np.mean(hand_values[at_maximum]) / (maximum_ltnd / 1000.0)
                ),
            )
            for class_id in classes:
                row[f"hru_{class_id}_pct"] = (
                    100.0 * np.count_nonzero(hru_values == class_id) / hru_values.size
                )
            rows.append(row)
            sampled_counts["catchment_cells"] += int(hand_values.size)
            sampled_counts["reach_cells"] += int(dem_values.size)

    result = pd.DataFrame(rows)
    _validate_finite_results(result)
    percentage_columns = [f"hru_{value}_pct" for value in classes]
    if not np.allclose(result[percentage_columns].sum(axis=1), 100.0):
        raise MiniSamplingError("HRU percentages do not sum to 100%")
    return MiniSamplingResult(result, sampled_counts)


def _validate_finite_results(result: pd.DataFrame) -> None:
    """Allow null outlet links while rejecting all other non-finite numbers."""

    numeric = result.select_dtypes(include=[np.number])
    for column in numeric:
        values = numeric[column].to_numpy(dtype=float)
        if column == "id_down":
            values = values[~np.isnan(values)]
        if not np.isfinite(values).all():
            raise MiniSamplingError(
                "Sampled mini-basin results contain non-finite values"
            )


def _validate_vectors(catchments: VectorTable, reaches: VectorTable) -> None:
    for frame, name, geometry_type in (
        (catchments, "catchments", {"Polygon", "MultiPolygon"}),
        (reaches, "segments", {"LineString", "MultiLineString"}),
    ):
        if not isinstance(frame, VectorTable) or list(frame.columns) != INPUT_COLUMNS:
            raise MiniSamplingError(f"{name} must have the exact Stage 2 schema")
        if not frame.crs.is_projected:
            raise MiniSamplingError(f"{name} must have a projected CRS")
        geometries = frame.geometries()
        attrs = _attributes(frame)
        if (
            len(frame) == 0
            or np.any(shapely.is_empty(geometries))
            or np.any(shapely.is_missing(geometries))
        ):
            raise MiniSamplingError(f"{name} contains missing or empty geometry")
        if attrs["id"].isna().any():
            raise MiniSamplingError(f"{name} contains missing mini IDs")
        type_names = set(shapely.get_type_id(geometries).tolist())
        allowed = {1, 5} if name == "segments" else {3, 6}
        if not type_names.issubset(allowed):
            raise MiniSamplingError(f"{name} has invalid geometry types")
        if attrs["id"].duplicated().any():
            raise MiniSamplingError(f"{name} contains duplicate mini IDs")
        metric_columns = [
            "sub",
            "strahler_order",
            "unit_length",
            "upstream_length",
            "unit_area",
            "upstream_area",
        ]
        if any(
            not pd.api.types.is_numeric_dtype(attrs[column])
            for column in metric_columns
        ):
            raise MiniSamplingError(f"{name} has non-numeric Stage 2 metric columns")
    if catchments.crs != reaches.crs:
        raise MiniSamplingError("Mini catchment and reach CRS do not match")
    if set(_attributes(catchments)["id"]) != set(_attributes(reaches)["id"]):
        raise MiniSamplingError("Mini catchment and reach IDs do not match")


def _validate_rasters(rasters, catchments) -> None:
    for name, dataset in rasters.items():
        if dataset.count != 1 or dataset.crs is None:
            raise MiniSamplingError(
                f"{name.upper()} must be a single-band georeferenced raster"
            )
        if dataset.crs != catchments.crs:
            raise MiniSamplingError(f"{name.upper()} CRS does not match requested CRS")
    hand, ltnd = rasters["hand"], rasters["ltnd"]
    if (
        hand.shape != ltnd.shape
        or hand.transform != ltnd.transform
        or hand.crs != ltnd.crs
    ):
        raise MiniSamplingError("HAND and LTND rasters are not aligned")


def _attributes(vector: VectorTable) -> pd.DataFrame:
    return (
        vector.table.drop([vector.geometry_column]).to_pandas().reset_index(drop=True)
    )


def _discover_hru_classes(dataset) -> tuple[int, ...]:
    """Validate the complete categorical raster and return sorted classes."""

    if not np.issubdtype(np.dtype(dataset.dtypes[0]), np.integer):
        raise MiniSamplingError("HRU raster must have an integer data type")

    seen = np.zeros(101, dtype=bool)
    for _, window in dataset.block_windows(1):
        block = dataset.read(1, window=window, masked=True)
        values = np.asarray(block.data[~np.ma.getmaskarray(block)])
        if values.size == 0:
            continue
        if int(values.min()) < 1 or int(values.max()) > 100:
            raise MiniSamplingError(
                "HRU raster values must be within the inclusive domain 1..100"
            )
        # Range validation must precede indexing by untrusted raster values.
        seen[values] = True

    classes = tuple(int(value) for value in np.flatnonzero(seen))
    if not classes:
        raise MiniSamplingError("HRU raster contains no valid class values")
    if len(classes) > 100:
        raise MiniSamplingError("HRU raster contains more than 100 unique classes")
    return classes


def _sample(dataset, geometry, mini_id, name, all_touched, return_mask=False):
    raster_bounds = box(*dataset.bounds)
    if not raster_bounds.covers(geometry):
        raise MiniSamplingError(f"Mini {mini_id} has incomplete {name} raster coverage")
    window = geometry_window(dataset, [mapping(geometry)])
    band = dataset.read(1, window=window, masked=True)
    selected = geometry_mask(
        [mapping(geometry)],
        out_shape=band.shape,
        transform=dataset.window_transform(window),
        invert=True,
        all_touched=all_touched,
    )
    if not selected.any():
        raise MiniSamplingError(f"Mini {mini_id} has no sampled {name} raster cells")
    if np.ma.getmaskarray(band)[selected].any():
        raise MiniSamplingError(
            f"Mini {mini_id} contains {name} nodata within sampled cells"
        )
    values = np.asarray(band.data[selected])
    if not np.isfinite(values.astype(float)).all():
        raise MiniSamplingError(f"Mini {mini_id} contains non-finite {name} values")
    return (values, selected) if return_mask else values
