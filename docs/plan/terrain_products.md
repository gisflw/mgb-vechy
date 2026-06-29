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

## Drainage-rooted routing

The implementation rasterizes explicit pixel ownership and computes an
eight-neighbour geodesic rank outward from each catchment's matching segment.
Elevation deterministically selects a parent only among neighbours one rank
closer to drainage. It prefers the steepest descent, then the least uphill
breach, with step length and N–NW direction order as tie-breakers.

This differs from the legacy workflow in several intentional ways:

- polygon outlines are not excluded, so adjacent catchments have no nodata seam;
- parent choice is deterministic rather than wavefront-order-sensitive;
- LTND accumulates floating-point distances rather than truncated integers;
- distances use the DEM's projected metric CRS and rectangular pixel dimensions;
- no AGREE burn or DEM fill is needed because ranks guarantee connection.

`hand.tif` and `ltnd.tif` are always written. `--write-flow-direction` also
writes codes `-1` nodata, `0` drainage, and `1` through `8` for N, NE, E, SE,
S, SW, W, and NW.

## Legacy Mapping

The legacy plugin bundled this responsibility into the final MGB task. Relevant legacy pieces include:

- `pols2lines`, which converted catchment polygons to boundary lines.
- stream and boundary rasterization in `legacy/MGB.py`.
- `Grid.burn_dem`, used to force the stream network into the DEM.
- `Grid.compute_hand_strbrn`, used to compute HAND.
- `Grid.compute_ltnd_strbrn`, used to compute local terrain distance to drainage.

The new boundary should isolate these terrain products from HRU mapping, mini sampling, and final file formatting.
