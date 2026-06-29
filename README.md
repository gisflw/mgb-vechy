# MGB-Vec-Hydro

MGB-Vec-Hydro is a standalone Python library and command-line interface for preparing vector hydrography inputs for MGB workflows. The project is extracting the computational parts of the legacy BHO2MGB plugin into a reusable package that works from explicit network topology columns instead of QGIS APIs.

The current implemented workflow covers ROI definition and mini-basin aggregation from normalized ROI products.

## Scope

This repository targets the Python package and CLI only. It must stay independent from QGIS, PyQt, QGIS Processing, and desktop GIS project/task APIs.

BHO remains the first regression target, but the package should support any vector network with configurable segment ID and downstream ID columns. New vector outputs should use FlatGeobuf (`fgb`) or GeoPackage (`gpkg`); Shapefile is not part of the forward-looking format direction.

## Current CLI

Define an ROI:

```bash
mgb-vec-hydro define-roi \
  --catchments path/to/catchments.gpkg \
  --segments path/to/segments.gpkg \
  --outlet-id 123 \
  --id-col id \
  --id-down-col id_down \
  --destine-crs ESRI:102033 \
  --output-dir output \
  --output-format fgb
```

This command writes:

- `roi_catchments.<ext>`
- `roi_segments.<ext>`

See [docs/stage1_roi_cli.md](docs/stage1_roi_cli.md) for Stage 1 CLI details.

Aggregate the ROI into mini-basins:

```bash
mgb-vec-hydro aggregate \
  --roi-catchments output/roi_catchments.fgb \
  --roi-segments output/roi_segments.fgb \
  --uparea-min 30 \
  --lmin 6 \
  --output-dir output \
  --output-format fgb
```

This command writes:

- `mini_catchments.<ext>`
- `mini_segments.<ext>`
- `bho2mini.<ext>`

See [docs/stage2_aggregation_cli.md](docs/stage2_aggregation_cli.md) for aggregation CLI details.

Generate drainage-rooted terrain products:

```bash
mgb-vec-hydro terrain-products \
  --dem path/to/dem.tif \
  --roi-catchments output/roi_catchments.fgb \
  --roi-segments output/roi_segments.fgb \
  --output-dir output \
  --write-flow-direction
```

This writes aligned, ROI-cropped `hand.tif` and `ltnd.tif`, and optionally
`flow_direction.tif`.

## Development

Use an isolated Python environment for local work. Installation packaging for end users is intentionally deferred while the library API and workflow stabilize.

```bash
python -m pip install -e ".[test]"
pytest
```

## Roadmap

The refactor goal is documented in [docs/refactor_goal.md](docs/refactor_goal.md). The planned workflow stages are:

1. Define a region of interest from catchment polygons, stream/segment networks, and outlet IDs.
2. Aggregate vector hydrographic units into MGB mini-basins.
3. Generate MGB output files such as `MINI.gtp`, `COTA_AREA.flp`, and vector mini-basin products.
