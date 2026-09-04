# Stage 4 Terrain Products CLI

`mgb-vec-hydro terrain-products` creates a versioned, canonical-grid terrain
dataset from prepared rasters, including the mini domain prepared after
aggregation. It
uses bounded complete-mini work units, deterministic local multiprocessing, and
coordinator-only COG assembly.

## Basic usage

```bash
mgb-vec-hydro terrain-products \
  --prepared prepared \
  --output-dir output/terrain \
  --direction-source dem \
  --agree-sharp 80 \
  --agree-smooth 8 \
  --agree-buffer 4
```

`--prepared` and `--output-dir` are required.
`--direction-source` is either `dem` (the default) or `d8`. Use
`--write-flow-direction` to publish the selected direction raster. Execution
defaults are four workers, 512 MB of admitted task memory, two concurrent I/O
operations, 10,000-row vector scans, and at most eight spatially adjacent minis
per packet. `--checkpoint-dir` enables resumable domain and terrain packets.

AGREE controls apply only to DEM-derived directions. Sharp and smooth values
must be finite and non-negative, and the AGREE buffer is a non-negative number
of pixels.

## Inputs and ownership

The prepared version-4 dataset supplies the authoritative projected CRS,
transform, dimensions, DEM, optional canonical-clockwise D8 COG, rasterized
ownership, drainage, and the dense mini index. Terrain does not open mini
vectors or rasterize geometry.

Each aggregated mini is an indivisible processing unit. Gaps remain masked,
ownership conflicts are rejected, and ownership is never buffered. Terrain
workers read only prepared COG windows.

## Direction behavior

DEM mode applies catchment-confined AGREE conditioning, retains steepest metric
D8 descent, resolves flats deterministically, and uses targeted shallow
breaching to connect trapped basins to matching drainage. HAND elevations
always come from the unmodified DEM, so negative HAND is possible.

D8 mode requires the prepared `d8` asset. Rasterized drainage cells become
terminals. Every other owned cell must have direction 1 through 8, remain inside
the same mini, avoid nodata, contain no cycle, and terminate on matching
drainage. Invalid minis fail the job without publishing output.

## Output contract

The directory is staged privately, validated, and published with one rename:

```text
terrain/
├── manifest.json
├── mini_index.parquet
└── rasters/
    ├── mini_ownership.tif
    ├── drainage.tif
    ├── hand.tif
    ├── ltnd.tif
    └── flow_direction.tif  # optional
```

`mini_index.parquet` maps deterministic one-based `int32` labels to original
mini IDs without changing their type. Every raster is a full canonical-grid COG
with an internal validity mask. Ownership is `int32`; drainage and optional
directions are `uint8`; HAND and LTND are `float32`. Direction codes are
0 for drainage and 1 through 8 for N, NE, E, SE, S, SW, W, and NW.

The report separates planning, vector reads, rasterization, raster reads,
conditioning or D8 validation, routing, product calculation, coordination,
checkpointing, output writes, compression, and total time. It also reports
mini, owned-cell, drainage-cell, and negative-HAND counts.
