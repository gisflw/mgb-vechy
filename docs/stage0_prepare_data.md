# Stage 0: prepare data

`mgb-vec-hydro prepare` stages raw hydrography and raster sources for the
larger-than-memory workflow. It performs format conversion and alignment, not
full scientific or topological validation.

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
  --categorical-raster hru data/hru.tif \
  --output-dir prepared
```

Use `--catchments-layer` and `--segments-layer` for multi-layer sources such
as FileGDB directories. Missing or incorrect source CRS metadata can be
replaced with `--catchments-source-crs` or `--segments-source-crs`.

Named raster options may be repeated. Names begin with a lowercase letter and
contain only lowercase letters, digits, underscores, or hyphens. `dem` and
`d8` are reserved.

## Vector staging

Segments are read first in Arrow batches. Rows are retained only when their
Strahler value is numeric, finite, non-null, and at least one. Positive
fractional values are retained unchanged; Stage 0 does not impose an integral
Strahler contract.

Catchments are then restricted to the retained segment IDs. This removes the
catchments paired with rejected segment rows as well as unmatched polygons
such as the HydroSHEDS coastline records identified by `STRM_ID = -1`.

The selected fields are renamed and the geometries are reprojected to the
canonical CRS before being streamed to spatially indexed FlatGeobuf files:

- `catchments.fgb`: `id` and geometry.
- `segments.fgb`: `id`, `id_down`, `strahler_order`, and geometry.

Source value types and polygon/line geometry variants are preserved. Stage 0
does not normalize identifiers, reject duplicates, compare topology, validate
or repair geometries, or require downstream IDs to be internal. Those checks
belong to the later stage that defines the selected network domain.

## Canonical grid and rasters

The DEM bounds are transformed to the requested projected, metre-based CRS and
snapped outward to square cells anchored at `(0, 0)`. All prepared rasters use
that exact CRS, affine transform, width, and height.

Prepared rasters are 512-pixel tiled COGs with internal validity masks:

- DEM and named continuous rasters are `float32` with bilinear resampling.
- Categorical rasters contain integral `int32` values and use nearest-neighbor
  resampling.
- D8 is `uint8`, is not reprojected, and must already match the canonical grid.

Canonical D8 values are `0` for a terminal and `1` through `8` for N, NE, E,
SE, S, SW, W, and NW. ESRI power-of-two input codes are converted when
`--d8-encoding esri` is selected.

## Layout and manifest

```text
prepared/
├── manifest.json
├── rasters/
│   ├── dem.tif
│   ├── <name>.tif
│   └── d8.tif             # optional
└── vectors/
    ├── catchments.fgb
    └── segments.fgb
```

The version-2 manifest records the canonical grid, source paths and mappings,
asset paths and schemas, raster metadata, output counts, and filtered counts.
It intentionally contains no checksums or feature lookup database.

`PreparedDataset.open()` only parses the manifest and therefore does not scan
continental assets. Call `validate()` when a shallow contract, safe-path, and
asset-existence check is wanted:

```python
from mgb_vec_hydro import PreparedDataset

dataset = PreparedDataset.open("prepared")
dataset.validate()
```

Preparation writes into a private sibling directory and renames it into place
only after every output has been written. Existing destinations are rejected,
and private staging data is removed after errors or cancellation.
