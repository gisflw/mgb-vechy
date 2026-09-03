# Stage 0: prepare raster data

`mgb-vec-hydro prepare` establishes the authoritative projected CRS and grid
and stages single-band rasters as tiled COGs. It neither opens nor publishes
vector data.

```bash
mgb-vec-hydro prepare \
  --dem data/dem.tif \
  --crs ESRI:102033 \
  --resolution 30 \
  --categorical-raster hru data/hru.tif \
  --output-dir prepared
```

Continuous rasters use bilinear resampling and `float32`; categorical rasters
use nearest-neighbour resampling and integral `int32`. Optional D8 input must
already match the canonical grid and requires `--d8-encoding canonical|esri`.
COGs use 512-pixel tiles and internal validity masks.

The version-3 layout is raster-only:

```text
prepared/
├── manifest.json
└── rasters/
    ├── dem.tif
    ├── <name>.tif
    └── d8.tif              # optional
```

The manifest records the exact CRS, transform, extent, shape, resolution,
nodata convention, raster sources, and raster assets. Version-2 manifests and
manifests containing vector assets are rejected. All COGs are validated before
the private staging directory is atomically renamed into place.

Defaults are 512 MB of execution memory. Existing output directories are
rejected and private staging data is removed after errors or cancellation.
