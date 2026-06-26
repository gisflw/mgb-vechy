# HRU Mapping

This step will define an improved HRU mapping workflow for MGB preparation.

## Purpose

The legacy workflow sampled a pre-existing HRU raster directly over mini-basins. The planned workflow should instead support generating HRU classes from raster inputs that better describe hydrologic response.

The required raster inputs are expected to include:

- HAND or another terrain-drainage product.
- Terrain maps.
- Land-cover maps.

The exact raster algebra, class definitions, and classification thresholds are intentionally deferred.

## Planned Inputs

- `hand.tif` or equivalent terrain-drainage raster from the terrain-products step.
- Terrain raster layers needed for classification.
- Land-cover raster layers needed for classification.
- A class-definition configuration, once the raster algebra design is settled.

## Planned Outputs

- HRU class raster aligned to the terrain-products grid.
- HRU class metadata describing class IDs and their source rule definitions.
- Diagnostics useful for reviewing unmapped, nodata, or unexpected raster combinations.

## Planned Responsibilities

- Combine terrain and land-cover rasters into MGB-ready HRU classes.
- Keep class generation separate from mini-basin sampling.
- Preserve enough metadata for generated classes to be auditable.
- Handle nodata and unmatched raster combinations explicitly.

This step should not sample mini-basins or write `MINI.gtp`.

## Legacy Mapping

The closest legacy function is `get_hrus`, but that function only computes categorical percentages from an existing HRU raster. The new workflow should keep that sampling concept for later, while adding a separate class-generation step before mini-basin attributes are assembled.
