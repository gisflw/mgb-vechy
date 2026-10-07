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
| `--memory-limit-mb` | Optional | Positive integer MB; default `512` | Soft memory sizing hint; see [shared execution](shared_execution.md#local-execution). |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent source-I/O operations. |
| `--batch-size` | Optional | Positive integer rows; default `10000` | Number of provider rows inspected per bounded attribute scan. |

Click also provides `--help` to display the command’s generated option list.

Use `--catchments-layer` and `--segments-layer` for multi-layer containers.
`--catchments-source-crs` and `--segments-source-crs` are the only source-CRS
overrides; they replace missing or incorrect provider metadata. The target
CRS remains the explicit `--crs` value.

Topology attributes are streamed in bounded Arrow batches. Null, non-finite,
and below-one Strahler rows are removed before traversal; selected values must
then be integral. Null downstream IDs are sinks. Duplicate segment IDs and
duplicate catchment IDs within the selected ROI, cycles, missing source pairs,
incompatible CRS values, and invalid polygon/line geometries are rejected.

The normalized output schema is:

`id`, `id_down`, `sub`, `strahler_order`, `unit_length`, `upstream_length`,
`unit_area`, `upstream_area`, `water_course`, `geometry`.

`unit_length` is geodesic length in km and `unit_area` is geodesic area in
km². Upstream metrics are deterministic topology reductions. Selected
geometry is processed in bounded worker packets and written to spatially
indexed FlatGeobuf files.

The published directory contains these root-level files, including an audit
manifest with the input paths and processing parameters:

```text
roi/
├── manifest-define-roi.json
├── roi_catchments.fgb
└── roi_segments.fgb
```

There is no nested output directory. The report and CLI status identify both
concrete paths. Defaults are 512 MB, four workers,
two concurrent I/O operations, and 10,000-row scans. Worker counts may be any
positive integer.

`--memory-limit-mb` is a soft sizing hint for task packets and retained
intermediates, without separate quotas. Workers and a small queue bound
concurrency; library caches have explicit sizes. Actual RSS can exceed the
hint. See [shared memory sizing](shared_execution.md#local-execution).

`--batch-size` bounds provider read batches, not geometry processing packets.
Geometry packets use source-size estimates and the memory hint, distributed
across workers. The shared reader splits OGRSQL FID requests at 4,997 IDs;
GeoPackage uses its native SQL without that request cap.
