# Stage 3 Terrain Products CLI

`mgb-vec-hydro terrain-products` generates HAND and local terrain-to-drainage
distance rasters whose drainage follows the supplied ROI hydrography. It uses
catchment-confined AGREE conditioning and targeted shallow breaching without
modifying the source DEM.

## Basic Usage

```bash
mgb-vec-hydro terrain-products \
  --dem path/to/dem.tif \
  --roi-catchments output/roi_catchments.fgb \
  --roi-segments output/roi_segments.fgb \
  --crs EPSG:6933 \
  --id-col id \
  --output-dir output \
  --write-flow-direction \
  --buffer-cells 1 \
  --agree-sharp 80 \
  --agree-smooth 8 \
  --agree-buffer 4
```

`--dem`, `--roi-catchments`, `--roi-segments`, and `--output-dir` are
required. The remaining options are:

- `--id-col TEXT`: matching catchment and segment ID column; default `id`.
- `--write-flow-direction`: also write the optional D8 direction raster.
- `--buffer-cells INTEGER`: output-coverage cells beyond catchment edges;
  default `1`. This is separate from AGREE conditioning.
- `--agree-sharp FLOAT`: additional stream-cell incision in DEM elevation
  units; default `80.0`.
- `--agree-smooth FLOAT`: AGREE ramp depth per pixel toward the stream;
  default `8.0`.
- `--agree-buffer INTEGER`: conditioning radius in raster pixels; default `4`.

All three AGREE values and `--buffer-cells` must be non-negative. An AGREE
buffer of zero applies only the sharp stream-cell incision. An output buffer
of zero preserves the catchment-bounds crop.

## Input Requirements

The DEM must:

- have a projected CRS whose horizontal units are metres;
- use a north-up, unrotated transform with non-zero pixel dimensions; and
- cover the complete catchment ROI.

The command reprojects both vector layers to the DEM CRS. Catchments must be
polygons or multipolygons, and segments must be lines or multilines. Both
layers must declare a CRS, contain non-empty geometry, and have unique,
non-null values in `--id-col`. Their ID sets must match exactly, and
catchments must not overlap by positive area.

Inputs from `define-roi` satisfy the expected vector schema. Terrain products
use the ROI catchments and segments rather than the aggregated mini-basin
outputs, keeping the raster drainage tied to the selected source network.

## Outputs

The output directory is created when needed. The command always writes tiled,
Deflate-compressed GeoTIFFs cropped to the buffered ROI on the transformed DEM
grid:

- `hand.tif`: `float32` height above the nearest drainage cell, in the DEM's
  elevation units. Values are calculated from the unmodified DEM; nodata is
  `NaN`.
- `ltnd.tif`: `float32` along-route distance to the nearest drainage cell, in
  metres. Distances account for rectangular pixels; nodata is `NaN`.
- `flow_direction.tif`: optional `int8` D8 directions when
  `--write-flow-direction` is supplied. Codes are `-1` nodata, `0` drainage,
  and `1` through `8` for N, NE, E, SE, S, SW, W, and NW.

Files are staged privately and published only after every requested raster is
successfully written. Each raster records the routing method, AGREE parameters,
and direction-code legend in its GeoTIFF metadata.

## Routing Behavior

Segments are rasterized with all-touched semantics and clipped to their
matching catchments. Stream cells receive the sharp AGREE incision, with a
smooth Euclidean-distance ramp extending outward by the configured number of
pixels. Conditioning is confined to each catchment and is used only to choose
routing; HAND elevations still come from the source DEM.

Rasterized stream cells are drainage terminals. Other cells retain their
steepest metric downhill D8 direction where possible, while flats route toward
their lowest natural outlet. Trapped basins are connected to stream-draining
basins using targeted corridors chosen by required cut depth, cumulative
excavation, and corridor length. Routing never crosses catchment ownership,
and deterministic tie-breaking makes repeated runs stable.

Because routing follows the conditioned surface while HAND uses original
elevations, negative HAND values are possible where conditioning directs a
cell toward a higher source-DEM drainage elevation.

## Command Report

After writing the files, the CLI reports:

- each output path;
- owned and drainage cell counts;
- unreachable component count;
- conditioning, routing, JIT/cache initialization, and raster I/O timings; and
- the count and range of negative HAND cells.

Validation and processing failures are returned as CLI errors without
publishing a partial output set.
