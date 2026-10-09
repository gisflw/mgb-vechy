# Rust-native architecture

Implementation plan for a standalone Rust library and CLI. The goal is a fresh
architecture with simpler data flow, lower runtime overhead, and predictable
resource use. Python production code and management APIs may be replaced
outright; this isolated branch does not need a transition framework.

## What stays fixed

The [stage guides](../../README.md#implemented-workflow) and
[shared data contracts](../../docs/shared_data_contracts.md) define scientific
behavior and data products. Preserve topology, aggregation assignments,
canonical ownership, routing, numeric conventions, units, masks, filenames,
schemas, and meaningful ordering. Current scientific source and focused tests
are references; copied QGIS-era products are not the oracle.

CLI spelling, Python APIs, worker/process management, batching, caches, IPC,
publication/rollback machinery, and runtime exception types are replaceable.
Audit manifests retain their stage filenames and `step`/`parameters` envelope;
runtime-specific fields may change. Do not make Python cleanup, compatibility
wrappers, or dual-runtime operation prerequisites for Rust work.

Percentiles remain exact linear percentiles; maximum-LTND ties retain their
scientific tolerance. Geodesic measurements use the source CRS ellipsoid.
A polygon or line union, raster boundary decision, or flat/breach routing rule
is a scientific operation, even when a different library implements it.

## Small initial architecture

Start with one Cargo package exposing a library and a thin CLI. Use modules,
not a crate per stage or a general workflow framework:

| Module | Responsibility |
| --- | --- |
| `model` | Typed source/mini IDs, topology, grids, masks, and scientific parameters. |
| `science` | ROI, aggregation, ownership, terrain routing, and mini statistics. |
| `io` | Vector records, raster windows, metadata, geometry, and CRS operations. |
| `execution` | The small set of shared resource and temporary-storage helpers needed by implemented stages. |
| `cli` | Parse requests, call the library, and present results. |

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
The capture tool extracts raw basin-only vectors and aligned source raster
crops, runs the reference implementation with the three configured outlets,
and records regenerated expected products and performance. Candidate regression
then consumes only the local capture. Baseline refresh is an explicit
reference-capture operation, not part of candidate regression.

BHAE uses outlets `171984`, `420329`, `178658`, fields
`cotrecho`/`nutrjus`/`nustrahler`, and has 4,071 source units and 527 minis.
TDXHydro uses outlets `640538827`, `640543432`, `640538824`, region `610`,
fields `linkno`/`dslinkno`/`strmOrder`, and has 4,351 source units and 546
minis. In each case the listed order maps to `sub` 3, 2, 1, with later
outlets taking precedence in overlaps. Both cases use EPSG:4326, minimum upstream area 60 km², minimum
mini length 6 km, DEM scale 0.01, and AGREE 80/8/4.

The stage runner can exercise one stage using captured upstream products, or
all five stages using candidate upstream products. Its current command adapter
invokes the Python reference; adapt the command layer to the Rust CLI rather
than treating those flags as a Rust compatibility requirement.

Data comparisons cover exact schemas, IDs, discrete ownership, masks, required
metadata, and ordering where specified; compare vector geometry semantically.
Sampling rows may be matched by mini ID. CSV numeric comparisons use
`rtol=1e-10, atol=1e-10`; continuous raster comparisons use
`rtol=1e-6, atol=1e-6`, matching float32 products. Integer raster values and masks are
exact. Affine coordinates and embedded mini bounds allow `atol=1e-12`
coordinate units (`rtol=0`) for source-crop rounding; this is below one
Jacui pixel by more than eight orders of magnitude. These are initial fixture comparison tolerances, not permission to
change scientific branch decisions. Port synthetic flow-direction fixtures too:
the captured dataset products do not include a flow-direction raster.

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

1. Capture and verify both Jacui references and establish fixture-local
   performance measurements before changing production science.
2. Establish the Rust package, typed model, thin CLI, and the minimum I/O and
   resource helpers needed for a working stage.
3. Implement mini sampling first against captured upstream files. This covers
   windowed reads, exact percentiles, HRU percentages, geodesic flooded areas,
   tie behavior, and partial nodata without a new raster writer or dissolution.
4. Implement terrain against the prepared Jacui inputs and synthetic routing
   fixtures. Preserve directions, confinement, raw-DEM HAND, and geodesic LTND.
5. Implement ROI and aggregation with typed topology and geometry adapters.
   Preserve outlet precedence, evolving merges, representative IDs, and dense
   processing order. Use basin-only inputs for the routine performance loop.
6. Implement preparation with verified ownership/rasterization semantics.
   Exercise the full candidate pipeline on both networks once all stages exist.
7. Replace GIS adapters natively where doing so demonstrably simplifies or
   improves the implementation. Remove replaced Python production components
   without introducing a migration framework.

Each stage is ready when its scientific comparisons and focused edge cases
pass on both applicable Jacui inputs and its performance has been recorded.
The completed product is a native library/CLI with simpler execution and
preserved scientific products. The fixture suite survives implementation;
this temporary architecture document is removed once applied.
