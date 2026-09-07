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

The DEM defines the native resolution and orientation. Source rasters must
already be aligned, cover the buffered mini-catchment domain, and use a single
band. No implicit reprojection or resampling is performed. Optional D8 input
requires `--d8-encoding canonical|esri` and is normalized to canonical
clockwise codes. COGs use 512-pixel tiles and internal validity masks.

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
place. Defaults are 512 MB and one native DEM-cell buffer. Existing output
directories are rejected and staging-only working files are removed before
publication. The CLI prints every concrete file path written.
