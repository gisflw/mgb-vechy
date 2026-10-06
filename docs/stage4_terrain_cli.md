# Stage 4: terrain products

`mgb-vec-hydro terrain-products` consumes explicit prepared files. The DEM is
authoritative for the canonical CRS, transform, dimensions, and grid. The cells,
drainage, and optional D8 inputs must match that grid exactly.

```bash
mgb-vec-hydro terrain-products \
  --dem prepared/dem.tif \
  --cells prepared/cells.tif \
  --drainage prepared/drainage.tif \
  --direction-source dem \
  --agree-sharp 80 \
  --agree-smooth 8 \
  --agree-buffer 4 \
  --output-dir terrain
```

## Options

| Option | Status | Type/default | Meaning |
| --- | --- | --- | --- |
| `--dem` | Required | Existing raster path | Canonical DEM defining the CRS, transform, dimensions, and grid. |
| `--cells` | Required | Existing raster path | Prepared mini-ID raster with embedded bounds defining the cells for each mini. |
| `--drainage` | Required | Existing raster path | Prepared drainage raster identifying the matching drainage cells. |
| `--d8` | Optional; required in D8 mode | Existing raster path | Canonical clockwise D8 raster used when `--direction-source=d8`; it must match the DEM grid. |
| `--output-dir` | Required | Directory path | New directory where `hand.tif`, `ltnd.tif`, and any requested flow-direction output are published. |
| `--direction-source` | Optional | `dem` or `d8`; default `dem` | Selects DEM-conditioned routing or the explicit D8 raster as the flow-direction source. |
| `--write-flow-direction` | Optional flag | Disabled by default | Publishes the selected directions as `flow_direction.tif`. |
| `--agree-sharp` | Optional | Non-negative number; default `80.0` | Additional stream-cell incision in DEM elevation units for AGREE conditioning in DEM mode. |
| `--agree-smooth` | Optional | Non-negative number; default `8.0` | AGREE ramp depth per pixel toward the stream in DEM mode. |
| `--agree-buffer` | Optional | Non-negative integer pixels; default `4` | AGREE conditioning radius around the stream in DEM mode. |
| `--workers` | Optional | Positive integer; default `4` | Number of worker processes used for bounded terrain work. There is no upper limit imposed by the CLI or stage validator. |
| `--memory-limit-mb` | Optional | Positive integer MB; default `512` | Admitted memory budget used to size terrain packets. |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent raster reads. |

Click also provides `--help` to display the command’s generated option list.

`--direction-source` is `dem` by default or `d8`. D8 mode requires an explicit
`--d8` raster containing canonical clockwise codes. Use
`--write-flow-direction` to publish the directions selected by the run.
Execution defaults are four workers, 512 MB of admitted task memory, and two I/O
slots, with at most eight complete minis per packet. Worker counts may be any
positive integer.

Terrain reads mini IDs and bounds from the `mini_index` JSON tag in
`cells.tif`. It does not read mini vectors or regenerate cells and drainage.
Each mini is an indivisible work unit. Workers use the shared aligned COG reader, preserve
strict ownership without buffering, and the coordinator alone assembles final
COGs.

DEM mode applies catchment-confined AGREE conditioning, deterministic flat
handling, and targeted shallow breaching to matching drainage. HAND always
uses the unmodified DEM. D8 mode terminalizes matching drainage cells; every
other owned cell must have a valid direction, stay within its mini, avoid
cycles, and terminate on matching drainage. Invalid D8 paths fail before any
output is published.

The published directory contains these root-level files, including an audit
manifest with the input paths and processing parameters:

```text
terrain/
├── manifest-terrain-products.json
├── hand.tif
├── ltnd.tif
└── flow_direction.tif     # optional
```

There is no copied domain raster, no index copy, and no nested directory. All
outputs are full canonical-grid COGs with internal
validity masks: HAND and LTND are `float32`, and flow direction is `uint8`
with codes 0 for drainage and 1–8 for N, NE, E, SE, S, SW, W, and NW. The
report and CLI status identify the concrete paths written and include planning,
raster-read, conditioning/D8-validation, routing, compression, and
cell-count diagnostics.

The prepared DEM must declare `units=m`. HAND stores metre elevation
differences from that DEM. LTND stores **metres**, regardless of whether the
canonical grid uses geographic degrees or projected coordinates (including
feet). Its distances are sums of geodesic cell-centre-to-parent steps on the
source CRS's ellipsoid, using the same ellipsoid handling as Stage 1. Neither
product requires raster reprojection. HAND and LTND declare `units=m`; LTND
also declares `distance_method=geodesic`.

North-up geographic, Mercator, and cylindrical equal-area grids reuse exact
latitude-dependent step tables. Other projected grids transform actual route
edges in bounded vectorized batches. Memory estimates account for those tables
and buffers. Routing and deterministic
tie-breaking are unchanged.

AGREE defaults remain **80.0** for sharp incision, **8.0** for smooth depth,
and **4** buffer pixels. The two depth parameters operate in normalized DEM
units (metres); they are not automatically rescaled by `--dem-scale`.

`compute_ltnd(..., crs=...)` requires an explicit CRS and returns metres.
