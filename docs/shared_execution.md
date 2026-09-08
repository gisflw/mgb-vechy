# Shared vector and raster execution

The shared execution layer provides bounded, deterministic infrastructure for
the CLI stages. It is internal and is not exported from the package root.
Stages import contracts from `execution.executor`, `execution.checkpoints`,
`execution.vector`, `execution.raster`, and `execution.publication`.

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

## Checkpoints and publication

`CheckpointStore` is optional. `execution_fingerprint` hashes the algorithm,
version, explicit input identity, parameters, and ordered work descriptors.
Stage input identity records each concrete local file path, size, and
modification time, along with authoritative CRS where relevant. This means a
checkpoint cannot silently resume against a different input file or processing
contract. Stage checkpoint directories are scratch locations outside the
published output and are cleaned only after successful atomic publication.

`AtomicOutputDirectory` creates a private sibling staging directory. A stage
validates every expected file, removes packet/work artifacts, rejects extra
files and nested directories, and publishes with one directory rename. A
published stage output is therefore the documented flat root-level file set;
there is no generated `manifest.json`.

## Vector access

Stage 1 is the raw-provider boundary. It inspects GeoPackage, FlatGeobuf, and
FileGDB schemas and CRS without eagerly loading all geometry. Topology is
streamed through Arrow, selected IDs are read in bounded packets, and selected
geometry is validated, transformed, and reduced deterministically.

Stages 2–5 receive explicit paths to the files they consume. They infer CRS
from the authoritative explicit input for that stage and reject missing or
mismatched CRS metadata. Stage 1 FlatGeobuf output is spatially indexed;
Stage 2 deliberately omits the index to preserve processing order. All
published vector files are root-level files.

## Raster access

`grid_from_dem` discovers the canonical grid directly from the explicit DEM.
`plan_raster_units` maps complete mini bounds to covering grid windows and
orders them by deterministic Morton block key. `packet_raster_units` and
`packet_raster_units_by_block` charge conservative memory estimates without
splitting a mini. `plan_raster_blocks` produces complete canonical blocks in
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
bounded compression threads. Working rasters are deleted before publication.

Preparation derives the raster domain from the explicit mini-catchment file and
produces `mini_index.parquet` with exactly `mini_label`, `mini_id`, `minx`,
`miny`, `maxx`, and `maxy`. Terrain and sampling consume that same index by
direct path; terrain does not copy or republish it.

## Stage execution

ROI and aggregation use bounded vector packets and coordinator-side grouped
geometry publication. Aggregation first streams attributes without geometry,
finalizes topology, processing order, and dense IDs, then reads geometry in
bounded packets for dissolution. Preparation clips aligned sources and
rasterizes strict ownership/drainage in bounded parallel blocks, with deterministic ordered
reduction and coordinator-only connectivity correction. Terrain reads
direct COG windows for complete minis and assembles HAND, LTND, and optional
flow direction. Sampling derives block-aware packets and reduces exact mini
statistics into the single sampled CSV.

Scientific kernels remain responsible for their schemas, validation,
memory factors, checkpoint codecs, topology, ownership, and product rules;
shared infrastructure remains independent of those rules.
