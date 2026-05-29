# Stage 1 ROI Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first milestone of `mgb-vec-hydro`: a QGIS-free Python package and CLI that defines ROI catchment and segment vectors from explicit network topology and an ordered downstream-to-upstream outlet list.

**Architecture:** Keep topology traversal as pure pandas logic, with no geospatial dependency. Put vector file handling behind small GeoPandas-based I/O helpers, and keep Stage 1 orchestration in `roi.py` so the CLI is a thin command boundary. Preserve BHO compatibility through configurable column names, not hard-coded BHO traversal rules.

**Tech Stack:** Python 3.11+, pandas, GeoPandas, pyogrio or Fiona through GeoPandas, Click, pytest.

---

## File Structure

Create:

- `pyproject.toml`: packaging metadata, runtime dependencies, console script, pytest configuration.
- `src/mgb_vec_hydro/__init__.py`: package version export.
- `src/mgb_vec_hydro/exceptions.py`: package-specific exception hierarchy.
- `src/mgb_vec_hydro/topology.py`: pure pandas upstream traversal and column validation.
- `src/mgb_vec_hydro/roi.py`: Stage 1 ROI selection and `sub` assignment.
- `src/mgb_vec_hydro/io.py`: vector read/write helpers and output format handling.
- `src/mgb_vec_hydro/cli.py`: Click CLI with `define-roi`.
- `tests/test_topology.py`: pure unit tests for traversal and validation.
- `tests/test_roi.py`: unit tests for ROI filtering and `sub` assignment with GeoDataFrames.
- `tests/test_cli.py`: CLI behavior tests using Click's isolated runner.
- `tests/test_import_boundaries.py`: asserts package imports without QGIS-related modules.
- `tests/regression/test_carinhanha_roi.py`: optional geospatial regression test that runs only when prepared vector inputs are present.

Modify:

- `.gitignore`: add Python build, cache, and generated output patterns.
- `docs/refactor_goal.md`: no changes for this milestone unless implementation uncovers a spec mismatch.

Do not modify:

- `legacy/`: use it as reference only.
- `tests/carinhanha/output/`: keep existing fixture outputs unchanged.
- `data/`: read-only reference data.

---

### Task 1: Package Skeleton And Import Boundary

**Files:**
- Create: `pyproject.toml`
- Create: `src/mgb_vec_hydro/__init__.py`
- Create: `src/mgb_vec_hydro/exceptions.py`
- Create: `tests/test_import_boundaries.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing import-boundary test**

Create `tests/test_import_boundaries.py`:

```python
import importlib
import sys


def test_package_import_does_not_load_qgis_modules():
    importlib.import_module("mgb_vec_hydro")

    forbidden_prefixes = ("qgis", "PyQt5", "processing")
    loaded = [name for name in sys.modules if name.startswith(forbidden_prefixes)]

    assert loaded == []
```

- [ ] **Step 2: Run the test to verify it fails because the package is missing**

Run:

```bash
pytest tests/test_import_boundaries.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'mgb_vec_hydro'`.

- [ ] **Step 3: Add packaging metadata**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "mgb-vec-hydro"
version = "0.1.0"
description = "Vector hydrography preprocessing tools for MGB model inputs"
readme = "docs/refactor_goal.md"
requires-python = ">=3.11"
license = { file = "LICENSE" }
authors = [
  { name = "MGB-Vec-Hydro contributors" }
]
dependencies = [
  "click>=8.1",
  "geopandas>=0.14",
  "pandas>=2.0",
  "pyogrio>=0.7",
  "shapely>=2.0",
]

[project.optional-dependencies]
test = [
  "pytest>=8.0",
]

[project.scripts]
mgb-vec-hydro = "mgb_vec_hydro.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 4: Add package init and exceptions**

Create `src/mgb_vec_hydro/__init__.py`:

```python
"""QGIS-free vector hydrography preprocessing for MGB inputs."""

__version__ = "0.1.0"
```

Create `src/mgb_vec_hydro/exceptions.py`:

```python
class MgbVecHydroError(Exception):
    """Base error for package-level failures."""


class MissingColumnsError(MgbVecHydroError):
    """Raised when an input table does not contain required columns."""


class DuplicateSegmentIdError(MgbVecHydroError):
    """Raised when segment IDs are duplicated in a topology table."""


class OutletNotFoundError(MgbVecHydroError):
    """Raised when a requested outlet segment ID is absent."""


class TopologyCycleError(MgbVecHydroError):
    """Raised when upstream traversal detects a cycle."""


class UnsupportedOutputFormatError(MgbVecHydroError):
    """Raised when the requested vector output format is unsupported."""
```

- [ ] **Step 5: Update `.gitignore`**

Append these lines to `.gitignore` if they are not already present:

```gitignore
__pycache__/
.pytest_cache/
.ruff_cache/
*.egg-info/
build/
dist/
output/
tmp/
```

- [ ] **Step 6: Run the import-boundary test**

Run:

```bash
pytest tests/test_import_boundaries.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit package skeleton**

Run:

```bash
git add pyproject.toml .gitignore src/mgb_vec_hydro/__init__.py src/mgb_vec_hydro/exceptions.py tests/test_import_boundaries.py
git commit -m "chore: add package skeleton"
```

---

### Task 2: Generic Topology Traversal

**Files:**
- Create: `src/mgb_vec_hydro/topology.py`
- Create: `tests/test_topology.py`

- [ ] **Step 1: Write topology tests**

Create `tests/test_topology.py`:

```python
import pandas as pd
import pytest

from mgb_vec_hydro.exceptions import (
    DuplicateSegmentIdError,
    MissingColumnsError,
    OutletNotFoundError,
    TopologyCycleError,
)
from mgb_vec_hydro.topology import (
    find_upstream_catchments,
    find_upstream_segments,
)


def test_find_upstream_segments_with_generic_columns():
    segments = pd.DataFrame(
        {
            "seg_id": [1, 2, 3, 4, 5],
            "seg_id_down": [None, 1, 2, 2, 4],
            "catch_id": ["a", "b", "c", "d", "e"],
        }
    )

    result = find_upstream_segments(segments, [2])

    assert result == {2, 3, 4, 5}


def test_find_upstream_catchments_with_bho_columns():
    segments = pd.DataFrame(
        {
            "cotrecho": [10, 20, 30, 40],
            "nutrjus": [None, 10, 20, 20],
            "cobacia": [100, 200, 300, 400],
        }
    )

    result = find_upstream_catchments(
        segments,
        [20],
        seg_id_col="cotrecho",
        seg_id_down_col="nutrjus",
        catch_id_col="cobacia",
    )

    assert result == {200, 300, 400}


def test_multiple_outlets_return_union():
    segments = pd.DataFrame(
        {
            "seg_id": [1, 2, 3, 4, 5, 6],
            "seg_id_down": [None, 1, 2, 1, 4, None],
            "catch_id": ["a", "b", "c", "d", "e", "f"],
        }
    )

    result = find_upstream_segments(segments, [2, 4])

    assert result == {2, 3, 4, 5}


def test_empty_string_downstream_is_sink():
    segments = pd.DataFrame(
        {
            "seg_id": ["a", "b", "c"],
            "seg_id_down": ["", "a", "b"],
            "catch_id": [1, 2, 3],
        }
    )

    result = find_upstream_segments(segments, ["a"])

    assert result == {"a", "b", "c"}


def test_missing_columns_raise_package_error():
    segments = pd.DataFrame({"seg_id": [1], "catch_id": [1]})

    with pytest.raises(MissingColumnsError, match="seg_id_down"):
        find_upstream_segments(segments, [1])


def test_duplicate_segment_ids_raise_package_error():
    segments = pd.DataFrame(
        {
            "seg_id": [1, 1],
            "seg_id_down": [None, None],
            "catch_id": [10, 20],
        }
    )

    with pytest.raises(DuplicateSegmentIdError, match="duplicate"):
        find_upstream_segments(segments, [1])


def test_missing_outlet_raises_package_error():
    segments = pd.DataFrame(
        {
            "seg_id": [1],
            "seg_id_down": [None],
            "catch_id": [10],
        }
    )

    with pytest.raises(OutletNotFoundError, match="99"):
        find_upstream_segments(segments, [99])


def test_cycle_detection_raises_package_error():
    segments = pd.DataFrame(
        {
            "seg_id": [1, 2, 3],
            "seg_id_down": [3, 1, 2],
            "catch_id": [10, 20, 30],
        }
    )

    with pytest.raises(TopologyCycleError, match="cycle"):
        find_upstream_segments(segments, [1])
```

- [ ] **Step 2: Run topology tests to verify they fail**

Run:

```bash
pytest tests/test_topology.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'mgb_vec_hydro.topology'`.

- [ ] **Step 3: Implement topology traversal**

Create `src/mgb_vec_hydro/topology.py`:

```python
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
    missing_outlets = [outlet_id for outlet_id in outlet_list if outlet_id not in segment_ids]
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
        selected_segments.update(
            _walk_upstream(outlet_id, upstream_by_downstream)
        )

    catchment_ids = set(
        segments.loc[segments[seg_id_col].isin(selected_segments), catch_id_col].tolist()
    )
    return UpstreamSelection(segment_ids=selected_segments, catchment_ids=catchment_ids)


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
            raise TopologyCycleError(f"Detected topology cycle at segment ID {segment_id}")
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
```

- [ ] **Step 4: Run topology tests**

Run:

```bash
pytest tests/test_topology.py -v
```

Expected: PASS.

- [ ] **Step 5: Run import-boundary test again**

Run:

```bash
pytest tests/test_import_boundaries.py tests/test_topology.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit topology traversal when working in a Git repository**

```bash
git add src/mgb_vec_hydro/topology.py tests/test_topology.py
git commit -m "feat: add generic topology traversal"
```

---

### Task 3: ROI Selection And Ordered `sub` Assignment

**Files:**
- Create: `src/mgb_vec_hydro/roi.py`
- Create: `tests/test_roi.py`

- [ ] **Step 1: Write ROI unit tests**

Create `tests/test_roi.py`:

```python
import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.roi import define_roi


def _segments_gdf():
    return gpd.GeoDataFrame(
        {
            "seg_id": [1, 2, 3, 4, 5, 6],
            "seg_id_down": [None, 1, 2, 1, 4, None],
            "catch_id": [10, 20, 30, 40, 50, 60],
            "length": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "geometry": [
                LineString([(0, 0), (1, 0)]),
                LineString([(1, 0), (2, 0)]),
                LineString([(2, 0), (3, 0)]),
                LineString([(1, 1), (2, 1)]),
                LineString([(2, 1), (3, 1)]),
                LineString([(0, 2), (1, 2)]),
            ],
        },
        crs="EPSG:4326",
    )


def _catchments_gdf():
    return gpd.GeoDataFrame(
        {
            "catch_id": [10, 20, 30, 40, 50, 60],
            "area": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
                Polygon([(2, 0), (3, 0), (3, 1), (2, 1)]),
                Polygon([(1, 1), (2, 1), (2, 2), (1, 2)]),
                Polygon([(2, 1), (3, 1), (3, 2), (2, 2)]),
                Polygon([(0, 2), (1, 2), (1, 3), (0, 3)]),
            ],
        },
        crs="EPSG:4326",
    )


def test_define_roi_returns_union_of_upstream_segments_and_catchments():
    roi = define_roi(_catchments_gdf(), _segments_gdf(), outlet_ids=[2, 4])

    assert set(roi.segments["seg_id"]) == {2, 3, 4, 5}
    assert set(roi.catchments["catch_id"]) == {20, 30, 40, 50}


def test_define_roi_assigns_sub_from_downstream_to_upstream_order():
    roi = define_roi(_catchments_gdf(), _segments_gdf(), outlet_ids=[1, 2])

    segment_sub = dict(zip(roi.segments["seg_id"], roi.segments["sub"]))
    catchment_sub = dict(zip(roi.catchments["catch_id"], roi.catchments["sub"]))

    assert segment_sub[1] == 2
    assert segment_sub[4] == 2
    assert segment_sub[5] == 2
    assert segment_sub[2] == 1
    assert segment_sub[3] == 1
    assert catchment_sub[10] == 2
    assert catchment_sub[40] == 2
    assert catchment_sub[50] == 2
    assert catchment_sub[20] == 1
    assert catchment_sub[30] == 1


def test_define_roi_supports_bho_column_mapping():
    segments = gpd.GeoDataFrame(
        {
            "cotrecho": [10, 20, 30],
            "nutrjus": [None, 10, 20],
            "cobacia": [100, 200, 300],
            "geometry": [
                LineString([(0, 0), (1, 0)]),
                LineString([(1, 0), (2, 0)]),
                LineString([(2, 0), (3, 0)]),
            ],
        },
        crs="EPSG:4326",
    )
    catchments = gpd.GeoDataFrame(
        {
            "cobacia": [100, 200, 300],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
                Polygon([(2, 0), (3, 0), (3, 1), (2, 1)]),
            ],
        },
        crs="EPSG:4326",
    )

    roi = define_roi(
        catchments,
        segments,
        outlet_ids=[20],
        seg_id_col="cotrecho",
        seg_id_down_col="nutrjus",
        catch_id_col="cobacia",
    )

    assert list(roi.segments["cotrecho"]) == [20, 30]
    assert list(roi.catchments["cobacia"]) == [200, 300]
    assert list(roi.segments["sub"]) == [1, 1]
    assert list(roi.catchments["sub"]) == [1, 1]
```

- [ ] **Step 2: Run ROI tests to verify they fail**

Run:

```bash
pytest tests/test_roi.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'mgb_vec_hydro.roi'`.

- [ ] **Step 3: Implement ROI orchestration**

Create `src/mgb_vec_hydro/roi.py`:

```python
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Hashable

import geopandas as gpd
import pandas as pd

from mgb_vec_hydro.topology import (
    DEFAULT_CATCH_ID_COL,
    DEFAULT_SEG_ID_COL,
    DEFAULT_SEG_ID_DOWN_COL,
    find_upstream_selection,
)


@dataclass(frozen=True)
class RoiResult:
    """ROI catchments and segments produced by Stage 1."""

    catchments: gpd.GeoDataFrame
    segments: gpd.GeoDataFrame


def define_roi(
    catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    *,
    outlet_ids: Iterable[Hashable],
    seg_id_col: str = DEFAULT_SEG_ID_COL,
    seg_id_down_col: str = DEFAULT_SEG_ID_DOWN_COL,
    catch_id_col: str = DEFAULT_CATCH_ID_COL,
) -> RoiResult:
    """Select ROI catchments and segments upstream of ordered outlets."""

    outlet_list = list(outlet_ids)
    selected_segments: set[Hashable] = set()
    selected_catchments: set[Hashable] = set()
    segment_sub = pd.Series(0, index=segments.index, dtype="int64")
    catchment_sub = pd.Series(0, index=catchments.index, dtype="int64")

    outlet_count = len(outlet_list)
    for outlet_index, outlet_id in enumerate(outlet_list):
        selection = find_upstream_selection(
            segments,
            [outlet_id],
            seg_id_col=seg_id_col,
            seg_id_down_col=seg_id_down_col,
            catch_id_col=catch_id_col,
        )
        sub_value = outlet_count - outlet_index
        selected_segments.update(selection.segment_ids)
        selected_catchments.update(selection.catchment_ids)

        segment_mask = segments[seg_id_col].isin(selection.segment_ids)
        catchment_mask = catchments[catch_id_col].isin(selection.catchment_ids)
        segment_sub.loc[segment_mask] = sub_value
        catchment_sub.loc[catchment_mask] = sub_value

    roi_segments = segments.loc[segments[seg_id_col].isin(selected_segments)].copy()
    roi_catchments = catchments.loc[catchments[catch_id_col].isin(selected_catchments)].copy()

    roi_segments.insert(0, "sub", segment_sub.loc[roi_segments.index].to_numpy())
    roi_catchments.insert(0, "sub", catchment_sub.loc[roi_catchments.index].to_numpy())

    roi_segments = roi_segments.reset_index(drop=True)
    roi_catchments = roi_catchments.reset_index(drop=True)

    return RoiResult(catchments=roi_catchments, segments=roi_segments)
```

- [ ] **Step 4: Run ROI tests**

Run:

```bash
pytest tests/test_roi.py -v
```

Expected: PASS.

- [ ] **Step 5: Run topology and ROI tests together**

Run:

```bash
pytest tests/test_topology.py tests/test_roi.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit ROI orchestration when working in a Git repository**

```bash
git add src/mgb_vec_hydro/roi.py tests/test_roi.py
git commit -m "feat: add roi selection"
```

---

### Task 4: Vector I/O Helpers

**Files:**
- Create: `src/mgb_vec_hydro/io.py`
- Create: `tests/test_io.py`

- [ ] **Step 1: Write I/O tests**

Create `tests/test_io.py`:

```python
import geopandas as gpd
import pytest
from shapely.geometry import Point

from mgb_vec_hydro.exceptions import UnsupportedOutputFormatError
from mgb_vec_hydro.io import output_paths, read_vector, write_vector


def test_output_paths_use_legacy_roi_names(tmp_path):
    paths = output_paths(tmp_path, "gpkg")

    assert paths.catchments == tmp_path / "roi_areas.gpkg"
    assert paths.segments == tmp_path / "roi_trecs.gpkg"


def test_output_paths_reject_unsupported_format(tmp_path):
    with pytest.raises(UnsupportedOutputFormatError, match="xyz"):
        output_paths(tmp_path, "xyz")


def test_write_and_read_vector_round_trip_gpkg(tmp_path):
    gdf = gpd.GeoDataFrame(
        {"value": [1], "geometry": [Point(0, 0)]},
        crs="EPSG:4326",
    )
    path = tmp_path / "points.gpkg"

    write_vector(gdf, path, output_format="gpkg")
    result = read_vector(path)

    assert list(result["value"]) == [1]
    assert result.crs == gdf.crs
```

- [ ] **Step 2: Run I/O tests to verify they fail**

Run:

```bash
pytest tests/test_io.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'mgb_vec_hydro.io'`.

- [ ] **Step 3: Implement I/O helpers**

Create `src/mgb_vec_hydro/io.py`:

```python
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
```

- [ ] **Step 4: Run I/O tests**

Run:

```bash
pytest tests/test_io.py -v
```

Expected: PASS. If the environment does not have GeoPandas or its vector engine installed, create the environment before continuing:

```bash
python -m pip install -e ".[test]"
```

If network access is blocked in the sandbox, rerun the install command with approved escalation.

- [ ] **Step 5: Commit I/O helpers when working in a Git repository**

```bash
git add src/mgb_vec_hydro/io.py tests/test_io.py
git commit -m "feat: add vector io helpers"
```

---

### Task 5: `define-roi` CLI

**Files:**
- Create: `src/mgb_vec_hydro/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write CLI tests**

Create `tests/test_cli.py`:

```python
import geopandas as gpd
from click.testing import CliRunner
from shapely.geometry import LineString, Polygon

from mgb_vec_hydro.cli import main


def test_define_roi_cli_writes_outputs(tmp_path):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    output_dir = tmp_path / "out"

    catchments = gpd.GeoDataFrame(
        {
            "catch_id": [10, 20],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
            ],
        },
        crs="EPSG:4326",
    )
    segments = gpd.GeoDataFrame(
        {
            "seg_id": [1, 2],
            "seg_id_down": [None, 1],
            "catch_id": [10, 20],
            "geometry": [
                LineString([(0, 0), (1, 0)]),
                LineString([(1, 0), (2, 0)]),
            ],
        },
        crs="EPSG:4326",
    )
    catchments.to_file(catchments_path, driver="GPKG")
    segments.to_file(segments_path, driver="GPKG")

    result = CliRunner().invoke(
        main,
        [
            "define-roi",
            "--catchments",
            str(catchments_path),
            "--segments",
            str(segments_path),
            "--outlet-id",
            "1",
            "--output-dir",
            str(output_dir),
            "--output-format",
            "gpkg",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (output_dir / "roi_areas.gpkg").exists()
    assert (output_dir / "roi_trecs.gpkg").exists()


def test_define_roi_cli_reports_package_errors(tmp_path):
    catchments_path = tmp_path / "catchments.gpkg"
    segments_path = tmp_path / "segments.gpkg"
    catchments = gpd.GeoDataFrame(
        {
            "catch_id": [10],
            "geometry": [Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
        },
        crs="EPSG:4326",
    )
    segments = gpd.GeoDataFrame(
        {
            "seg_id": [1],
            "seg_id_down": [None],
            "catch_id": [10],
            "geometry": [LineString([(0, 0), (1, 0)])],
        },
        crs="EPSG:4326",
    )
    catchments.to_file(catchments_path, driver="GPKG")
    segments.to_file(segments_path, driver="GPKG")

    result = CliRunner().invoke(
        main,
        [
            "define-roi",
            "--catchments",
            str(catchments_path),
            "--segments",
            str(segments_path),
            "--outlet-id",
            "1",
            "--output-dir",
            str(tmp_path / "out"),
            "--output-format",
            "xyz",
        ],
    )

    assert result.exit_code != 0
    assert "Unsupported output format" in result.output
```

- [ ] **Step 2: Run CLI tests to verify they fail**

Run:

```bash
pytest tests/test_cli.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'mgb_vec_hydro.cli'`.

- [ ] **Step 3: Implement CLI**

Create `src/mgb_vec_hydro/cli.py`:

```python
from __future__ import annotations

from pathlib import Path

import click

from mgb_vec_hydro.exceptions import MgbVecHydroError, MissingColumnsError
from mgb_vec_hydro.io import output_paths, read_vector, write_vector
from mgb_vec_hydro.roi import define_roi


@click.group()
def main() -> None:
    """MGB vector hydrography preprocessing tools."""


@main.command("define-roi")
@click.option("--catchments", "catchments_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--segments", "segments_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--outlet-id", "outlet_ids", multiple=True, required=True)
@click.option("--seg-id-col", default="seg_id", show_default=True)
@click.option("--seg-id-down-col", default="seg_id_down", show_default=True)
@click.option("--catch-id-col", default="catch_id", show_default=True)
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option("--output-format", default="fgb", show_default=True)
def define_roi_command(
    catchments_path: Path,
    segments_path: Path,
    outlet_ids: tuple[str, ...],
    seg_id_col: str,
    seg_id_down_col: str,
    catch_id_col: str,
    output_dir: Path,
    output_format: str,
) -> None:
    """Define ROI catchments and segments from explicit network topology."""

    try:
        paths = output_paths(output_dir, output_format)
        catchments = read_vector(catchments_path)
        segments = read_vector(segments_path)
        if seg_id_col not in segments.columns:
            raise MissingColumnsError(f"Missing required column(s): {seg_id_col}")
        coerced_outlet_ids = _coerce_outlet_ids(outlet_ids, segments[seg_id_col])
        roi = define_roi(
            catchments,
            segments,
            outlet_ids=coerced_outlet_ids,
            seg_id_col=seg_id_col,
            seg_id_down_col=seg_id_down_col,
            catch_id_col=catch_id_col,
        )
        write_vector(roi.catchments, paths.catchments, output_format=output_format)
        write_vector(roi.segments, paths.segments, output_format=output_format)
    except MgbVecHydroError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Wrote {paths.catchments}")
    click.echo(f"Wrote {paths.segments}")


def _coerce_outlet_ids(outlet_ids: tuple[str, ...], segment_id_series):
    """Convert CLI outlet strings to the dtype used by the segment ID column."""

    dtype = segment_id_series.dtype
    if dtype.kind in {"i", "u"}:
        return [int(value) for value in outlet_ids]
    if dtype.kind == "f":
        return [float(value) for value in outlet_ids]
    return list(outlet_ids)
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
pytest tests/test_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Run all non-regression tests**

Run:

```bash
pytest tests/test_import_boundaries.py tests/test_topology.py tests/test_roi.py tests/test_io.py tests/test_cli.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit CLI when working in a Git repository**

```bash
git add src/mgb_vec_hydro/cli.py tests/test_cli.py pyproject.toml
git commit -m "feat: add define-roi cli"
```

---

### Task 6: BHO Field Ordering And Regression Test Harness

**Files:**
- Modify: `src/mgb_vec_hydro/roi.py`
- Create: `tests/regression/test_carinhanha_roi.py`

- [ ] **Step 1: Write regression harness**

Create `tests/regression/test_carinhanha_roi.py`:

```python
from pathlib import Path

import geopandas as gpd
import pytest

from mgb_vec_hydro.roi import define_roi


pytest.importorskip("geopandas")

ROOT = Path(__file__).resolve().parents[2]
CARINHANHA = ROOT / "tests" / "carinhanha"
SEGMENTS_INPUT = ROOT / "data" / "geoft_bhae_trecho_drenagem.gpkg"
CATCHMENTS_INPUT = ROOT / "data" / "geoft_bhae_area_drenagem.gpkg"
EXPECTED_SEGMENTS = CARINHANHA / "output" / "roi_trecs.shp"
EXPECTED_CATCHMENTS = CARINHANHA / "output" / "roi_areas.shp"
OUTLET_IDS = [7656111, 765639, 76562193]


@pytest.mark.skipif(
    not SEGMENTS_INPUT.exists() or not CATCHMENTS_INPUT.exists(),
    reason="reference BHO input vectors are not available",
)
def test_carinhanha_roi_matches_reference_properties():
    segments = gpd.read_file(SEGMENTS_INPUT)
    catchments = gpd.read_file(CATCHMENTS_INPUT)
    expected_segments = gpd.read_file(EXPECTED_SEGMENTS)
    expected_catchments = gpd.read_file(EXPECTED_CATCHMENTS)

    roi = define_roi(
        catchments,
        segments,
        outlet_ids=OUTLET_IDS,
        seg_id_col="cotrecho",
        seg_id_down_col="nutrjus",
        catch_id_col="cobacia",
    )

    assert len(roi.segments) == len(expected_segments)
    assert len(roi.catchments) == len(expected_catchments)
    assert set(roi.segments["cotrecho"]) == set(expected_segments["cotrecho"])
    assert set(roi.catchments["cobacia"]) == set(expected_catchments["cobacia"])
    assert set(roi.segments["nutrjus"]) == set(expected_segments["nutrjus"])
    assert roi.segments.crs == expected_segments.crs
    assert roi.catchments.crs == expected_catchments.crs
    assert set(roi.segments["sub"]) == set(expected_segments["sub"])
    assert set(roi.catchments["sub"]) == set(expected_catchments["sub"])
```

- [ ] **Step 2: Run regression test and observe the current result**

Run:

```bash
pytest tests/regression/test_carinhanha_roi.py -v
```

Expected in the current environment without GeoPandas: SKIP or FAIL due to missing geospatial dependencies. Expected in a prepared geospatial environment: the test may fail if the reference input GPKGs are full BHO layers rather than already clipped Carinhanha layers.

- [ ] **Step 3: Add BHO-compatible output column ordering**

Modify `src/mgb_vec_hydro/roi.py` by adding these helpers after `define_roi`:

```python
BHO_SEGMENT_COLUMNS = [
    "sub",
    "cotrecho",
    "cobacia",
    "nucomptrec",
    "nuareacont",
    "nuareamont",
    "nutrjus",
    "cocursodag",
    "nustrahler",
    "centroid_x",
    "centroid_y",
    "geometry",
]

BHO_CATCHMENT_COLUMNS = [
    "sub",
    "cotrecho",
    "cobacia",
    "nuareacont",
    "cocursodag",
    "centroid_x",
    "centroid_y",
    "geometry",
]


def _apply_legacy_bho_column_order(
    catchments: gpd.GeoDataFrame,
    segments: gpd.GeoDataFrame,
    *,
    seg_id_col: str,
    seg_id_down_col: str,
    catch_id_col: str,
) -> RoiResult:
    if (seg_id_col, seg_id_down_col, catch_id_col) != ("cotrecho", "nutrjus", "cobacia"):
        return RoiResult(catchments=catchments, segments=segments)

    segment_columns = [column for column in BHO_SEGMENT_COLUMNS if column in segments.columns]
    catchment_columns = [column for column in BHO_CATCHMENT_COLUMNS if column in catchments.columns]

    remaining_segment_columns = [
        column for column in segments.columns if column not in segment_columns
    ]
    remaining_catchment_columns = [
        column for column in catchments.columns if column not in catchment_columns
    ]

    return RoiResult(
        catchments=catchments[catchment_columns + remaining_catchment_columns],
        segments=segments[segment_columns + remaining_segment_columns],
    )
```

Then modify the end of `define_roi` from:

```python
    return RoiResult(catchments=roi_catchments, segments=roi_segments)
```

to:

```python
    return _apply_legacy_bho_column_order(
        roi_catchments,
        roi_segments,
        seg_id_col=seg_id_col,
        seg_id_down_col=seg_id_down_col,
        catch_id_col=catch_id_col,
    )
```

- [ ] **Step 4: Run ROI unit tests**

Run:

```bash
pytest tests/test_roi.py -v
```

Expected: PASS.

- [ ] **Step 5: Run regression harness again**

Run:

```bash
pytest tests/regression/test_carinhanha_roi.py -v
```

Expected: PASS if the full BHO reference inputs and fixture expectations align with topology-only ROI. If it fails because the fixture expects DEM-clipped inputs, create a small prepared input fixture in `tests/carinhanha/input/` using external GIS tooling and change `SEGMENTS_INPUT` and `CATCHMENTS_INPUT` in the test to those prepared files. Keep the test assertions unchanged.

- [ ] **Step 6: Commit regression harness when working in a Git repository**

```bash
git add src/mgb_vec_hydro/roi.py tests/regression/test_carinhanha_roi.py
git commit -m "test: add carinhanha roi regression harness"
```

---

### Task 7: Documentation And CLI Smoke Test

**Files:**
- Create: `docs/stage1_roi_cli.md`

- [ ] **Step 1: Add CLI documentation**

Create `docs/stage1_roi_cli.md`:

```markdown
# Stage 1 ROI CLI

`mgb-vec-hydro define-roi` selects catchment and segment features upstream of one or more outlet segment IDs.

The command expects prepared vector inputs. It does not clip to a DEM extent and it does not call QGIS.

## Generic Schema

```bash
mgb-vec-hydro define-roi \
  --catchments path/to/catchments.gpkg \
  --segments path/to/segments.gpkg \
  --outlet-id 123 \
  --outlet-id 456 \
  --seg-id-col seg_id \
  --seg-id-down-col seg_id_down \
  --catch-id-col catch_id \
  --output-dir output \
  --output-format fgb
```

Supply repeated `--outlet-id` values in downstream-to-upstream order. The first outlet gets the highest `sub` value, and later upstream outlet selections overwrite overlapping `sub` assignments.

## BHO Schema

```bash
mgb-vec-hydro define-roi \
  --catchments data/geoft_bhae_area_drenagem.gpkg \
  --segments data/geoft_bhae_trecho_drenagem.gpkg \
  --outlet-id 7656111 \
  --outlet-id 765639 \
  --outlet-id 76562193 \
  --seg-id-col cotrecho \
  --seg-id-down-col nutrjus \
  --catch-id-col cobacia \
  --output-dir output \
  --output-format shp
```

Outputs use legacy Stage 1 names:

- `roi_areas.<ext>`
- `roi_trecs.<ext>`

Supported formats are `fgb`, `gpkg`, and `shp`. New workflows should prefer `fgb` or `gpkg`; Shapefile is kept for compatibility checks.
```

- [ ] **Step 2: Run CLI help**

Run:

```bash
python -m mgb_vec_hydro.cli --help
```

Expected: command help lists `define-roi`. If `python -m mgb_vec_hydro.cli --help` prints nothing because `main()` is not called for module execution, add this block to `src/mgb_vec_hydro/cli.py`:

```python
if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run full non-regression test suite**

Run:

```bash
pytest tests/test_import_boundaries.py tests/test_topology.py tests/test_roi.py tests/test_io.py tests/test_cli.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit docs and smoke support when working in a Git repository**

```bash
git add docs/stage1_roi_cli.md src/mgb_vec_hydro/cli.py
git commit -m "docs: describe stage 1 roi cli"
```

---

### Task 8: Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Search for forbidden runtime imports**

Run:

```bash
rg "qgis|PyQt5|processing|QApplication" src tests
```

Expected: no matches in `src/`. Test names may mention QGIS only in the import-boundary test if future wording is added.

- [ ] **Step 2: Run all tests**

Run:

```bash
pytest -v
```

Expected: PASS for all tests in a prepared geospatial environment. In the current seed environment, tests that require GeoPandas may fail until dependencies are installed.

- [ ] **Step 3: Verify CLI entry point**

Run:

```bash
mgb-vec-hydro --help
```

Expected: help output includes `define-roi`.

- [ ] **Step 4: Run package import smoke test**

Run:

```bash
python -c "import mgb_vec_hydro; print(mgb_vec_hydro.__version__)"
```

Expected: `0.1.0`.

- [ ] **Step 5: Record geospatial environment limitation if dependencies are unavailable**

If GeoPandas or vector engines are not installed and dependency installation is not approved, record this exact note in the final handoff:

```text
Pure package and topology tests were verified. GeoPandas-backed I/O, CLI, and regression tests could not be run in this environment because geospatial dependencies are not installed.
```

- [ ] **Step 6: Commit final verification adjustments when working in a Git repository**

```bash
git status --short
git add pyproject.toml .gitignore src tests docs
git commit -m "feat: implement stage 1 roi migration"
```

Skip this commit if the directory is still not a Git repository.

---

## Self-Review

Spec coverage:

- Package skeleton: Task 1.
- QGIS-free import boundary: Tasks 1 and 8.
- Generic topology traversal: Task 2.
- BHO configurable mapping: Tasks 2, 3, 6, and 7.
- Ordered multi-outlet `sub` behavior: Tasks 3 and 7.
- Vector I/O helpers and output formats: Task 4.
- `define-roi` CLI: Task 5.
- Regression harness for Carinhanha ROI properties: Task 6.
- DEM clipping excluded: Tasks 6 and 7 keep prepared vectors as the contract.

Type consistency:

- `define_roi()` returns `RoiResult`.
- `find_upstream_selection()` returns `UpstreamSelection`.
- CLI outlet values are coerced before topology traversal.
- Output path helpers use `RoiOutputPaths.catchments` and `RoiOutputPaths.segments`.

Known execution note:

The current workspace path is not a Git repository, so commit steps are conditional. The current Python environment previously lacked GeoPandas, Fiona, and OSGeo, so geospatial test execution may require environment setup before Tasks 3 through 7 can pass.
