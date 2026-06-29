# Post-Aggregation Planning Notes

These notes describe the remaining MGB preparation work after the implemented
ROI definition, mini-basin aggregation, and terrain-product stages.

The implemented commands are:

1. `define-roi`, which writes normalized ROI catchments and segments.
2. `aggregate`, which writes aggregated mini-basin catchments, reaches, and
   source-to-mini mapping.
3. `terrain-products`, which writes vector-compatible HAND and local
   terrain-to-drainage rasters. See the
   [Stage 3 CLI guide](../stage3_terrain_cli.md).

The remaining work is intentionally split into self-contained steps instead
of one large workflow:

- [HRU mapping](hru_mapping.md): build improved HRU classes from terrain and land-cover rasters.
- [Mini sampling](mini_sampling.md): sample terrain and HRU attributes onto mini-basins and reaches.
- [MGB files](mgb_files.md): write the final files needed by an MGB simulation.

The remaining documents are design notes, not implemented CLI documentation.
Command names, module names, and exact APIs will be decided during
implementation.
