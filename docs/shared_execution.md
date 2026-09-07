# Shared vector and raster execution

The shared execution layer provides internal infrastructure for bounded vector
and raster processing. It is not a CLI and is not exported from the package
root. Processing stages import the contracts from
`mgb_vec_hydro.execution.executor`, `mgb_vec_hydro.execution.checkpoints`,
`mgb_vec_hydro.execution.vector`, `mgb_vec_hydro.execution.raster`, and
`mgb_vec_hydro.execution.publication`.

## Local execution

`LocalExecutor` starts persistent worker processes with Python's `spawn`
context. Worker callables and payloads must therefore be pickleable, and worker
callables should be module-level functions. Applications invoking it from a
script must use the normal `if __name__ == "__main__"` guard.

Every `WorkItem` has a unique stable text key, a contiguous zero-based ordinal,
and an estimated peak byte cost. The executor admits an item only when the sum
of live estimates remains within `ExecutionConfig.memory_limit_bytes`. The
limit covers declared task and result payloads, not interpreter or imported
library overhead. A task larger than the complete budget is rejected.

Task and result queues are bounded by `max_in_flight`, which defaults to the
worker count. Results may finish in any order but are reduced in ordinal order.
The executor retains admission for an out-of-order result until it is reduced,
so completed values cannot form an unbounded queue behind a slow task.

`WorkerContext` provides:

- A process-local LRU cache for context-managed data sources.
- `io_bound()`, backed by a semaphore shared by all workers. Vector and raster
  readers use it automatically.

Progress callbacks execute in the coordinator. Reports contain work counts,
peak admitted bytes, planning, coordination, checkpoint, reduction and worker
timings, ordered worker diagnostics, cancellation state, and remote failure
details. Cancellation or failure terminates workers and closes their cached
resources.

## Checkpoints and publication

`CheckpointStore` is opt-in. A job supplies a fingerprint produced by
`execution_fingerprint`; it covers the algorithm and version, relevant prepared
manifest, parameters, and ordered work descriptors. Each result is serialized
by a stage-provided `CheckpointCodec`. The coordinator writes and hashes the
artifact before atomically creating its completion marker.

On restart, compatible completed results are loaded and reduced in their
original order. Missing, changed, or corrupt state is rejected. Checkpoints are
retained after failure, cancellation, and successful execution. The stage calls
`CheckpointStore.cleanup()` only after its atomic output publication succeeds.

`AtomicOutputDirectory` creates a private sibling staging directory. A caller
builds every final product there, validates its expected files, and publishes
the directory with one rename. Destinations that already exist are rejected.

## Raw-provider and ROI vector access

Stage 1 inspects GeoPackage and FlatGeobuf schemas and CRS without loading
geometry. Topology attributes are streamed through Arrow. Selected IDs use
safely quoted provider predicates when practical; otherwise one geometry-free
ID/FID scan is followed by bounded random-FID reads. Exact IDs, duplicates,
geometry types, and packet estimates are checked before results are admitted.

Versioned ROI assets are indexed FlatGeobuf. Vector packets use Arrow-native WKB
with explicit CRS metadata; checkpoint artifacts use Arrow IPC. Ordered
reduction and central topology resolution keep output deterministic, and GDAL's
SQLite engine performs the final grouped geometry union without GeoDataFrame
materialization.

## Prepared raster access

`prepared_grid` reconstructs the canonical grid from the versioned manifest.
`plan_raster_units` maps complete-unit bounds to covering grid windows, derives
conservative byte estimates, and sorts units by a deterministic Morton block
key. `packet_raster_units` combines adjacent complete units up to byte and count
limits; it never splits a unit.

`PreparedRasterReader` verifies every named COG against the canonical CRS,
transform, shape, and band contract, then reuses its Rasterio handle inside the
worker. `AlignedRasterReader` provides the same cached access for derived COGs
that have already been tied to a canonical grid. Reads require bounded integer
windows.

`RasterAssembler` is coordinator-only. It merges valid cells from bounded
`RasterPatch` values into tiled working rasters, reading the existing mask to
reject duplicate cell ownership without a continent-wide ownership array. Its
exclusive initial-assembly path accepts each canonical block exactly once and
writes its data and mask without a read-modify-write cycle; duplicate blocks and
mixing later non-exclusive writes back into that path are rejected. Bounded
replacement writes support corrections staged before mutation. On the first
patch it creates the mask lazily, leaving untouched blocks invalid instead of
initializing the complete grid. On completion it creates one internally masked
COG per `RasterProductSpec`, with a bounded number of GDAL compression threads.

Stage 4 first rasterizes complete aggregated minis into ownership and matching
drainage COGs. A second bounded execution pass reads those products with the
prepared DEM or D8 COG; terrain workers never receive vector geometry and only
the coordinator assembles final products. Both passes use independent,
compatible checkpoints. Stage 4 also caps GDAL's otherwise machine-relative
block cache in the coordinator and each worker; task admission estimates still
exclude fixed Python and imported-library process overhead.

Scientific work areas remain responsible for work payloads, memory factors,
checkpoint codecs, topology and ownership rules, and product schemas.
