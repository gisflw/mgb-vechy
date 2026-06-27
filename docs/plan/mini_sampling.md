# Mini Sampling

This step will sample terrain and HRU attributes onto mini-basins and reaches.

## Purpose

Mini-basin sampling should create the attributed data needed to write final MGB simulation files. It should consume prepared terrain and HRU products rather than generating them internally.

## Planned Inputs

- Aggregated mini-basin catchments, currently produced as `mini_catchments.<ext>`.
- Aggregated mini-basin reaches, currently produced as `mini_segments.<ext>`.
- Terrain products such as `hand.tif`, `ltnd.tif`, and DEM-derived slope inputs.
- HRU class raster and class metadata from the HRU mapping step.

## Planned Outputs

- Attributed mini-basin vector product, eventually written as `minis_mgb.<ext>`.
- Tabular attributes needed by final MGB file formatting.
- Diagnostics for missing raster coverage, invalid percentages, or inconsistent mini-basin/reach topology.

## Planned Responsibilities

- Compute or collect reach terrain attributes, including main reach slope.
- Compute or collect local tributary attributes from HAND and local terrain-drainage distance products.
- Compute HRU percentages by mini-basin.
- Preserve mini-basin IDs and downstream relationships needed for final ordering.

This step should not generate HAND/LTND rasters, define HRU classes, or format final MGB text files.

## Legacy Mapping

Legacy responsibilities that belong here include:

- `get_slopes_main`, which sampled DEM elevation statistics along reaches.
- `get_slopes_afl`, which used HAND and local terrain-drainage distance to estimate tributary slope and length.
- the categorical sampling part of `get_hrus`.
- the attribute assembly portion of `write_mini`, before text formatting.

The new implementation should separate attribute sampling from final file serialization.
