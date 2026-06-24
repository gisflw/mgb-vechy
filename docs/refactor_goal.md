# MGB-Vec-Hydro CLI

Standalone Python package and command-line interface for preprocessing MGB model inputs from vector hydrographic/catchment networks with topological attributes.

This repository is the computational core only. It must not import QGIS, PyQt, or QGIS Processing, and it does not carry a QGIS integration roadmap. The target product is a reusable Python library with a CLI.

## Goal

Extract the BHO2MGB workflow from the old QGIS plugin into a reproducible standalone Python package with a CLI, while generalizing the implementation to work with any vector hydrographic network that exposes explicit segment and downstream topology columns.

The CLI should support three main workflow stages:

1. Define a region of interest from catchment polygons, stream/segment networks, and outlet segment or catchment IDs.
2. Aggregate vector hydrographic units into MGB mini-basins.
3. Generate MGB output files:
   - `MINI.gtp`
   - `COTA_AREA.flp`
   - `minis_mgb.fgb`
   - intermediate ROI and aggregated vector outputs.

## Source Context

The original repository was a QGIS plugin located at:

`HGE-IPH/BHO2MGB`

BHO is the initial legacy dataset and regression target, not the product boundary. Keep BHO compatibility as a first-class requirement, but do not hard-code BHO topology assumptions into the new core.

Important source files copied from the old repo:

- `legacy/model.py`: current QGIS-coupled implementation.
- `legacy/MGB.py`: QGIS task orchestration; use only to understand the step order.
- `legacy/rasterstats_gdal.py`: custom GDAL zonal statistics helper.
- `legacy/pysheds/`: vendored hydrology code used by the plugin; replace with a dependency or isolated maintained module if possible.

## Critical Refactor Rule

The package core must not depend on:

- `qgis`
- `processing`
- `PyQt5`
- `QApplication`
- QGIS project/layer/task APIs

QGIS integration is out of scope for this repository.

## Generic Network Data Model

The core workflow must operate on generic vector network topology rather than BHO-specific field semantics.

Required topology columns:

- `seg_id`: segment ID column.
- `seg_id_down`: downstream segment ID column.
- `catch_id`: catchment/polygon ID column associated with each segment.

BHO mapping example:

- `seg_id=cotrecho`
- `seg_id_down=nutrjus`
- `catch_id=cobacia`

Implementations may expose these as configurable column parameters. A future `--schema bho` preset may provide the BHO mapping, but BHO column names must not be the only supported path.

Upstream traversal should use an explicit topology function, not BHO ordering shortcuts:

```python
find_upstream_catchments(
    segments,
    outlet_ids,
    *,
    seg_id_col="seg_id",
    seg_id_down_col="seg_id_down",
    catch_id_col="catch_id",
)
```

Expected behavior:

- build topology from `seg_id_col -> seg_id_down_col`;
- return all upstream catchments and/or segments draining to the outlet IDs;
- support one or multiple outlets;
- detect missing required columns before traversal;
- reject or report cycles instead of infinite looping;
- treat null or empty downstream IDs as outlets/sinks.

Do not rely on BHO-specific string ordering, `cobacia >= cod`, or `cocursodag.startswith(...)` for upstream traversal.

## Current Known Workflow

The old plugin exposes three user-facing steps. Preserve the workflow shape, but make the data model generic.

### Step 1: ROI definition

Inputs:

- Catchment/polygon vector file.
- Stream/segment vector network file.
- DEM raster.
- Outlet segment or catchment IDs, using configured ID columns.
- Topology column names, with public defaults `seg_id`, `seg_id_down`, and `catch_id`.

Old functions:

- `carrega_bho`
- `find_upstream_catchments` or `find_upstream_network`, replacing `upstream_bho`
- `roi_define`

Outputs:

- `roi_areas.fgb` or `roi_areas.gpkg`: ROI catchments; keep the base name for regression compatibility.
- `roi_trecs.fgb` or `roi_trecs.gpkg`: ROI segments/streams; keep the base name for regression compatibility.

### Step 2: Mini-basin aggregation

Inputs:

- `roi_areas`: ROI catchments.
- `roi_trecs`: ROI stream/segment network.
- upstream area threshold: `uparea_min`
- minimum stream length: `lmin`

Old functions:

- `load_df`
- `bho2mini`
- `dissolver`

Outputs:

- `mtrecs.fgb` or `mtrecs.gpkg`: aggregated stream/segment network.
- `mareas.fgb` or `mareas.gpkg`: aggregated catchments.
- `bho2mini.fgb` or `bho2mini.gpkg`: legacy regression base name.

### Step 3: MGB files

Inputs:

- DEM raster.
- Aggregated streams/segments: `mtrecs`.
- Aggregated catchments: `mareas`.
- HRU raster.
- Geomorphological relation parameters:
  - `a`
  - `b`
  - `c`
  - `d`
- slope limits:
  - `smin`
  - `smax`
- Manning coefficient:
  - `nman`

Old functions:

- `pols2lines`
- `get_slopes_main`
- `get_slopes_afl`
- `get_hrus`
- `write_mini`
- `write_cota_area`
- vendored `Grid.compute_hand_strbrn`
- vendored `Grid.compute_ltnd_strbrn`

Outputs:

- `hand.tif`
- `ltnd.tif`
- `MINI.gtp`
- `COTA_AREA.flp`
- `minis_mgb.fgb` or `minis_mgb.gpkg`

## Proposed Package Structure

```text
src/mgb-vec-hydro/
  __init__.py
  cli.py
  pipeline.py
  utils.py
  io.py
  vector_hydrography.py
  aggregation.py
  terrain.py
  hru.py
  mini.py
  stage_area.py
  formatting.py
  exceptions.py
tests/
  regression/
    roi-name/
      input/
      expected_output/
```

## CLI Sketch

```bash
define-roi \
  --catchments path/to/catchments.fgb \
  --segments path/to/segments.fgb \
  --dem path/to/dem.tif \
  --outlet-id 12345 \
  --seg-id-col cotrecho \
  --seg-id-down-col nutrjus \
  --catch-id-col cobacia \
  --output-dir output/

aggregate \
  --roi-areas output/roi_areas.fgb \
  --roi-trecs output/roi_trecs.fgb \
  --uparea-min 30 \
  --lmin 6 \
  --output-dir output/

build-mini \
  --dem path/to/dem.tif \
  --mtrecs output/mtrecs.fgb \
  --mareas output/mareas.fgb \
  --hru path/to/hru.tif \
  --geo-a 0.89 \
  --geo-b 0.52 \
  --geo-c 0.05 \
  --geo-d 0.44 \
  --smin 0.01 \
  --smax 10000 \
  --nman 0.030 \
  --output-dir output/

run-all ...
```

BHO users should pass BHO column names explicitly, as shown above, or use a future `--schema bho` preset. Keep output base names like `roi_areas`, `roi_trecs`, `mtrecs`, and `mareas` for now to preserve old regression comparisons, but describe them as generic catchment and segment products in new code and documentation.

## Dependency Direction

Keep `pandas` for now. It is central to the existing algorithm.

First priority is keeping the computational core independent from QGIS/PyQt and usable as a normal Python package.

Likely package dependencies:

- `numpy`
- `pandas`
- `geopandas`
- `shapely`
- `rasterio`
- `pyproj`
- `scipy`
- `scikit-image`
- `click` or `typer`
- `rich`, optional, for CLI progress/logging

User installation packaging is deferred. Development can use an isolated local environment while the library and CLI stabilize.

## Compatibility Problems To Fix

- Remove all `os.chdir` usage.
- Replace hardcoded `output\\` paths with `pathlib.Path`.
- Replace deprecated `DataFrame.append`.
- Stop writing temporary vector files into the current working directory.
- Avoid Shapefile in new workflow outputs; prefer FlatGeobuf and GeoPackage.
- Make CRS handling explicit.
- Validate required input fields and topology columns before running.
- Replace BHO-specific upstream traversal with configurable topology traversal.
- Raise package exceptions instead of crashing with low-level GDAL/Pandas errors.
- Add deterministic tests before changing algorithms.

## Output Compatibility

The first target is behavior-compatible output with the old plugin, not a scientific redesign.

The existing `tests/carinhanha` fixture is a BHO regression fixture. Use it to protect legacy BHO behavior while generalizing the implementation.

Before refactoring aggressively, create golden fixtures from a small real dataset and compare:

- row counts
- key IDs
- downstream topology
- areas
- slope columns
- HRU percentages
- `MINI.gtp` text
- `COTA_AREA.flp` text

Exact byte-for-byte text matching is desirable but may be relaxed if formatting differences are documented.

Required future tests:

- BHO topology mapping: `cotrecho`/`nutrjus`/`cobacia`.
- Generic topology mapping: `seg_id`/`seg_id_down`/`catch_id`.
- Multiple outlets.
- Null or empty downstream sink values.
- Cycle detection.
- Missing topology columns.
