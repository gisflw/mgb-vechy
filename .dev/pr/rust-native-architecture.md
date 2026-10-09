# MGB Rust architecture

Implementation plan for the standalone `mgb` Rust library and CLI.
Preprocessing is its first module, `mgb::prepro`; commands start with
`mgb prepro <stage>`. The goal is a fresh architecture with simpler data flow,
lower runtime overhead, and predictable resource use. The Python implementation
has been removed from this branch. Its scientific source and tests remain
available at commit
`0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0`.

## Current state

The single Cargo package, CLI dispatcher, Rust devcontainer, and Jacui developer
tools are in place. Mini sampling is implemented in `prepro::sampling`, using
GDAL windowed I/O/CRS transforms, GEOS representative points, and native Rust
statistics and ellipsoidal cell areas. It passes both Jacui sampling comparisons.
Both captures include the explicitly authorized integer-`sub` schema correction
recorded in their inventory. [Sampling validation and measurements](mini-sampling-validation.md)
record the current results. Terrain is the next stage.

## What stays fixed

The [stage guides](../../README.md#frozen-preprocessing-workflow) and
[shared data contracts](../../docs/shared_data_contracts.md) define scientific
behavior and data products. Preserve topology, aggregation assignments,
canonical ownership, routing, numeric conventions, units, masks, filenames,
schemas, and meaningful ordering. Captured Jacui products, synthetic terrain
fixtures, and the scientific source/tests at the reference commit are the
references; copied QGIS-era products are not the oracle.

The `mgb prepro` namespace is fixed. Stage option spelling, historical Python
APIs, worker/process management, batching, caches, IPC,
publication/rollback machinery, and runtime exception types are replaceable.
Audit manifests retain their stage filenames and `step`/`parameters` envelope;
runtime-specific fields may change.

Percentiles remain exact linear percentiles; maximum-LTND ties retain their
scientific tolerance. Geodesic measurements use the source CRS ellipsoid.
A polygon or line union, raster boundary decision, or flat/breach routing rule
is a scientific operation, even when a different library implements it.

## Small initial architecture

Use one Cargo package named `mgb`, exposing library `mgb` and executable `mgb`.
The root CLI dispatches to preprocessing parsing under `prepro`. Use modules,
not a crate per stage or a general workflow framework:

| Module | Responsibility |
| --- | --- |
| `prepro::model` | Shared typed aggregation attributes, grids, and windows; add topology when needed. |
| `prepro::sampling` | Sampling inputs/report, validation, statistics, and stage coordination. |
| `prepro::io` | Vector records, raster windows, metadata, geometry, and CRS operations. |
| `prepro::execution` | Shared GDAL cache budget and safe temporary-output publication. |
| `cli` / `prepro::cli` | Dispatch modules / parse preprocessing requests and present results. |

Each scientific stage gets one file and module (`sampling.rs` now; terrain,
ROI, aggregation, and preparation when implemented), owning its science,
validation, parameters, and orchestration. Keep shared GIS and resource helpers
in `io` and `execution`.

Use dense internal indices for graph operations while retaining original IDs
and prescribed string-ID ties. Move owned typed buffers between threads;
avoid process serialization and dataframe conversion chains. Scientific
operations consume typed inputs; file handles and CLI parsing belong at the
boundary. Keep geometry and CRS operations explicit where external libraries
supply them.

One CPU pool and a coherent application-memory budget are enough initially.
Bound live raster windows and queued results, reuse buffers, and spill exact
samples or intermediate products when needed. Scientific reduction order must
be independent of completion order. Add shared abstractions when stages need
them; do not reproduce the Python executor's contracts as a Rust framework.

A dataset larger than RAM and a single oversized mini are different problems.
Use windowed raster access and complete-mini routing first. Report an
unsupported oversized unit clearly rather than splitting routing in a way
that changes connectivity. Paged graphs, external sorting, spill queues, and
external geometry algorithms belong in later work supported by measured need.
The initial Jacui cases establish behavior and performance, not universal
larger-than-memory coverage or a hard RSS ceiling.

## GIS backends

Implement hydrology, topology, and statistics natively. Use narrow GIS
adapters wherever mature libraries make the product simpler. Retaining PROJ
or selected GDAL/GEOS operations is compatible with native Rust execution;
removing every foreign library is not the first milestone.

Candidates from the initial survey include [FlatGeobuf](https://docs.rs/flatgeobuf/),
[GeographicLib Rust](https://docs.rs/geographiclib-rs/),
[Rust PROJ bindings](https://github.com/georust/proj),
[Rayon](https://docs.rs/rayon/), and native GeoTIFF/geometry libraries.
Confirm API suitability when implementing the relevant adapter. Prefer a
working, small adapter over a broad geospatial reimplementation.

Backend selection must respect the actual data contract: CRS/WKT and source
ellipsoids; internally masked COGs, grid transforms and required metadata;
ordered/unindexed mini vectors; and geometry/rasterization semantics. Native
replacements can follow after the scientific stages work. Compare decoded
results rather than requiring identical container bytes.

## Jacui regression and performance reference

`scratch/analysis` contains the existing basin comparison runs, source-data
locations, and expected products. It is the source of the
initial reference capture, and is reserved for the user's broader manual tests
when the tool is more mature. Do not make automated regression depend on its
scripts, run the multi-basin analysis during this work, or write candidate
outputs back into scratch.

Use only **Jacui / BHAE** and **Jacui / TDXHydro** for dataset regression and
performance work during this implementation. No HydroSHEDS or other basin
runs are required. Keep small synthetic scientific tests for ties, boundaries,
cycles, flats, and nodata cases the two real datasets do not isolate.

The working reference lives in
[tests/regression/jacui](../../tests/regression/jacui/README.md):

```text
tests/regression/jacui/
  config.json                 scientific settings and outlet/schema mapping
  inventory.json              captured file sizes and checksums
  input/                      shared source DEM/HRU crops and basin-only vectors
    bhae/                     raw catchments and segments
    tdxhydro/                  raw catchments and segments
  expected/bhae/              captured five-stage scientific products
  expected/tdxhydro/           captured five-stage scientific products
  benchmarks/                 bhae.json and tdxhydro.json timing records
  runs/                       disposable candidate products and measurements
```

Use the repository's existing `tests/` directory, not a new `test/` tree.
Carinhanha fixtures and their old regression tests are removed. Large binary
assets and the large sampled CSVs stay local and are gitignored; configuration,
provenance, checksums, diagnostics, and benchmark records are versioned.
The historical capture tool at the reference commit extracted raw basin-only
vectors and aligned source raster crops, ran the reference implementation with
the three configured outlets, and recorded expected products and performance.
Rust candidate regression consumes only this frozen local capture. Baseline
refresh requires an explicit historical reference checkout; the current Rust
tools do not regenerate baselines.

BHAE uses outlets `171984`, `420329`, `178658`, fields
`cotrecho`/`nutrjus`/`nustrahler`, and has 4,071 source units and 527 minis.
TDXHydro uses outlets `640538827`, `640543432`, `640538824`, region `610`,
fields `linkno`/`dslinkno`/`strmOrder`, and has 4,351 source units and 546
minis. In each case the listed order maps to `sub` 3, 2, 1, with later
outlets taking precedence in overlaps. Both cases use EPSG:4326, minimum upstream area 60 km², minimum
mini length 6 km, DEM scale 0.01, and AGREE 80/8/4.

The stage runner can exercise one stage using captured upstream products, or
all five stages using candidate upstream products. The Rust developer utility
invokes
`<executable prefix> prepro <stage>` using a small provisional option adapter.
Update that adapter with stage implementations; its flags are not a Rust
compatibility requirement.

Data comparisons cover exact schemas, IDs, discrete ownership, masks, required
metadata, and ordering where specified; compare vector geometry semantically.
Sampling rows may be matched by mini ID. CSV numeric comparisons use
`rtol=1e-10, atol=1e-10`; continuous raster comparisons use
`rtol=1e-6, atol=1e-6`, matching float32 products. Integer raster values and masks are
exact. Affine coordinates and embedded mini bounds allow `atol=1e-12`
coordinate units (`rtol=0`) for source-crop rounding; this is below one
Jacui pixel by more than eight orders of magnitude. These are initial fixture comparison tolerances, not permission to
change scientific branch decisions. The language-neutral synthetic terrain
fixtures preserve flow-direction examples too; captured dataset products do
not include a flow-direction raster.

The two benchmark records report timing observations for the current
three-outlet basin-only cases. Treat them as descriptive measurements, not
performance thresholds. The runner's `max_process_rss_kib` is the maximum
individual-process RSS reported by Linux wait4, including completed
descendants; it is not the sum of concurrent worker RSS.

Use release builds for Rust performance comparisons. Compare the same local
inputs, settings, machine, and cache conditions, with repeated runs and medians
for performance conclusions. Separate startup/JIT and warmed-reference effects.
Keep timing measurements out of scientific pass/fail assertions; there are no
machine-independent speed thresholds. Broader scaling and manual basin
comparisons come after the two Jacui implementations are useful.

## Implementation order

1. Both Jacui references and fixture-local baseline measurements are captured.
   Verify their inventory and keep them immutable during candidate work.
2. The Rust package and CLI are established, with shared records and the
   minimum I/O/resource helpers required by sampling.
3. Mini sampling is implemented against captured upstream files, covering
   windowed reads, exact percentiles, HRU percentages, geodesic flooded areas,
   ties, partial nodata, deterministic workers, and oversized-mini rejection.
4. Implement terrain against the prepared Jacui inputs and synthetic routing
   fixtures. Preserve directions, confinement, raw-DEM HAND, and geodesic LTND.
5. Implement ROI and aggregation with typed topology and geometry adapters.
   Preserve outlet precedence, evolving merges, representative IDs, and dense
   processing order. Use basin-only inputs for the routine performance loop.
6. Implement preparation with verified ownership/rasterization semantics.
   Exercise the full candidate pipeline on both networks once all stages exist.
7. Replace GIS adapters natively where doing so demonstrably simplifies or
   improves the implementation. Python production components are already
   removed; the historical source remains in Git for scientific reference.

Each stage is ready when its scientific comparisons and focused edge cases
pass on both applicable Jacui inputs and its performance has been recorded.
The completed product is a native library/CLI with simpler execution and
preserved scientific products. The fixture suite survives implementation;
this temporary architecture document is removed once applied.
