# Stage 4: terrain products

`mgb-vec-hydro terrain-products` consumes explicit prepared files. The DEM is
authoritative for the canonical CRS, transform, dimensions, and grid. The catchment-ID,
segment-ID, and optional D8 inputs must match that grid exactly.

```bash
mgb-vec-hydro terrain-products \
  --dem prepared/dem.tif \
  --grid-catchments prepared/grid_catchments.tif \
  --grid-segments prepared/grid_segments.tif \
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
| `--grid-catchments` | Required | Existing raster path | Prepared mini-ID raster with embedded bounds defining the cells for each mini. |
| `--grid-segments` | Required | Existing raster path | Prepared int32 segment-ID raster; IDs must match overlaid catchment ownership. |
| `--d8` | Optional; required in D8 mode | Existing raster path | Canonical clockwise D8 raster used when `--direction-source=d8`; it must match the DEM grid. |
| `--output-dir` | Required | Directory path | New directory where `hand.tif`, `ltnd.tif`, and any requested flow-direction output are published. |
| `--direction-source` | Optional | `dem` or `d8`; default `dem` | Selects DEM-conditioned routing or the explicit D8 raster as the flow-direction source. |
| `--write-flow-direction` | Optional flag | Disabled by default | Publishes the selected directions as `flow_direction.tif`. |
| `--agree-sharp` | Optional | Non-negative number; default `80.0` | Additional stream-cell incision in DEM elevation units for AGREE conditioning in DEM mode. |
| `--agree-smooth` | Optional | Non-negative number; default `8.0` | AGREE ramp depth per pixel toward the stream in DEM mode. |
| `--agree-buffer` | Optional | Non-negative integer pixels; default `4` | AGREE conditioning radius around the stream in DEM mode. |
| `--workers` | Optional | Positive integer; default `4` | Number of worker processes used for bounded terrain work. There is no upper limit imposed by the CLI or stage validator. |
| `--memory-limit-mb` | Optional | Positive integer MB; default `4096` | Soft memory sizing hint for terrain packets and working storage. |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent raster reads. |

Click also provides `--help` to display the command’s generated option list.

`--direction-source` is `dem` by default or `d8`. D8 mode requires an explicit
`--d8` raster containing canonical clockwise codes. Use
`--write-flow-direction` to publish the directions selected by the run.
Execution defaults are four workers, 4096 MB (4 GB) as a soft memory hint, and two I/O
slots. Packets target the memory hint divided by twice the worker count,
with no fixed mini-count cap. A complete mini above the target gets its own
packet if it fits the hint; otherwise processing raises `WorkMemoryError`.
Worker counts may be any positive integer.

Terrain reads mini IDs and bounds from the `mini_index` JSON tag in
`grid_catchments.tif`. It does not read mini vectors or regenerate catchment and segment grids.
Each mini is an indivisible work unit. Workers use the shared aligned COG reader, preserve
strict ownership without buffering, and the coordinator alone assembles final
COGs.

## Nodata policy

Matching drainage is `grid_segments == mini_id` within owned cells. Positive
segment IDs must match overlaid ownership. Every owned cell must have valid DEM
and segment-grid data. Owned cells in an 8-connected component without matching
drainage are NoData in HAND and LTND. In D8 mode, invalid directions and routes
still fail within components connected to matching drainage. Terrain products
keep validity masks, and sampling applies its own partial-nodata rules. See
[shared raster nodata policy](shared_execution.md#nodata-policy).

DEM mode applies catchment-confined AGREE conditioning, deterministic flat
handling, and targeted shallow breaching to matching drainage. HAND always
uses the unmodified DEM. D8 mode terminalizes matching drainage cells; every
other owned cell connected to drainage must have a valid direction, stay
within its mini, avoid cycles, and terminate on matching drainage. Invalid D8
paths fail before any output is published.

The published directory contains these root-level files, including an audit
manifest with the input paths and processing parameters:

```text
terrain/
├── manifest-terrain-products.json
├── hand.tif
├── ltnd.tif
├── undrained_cells.csv
└── flow_direction.tif     # optional
```

`undrained_cells.csv` lists only minis with undrained cells, in mini-ID order,
with cell counts, total owned cells, and the percentage undrained. It contains
only its header when every mini drains.

There is no copied domain raster, no index copy, and no nested directory. All
outputs are full canonical-grid COGs with internal
validity masks: HAND and LTND are `float32`, and flow direction is `uint8`
with codes 0 for drainage and 1–8 for N, NE, E, SE, S, SW, W, and NW. The
report and CLI status identify the concrete paths written and include planning,
raster-read, conditioning/D8-validation, routing, compression, and
cell-count diagnostics, including the total number of undrained cells.

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

`--memory-limit-mb` is a soft sizing hint for task packets and retained
intermediates, without separate quotas. Workers and a small queue bound
concurrency; library caches have explicit sizes. Actual RSS can exceed the
hint. See [shared memory sizing](shared_execution.md#local-execution).
