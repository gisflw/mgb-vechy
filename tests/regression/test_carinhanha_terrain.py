import math
from pathlib import Path

from affine import Affine
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.windows import Window, from_bounds

from mgb_vec_hydro.terrain import (
    compute_flow_directions,
    compute_hand,
    compute_ltnd,
)


FIXTURE = Path(__file__).parents[1] / "carinhanha"


def test_terrain_routing_matches_reference_statistics_on_representative_catchment():
    """Protect broad terrain behavior without requiring pixel-identical routing."""

    catchments = gpd.read_file(FIXTURE / "expected" / "mareas.shp")
    segments = gpd.read_file(FIXTURE / "expected" / "mtrecs.shp").set_index("cotrecho")
    catchment = catchments.loc[catchments["cotrecho"] == 921256].iloc[0]

    with (
        rasterio.open(FIXTURE / "input" / "dem90.tif") as dem_source,
        rasterio.open(FIXTURE / "expected" / "hand.tif") as reference_hand_source,
        rasterio.open(FIXTURE / "expected" / "ltnd.tif") as reference_ltnd_source,
    ):
        raw = from_bounds(*catchment.geometry.bounds, transform=dem_source.transform)
        col0 = max(0, math.floor(raw.col_off) - 1)
        row0 = max(0, math.floor(raw.row_off) - 1)
        col1 = min(dem_source.width, math.ceil(raw.col_off + raw.width) + 1)
        row1 = min(dem_source.height, math.ceil(raw.row_off + raw.height) + 1)
        window = Window(col0, row0, col1 - col0, row1 - row0)
        geographic_transform = dem_source.window_transform(window)
        elevation = dem_source.read(1, window=window).astype(float)
        labels = rasterize(
            [(catchment.geometry, 0)],
            out_shape=elevation.shape,
            transform=geographic_transform,
            fill=-1,
            dtype="int32",
        )
        drainage = rasterize(
            [(segments.loc[catchment["cotrecho"]].geometry, 1)],
            out_shape=elevation.shape,
            transform=geographic_transform,
            fill=0,
            dtype="uint8",
        ).astype(bool) & (labels == 0)
        reference_hand = reference_hand_source.read(1, window=window)
        reference_ltnd = reference_ltnd_source.read(1, window=window)

    latitude = catchment.geometry.centroid.y
    metric_transform = Affine(
        abs(geographic_transform.a) * 111_320 * math.cos(math.radians(latitude)),
        0,
        0,
        0,
        -abs(geographic_transform.e) * 110_574,
        0,
    )
    direction, rank = compute_flow_directions(
        elevation, labels, drainage, metric_transform
    )
    hand = compute_hand(elevation, direction, rank)
    ltnd_km = compute_ltnd(direction, metric_transform, rank) / 1_000
    compared = (
        (labels == 0)
        & np.isfinite(hand)
        & np.isfinite(reference_hand)
        & np.isfinite(reference_ltnd)
    )

    assert compared.sum() > 1_000
    assert np.corrcoef(hand[compared], reference_hand[compared])[0, 1] > 0.9
    assert np.mean(np.abs(hand[compared] - reference_hand[compared])) < 5
    assert np.corrcoef(ltnd_km[compared], reference_ltnd[compared])[0, 1] > 0.9
    assert np.mean(np.abs(ltnd_km[compared] - reference_ltnd[compared])) < 0.5
    assert abs(np.count_nonzero(hand[compared] < 0)
               - np.count_nonzero(reference_hand[compared] < 0)) < 15
