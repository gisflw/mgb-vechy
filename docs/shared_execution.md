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

Versioned ROI assets are indexed FlatGeobuf. Aggregation partitions them by
`water_course`; multiple complete courses may share a task when their combined
estimate remains within the configured budget. Ordered reduction and central
topology resolution keep serial and parallel output deterministic.

## Prepared raster access

`prepared_grid` reconstructs the canonical grid from the versioned manifest.
`plan_raster_units` maps complete-unit bounds to covering grid windows, derives
conservative byte estimates, and sorts units by a deterministic Morton block
key. `packet_raster_units` combines adjacent complete units up to byte and count
limits; it never splits a unit.

`PreparedRasterReader` verifies every named COG against the canonical CRS,
transform, shape, and band contract, then reuses its Rasterio handle inside the
worker. Reads require bounded integer windows.

`RasterAssembler` is coordinator-only. It merges valid cells from bounded
`RasterPatch` values into tiled working rasters, reading the existing mask to
reject duplicate cell ownership without a continent-wide ownership array. On
completion it creates one internally masked COG per `RasterProductSpec`.

Scientific work areas remain responsible for work payloads, memory factors,
checkpoint codecs, topology and ownership rules, and product schemas.
