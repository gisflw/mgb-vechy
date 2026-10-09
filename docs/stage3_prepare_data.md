# Stage 3: prepare raster data

Frozen scientific reference for `mgb::prepro`; Rust implementation is pending.

Current stage name: `prepare`.

Clip aligned rasters and establish mini ownership and matching drainage on a
canonical DEM grid. See the [shared data contracts](shared_data_contracts.md).

## Inputs and parameters

| Input or parameter | Meaning |
| --- | --- |
| DEM | Single-band source defining resolution, pixel orientation, and grid origin. |
| Mini catchments and segments | Aggregation products with matching dense IDs `1..N`; catchment CRS is authoritative. Segments include `id_down`. |
| `dem_scale` | Finite positive conversion from stored DEM values to metres; default `1.0`. For centimetres, use `0.01`. |
| Named continuous/categorical rasters | Optional aligned single-band inputs, each with a unique, valid, non-reserved output name. |
| D8 raster and encoding | Optional aligned raster, with explicit `canonical` or `esri` encoding. |

All sources must declare the authoritative CRS. Rasters must share DEM
resolution and pixel origin and cover the prepared extent. There is no
implicit reprojection or resampling.

## Grid and ownership

The combined polygon and segment bounds are rounded outward to DEM pixel
edges, with a one-cell halo. Only the halo is clipped at DEM edges; geometry
outside DEM coverage fails. All source rasters must cover this final grid.

Polygons use center-of-pixel ownership. A sole interior owner takes precedence
over boundary owners; shared boundaries use the lowest mini ID. True multiple
interior owners at a raster cell fail. Ambiguous rasterized boundary cells
without a geometric owner use the most frequent nonzero owner in the immediate
3×3 neighborhood, with lowest-ID ties; a cell without a defensible owner fails.

Segments cover every touched cell and override polygon ownership, including
polygon gaps and exterior stream corridors. At collisions, eliminate upstream
contenders whenever another contender lies downstream, including through
non-contending intermediate segments; choose the lowest remaining ID.

Segment topology must be acyclic with valid downstream targets; null and `-1`
are sinks. Disconnected ownership components remain in the prepared domain.

## Values and nodata policy

Multiply valid stored DEM values by `dem_scale` before conversion to
`float32`. Do not infer units from magnitude or implicitly apply source
scale/offset metadata. Conversion affects only the DEM. Prepared DEM values
already include it; downstream stages use those metre values directly.

Preserve source validity inside the final polygon/segment ownership domain.
Non-finite continuous values become invalid. Valid categorical values must be
finite and integral; outputs store them as `int32`. Mask cells outside ownership.

`grid_catchments.tif` stores final ownership as `int32` mini IDs.
`grid_segments.tif` stores `int32` mini IDs on drainage cells and zero on
non-drainage cells. Both grids have identical final ownership masks. Positive
segment IDs equal catchment IDs at each cell.

Canonical D8 codes are 0 and 1–8 clockwise from north. ESRI codes normalize as:

| ESRI | Canonical |
| --- | --- |
| 0 | 0 |
| 1 (E) | 3 |
| 2 (SE) | 4 |
| 4 (S) | 5 |
| 8 (SW) | 6 |
| 16 (W) | 7 |
| 32 (NW) | 8 |
| 64 (N) | 1 |
| 128 (NE) | 2 |

## Outputs

| Filename | Content |
| --- | --- |
| `dem.tif` | `float32` DEM in metres, with `units=m` and `dem_scale` metadata. |
| `<name>.tif` | Each requested raster: `float32` continuous or `int32` categorical values. |
| `d8.tif` | Optional normalized `uint8` direction raster. |
| `grid_catchments.tif` | Final mini ownership, with `mini_index` metadata. |
| `grid_segments.tif` | Matching drainage IDs and zero background. |
| `manifest-prepare.json` | Input paths and processing parameters. |

All rasters are canonical-grid COGs with internal validity masks. The
`mini_index` dataset tag contains ordered JSON records
`[mini_id, minx, miny, maxx, maxy]` with tight pixel-edge bounds of final
ownership in the raster CRS. No index sidecar is produced.

## Invalid inputs

Reject missing/mismatched CRS, rotated or misaligned grids, insufficient
coverage, non-single-band rasters, invalid mini IDs or geometries, missing
segment targets, cycles, ownership conflicts, invalid raster names or
categorical values, non-finite scaled DEM values, invalid DEM
scales, and unsupported D8 codes or missing encoding.
