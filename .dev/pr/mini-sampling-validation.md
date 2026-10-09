# Mini sampling validation

Validated on 2026-10-09 using the corrected Jacui capture.

## Checks

- Formatting, Clippy with warnings denied, and all-target tests pass.
- The opt-in sampling regression passes for BHAE (527 minis) and TDXHydro (546 minis).
- All 46 inventory entries pass existence, size, and checksum verification.
- Synthetic tests cover exact percentiles, LTND ties, disjoint validity, partial/total nodata, invalid inputs, strict integer `sub`, reach-specific length, worker determinism, output collisions/rollback, ownership, memory admission, and geodesic areas.
- Reference CSV tolerances remain `rtol=1e-10, atol=1e-10`; no comparison rules were relaxed.

## Release measurements

Three sequential runs per network, alternating networks, with 4 requested workers and a 4096 MiB application budget. Inputs were already accessed by prior regression runs; caches were not cleared. Each run used a fresh output directory and passed the scientific comparison. These are observations, not performance thresholds.

| Network | Run | Wall time (s) | Maximum process RSS (MiB) |
| --- | --- | ---: | ---: |
| bhae | 1 | 10.652 | 231.9 |
| bhae | 2 | 10.449 | 228.6 |
| bhae | 3 | 10.417 | 238.8 |
| bhae | Median | 10.449 | 231.9 |
| tdxhydro | 1 | 11.562 | 232.0 |
| tdxhydro | 2 | 11.562 | 233.7 |
| tdxhydro | 3 | 11.574 | 228.6 |
| tdxhydro | Median | 11.562 | 232.0 |

The Linux RSS measurement is the maximum individual-process RSS, not summed worker/process RSS. Rust uses threads in one candidate process. Historical timing records remain unchanged; their single observations do not establish a controlled speedup comparison.

Logs, decoded products, and raw `benchmark.json` records are in `tests/regression/jacui/runs/rust-sampling-final-<network>-<1..3>/` (gitignored).

Measured release executable SHA-256: `e11453b7bf827f100126eea68f82f1d1040893f744e1bc16e15b7890ad68eef1`.
Repository HEAD: `eebdbafb625c32373fb4a78d2e1c7fe63450a1e8`; measurements use the uncommitted sampling implementation, not that skeleton commit alone.
Platform: `Linux-7.0.0-38-generic-x86_64-with-glibc2.41`; GDAL 3.10.3, PROJ 9.6.0, GEOS 3.13.1; runner reports 20 available CPUs.