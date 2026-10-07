# Stage 3: prepare raster data

`mgb-vec-hydro prepare` consumes the explicit aggregated mini-catchment and
mini-segment files. The mini-catchment CRS is authoritative; the segment file,
DEM, and every optional raster must declare that CRS.

```bash
mgb-vec-hydro prepare \
  --dem data/dem.tif \
  --mini-catchments minis/mini_catchments.fgb \
  --mini-segments minis/mini_segments.fgb \
  --categorical-raster hru data/hru.tif \
  --output-dir prepared
```

## Options

| Option | Status | Type/default | Meaning |
| --- | --- | --- | --- |
| `--dem` | Required | Existing raster path | DEM defining the canonical CRS, resolution, orientation, and raster grid. |
| `--dem-scale` | Optional | Finite positive number; default `1.0` | Multiply stored DEM elevations by this factor to normalize them to metres. Use `0.01` for centimetres. |
| `--mini-catchments` | Required | Existing vector path | Aggregated mini-catchment polygons used to define ownership and the prepared domain. |
| `--mini-segments` | Required | Existing vector path | Aggregated mini-segment lines used to create drainage and validate the mini domain. |
| `--continuous-raster` | Optional; repeatable | `NAME PATH` | Named single-band continuous raster to clip and publish as `<name>.tif`; names must be valid and non-reserved. |
| `--categorical-raster` | Optional; repeatable | `NAME PATH` | Named single-band categorical raster to clip and publish as `<name>.tif`; names must be valid and non-reserved. |
| `--d8` | Optional; conditional | Existing raster path | Optional D8 raster to normalize and publish as `d8.tif`; must be supplied together with `--d8-encoding`. |
| `--d8-encoding` | Optional; conditional | `canonical` or `esri` | Encoding of `--d8`; must be supplied together with `--d8`. Output is normalized to canonical clockwise codes. |
| `--memory-limit-mb` | Optional | Positive integer MB; default `512` | Soft memory sizing hint for raster tasks and working storage. |
| `--workers` | Optional | Positive integer; default `4` | Number of worker processes used for bounded block processing. There is no upper limit imposed by the CLI or stage validator. |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent source-raster reads. |
| `--output-dir` | Required | Directory path | New directory where prepared COGs, cells with embedded mini bounds, and drainage are published. |

Click also provides `--help` to display the command’s generated option list.

The DEM defines the native resolution and orientation. Source rasters must
already be aligned, cover the buffered mini-catchment domain, and use a single
band. No implicit reprojection or resampling is performed. Optional D8 input
requires `--d8-encoding canonical|esri` and is normalized to canonical
clockwise codes. COGs use 512-pixel tiles and internal validity masks.

The canonical DEM stores elevations in **metres**. `--dem-scale` applies only
to valid DEM cells during the existing block processing; it does not change
the grid, masks, or named continuous/categorical rasters. The default assumes
stored source values are already metres. No units are inferred from elevation
magnitude, and raster scale/offset metadata is not applied implicitly: the
factor multiplies the stored source values. Prepared DEM metadata records
`units=m` and `dem_scale`; its values already include that conversion.

For a centimetre DEM, add `--dem-scale 0.01` to the command above. Library
callers use `PreparationSpec(dem_scale=0.01, ...)`. Regenerate preparation,
terrain products, and sampling into new output directories when migrating
existing centimetre-based datasets; downstream stages do not scale a second
time. Prepared elevations and distances must declare `units=m`.

Source clipping, domain masking, cell and drainage rasterization, and
local connectivity labeling run as bounded 512-pixel block tasks through the
shared process executor. Results may finish out of order, but the coordinator
reduces them in row-major order and is the only process that writes working
rasters. Use `--workers` and `--io-slots` to control CPU and concurrent source
reads.

Preparation derives its raster domain from the explicit mini-catchment file.
Catchments and matching segments are jointly rasterized in deterministic
512-pixel blocks. Cells exactly cover the center-of-pixel catchment union;
shared boundary cells use the lowest mini ID, while true cell
overlaps are rejected. Connectivity validation keeps one drainage-bearing
8-connected component per mini, deterministically reassigning enclosed
discarded components and masking exterior fragments. Drainage always has the
ownership validity mask.

Preparation requires dense integer mini IDs `1..N`, matching aggregation
output. `cells.tif` stores those IDs directly as `int32` values. Its dataset
metadata contains a compact JSON tag named `mini_index`: ordered records
`[mini_id, minx, miny, maxx, maxy]` in the raster CRS. Bounds are tight
pixel-edge rectangles derived from final ownership after connectivity
corrections, using a bounded block scan.

Terrain and sampling read this tag directly. There is no separate index file,
version, or compatibility reader. Previously prepared rasters must be
regenerated. The published directory contains these root-level files, plus
one `<name>.tif` for each requested named raster and an audit manifest:

```text
prepared/
├── manifest-prepare.json
├── dem.tif
├── <name>.tif
├── d8.tif                  # optional
├── cells.tif
└── drainage.tif
```

There is no nested output directory. All files are validated before the
staged files are published into the output folder. Defaults are four
workers, 512 MB as a soft memory hint, two I/O
slots, and one native DEM-cell buffer; worker counts may be any positive integer.
Existing output folders are reused. The CLI asks before processing if any
requested output files already exist. Staging-only working files are removed
before publication. The CLI prints
every concrete file path written.

`--memory-limit-mb` is a soft sizing hint for task packets and retained
intermediates, without separate quotas. Workers and a small queue bound
concurrency; library caches have explicit sizes. Actual RSS can exceed the
hint. See [shared memory sizing](shared_execution.md#local-execution).
