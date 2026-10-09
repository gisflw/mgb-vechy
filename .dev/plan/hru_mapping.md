# HRU mapping

Status: unimplemented. This is an intended capability, outside the frozen
implemented tool contracts.

## Purpose and inputs

Generate HRU classes describing hydrologic response from aligned terrain and
land-cover inputs. Expected inputs include HAND or another drainage-related
terrain product, additional terrain layers, land-cover rasters, and class
rules defining their hydrologic interpretation.

Class definitions, scientific combinations, thresholds, and the class-rule
configuration are not yet settled.

## Intended results

- An HRU class raster aligned to the terrain-products grid.
- Class metadata relating identifiers to their scientific definitions.
- Diagnostics for missing data, unmatched combinations, and unexpected values.

Class generation is a separate capability from
[mini-basin sampling](../stage5_mini_sampling_cli.md), which already summarizes
an existing categorical HRU raster. This capability produces classifications;
mini statistics and final simulation files belong to subsequent steps.
