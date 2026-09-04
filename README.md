# MGB-Vec-Hydro

MGB-Vec-Hydro is a standalone Python library and command-line interface for
preparing vector hydrography inputs for MGB workflows. It works with generic
vector networks that expose explicit segment and downstream topology columns;
BHO is supported as the initial regression dataset rather than as a fixed
schema.

The implemented workflow selects raw vectors into a region of interest, aggregates
source units into mini-basins, prepares aligned raster and mini-domain inputs, generates HAND
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

Run the stages in this order: define an ROI, aggregate mini-basins, prepare
the raster/mini domain, then generate terrain products. The preparation command
is documented first because it defines the reusable raster input contract.

### Prepare canonical inputs

Clip already aligned rasters and rasterize the aggregated mini domain:

```bash
mgb-vec-hydro prepare \
  --dem data/dem.tif \
  --minis output/minis \
  --categorical-raster hru data/hru.tif \
  --output-dir prepared
```

### Define a region of interest

Select all catchments and segments upstream of one or more outlets:

```bash
mgb-vec-hydro define-roi \
  --crs EPSG:6933 \
  --catchments data/catchments.gpkg \
  --segments data/segments.gpkg \
  --outlet-id 123 \
  --id-col id \
  --id-down-col id_down \
  --strahler-order-col strahler_order \
  --upstream-area-col upstream_area \
  --unit-length-col unit_length_km \
  --unit-area-col unit_area_km2 \
  --output-dir output/roi
```

### Aggregate mini-basins

Aggregate the normalized ROI using upstream-area and minimum-length thresholds:

```bash
mgb-vec-hydro aggregate \
  --roi output/roi \
  --uparea-min 30 \
  --lmin 6 \
  --output-dir output/minis
```

### Generate terrain products

Create strict mini-confined HAND and local terrain-to-drainage COGs. Add
`--write-flow-direction` to publish the selected D8 raster, or use
`--direction-source d8` to route from a prepared D8 input.

```bash
mgb-vec-hydro terrain-products \
  --prepared prepared \
  --direction-source dem \
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
  --hand output/terrain/rasters/hand.tif \
  --ltnd output/terrain/rasters/ltnd.tif \
  --hru prepared/rasters/hru.tif \
  --crs EPSG:6933 \
  --output-dir output/sampled
```

Use `mgb-vec-hydro COMMAND --help` for the complete option list.

## Documentation

- [ROI and working CRS](docs/stage1_roi_cli.md)
- [Prepare-data contract](docs/stage3_prepare_data.md)
- [ROI CLI and normalized schema](docs/stage1_roi_cli.md)
- [Mini-basin aggregation CLI](docs/stage2_aggregation_cli.md)
- [Terrain-products CLI](docs/stage4_terrain_cli.md)
- [Mini-basin sampling CLI](docs/stage5_mini_sampling_cli.md)
- [Remaining workflow plans](docs/plan/README.md)
- [Larger-than-memory processing change](docs/changes/larger-than-memory-processing.md)

Internal shared execution contracts are documented in
[docs/shared_execution.md](docs/shared_execution.md). Contributor and coding
guidance lives in [AGENTS.md](AGENTS.md).
