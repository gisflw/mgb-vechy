# MGB-Vec-Hydro

MGB-Vec-Hydro is a standalone Python library and command-line interface for
preparing vector hydrography inputs for MGB workflows. It works with generic
vector networks that expose explicit segment and downstream topology columns;
BHO is supported as the initial regression dataset rather than as a fixed
schema.

The implemented workflow prepares canonical vector and raster inputs, selects
a region of interest, aggregates source units into mini-basins, generates HAND
and local terrain-to-drainage products, and samples terrain and existing HRU
classes onto mini-basins. HRU class construction and final MGB file generation
remain planned work.

## Installation

MGB-Vec-Hydro requires Python 3.11 or newer. From a repository checkout, install
the package into an isolated environment:

```bash
python -m pip install -e .
```

## Commands

### Prepare canonical inputs

Convert raw sources into filtered, indexed FlatGeobuf vectors and aligned COG
rasters on one authoritative grid:

```bash
mgb-vec-hydro prepare \
  --catchments data/catchments.gpkg \
  --segments data/segments.gpkg \
  --dem data/dem.tif \
  --id-col id \
  --id-down-col id_down \
  --strahler-order-col strahler_order \
  --crs EPSG:6933 \
  --resolution 30 \
  --categorical-raster hru data/hru.tif \
  --output-dir prepared
```

### Define a region of interest

Select all catchments and segments upstream of one or more outlets:

```bash
mgb-vec-hydro define-roi \
  --catchments prepared/vectors/catchments.fgb \
  --segments prepared/vectors/segments.fgb \
  --outlet-id 123 \
  --id-col id \
  --id-down-col id_down \
  --strahler-order-col strahler_order \
  --crs EPSG:6933 \
  --output-dir output/roi \
  --output-format fgb
```

### Aggregate mini-basins

Aggregate the normalized ROI using upstream-area and minimum-length thresholds:

```bash
mgb-vec-hydro aggregate \
  --roi-catchments output/roi/roi_catchments.fgb \
  --roi-segments output/roi/roi_segments.fgb \
  --uparea-min 30 \
  --lmin 6 \
  --crs EPSG:6933 \
  --output-dir output/minis \
  --output-format fgb
```

### Generate terrain products

Create catchment-confined HAND and local terrain-to-drainage rasters. Add
`--write-flow-direction` to retain the computed D8 raster.

```bash
mgb-vec-hydro terrain-products \
  --dem prepared/rasters/dem.tif \
  --roi-catchments output/roi/roi_catchments.fgb \
  --roi-segments output/roi/roi_segments.fgb \
  --crs EPSG:6933 \
  --buffer-cells 1 \
  --output-dir output/terrain
```

### Sample mini-basin attributes

Sample DEM, HAND, local terrain-to-drainage distance, and an existing
categorical HRU raster into a geometry-free CSV:

```bash
mgb-vec-hydro sample-minis \
  --catchments output/minis/mini_catchments.fgb \
  --segments output/minis/mini_segments.fgb \
  --dem prepared/rasters/dem.tif \
  --hand output/terrain/hand.tif \
  --ltnd output/terrain/ltnd.tif \
  --hru prepared/rasters/hru.tif \
  --crs EPSG:6933 \
  --output-dir output/sampled
```

Use `mgb-vec-hydro COMMAND --help` for the complete option list.

## Documentation

- [Prepare-data contract](docs/stage0_prepare_data.md)
- [ROI CLI and normalized schema](docs/stage1_roi_cli.md)
- [Mini-basin aggregation CLI](docs/stage2_aggregation_cli.md)
- [Terrain-products CLI](docs/stage3_terrain_cli.md)
- [Mini-basin sampling CLI](docs/stage5_mini_sampling_cli.md)
- [Remaining workflow plans](docs/plan/README.md)
- [Larger-than-memory processing change](docs/changes/larger-than-memory-processing.md)

Internal shared execution contracts are documented in
[docs/shared_execution.md](docs/shared_execution.md). Contributor and coding
guidance lives in [AGENTS.md](AGENTS.md).
