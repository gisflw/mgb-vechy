# Post-Aggregation Planning Notes

These notes describe the planned MGB preparation workflow after ROI definition and mini-basin aggregation.

The implemented commands currently stop after:

1. `define-roi`, which writes normalized ROI catchments and segments.
2. `aggregate`, which writes aggregated mini-basin catchments, reaches, and source-to-mini mapping.

The remaining work is intentionally split into self-contained steps instead of one large workflow:

- [Terrain products](terrain_products.md): generate vector-compatible HAND and local terrain-drainage rasters.
- [HRU mapping](hru_mapping.md): build improved HRU classes from terrain and land-cover rasters.
- [Mini sampling](mini_sampling.md): sample terrain and HRU attributes onto mini-basins and reaches.
- [MGB files](mgb_files.md): write the final files needed by an MGB simulation.

These documents are design notes, not implemented CLI documentation. Command names, module names, and exact APIs should be decided during implementation.
