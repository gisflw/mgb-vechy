import time
from pathlib import Path

import pandas as pd
import pytest

from mgb_vec_hydro.aggregation import AGGREGATION_COLUMNS, aggregate_minibasins
from mgb_vec_hydro.execution.vector import VectorTable, read_vector_table

ROOT = Path(__file__).resolve().parents[2]
CARINHANHA = ROOT / "tests" / "carinhanha"
LEGACY_ROI_SEGMENTS = CARINHANHA / "output" / "roi_trecs.shp"
LEGACY_ROI_CATCHMENTS = CARINHANHA / "output" / "roi_areas.shp"


def test_carinhanha_aggregation_regression_properties():
    legacy_segments = read_vector_table(LEGACY_ROI_SEGMENTS).to_pandas()
    legacy_catchments = read_vector_table(LEGACY_ROI_CATCHMENTS).to_pandas()
    roi_segments, roi_catchments = _legacy_roi_to_input(
        legacy_segments,
        legacy_catchments,
    )

    started = time.perf_counter()
    result = aggregate_minibasins(
        roi_catchments,
        roi_segments,
        uparea_min=30,
        lmin=6,
    )
    elapsed = time.perf_counter() - started

    assert len(result.segments) == 207
    assert len(result.catchments) == 207
    assert len(result.mapping) == len(roi_catchments)
    assert list(result.segments.columns) == AGGREGATION_COLUMNS
    assert list(result.catchments.columns) == AGGREGATION_COLUMNS
    assert list(result.mapping.columns) == [
        "id",
        "mini_id",
        "sub",
        "longitude",
        "latitude",
    ]
    result_segments = result.segments.to_pandas()
    result_catchments = result.catchments.to_pandas()
    assert result_catchments["unit_area"].sum() == pytest.approx(
        roi_catchments.to_pandas()["unit_area"].sum()
    )
    assert result_segments["id"].is_unique
    assert result_catchments["id"].is_unique
    assert result.mapping["id"].is_unique
    assert (result_segments["upstream_area"] < 30).sum() == 0
    assert (result_segments["unit_length"] < 6).sum() == 0
    assert list(result_segments["id"].head(10)) == list(range(1, 11))
    assert set(result_segments["id_down"]).issubset(set(result_segments["id"]) | {-1})
    # The pre-refactor baseline on this fixture was approximately 16.8 seconds.
    assert elapsed < 8.4


def _legacy_roi_to_input(
    legacy_segments: pd.DataFrame,
    legacy_catchments: pd.DataFrame,
):
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

    crs = read_vector_table(LEGACY_ROI_SEGMENTS).crs
    roi_segments = VectorTable.from_pydict(
        common, legacy_segments["geometry"].to_numpy(), crs=crs
    )
    roi_catchments = VectorTable.from_pydict(common, catchment_geometry, crs=crs)
    return roi_segments, roi_catchments


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
