# Terrain validation and performance

Validated on 2026-10-09 against the frozen, corrected Jacui capture.

## Scientific and integration checks

- All 26 synthetic cases execute the Rust kernels. Directions and ranks match
  exactly; HAND and LTND meet the captured numeric tolerances, including native
  ellipsoids, geographic grads, and projected coordinate units.
- Both Jacui terrain comparisons pass for every measured run: BHAE has 527
  minis and 50 undrained cells; TDXHydro has 546 minis and 40 undrained cells.
- Sampling with candidate HAND/LTND and captured remaining inputs passes for
  both networks, including the sampled CSV and nodata diagnostics.
- Focused tests cover canonical grids, dtypes, units, masks, malformed indexes,
  ownership, finite DEM coverage, D8 codes/coverage/cycles/domain exits,
  disconnected components, negative HAND, optional directions, worker
  determinism, memory admission/waits/cancellation, collisions, and rollback.
  A multiple-tile overlap test exceeds the GDAL cache to exercise eviction.
- Formatting, Clippy with warnings denied, and all-target tests pass. Explicit
  terrain regression and all 46 inventory checks pass. Captures, historical
  benchmarks, and comparison tolerances remain unchanged.

## Release measurements

Three sequential measurements per network, alternating BHAE and TDXHydro,
with four requested workers and a 4096 MiB application budget. Every run reports
peak admission of four mini workers and passes the decoded scientific
comparison. Earlier runs warmed inputs; caches were not cleared. Downstream
sampling checks ran outside the timed terrain invocations after each network's
first measurement. Each run used a fresh candidate output directory.

| Network | Run | Wall time (s) | Maximum process RSS (MiB) |
| --- | --- | ---: | ---: |
| bhae | 1 | 15.711 | 446.1 |
| bhae | 2 | 15.654 | 391.3 |
| bhae | 3 | 15.774 | 446.6 |
| bhae | Median | 15.711 | 446.1 |
| tdxhydro | 1 | 15.845 | 409.0 |
| tdxhydro | 2 | 15.656 | 409.4 |
| tdxhydro | 3 | 15.746 | 387.9 |
| tdxhydro | Median | 15.746 | 409.0 |

The medians meet the PR's provisional local historical targets: 17.19 seconds
for BHAE and 16.92 seconds for TDXHydro. Historical records are single
observations from a different native-library environment; no controlled
historical rerun or portable speedup claim is implied. Linux RSS is the maximum
individual-process RSS, not summed worker RSS. The application budget is not a
hard RSS ceiling; oversized routing windows still fail explicitly.

Products, logs, and raw measurements are in
`tests/regression/jacui/runs/rust-terrain-accepted-<network>-<1..3>/`.
Downstream sampling products are in
`tests/regression/jacui/runs/rust-terrain-accepted-sampling-<network>/`.
These paths are gitignored.

## Changes that removed the bottlenecks

The first implementation took approximately 90 seconds and used only one
worker. Largest-window reservation for every worker prevented concurrency,
geodesic batches repeated row-equivalent distances, and compressed staging
repeatedly encoded overwritten TIFF tiles.

The accepted implementation admits each complete mini against its own budget,
uses standard-library hash lookup followed by deterministic edge ordering,
computes three geodesic lengths per geographic row, and checks DEM coverage
once during mini processing. Ownership scanning reuses the previous ID lookup.
Sparse, uncompressed disk staging merges only owned cells. One byte validity
raster becomes the internal masks in a final tile pass, avoiding incremental
bit-mask read/write problems across overlapping windows. Final COG compression
uses lossless ZSTD level 1 and the requested, admitted worker-pool size after
routing has completed. No dependencies or general executor framework were
added.

Temporary profiling code was removed before the measured release build.

Executable SHA-256: `ad4838284d5cfb1b3ffdfdcf7afdfd98042e1f5e1b47dbb71068ceb81bc4c038`.
Repository HEAD: `281db014cd48ddb7954fd186695755fa71cf5bbb`; measurements use the
uncommitted terrain implementation, not that sampling commit alone.
Rust/Cargo 1.99.0; Linux x86_64; GDAL 3.10.3, PROJ 9.6.0, GEOS 3.13.1;
runner reports 20 available CPUs.
