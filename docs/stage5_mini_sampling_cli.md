# Stage 5 Mini-Basin Sampling CLI

Stage 4 HRU construction is currently deferred. Stage 5 consumes an existing
HRU raster together with the Stage 2 mini-basin vectors and Stage 3 terrain
products.

`mgb-vec-hydro sample-minis` samples terrain and HRU attributes onto each
mini-basin and writes one combined, geometry-free CSV.

## Basic Usage

```bash
mgb-vec-hydro sample-minis \
  --catchments output/mini_catchments.fgb \
  --segments output/mini_segments.fgb \
  --dem path/to/dem.tif \
  --hand output/hand.tif \
  --ltnd output/ltnd.tif \
  --hru path/to/hru.tif \
  --crs EPSG:6933 \
  --output-dir output/sampled
```

All options are required:

- `--catchments PATH`: Stage 2 aggregated catchment polygons.
- `--segments PATH`: Stage 2 aggregated reach lines, normally
  `mini_segments.<ext>`.
- `--dem PATH`: elevation raster in metres.
- `--hand PATH`: height-above-drainage raster in metres.
- `--ltnd PATH`: local terrain-to-drainage distance raster in metres.
- `--hru PATH`: categorical HRU raster.
- `--output-dir DIRECTORY`: destination for `sampled_minis.csv`.

HRU IDs are inferred from the raster; they are not supplied on the command
line.

## Vector Requirements

Both mini vector inputs must use the exact Stage 2 schema and column order:

- `id`
- `id_down`
- `sub`
- `strahler_order`
- `unit_length`
- `upstream_length`
- `unit_area`
- `upstream_area`
- `water_course`
- `geometry`

The layers must have the same projected CRS, matching unique and non-null
`id` values, and non-empty geometry. Catchments must be polygons or
multipolygons, and reaches must be lines or multilines. Metric columns must be
numeric. Rows are matched by `id`, so input row order does not need to match.

`unit_length` is interpreted as kilometres and must be positive. A reach with
zero geometry length is rejected.

## Raster Requirements

Every raster must be single-band, georeferenced, use the same CRS as the mini
vectors, and completely cover each geometry sampled from it. HAND and LTND
must additionally have identical dimensions, transforms, and CRS.

Coverage is strict. The command rejects:

- nodata or non-finite values in any sampled cells;
- a mini with no selected cells;
- geometry extending outside a raster; and
- misaligned HAND and LTND rasters.

The complete HRU raster is validated before mini-basin sampling. Its band must
have an integer data type, and every valid, non-nodata value must be in the
inclusive domain `1..100`. Class discovery reads the raster block by block,
validates values before using them as indices, and supports at most 100
classes. A raster containing no valid HRU cells is rejected.

Discovered HRU IDs are sorted numerically. Every discovered class gets an
output column, even when its percentage is zero for a particular mini.
Declared HRU nodata may occur outside mini catchments but is rejected inside
sampled catchment cells.

## Sampled Attributes

The output preserves the normalized Stage 2 catchment attributes, excluding
geometry, and appends:

- `longitude` and `latitude`: catchment centroid coordinates in EPSG:4326.
  Centroids are calculated in the projected catchment CRS before
  transformation.
- `reach_slope_m_per_km`: DEM 85th percentile minus the 10th percentile along
  the reach, divided by 75% of `unit_length`.
- `tributary_length_km`: maximum catchment LTND converted from metres to
  kilometres.
- `tributary_slope_m_per_km`: mean HAND among cells at maximum LTND, divided
  by the tributary length in kilometres.
- `hru_<id>_pct`: percentage of sampled valid HRU cells belonging to each
  discovered class.

HRU columns follow the terrain attributes in ascending class-ID order. Each
row's HRU percentages must sum to 100%, and all output numeric values must be
finite.

## Output and Command Report

The output directory is created when needed. The command writes exactly:

```text
sampled_minis.csv
```

No diagnostic file or vector layer is produced. After a successful run, the
CLI reports the output path, number of minis, sampled catchment and reach cell
counts, and the discovered HRU class IDs. Validation failures are returned as
CLI errors.

This stage only samples and assembles attributes. It does not construct HRU
classes, clamp slopes, calculate final MGB ordering or hydraulic geometry,
write vectors, or format MGB model files.
