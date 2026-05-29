# Stage 1 ROI Refactor Design

Date: 2026-05-29

## Context

The repository is being refactored from the legacy BHO2MGB QGIS plugin into a standalone Python package and CLI. The first milestone is limited to Stage 1: defining a region of interest from prepared catchment polygons, stream or segment vectors, and an ordered list of outlet segment IDs.

The new package core must not import QGIS, PyQt, QGIS Processing, or QGIS project and layer APIs. BHO remains a regression target, but the core topology model must be generic.

## Scope

This milestone will create an installable Python package for ROI definition only. It includes:

- package skeleton under `src/mgb_vec_hydro/`
- generic upstream topology traversal
- ROI filtering for catchment and segment vectors
- ordered multi-outlet `sub` assignment
- `define-roi` CLI command
- vector read and write helpers
- package-specific exceptions
- focused unit and regression tests

This milestone does not include mini-basin aggregation, terrain processing, HRU statistics, `MINI.gtp`, `COTA_AREA.flp`, DEM clipping, or any QGIS plugin code.

## Package Modules

`topology.py` will contain pure pandas topology functions. It will validate required columns, build upstream adjacency from explicit segment IDs and downstream segment IDs, traverse from one or more outlet segment IDs, detect cycles, and return selected segment and catchment IDs.

`io.py` will contain vector read and write helpers using GeoPandas and the supported geospatial engine available in the project environment. It will use `pathlib.Path`, preserve CRS, and choose the output driver from the requested format.

`roi.py` will orchestrate Stage 1. It will read or receive catchment and segment GeoDataFrames, call the topology traversal, filter ROI rows, assign `sub`, preserve legacy-compatible field order where BHO columns are present, and write `roi_areas` and `roi_trecs`.

`cli.py` will expose `define-roi`.

`exceptions.py` will define package errors such as `MissingColumnsError`, `TopologyCycleError`, and `OutletNotFoundError`.

## Topology Model

The public topology columns are configurable:

- `seg_id_col`, default `seg_id`
- `seg_id_down_col`, default `seg_id_down`
- `catch_id_col`, default `catch_id`

BHO users pass:

```bash
--seg-id-col cotrecho --seg-id-down-col nutrjus --catch-id-col cobacia
```

Traversal builds a reverse adjacency map from `seg_id_down -> [seg_id]`. Null, missing, or empty downstream IDs are treated as sinks and do not create upstream links to a real segment.

The traversal starts from the ordered outlet segment IDs supplied by the user. It returns all segment IDs and catchment IDs upstream of those outlets, including the outlet segments themselves. Missing required columns are rejected before traversal. Missing outlet IDs raise a package exception. Cycles are detected and reported rather than causing an infinite loop.

The implementation must not rely on BHO-specific string ordering, `cobacia >= cod`, or `cocursodag.startswith(...)` for upstream traversal.

## Outlet Ordering And `sub`

The CLI accepts repeated `--outlet-id` values. The user supplies these IDs in downstream-to-upstream order:

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

For `N` outlets, the first downstream-most outlet receives `sub=N`, the next receives `sub=N-1`, and the final upstream-most outlet receives `sub=1`. Each outlet is traversed independently. If an upstream outlet's contributing area overlaps a previous downstream outlet's contributing area, the later upstream assignment overwrites `sub`, matching the legacy `roi_define` behavior.

The combined ROI output contains the union of all upstream segment and catchment IDs selected by the ordered outlet list.

## CLI Contract

Initial command:

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

`--output-format` supports at least `fgb`, `gpkg`, and `shp`. The default for new outputs is `fgb`. Regression tests may request `shp` to preserve fixture names.

The command writes:

- `roi_areas.<ext>`
- `roi_trecs.<ext>`

The command does not accept `--dem` and does not clip vectors to a DEM extent. Users who need spatial clipping should prepare the vectors before calling `define-roi` with GIS tooling, `ogr2ogr`, GeoPandas, QGIS, or a future separate utility.

## Error Handling

Validation errors should be raised as package exceptions inside the library and rendered as clear CLI errors at the command boundary.

Required error cases:

- missing topology columns
- duplicated segment IDs if they make topology ambiguous
- requested outlet segment IDs not found
- cycle detection during upstream traversal
- unsupported output format
- unreadable input vector path
- input layers without CRS when CRS-sensitive operations are requested in the future

## Testing

Unit tests will cover topology behavior without geospatial dependencies:

- BHO-style column mapping: `cotrecho`, `nutrjus`, `cobacia`
- generic column mapping: `seg_id`, `seg_id_down`, `catch_id`
- one outlet
- multiple outlets in downstream-to-upstream order
- overlapping upstream areas and deterministic `sub` overwrite behavior
- null or empty downstream sink values
- missing topology columns
- missing outlet IDs
- cycle detection

Regression tests will use the Carinhanha fixture and prepared vector inputs. For Stage 1, comparisons should focus on stable properties rather than byte-for-byte shapefile equality:

- row counts for `roi_areas` and `roi_trecs`
- selected catchment and segment IDs
- downstream topology fields
- `sub` values from the ordered outlet list
- CRS preservation
- selected legacy attributes where present

Exact text outputs and later-stage vector outputs are out of scope for this milestone.

## Environment And Packaging

The package should declare dependencies needed by Stage 1, including pandas and GeoPandas. The preferred supported installation path should account for geospatial binary dependencies, likely through conda-forge first.

The current local Python environment does not have GeoPandas, Fiona, or OSGeo installed, so implementation work must include environment setup before running geospatial tests.

## Acceptance Criteria

- The package imports without QGIS, PyQt, or QGIS Processing installed.
- `define-roi` can run on prepared vector inputs with explicit topology columns.
- BHO-style topology columns work through configuration, not hard-coded assumptions.
- Ordered multi-outlet `sub` assignment is tested and deterministic.
- Topology edge cases fail with package-specific errors.
- Stage 1 regression tests protect Carinhanha ROI behavior at the property level.
- No DEM clipping or later-stage processing is included in this milestone.
