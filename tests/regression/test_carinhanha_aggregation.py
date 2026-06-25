from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest

from mgb_vec_hydro.aggregation import INPUT_COLUMNS, aggregate_minibasins


pytest.importorskip("geopandas")

ROOT = Path(__file__).resolve().parents[2]
CARINHANHA = ROOT / "tests" / "carinhanha"
LEGACY_ROI_SEGMENTS = CARINHANHA / "output" / "roi_trecs.shp"
LEGACY_ROI_CATCHMENTS = CARINHANHA / "output" / "roi_areas.shp"


def test_carinhanha_aggregation_regression_properties():
    legacy_segments = gpd.read_file(LEGACY_ROI_SEGMENTS)
    legacy_catchments = gpd.read_file(LEGACY_ROI_CATCHMENTS)
    roi_segments, roi_catchments = _legacy_roi_to_input(
        legacy_segments,
        legacy_catchments,
    )

    result = aggregate_minibasins(
        roi_catchments,
        roi_segments,
        uparea_min=30,
        lmin=6,
    )

    assert len(result.segments) == 556
    assert len(result.catchments) == 556
    assert len(result.mapping) == len(roi_catchments)
    assert list(result.segments.columns) == INPUT_COLUMNS
    assert list(result.catchments.columns) == INPUT_COLUMNS
    assert list(result.mapping.columns) == ["id", "mini_id", "sub", "geometry"]
    assert result.catchments["unit_area"].sum() == pytest.approx(
        roi_catchments["unit_area"].sum()
    )
    assert result.segments["id"].is_unique
    assert result.catchments["id"].is_unique
    assert result.mapping["id"].is_unique
    assert list(result.segments["id"].head(10)) == [
        100864,
        103105,
        116794,
        118204,
        118409,
        91665,
        120326,
        120819,
        120832,
        121339,
    ]


def _legacy_roi_to_input(
    legacy_segments: gpd.GeoDataFrame,
    legacy_catchments: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    segment_ids = set(legacy_segments["cotrecho"])
    id_down = legacy_segments["nutrjus"].where(
        legacy_segments["nutrjus"].isin(segment_ids),
        None,
    )
    common = {
        "id": legacy_segments["cotrecho"].to_numpy(),
        "id_down": id_down.to_numpy(),
        "sub": legacy_segments["sub"].to_numpy(),
        "strahler_order": legacy_segments["nustrahler"].to_numpy(),
        "unit_length": legacy_segments["nucomptrec"].to_numpy(),
        "upstream_length": legacy_segments["nucomptrec"].to_numpy(),
        "unit_area": legacy_segments["nuareacont"].to_numpy(),
        "upstream_area": legacy_segments["nuareamont"].to_numpy(),
    }
    common["water_course"] = _legacy_water_course(common)
    catchment_geometry = (
        legacy_catchments.set_index("cotrecho")
        .loc[legacy_segments["cotrecho"], "geometry"]
        .to_numpy()
    )

    roi_segments = gpd.GeoDataFrame(
        {**common, "geometry": legacy_segments.geometry.to_numpy()},
        crs=legacy_segments.crs,
    )
    roi_catchments = gpd.GeoDataFrame(
        {**common, "geometry": catchment_geometry},
        crs=legacy_catchments.crs,
    )
    return roi_segments[INPUT_COLUMNS], roi_catchments[INPUT_COLUMNS]


def _legacy_water_course(common: dict[str, object]) -> pd.Series:
    segments = pd.DataFrame(common)
    water_course_by_id = {}
    for _, group in segments.groupby("sub", sort=False):
        ids = set(group["id"].tolist())
        downstream_by_id = dict(
            group[["id", "id_down"]].itertuples(index=False, name=None)
        )
        upstream_by_downstream = {segment_id: [] for segment_id in ids}
        for segment_id, downstream_id in downstream_by_id.items():
            if downstream_id in ids:
                upstream_by_downstream[downstream_id].append(segment_id)

        attrs = group.set_index("id")[["upstream_area", "unit_length"]]
        roots = [
            segment_id
            for segment_id, downstream_id in downstream_by_id.items()
            if downstream_id not in ids
        ]
        stack = list(roots)
        for root in roots:
            water_course_by_id[root] = root
        while stack:
            segment_id = stack.pop()
            children = upstream_by_downstream.get(segment_id, [])
            if not children:
                continue
            main_child = max(
                children,
                key=lambda child: (
                    attrs.at[child, "upstream_area"],
                    attrs.at[child, "unit_length"],
                    str(child),
                ),
            )
            for child in children:
                water_course_by_id[child] = (
                    water_course_by_id[segment_id] if child == main_child else child
                )
                stack.append(child)

    return pd.Series(common["id"]).map(water_course_by_id)
