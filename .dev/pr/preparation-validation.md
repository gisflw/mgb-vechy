# Raster preparation and full-pipeline validation

Validated on 2026-10-09 against the frozen, corrected Jacui capture.

## Scientific and integration checks

- Standalone preparation against captured mini vectors passes for BHAE and
  TDXHydro. DEM, HRU, ownership, and drainage match the frozen decoded products,
  including grid transforms, masks, types, metadata, and tight mini bounds.
- All six final full-pipeline runs pass every product comparison. Each stage
  consumes candidate upstream products, starting from the captured source
  vectors and DEM/HRU inputs. BHAE retains 4,071 source units and 527 minis;
  TDXHydro retains 4,351 source units and 546 minis. HAND/LTND, sampled attributes,
  source mapping, undrained diagnostics, and nodata reports pass unchanged.
- Four preparation kernel tests and ten preparation integration tests cover
  shared boundaries, sole-interior precedence, true overlaps, holes, downstream
  ancestry and non-contending connectors, pixel/block junctions, dense IDs,
  invalid topology/geometries/CRS/grids/coverage, masks, scaling and overflow,
  categorical conversion, both D8 encodings, stream-only ownership, disconnected
  terrain, tight bounds, parallel determinism, reduced memory admission,
  publication collisions, cleanup, and repeated CLI raster options.
- The shared raster helpers retain terrain's existing GDAL overview default.
  A focused interoperability test checks that default and explicit resampling.
  Preparation uses bilinear continuous overviews and nearest categorical/D8/ID
  overviews, preserving the reference choices.
- Formatting, Clippy with warnings denied, and all-target tests pass: 70 tests
  pass; the opt-in dataset test remains ignored in the ordinary suite. The
  explicit developer-tool comparisons above exercise both real networks.
- All 46 fixture inventory entries verify after the final runs. Captures,
  historical benchmarks, scratch, and scientific comparison tolerances remain
  unchanged.

## Release measurements

Three sequential full-pipeline measurements per network, alternating BHAE and
TDXHydro, with four requested workers and a 4096 MiB application budget. Every
run uses a fresh output directory and is scientifically compared afterward.
Earlier development runs and comparisons warmed inputs; caches were not
cleared. Final standalone preparation checks and inventory verification ran
after the timed pipeline sequence.

`workers_used` is four for every stage in every final measurement. Preparation
and terrain report peak processing admission; ROI, aggregation, and sampling
report admitted worker threads. Linux RSS is the maximum individual-process
RSS, not summed concurrent-worker RSS. Total times below sum the five measured
stage invocations, excluding comparisons.

| Network | Stage | Run 1 (s) | Run 2 (s) | Run 3 (s) | Median (s) | Historical (s) | Median RSS (MiB) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| bhae | define-roi | 1.096 | 0.988 | 1.015 | 1.015 | 3.248 | 360.9 |
| bhae | aggregate | 1.453 | 1.393 | 1.403 | 1.403 | 4.848 | 223.2 |
| bhae | prepare | 9.245 | 9.283 | 9.170 | 9.245 | 20.577 | 623.7 |
| bhae | terrain-products | 16.401 | 16.442 | 16.479 | 16.442 | 17.191 | 458.1 |
| bhae | sample-minis | 10.779 | 10.840 | 10.821 | 10.821 | 13.132 | 260.0 |
| tdxhydro | define-roi | 1.187 | 1.210 | 1.187 | 1.187 | 3.793 | 416.4 |
| tdxhydro | aggregate | 4.234 | 4.265 | 4.246 | 4.246 | 7.310 | 266.8 |
| tdxhydro | prepare | 9.975 | 9.907 | 9.966 | 9.966 | 21.425 | 704.0 |
| tdxhydro | terrain-products | 16.626 | 16.283 | 16.497 | 16.497 | 16.918 | 412.9 |
| tdxhydro | sample-minis | 11.989 | 12.076 | 11.991 | 11.991 | 13.225 | 252.7 |

| Network | Total run 1 (s) | Total run 2 (s) | Total run 3 (s) | Total median (s) | Historical total (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| bhae | 38.975 | 38.946 | 38.888 | 38.946 | 58.997 |
| tdxhydro | 44.011 | 43.741 | 43.888 | 43.888 | 62.671 |

Every per-stage median and total median meets the PR's provisional local
historical target. Historical records are single observations from a different
native-library environment; these results do not establish a controlled
historical speedup or a universal runtime guarantee.

Final standalone preparation checks against captured upstream products:

| Network | Wall time (s) | Workers used | Maximum process RSS (MiB) |
| --- | ---: | ---: | ---: |
| bhae | 9.216 | 4 | 635.8 |
| tdxhydro | 9.908 | 4 | 707.2 |

Raw products, logs, and complete measurements are in the gitignored
`tests/regression/jacui/runs/rust-preparation-final-pipeline-<network>-<1..3>/`
and `rust-preparation-final-standalone-<network>/` directories. Earlier
`rust-prepare-dev-*`, `rust-pipeline-dev-*`, and
`rust-preparation-accepted-pipeline-*` runs are development evidence; the final
measurements above use the corrected helper and executable identified below.

## Implementation limits and provenance

Vectors remain resident. Conservative allocation estimates account for
geometry, worker scratch, queued results, collision workspace, and GDAL cache;
concurrency is reduced or unsupported work fails. The allocation budget is not
a hard RSS ceiling. Workers own GIS handles and use 512-pixel blocks. The
coordinator writes sparse staging rasters and converts each once to an
internally masked COG. Existing products are protected by collision-safe
publication. No dependency, general executor framework, or overwrite option
was added.

Executable SHA-256: `8b00287cd497884ea745db4d1ee5a864cb9b9ef874a01ef2618b99f305cf71ce`.
Repository HEAD: `fce9eca69846daa1416a04937989ec59e6125bc1`; measurements include the uncommitted
preparation implementation and shared-helper/concurrency-report changes.
Rust/Cargo 1.99.0; Linux x86_64; GDAL 3.10.3, PROJ 9.6.0, GEOS 3.13.1;
the runner reports 20 available CPUs.
