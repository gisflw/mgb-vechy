# Stage 1: define ROI

`mgb-vec-hydro define-roi` reads raw GeoPackage or FlatGeobuf providers and
selects topology upstream of one or more outlets. Its target CRS comes only
from the Stage 0 manifest.

```bash
mgb-vec-hydro define-roi \
  --prepared prepared \
  --catchments data/catchments.gpkg \
  --segments data/segments.gpkg \
  --outlet-id 90497 \
  --id-col cotrecho \
  --id-down-col nutrjus \
  --strahler-order-col nustrahler \
  --upstream-area-col nuareamont \
  --output-dir roi
```

Use `--catchments-layer` and `--segments-layer` for multi-layer containers.
The independent `--catchments-source-crs` and `--segments-source-crs` options
replace missing or incorrect provider metadata. There is no target-CRS option.
Column matching is case-insensitive.

Topology attributes are streamed without geometry in batches of 10,000.
Null, non-finite, and below-one Strahler rows are removed before traversal;
selected values must then be integral. Selected provider upstream-area values
must be finite and non-negative. Null downstream IDs are sinks. An outlet may
drain outside the ROI, while every other selected segment must connect toward
a selected outlet. Duplicate IDs, cycles, missing source pairs, and invalid
polygon/line geometries are rejected.

Only selected geometry is decoded and reprojected. The output columns are:

`id`, `id_down`, `sub`, `strahler_order`, `unit_length`, `upstream_length`,
`unit_area`, `upstream_area`, `water_course`, `geometry`.

Lengths (km), areas (km²), and upstream length are computed after reprojection.
`upstream_area` is copied directly from the required provider column.
Repeated outlets are ordered downstream to upstream; later overlapping outlet
domains overwrite `sub` assignments.

The versioned directory is atomically published as:

```text
roi/
├── manifest.json
└── vectors/
    ├── roi_catchments.fgb
    └── roi_segments.fgb
```

Published vectors are spatially indexed FlatGeobuf. Execution defaults are
512 MB, four workers, two concurrent I/O operations, and 10,000-row scans.
