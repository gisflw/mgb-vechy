# Stage 1: define ROI

`mgb-vec-hydro define-roi` reads raw GeoPackage, FlatGeobuf, or ESRI FileGDB
providers and selects topology upstream of one or more outlets. It defines the output CRS;
the CRS may be geographic or projected. The DEM is validated against that CRS
later by `prepare`.

```bash
mgb-vec-hydro define-roi \
  --crs ESRI:102033 \
  --catchments data/catchments.gpkg \
  --segments data/segments.gpkg \
  --outlet-id 90497 \
  --id-col cotrecho \
  --id-down-col nutrjus \
  --strahler-order-col nustrahler \
  --output-dir roi
```

Use `--catchments-layer` and `--segments-layer` for multi-layer containers.
FileGDB inputs require the corresponding layer option; the layer options also
select layers in GeoPackage inputs.
The independent `--catchments-source-crs` and `--segments-source-crs` options
replace missing or incorrect provider metadata. There is no target-CRS option.
Column matching is case-insensitive.

Topology attributes are streamed without geometry in Arrow batches of 10,000.
Null, non-finite, and below-one Strahler rows are removed before traversal;
selected values must then be integral. Null downstream IDs are sinks. An outlet may
drain outside the ROI, while every other selected segment must connect toward
a selected outlet. Duplicate IDs, cycles, missing source pairs, and invalid
polygon/line geometries are rejected.

Only selected geometry is decoded. Selected FIDs are processed in bounded worker
packets, validated and reprojected with Shapely/PyProj, checkpointed as Arrow IPC,
and written directly through Pyogrio. Each source geometry is read and transformed
once. The output columns are:

`id`, `id_down`, `sub`, `strahler_order`, `unit_length`, `upstream_length`,
`unit_area`, `upstream_area`, `water_course`, `geometry`.

`unit_length` is computed from segment geometry using the source CRS ellipsoid
and geodesic calculations, in km. `unit_area` is computed similarly from
catchment geometry, in km². `upstream_length` and `upstream_area` are derived
from topology by summing the selected unit metrics.
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
On successful completion, the command also prints runtime timings for
provider/topology loading, metric calculation, output publication, and total
time.
