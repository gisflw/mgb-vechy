# Execution controls

All five Rust preprocessing commands accept `--overwrite`, `--workers` (default
4), `--memory-limit-mb` (default 4096), and `--io-slots` (default 2). ROI,
aggregation, and sampling also accept `--batch-size` (default 10000). Resource
settings must be positive. Stage-specific admission may reduce worker counts.

When outputs already exist, terminals ask `[y/N]`; Enter declines. Unattended
runs fail with instructions to pass `--overwrite`. Replacement stages and
validates products before publishing them, backs up affected entries, and
restores those entries on publication failure. Output symlinks are replaced as
entries; their targets are preserved. Directory collisions fail. Inputs and
unrelated files are protected. Temporary storage stays beside staged outputs.

Progress appears on stderr, refreshing about four times per second in terminals.
Redirected stderr receives plain phase messages. Successful runs print preparing,
processing, finalizing, and total elapsed seconds. Failed runs print elapsed time
before failure.

Each successful stage manifest includes a top-level `elapsed_seconds` value for
the total stage run. Reports also expose wall-clock phase timings for the CLI and
progress callbacks. Dataset-sized raster and vector inputs are processed with
bounded windows or complete-mini jobs; concurrent work is reduced to fit the
configured managed-memory budget. One mini that cannot fit its required working
memory fails with an estimated requirement. Configured GIS cache reservations
and bounded application buffers are included; GDAL/GEOS allocations and
operating-system overhead lie outside this estimate.

Existing library entry points retain their names. Progress-enabled variants add
`_with_progress` and accept a shared `ProgressCallback`, for example:

```rust,ignore
let report = mgb::prepro::create_terrain_dataset_with_progress(&spec, &|event| {
    eprintln!("{}: {} ({:.1}s)", event.operation, event.completed, event.elapsed_seconds);
})?;
```

Callbacks can run on worker threads and must be `Sync`. `StageProgress` carries
phase, operation, completed count, optional total, and elapsed seconds.
