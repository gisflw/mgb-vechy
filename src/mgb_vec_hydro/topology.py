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


DEFAULT_ID_COL = "id"
DEFAULT_ID_DOWN_COL = "id_down"


@dataclass(frozen=True)
class UpstreamSelection:
    """IDs selected by upstream traversal."""

    ids: set[Hashable]


def find_upstream_segments(
    segments: pd.DataFrame,
    outlet_ids: Iterable[Hashable],
    *,
    id_col: str = DEFAULT_ID_COL,
    id_down_col: str = DEFAULT_ID_DOWN_COL,
) -> set[Hashable]:
    """Return all IDs upstream of the ordered outlet IDs."""

    selection = find_upstream_selection(
        segments,
        outlet_ids,
        id_col=id_col,
        id_down_col=id_down_col,
    )
    return selection.ids


def find_upstream_selection(
    segments: pd.DataFrame,
    outlet_ids: Iterable[Hashable],
    *,
    id_col: str = DEFAULT_ID_COL,
    id_down_col: str = DEFAULT_ID_DOWN_COL,
) -> UpstreamSelection:
    """Return upstream IDs for one or more outlets."""

    outlet_list = list(outlet_ids)
    _validate_columns(segments, [id_col, id_down_col])
    _validate_unique_segments(segments, id_col)

    ids = set(segments[id_col].tolist())
    missing_outlets = [outlet_id for outlet_id in outlet_list if outlet_id not in ids]
    if missing_outlets:
        missing = ", ".join(str(value) for value in missing_outlets)
        raise OutletNotFoundError(f"Outlet segment ID(s) not found: {missing}")

    upstream_by_downstream = _build_reverse_adjacency(
        segments,
        id_col=id_col,
        id_down_col=id_down_col,
    )

    selected_ids: set[Hashable] = set()
    for outlet_id in outlet_list:
        selected_ids.update(_walk_upstream(outlet_id, upstream_by_downstream))

    return UpstreamSelection(ids=selected_ids)


def _validate_columns(table: pd.DataFrame, required_columns: Iterable[str]) -> None:
    missing = [column for column in required_columns if column not in table.columns]
    if missing:
        raise MissingColumnsError(
            "Missing required column(s): " + ", ".join(missing)
        )


def _validate_unique_segments(table: pd.DataFrame, id_col: str) -> None:
    duplicated = table.loc[table[id_col].duplicated(), id_col].tolist()
    if duplicated:
        values = ", ".join(str(value) for value in duplicated)
        raise DuplicateSegmentIdError(f"Found duplicate segment ID(s): {values}")


def _build_reverse_adjacency(
    segments: pd.DataFrame,
    *,
    id_col: str,
    id_down_col: str,
) -> dict[Hashable, list[Hashable]]:
    upstream_by_downstream: dict[Hashable, list[Hashable]] = defaultdict(list)
    ids = set(segments[id_col].tolist())

    for segment_id, downstream_id in segments[[id_col, id_down_col]].itertuples(
        index=False,
        name=None,
    ):
        if _is_sink_value(downstream_id):
            continue
        if downstream_id in ids:
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
