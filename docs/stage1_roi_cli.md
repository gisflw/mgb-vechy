# Stage 1: define ROI

`mgb-vec-hydro define-roi` reads raw GeoPackage, FlatGeobuf, or ESRI
FileGDB providers and selects topology upstream of one or more outlets. It
defines the authoritative output CRS for the downstream vector stages.

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
`--catchments-source-crs` and `--segments-source-crs` are the only source-CRS
overrides; they replace missing or incorrect provider metadata. The target
CRS remains the explicit `--crs` value.

Topology attributes are streamed in bounded Arrow batches. Null, non-finite,
and below-one Strahler rows are removed before traversal; selected values must
then be integral. Null downstream IDs are sinks. Duplicate IDs, cycles,
missing source pairs, incompatible CRS values, and invalid polygon/line
geometries are rejected.

The normalized output schema is:

`id`, `id_down`, `sub`, `strahler_order`, `unit_length`, `upstream_length`,
`unit_area`, `upstream_area`, `water_course`, `geometry`.

`unit_length` is geodesic length in km and `unit_area` is geodesic area in
km². Upstream metrics are deterministic topology reductions. Selected
geometry is processed in bounded worker packets, checkpointed when requested,
and written to spatially indexed FlatGeobuf files.

The published directory contains exactly these root-level files:

```text
roi/
├── roi_catchments.fgb
└── roi_segments.fgb
```

There is no `manifest.json` and no nested output directory. The report and
CLI status identify both concrete paths. Defaults are 512 MB, four workers,
two concurrent I/O operations, and 10,000-row scans. Checkpoints are scratch
state outside `--output-dir` and are removed after successful publication.
