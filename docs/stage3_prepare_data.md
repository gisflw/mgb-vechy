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
| `--mini-catchments` | Required | Existing vector path | Aggregated mini-catchment polygons used to define ownership and the prepared domain. |
| `--mini-segments` | Required | Existing vector path | Aggregated mini-segment lines used to create drainage and validate the mini domain. |
| `--continuous-raster` | Optional; repeatable | `NAME PATH` | Named single-band continuous raster to clip and publish as `<name>.tif`; names must be valid and non-reserved. |
| `--categorical-raster` | Optional; repeatable | `NAME PATH` | Named single-band categorical raster to clip and publish as `<name>.tif`; names must be valid and non-reserved. |
| `--d8` | Optional; conditional | Existing raster path | Optional D8 raster to normalize and publish as `d8.tif`; must be supplied together with `--d8-encoding`. |
| `--d8-encoding` | Optional; conditional | `canonical` or `esri` | Encoding of `--d8`; must be supplied together with `--d8`. Output is normalized to canonical clockwise codes. |
| `--memory-limit-mb` | Optional | Positive integer MB; default `512` | Admitted memory budget used to size bounded raster block tasks. |
| `--workers` | Optional | Positive integer; default `4` | Number of worker processes used for bounded block processing. There is no upper limit imposed by the CLI or stage validator. |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent source-raster reads. |
| `--output-dir` | Required | Directory path | New directory where prepared COGs, ownership, drainage, and the shared mini index are published. |

Click also provides `--help` to display the command’s generated option list.

The DEM defines the native resolution and orientation. Source rasters must
already be aligned, cover the buffered mini-catchment domain, and use a single
band. No implicit reprojection or resampling is performed. Optional D8 input
requires `--d8-encoding canonical|esri` and is normalized to canonical
clockwise codes. COGs use 512-pixel tiles and internal validity masks.

Source clipping, domain masking, ownership and drainage rasterization, and
local connectivity labeling run as bounded 512-pixel block tasks through the
shared process executor. Results may finish out of order, but the coordinator
reduces them in row-major order and is the only process that writes working
rasters. Use `--workers` and `--io-slots` to control CPU and concurrent source
reads.

Preparation derives its raster domain from the explicit mini-catchment file.
Catchments and matching segments are jointly rasterized in deterministic
512-pixel blocks. Ownership exactly covers the center-of-pixel catchment union;
shared boundary cells use the lowest stable dense label, while true cell
overlaps are rejected. Connectivity validation keeps one drainage-bearing
8-connected component per mini, deterministically reassigning enclosed
discarded components and masking exterior fragments. Drainage always has the
ownership validity mask.

The six-column `mini_index.parquet` is the single shared mini index for later
stages. Its columns, in order, are:

`mini_label`, `mini_id`, `minx`, `miny`, `maxx`, `maxy`.

Labels are dense one-based `int32` values and bounds are finite. The published
directory contains exactly these root-level files, plus one `<name>.tif` for
each requested named raster:

```text
prepared/
├── dem.tif
├── <name>.tif
├── d8.tif                  # optional
├── mini_ownership.tif
├── drainage.tif
└── mini_index.parquet
```

There is no `manifest.json` and no nested output directory. All files are
validated before the private staging directory is atomically renamed into
place. Defaults are four workers, 512 MB of admitted block memory, two I/O
slots, and one native DEM-cell buffer; worker counts may be any positive integer.
Existing output directories are rejected
and staging-only working files are removed before publication. The CLI prints
every concrete file path written.
