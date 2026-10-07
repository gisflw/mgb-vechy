# Shared vector and raster execution

The shared execution layer provides bounded, deterministic infrastructure for
the CLI stages. It is internal and is not exported from the package root.
Stages import contracts from `execution.executor`, `execution.vector`, `execution.raster`, `execution.memory`, and `execution.publication`.

## Local execution

`LocalExecutor` starts persistent worker processes with Python's `spawn`
context. Every `WorkItem` has a unique stable key, contiguous zero-based
ordinal, and estimated peak byte cost. Admission, task/result queues, and
backpressure keep live declared task memory within
`ExecutionConfig.memory_limit_bytes`; a complete unit is never split.

Results may finish out of order but are reduced in ordinal order. Worker
contexts provide process-local LRU caches for context-managed data sources and
an `io_bound()` semaphore shared by all workers. Reports include counts,
admitted-memory peaks, ordered diagnostics, timings, cancellation, and remote
failures.

`--memory-limit-mb` is a soft sizing hint, not an RSS ceiling. Tasks and
retained scratch each use the full hint independently; there are no reserved
partitions or transfers between them. Concurrency stays bounded by workers
and at most twice as many admitted tasks. Actual memory can exceed the hint
because task arrays, retained scratch, caches, Python/library overhead, and
OS page cache can coexist. Lower the hint if the observed peak is too high.

`MemorySizing` derives packet targets and explicit library cache sizes from
that hint. The coordinator cache uses one eighth of the hint; each worker
cache uses one eighth divided by the worker count. These are cache sizes,
not reserved capacity. Preparation, terrain, and sampling set GDAL caches;
aggregation sets `OGR_SQLITE_CACHE` for its temporary GeoPackage
([GDAL documents this cache in MB](https://gdal.org/en/stable/drivers/vector/sqlite.html#configuration-options)).
Buffered results remain admitted until reduction and are counted once.

`ArrowPacketStore` retains intermediate packets while their complete Arrow
backing buffers fit the scratch hint, otherwise writes IPC files.
Consumed packets release their memory or disk file. Both paths are cleaned
on success and failure. Slices are charged for their backing buffers, not
just visible rows.

## Publication

`AtomicOutputDirectory` creates a private sibling staging directory. A stage
validates every expected file, removes packet/work artifacts, rejects extra
files and nested directories, and publishes with one directory rename when the
output folder is new. Existing folders are reused: only the current stage’s
files are published, with rollback on publication failure; unrelated files stay
in place. The CLI lists conflicting output files and asks for confirmation
before starting any processing (default: no). Python callers must explicitly
set `overwrite=True` on the stage spec to replace existing output files. A
published stage output is the documented flat root-level file set,
including one `manifest-<step>.json` audit file with the inputs and parameters.
Stages may retire known stale output files in the same rollback-safe publish
operation; unrelated files remain untouched.

## Vector access

Stage 1 is the raw-provider boundary. It inspects GeoPackage, FlatGeobuf, and
FileGDB schemas and CRS without eagerly loading all geometry. Topology is
streamed through Arrow, selected IDs are read in bounded packets, and selected
geometry is validated, transformed, and reduced deterministically.
`--batch-size` bounds provider read batches, not geometry processing packets.
Geometry packets use conservative source-size estimates and distribute each
vector kind across workers. The shared reader splits OGRSQL FID selections
into requests of at most 4,997 IDs; GeoPackage uses native SQL without that
request cap. Exact requested-ID validation remains in each vector stage.

Stages 2–5 receive explicit paths to the files they consume. They infer CRS
from the authoritative explicit input for that stage and reject missing or
mismatched CRS metadata. Stage 1 FlatGeobuf output is spatially indexed;
Stage 2 deliberately omits the index to preserve processing order. All
published vector files are root-level files.

## Raster access

### Nodata policy

Raster validity masks are part of the data contract. Preparation carries source
masks into prepared rasters and treats non-finite continuous values as invalid;
cells outside mini ownership are masked. Terrain requires DEM and drainage
coverage for every owned cell, plus valid D8 coverage for every owned cell in
D8 mode. HAND and LTND outputs retain validity masks. Sampling excludes
ownership-exterior cells, warns with per-mini missing-cell counts for DEM,
HAND, LTND, HRU, and drainage, and omits missing values from each statistic's
denominator. It fails when a mini has no valid values for a required statistic.
See the [preparation](stage3_prepare_data.md#nodata-policy),
[terrain](stage4_terrain_cli.md#nodata-policy), and
[sampling](stage5_mini_sampling_cli.md#nodata-policy) guides for stage-specific
behavior.

`grid_from_dem` discovers the canonical grid directly from the explicit DEM.
`plan_raster_units` maps complete mini bounds to covering grid windows and
orders them by deterministic Morton block key. `packet_raster_units` and
`packet_raster_units_by_block` charge conservative memory estimates without
splitting a mini. The packet target is the memory hint divided by twice
the worker count. A complete mini above that target gets its own packet if
it fits the memory hint; otherwise `WorkMemoryError` explains the required
memory hint. Terrain and sampling have no fixed mini-count cap.
`plan_raster_blocks` produces complete canonical blocks in
row-major order for stages whose deterministic reducer depends on neighboring
blocks.

`AlignedRasterReader` accepts a direct map of raster names to COG paths. It
validates each source against the canonical CRS, transform, dimensions, band,
nodata, COG, and internal-mask contract, then reuses handles in each worker.
Downstream raster stages use this reader; there is no directory or
manifest-backed raster reader. Reads require bounded integer windows.

Preparation uses `CoveringRasterReader` for source rasters that share the
canonical resolution and pixel origin but cover a larger extent. It maps
canonical block windows to exact source windows, reuses worker-local handles,
and participates in the same shared I/O semaphore.

`RasterAssembler` is coordinator-only. It merges valid cells from bounded
`RasterPatch` values, rejects duplicate ownership, supports exclusive initial
block writes and bounded corrections, and creates internally masked COGs with
bounded compression threads. Working rasters use uncompressed Rasterio
`MemoryFile` storage when scratch permits, reserving
`ceil(1.25 × cells × (dtype bytes + 1)) + 8 MiB` per product. Preparation
prioritizes cells and drainage, then remaining products by name; terrain uses
stable name order. Disk fallback keeps the existing working compression
choices, and final COG compression is unchanged. Both working storage paths
are released before publication and on failure.

Preparation derives its raster domain from mini-catchment polygons and stores
mini IDs directly in `cells.tif`. A `mini_index` dataset tag contains JSON
records `[mini_id, minx, miny, maxx, maxy]`, ordered by ID. Bounds enclose the
final owned pixels after connectivity correction. Terrain and sampling read
this embedded metadata directly; no index sidecar is published.

## Stage execution

ROI and aggregation use bounded vector packets and coordinator-side grouped
geometry publication. ROI collects scalar metrics during reduction and streams
each final indexed FlatGeobuf through one writer. Aggregation first streams attributes without geometry,
finalizes topology, processing order, and dense IDs, then reads geometry in
bounded packets directly into the temporary GeoPackage for dissolution,
retaining only mapping attributes separately. Preparation clips aligned sources and
rasterizes strict ownership/drainage in bounded parallel blocks, with deterministic ordered
reduction and coordinator-only connectivity correction. Terrain reads
direct COG windows for complete minis and assembles HAND, LTND, and optional
flow direction. Sampling derives block-aware packets and reduces exact mini
statistics into bounded Arrow scratch packets before final HRU-column
discovery and ordered CSV assembly.

Scientific kernels remain responsible for their schemas, validation,
memory factors, topology, ownership, and product rules;
shared infrastructure remains independent of those rules.

## Progress and elapsed time

All five CLI commands show three broad phases on stderr: **Preparing inputs**,
**Processing batches**, and **Finalizing outputs**. In a terminal, one changing
progress line shows completed/total batches and a percentage during processing.
A batch advances after its result has been incorporated, even when workers finish
out of order. The percentage measures processing batches, not total runtime;
finalization may still take time. Redirected output contains only the three
phase labels, without repeated batch updates or terminal control sequences.

The final `Elapsed:` line prints preparing, processing, finalizing, and total
wall-clock durations to one decimal second. These phases cover the complete
stage call, including startup, validation, cleanup, and publication, without
overlap. They sum to total before display rounding. Existing worker and
coordinator timing diagnostics remain in Python reports; they can overlap and
must not be added together as elapsed time.

Python stage functions remain silent by default and accept an optional
keyword-only `progress` callback. It receives `StageProgress` updates from
`execution.progress`, with a `phase` (`preparing`, `processing`, or `finalizing`),
`completed` batch count, and `total` batch count during processing. Report
`timings` dictionaries add `preparing_wall`, `processing_wall`, and
`finalizing_wall`; `total` measures the complete call. Exceptions close the CLI
progress display without marking the operation successful.
