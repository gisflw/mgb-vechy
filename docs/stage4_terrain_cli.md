# Stage 4: terrain products

`mgb-vec-hydro terrain-products` consumes explicit prepared files. The DEM is
authoritative for the canonical CRS, transform, dimensions, and grid. The
ownership, drainage, optional D8, and index inputs must match that grid exactly.

```bash
mgb-vec-hydro terrain-products \
  --dem prepared/dem.tif \
  --mini-ownership prepared/mini_ownership.tif \
  --drainage prepared/drainage.tif \
  --mini-index prepared/mini_index.parquet \
  --direction-source dem \
  --agree-sharp 80 \
  --agree-smooth 8 \
  --agree-buffer 4 \
  --output-dir terrain
```

`--direction-source` is `dem` by default or `d8`. D8 mode requires an explicit
`--d8` raster containing canonical clockwise codes. Use
`--write-flow-direction` to publish the directions selected by the run.
Execution defaults are four workers, 512 MB of admitted task memory, two I/O
slots, and at most eight complete minis per packet. `--checkpoint-dir` enables
resumable terrain packets and must be outside `--output-dir`.

Terrain reads `mini_index.parquet` directly and never republishes it. It does
not read mini vectors or regenerate ownership and drainage. Each mini is an
indivisible work unit. Workers use the shared aligned COG reader, preserve
strict ownership without buffering, and the coordinator alone assembles final
COGs.

DEM mode applies catchment-confined AGREE conditioning, deterministic flat
handling, and targeted shallow breaching to matching drainage. HAND always
uses the unmodified DEM. D8 mode terminalizes matching drainage cells; every
other owned cell must have a valid direction, stay within its mini, avoid
cycles, and terminate on matching drainage. Invalid D8 paths fail before any
output is published.

The published directory contains exactly these root-level files:

```text
terrain/
├── hand.tif
├── ltnd.tif
└── flow_direction.tif     # optional
```

There is no `manifest.json`, no copied domain raster, no index copy, and no
nested directory. All outputs are full canonical-grid COGs with internal
validity masks: HAND and LTND are `float32`, and flow direction is `uint8`
with codes 0 for drainage and 1–8 for N, NE, E, SE, S, SW, W, and NW. The
report and CLI status identify the concrete paths written and include planning,
raster-read, conditioning/D8-validation, routing, compression, checkpoint, and
cell-count diagnostics.
