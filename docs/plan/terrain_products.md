# Terrain Products

This step will generate terrain-derived rasters used by later MGB preparation steps.

## Purpose

The first priority is compatibility between the raster products and the vector hydrography. The DEM may not naturally drain along the vectorized stream network or respect the vector catchment limits. This step should therefore use ROI segments and ROI catchments to enforce terrain products that are consistent with the selected vector data.

The initial outputs are:

- `hand.tif`: height above nearest drainage.
- `ltnd.tif`: local terrain distance to nearest drainage.

## Planned Inputs

- ROI stream or segment vectors, currently produced as `roi_segments.<ext>`.
- ROI catchment polygons, currently produced as `roi_catchments.<ext>`.
- DEM raster.

Later implementations may also accept aggregated mini-basin products when that is useful, but the first design target is terrain products from the ROI vectors so the terrain basis remains tied to the selected hydrographic network.

## Planned Responsibilities

- Align raster processing to the DEM grid and CRS handling rules.
- Rasterize the ROI stream network as the drainage mask.
- Rasterize catchment boundaries or limits as barriers where needed.
- Generate HAND and local terrain-drainage distance rasters that follow the vector drainage structure.
- Report or fail clearly when vector and raster inputs cannot be aligned safely.

This step should not compute HRU classes, sample mini-basin attributes, or write MGB text files.

## Terrain-driven basin routing with targeted shallow breaching

Rasterized stream cells are terminals. Every other non-flat cell initially
keeps its steepest metric downhill D8 direction within its catchment. Equal
elevation components route toward their lowest natural downslope outlet; closed
flats and pits remain local depression terminals.

The local D8 trees are labeled by terminal and reduced to a basin adjacency
graph. Trapped basins connect to stream-connected basins by paths minimizing,
in order, maximum required cut depth, cumulative excavation, and metric
corridor length. Only the chosen terminal-to-divide corridors are reversed.
All other natural D8 directions remain unchanged. Row-major boundary position
is used only as a final deterministic fallback when costs are exactly equal.

Important implementation properties are:

- polygon outlines are not excluded, so adjacent catchments have no nodata seam;
- parent choice is deterministic rather than wavefront-order-sensitive;
- LTND accumulates floating-point distances rather than truncated integers;
- distances use the DEM's projected metric CRS and rectangular pixel dimensions;
- no resolution-sensitive AGREE burn is required;
- explicit ownership confines every route to its matching catchment and stream.

Rank and order arrays are internal traversal aids for propagating raster
products. They do not constrain or select ordinary flow directions. Cell
traversal, flat resolution, basin labeling, boundary scanning, corridor
reversal, HAND, and LTND use cached Numba kernels; the priority queue operates
only on the much smaller basin graph.

`hand.tif` and `ltnd.tif` are always written. `--write-flow-direction` also
writes codes `-1` nodata, `0` drainage, and `1` through `8` for N, NE, E, SE,
S, SW, W, and NW.

## Historical implementation mapping

The legacy plugin bundled this responsibility into the final MGB task. Relevant legacy pieces include:

- `pols2lines`, which converted catchment polygons to boundary lines.
- stream and boundary rasterization in `legacy/MGB.py`.
- `Grid.burn_dem`, used to force the stream network into the DEM.
- `Grid.compute_hand_strbrn`, used to compute HAND.
- `Grid.compute_ltnd_strbrn`, used to compute local terrain distance to drainage.

The new boundary should isolate these terrain products from HRU mapping, mini sampling, and final file formatting.
