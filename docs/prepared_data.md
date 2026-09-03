# Prepared data contract

`mgb-vec-hydro prepare` converts raw hydrography and raster sources into the
versioned, bounded-access dataset required by the larger-than-memory workflow.
Preparation is the only stage that reprojects or aligns raw data.

## Usage

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
  --continuous-raster rainfall data/rainfall.tif \
  --categorical-raster hru data/hru.tif \
  --d8 data/d8.tif \
  --d8-encoding esri \
  --output-dir prepared
```

Named raster options may be repeated. Names must start with a lowercase letter
and contain only lowercase letters, digits, underscores, or hyphens. `dem` and
`d8` are reserved. Inputs are local, single-band files.

For a multi-layer vector data source, select layers with `--catchments-layer`
and `--segments-layer`. A missing or known-incorrect vector CRS can be replaced
explicitly with `--catchments-source-crs` or `--segments-source-crs`.

## Canonical grid and rasters

The DEM's bounds are transformed to the requested projected, metre-based CRS
and expanded to square cells at `--resolution`. Grid coordinates are anchored
at `(0, 0)`, making alignment independent of the source raster's pixel origin.
All prepared rasters use that exact CRS, affine transform, width, and height.

Prepared rasters are 512-pixel tiled COGs with internal validity masks:

- DEM and named continuous rasters are `float32` and use bilinear resampling.
- Categorical rasters must contain integral `int32` values and use nearest
  neighbour resampling.
- D8 is `uint8` and is never reprojected. It must already match the canonical
  grid exactly.

Canonical D8 values are `0` for a terminal and `1` through `8` for N, NE, E,
SE, S, SW, W, and NW. The `esri` input encoding maps `1, 2, 4, 8, 16, 32, 64,
128` (E through NE) into that representation. Source nodata is represented by
the output validity mask rather than a numeric sentinel.

## Vector contract

Preparation reads vectors in bounded Arrow batches, transforms them to the
canonical CRS, and writes spatially indexed FlatGeobuf files. The output
schemas are exactly:

- `catchments.fgb`: `id`, polygon/multipolygon geometry.
- `segments.fgb`: `id`, `id_down`, nullable integral `strahler_order`, and
  line/multiline geometry.

Shared IDs are normalized losslessly to either signed `int64` or UTF-8 text.
Rows with null or empty IDs are dropped and counted. Duplicate valid IDs or
different valid ID sets fail preparation. Downstream IDs outside the prepared
network are retained as boundary references.

Preparation preserves null and non-positive integral Strahler values. Their
scientific suitability is validated when an ROI is defined, where the selected
network and intended analysis domain are known.

Invalid geometry fails by default. `--repair-invalid-geometries` applies
`make_valid`, extracts only the required geometry family, and records repair
counts. Features outside the raster extent are preserved; selected-domain
coverage is validated by later processing stages.

The workspace BHAE source currently contains two null catchment IDs, which are
dropped as expected, and 1,292 null `nustrahler` values. The latter are a hard
contract error: preparation does not silently invent scientific stream-order
values. Correct or derive those values in the source before using the complete
BHAE preparation script.

## Layout and validation

Preparation writes to a private sibling directory and publishes only after all
assets pass validation. The destination must not already exist.

```text
prepared/
├── manifest.json
├── indexes/features.sqlite
├── rasters/dem.tif
├── rasters/<name>.tif
├── rasters/d8.tif             # optional
├── vectors/catchments.fgb
└── vectors/segments.fgb
```

The SQLite `features` table relates each typed ID to its downstream ID,
Strahler order, catchment and segment FIDs, and both feature bounds. It has
indexes for ID, downstream traversal, and vector FID lookup.

`manifest.json` records contract version 1, the complete grid, schemas,
normalization counts, source metadata and provenance hashes, and relative
prepared-asset paths and hashes. Library callers can use:

```python
from mgb_vec_hydro import PreparedDataset

dataset = PreparedDataset.open("prepared")
dataset.validate(verify_hashes=True)
```

Normal opening performs structural validation. Hash verification is explicit
because reading every continental asset on every command startup is expensive.
