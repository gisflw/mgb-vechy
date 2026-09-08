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

## Options

| Option | Status | Type/default | Meaning |
| --- | --- | --- | --- |
| `--crs` | Required | CRS text | Target CRS for the published ROI; geographic and projected CRSs are supported. |
| `--catchments` | Required | Existing vector path | Catchment provider containing the source catchment polygons. |
| `--catchments-layer` | Optional | Layer name | Selects a layer when the catchment provider contains multiple layers. |
| `--catchments-source-crs` | Optional | CRS text | Overrides missing or incorrect CRS metadata for the catchment provider. |
| `--segments` | Required | Existing vector path | Segment provider containing the source network and topology attributes. |
| `--segments-layer` | Optional | Layer name | Selects a layer when the segment provider contains multiple layers. |
| `--segments-source-crs` | Optional | CRS text | Overrides missing or incorrect CRS metadata for the segment provider. |
| `--outlet-id` | Required; repeatable | Text | Segment ID of an outlet; provide one or more outlets whose upstream union defines the ROI. |
| `--id-col` | Required | Field name | Source field containing the segment/catchment identifier. |
| `--id-down-col` | Required | Field name | Segment field containing the downstream segment identifier; null values represent sinks. |
| `--strahler-order-col` | Required | Field name | Segment field containing the Strahler order used during topology filtering. |
| `--output-dir` | Required | Directory path | New directory where `roi_catchments.fgb` and `roi_segments.fgb` are published. |
| `--workers` | Optional | Positive integer; default `4` | Number of worker processes used for bounded geometry work. There is no upper limit imposed by the CLI or stage validator. |
| `--memory-limit-mb` | Optional | Positive integer MB; default `512` | Memory budget used to size bounded processing packets. |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent source-I/O operations. |
| `--batch-size` | Optional | Positive integer rows; default `10000` | Number of provider rows inspected per bounded attribute scan. |
| `--checkpoint-dir` | Optional | Directory path | Scratch location for resumable geometry packets; it must be outside `--output-dir`. |

Click also provides `--help` to display the command’s generated option list.

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
two concurrent I/O operations, and 10,000-row scans. Worker counts may be any
positive integer. Checkpoints are scratch state outside `--output-dir` and are
removed after successful publication.
