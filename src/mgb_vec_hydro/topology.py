from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Hashable

import pandas as pd

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    MissingColumnsError,
    OutletNotFoundError,
    TopologyCycleError,
)


DEFAULT_SEG_ID_COL = "seg_id"
DEFAULT_SEG_ID_DOWN_COL = "seg_id_down"
DEFAULT_CATCH_ID_COL = "catch_id"


@dataclass(frozen=True)
class UpstreamSelection:
    """Segment and catchment IDs selected by upstream traversal."""

    segment_ids: set[Hashable]
    catchment_ids: set[Hashable]


def find_upstream_segments(
    segments: pd.DataFrame,
    outlet_ids: Iterable[Hashable],
    *,
    seg_id_col: str = DEFAULT_SEG_ID_COL,
    seg_id_down_col: str = DEFAULT_SEG_ID_DOWN_COL,
    catch_id_col: str = DEFAULT_CATCH_ID_COL,
) -> set[Hashable]:
    """Return all segment IDs upstream of the ordered outlet IDs."""

    selection = find_upstream_selection(
        segments,
        outlet_ids,
        seg_id_col=seg_id_col,
        seg_id_down_col=seg_id_down_col,
        catch_id_col=catch_id_col,
    )
    return selection.segment_ids


def find_upstream_catchments(
    segments: pd.DataFrame,
    outlet_ids: Iterable[Hashable],
    *,
    seg_id_col: str = DEFAULT_SEG_ID_COL,
    seg_id_down_col: str = DEFAULT_SEG_ID_DOWN_COL,
    catch_id_col: str = DEFAULT_CATCH_ID_COL,
) -> set[Hashable]:
    """Return all catchment IDs upstream of the ordered outlet IDs."""

    selection = find_upstream_selection(
        segments,
        outlet_ids,
        seg_id_col=seg_id_col,
        seg_id_down_col=seg_id_down_col,
        catch_id_col=catch_id_col,
    )
    return selection.catchment_ids


def find_upstream_selection(
    segments: pd.DataFrame,
    outlet_ids: Iterable[Hashable],
    *,
    seg_id_col: str = DEFAULT_SEG_ID_COL,
    seg_id_down_col: str = DEFAULT_SEG_ID_DOWN_COL,
    catch_id_col: str = DEFAULT_CATCH_ID_COL,
) -> UpstreamSelection:
    """Return upstream segment and catchment IDs for one or more outlets."""

    outlet_list = list(outlet_ids)
    _validate_columns(segments, [seg_id_col, seg_id_down_col, catch_id_col])
    _validate_unique_segments(segments, seg_id_col)

    segment_ids = set(segments[seg_id_col].tolist())
    missing_outlets = [
        outlet_id for outlet_id in outlet_list if outlet_id not in segment_ids
    ]
    if missing_outlets:
        missing = ", ".join(str(value) for value in missing_outlets)
        raise OutletNotFoundError(f"Outlet segment ID(s) not found: {missing}")

    upstream_by_downstream = _build_reverse_adjacency(
        segments,
        seg_id_col=seg_id_col,
        seg_id_down_col=seg_id_down_col,
    )

    selected_segments: set[Hashable] = set()
    for outlet_id in outlet_list:
        selected_segments.update(_walk_upstream(outlet_id, upstream_by_downstream))

    catchment_ids = set(
        segments.loc[
            segments[seg_id_col].isin(selected_segments), catch_id_col
        ].tolist()
    )
    return UpstreamSelection(
        segment_ids=selected_segments,
        catchment_ids=catchment_ids,
    )


def _validate_columns(table: pd.DataFrame, required_columns: Iterable[str]) -> None:
    missing = [column for column in required_columns if column not in table.columns]
    if missing:
        raise MissingColumnsError(
            "Missing required column(s): " + ", ".join(missing)
        )


def _validate_unique_segments(table: pd.DataFrame, seg_id_col: str) -> None:
    duplicated = table.loc[table[seg_id_col].duplicated(), seg_id_col].tolist()
    if duplicated:
        values = ", ".join(str(value) for value in duplicated)
        raise DuplicateSegmentIdError(f"Found duplicate segment ID(s): {values}")


def _build_reverse_adjacency(
    segments: pd.DataFrame,
    *,
    seg_id_col: str,
    seg_id_down_col: str,
) -> dict[Hashable, list[Hashable]]:
    upstream_by_downstream: dict[Hashable, list[Hashable]] = defaultdict(list)
    segment_ids = set(segments[seg_id_col].tolist())

    for segment_id, downstream_id in segments[[seg_id_col, seg_id_down_col]].itertuples(
        index=False,
        name=None,
    ):
        if _is_sink_value(downstream_id):
            continue
        if downstream_id in segment_ids:
            upstream_by_downstream[downstream_id].append(segment_id)

    return dict(upstream_by_downstream)


def _walk_upstream(
    outlet_id: Hashable,
    upstream_by_downstream: dict[Hashable, list[Hashable]],
) -> set[Hashable]:
    selected: set[Hashable] = set()
    visiting: set[Hashable] = set()

    def visit(segment_id: Hashable) -> None:
        if segment_id in visiting:
            raise TopologyCycleError(
                f"Detected topology cycle at segment ID {segment_id}"
            )
        if segment_id in selected:
            return

        visiting.add(segment_id)
        for upstream_id in upstream_by_downstream.get(segment_id, []):
            visit(upstream_id)
        visiting.remove(segment_id)
        selected.add(segment_id)

    visit(outlet_id)
    return selected


def _is_sink_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    try:
        return bool(pd.isna(value))
    except TypeError:
        return False
