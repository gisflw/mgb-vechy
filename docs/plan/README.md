# Remaining Workflow Plans

These notes describe the remaining MGB preparation work after the implemented
ROI definition, mini-basin aggregation, terrain-product, and mini-basin
sampling stages.

The implemented commands are:

1. `prepare`, which writes canonical vector and raster inputs. See the
   [Stage 0 CLI guide](../stage0_prepare_data.md).
2. `define-roi`, which writes normalized ROI catchments and segments. See the
   [Stage 1 CLI guide](../stage1_roi_cli.md).
3. `aggregate`, which writes aggregated mini-basin catchments, reaches, and
   source-to-mini mapping. See the
   [Stage 2 CLI guide](../stage2_aggregation_cli.md).
4. `terrain-products`, which writes vector-compatible HAND and local
   terrain-to-drainage rasters. See the
   [Stage 3 CLI guide](../stage3_terrain_cli.md).
5. `sample-minis`, which samples terrain and existing HRU classes into a
   mini-basin attribute table. See the
   [Stage 5 CLI guide](../stage5_mini_sampling_cli.md).

The remaining work is intentionally split into self-contained steps instead
of one large workflow:

- [HRU mapping](hru_mapping.md): build improved HRU classes from terrain and
  land-cover rasters.
- [MGB files](mgb_files.md): write the final files needed by an MGB simulation.

The remaining documents are design notes, not implemented CLI documentation.
Command names, module names, and exact APIs will be decided during
implementation.
