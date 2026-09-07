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
COGs use 512-pixel tiles and internal validity masks. Catchments and segments
are spatially indexed and jointly rasterized in deterministic 512-pixel blocks.
The ownership raster exactly covers the center-of-pixel rasterized catchment
union unless an exterior disconnected fragment is intentionally removed. Shared
boundary cells use the lowest stable dense label, while cells whose centers lie
inside more than one catchment are rejected as true ownership conflicts.

Every published mini is one 8-connected component containing matching drainage.
When a mini has multiple raster components, the component with the most matching
drainage cells is retained (then largest ownership area and row-major first cell
for ties). Enclosed discarded components are assigned to their strongest
adjacent owner; discarded components on the exterior become invalid. Drainage
is cleared on corrected cells and always has exactly the ownership validity mask.

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
On successful completion, the command also prints runtime timings for grid/domain
setup, raster preparation, domain rasterization, validation/publication, and total
time.
