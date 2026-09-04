# Stage 3: prepare raster data

`mgb-vec-hydro prepare` runs after aggregation. It uses the ROI CRS recorded
through `--minis`, clips already-aligned rasters to the buffered ROI domain,
and rasterizes aggregated mini ownership and drainage.

```bash
mgb-vec-hydro prepare \
  --dem data/dem.tif \
  --minis output/minis \
  --categorical-raster hru data/hru.tif \
  --output-dir prepared
```

No raster is reprojected or resampled. The DEM must already use the ROI CRS;
all optional rasters must have the same resolution and pixel alignment and
cover the ROI plus `--buffer-cells` (one native DEM cell by default). Optional
D8 requires `--d8-encoding canonical|esri`.
COGs use 512-pixel tiles and internal validity masks.

The version-4 layout includes terrain domain inputs:

```text
prepared/
├── manifest.json
└── rasters/
    ├── dem.tif
    ├── <name>.tif
    └── d8.tif              # optional
    ├── mini_ownership.tif
    └── drainage.tif
```

The manifest records the exact DEM-derived grid, source inputs, mini-domain
assets, and a deterministic mini index. All COGs are validated before
the private staging directory is atomically renamed into place.

Defaults are 512 MB of execution memory. Existing output directories are
rejected and private staging data is removed after errors or cancellation.
