# Larger-than-memory processing

## Status

Implementation in progress. Work areas 1 through 4 are implemented. Raster
preparation now follows aggregation: ROI defines the working CRS, and prepare
validates and clips already aligned source rasters while producing the bounded
mini ownership/drainage inputs used by terrain. Only work area 5 remains.

## Compatibility policy

This work is a complete break from the current processing model. The new
implementation will replace the existing behavior rather than coexist with it.

There will be no fallback to the old commands, input conventions, processing
paths, buffering behavior, output layout, or internal APIs. Compatibility
wrappers and parallel legacy implementations are explicitly out of scope.
Existing tests and documentation will be updated to describe the new contracts,
not used to preserve obsolete behavior.

The implementation may be delivered iteratively, but every completed stage must
use the new architecture exclusively.

## Objective

Support continental processing at 30 metre raster resolution with bounded
memory use, deterministic results, and parallel execution. Raster processing
will continue to use Rasterio and GDAL, with one COG per raster dataset. Vector
data will use compact, indexed formats and restricted schemas.

The design separates scientific processing from execution mechanics. Processing
kernels operate on bounded in-memory inputs, while shared execution layers plan
work, enforce resource limits, load data, coordinate workers, and publish
outputs.

## Architectural principles

- ROI defines the authoritative working CRS; the DEM establishes the prepared
  raster grid after aggregation.
- Stage 1 is the sole raw-vector ingestion boundary; preparation reads only
  published ROI/aggregation products and source rasters.
- Catchment ownership is not expanded beyond the domain rasterization. There
  is no ownership buffer.
- Vector and raster workflows use domain-specific work units rather than a
  universal rectangular chunk abstraction.
- Vector aggregation is partitioned by water course.
- Terrain processing is partitioned by complete unit catchment.
- Parallel execution is bounded by memory and I/O capacity.
- Workers do not concurrently modify final output datasets.
- Outputs are deterministic and published only after successful completion.
- Shared infrastructure remains independent of the scientific details of each
  processing stage.

## Work areas

### 1. ROI and post-aggregation prepared data

ROI selects vectors and defines the working projected CRS. After aggregation,
preparation converts the already-aligned source rasters and aggregated mini
domain into the canonical inputs consumed by terrain.

The prepared dataset establishes:

- A DEM-derived resolution, transform, clipped extent, and nodata convention in
  the ROI CRS; it never reprojects or resamples source rasters.
- Tiled COG raster inputs using a block layout suitable for window access.
- Optional D8 data on exactly the same grid, with an explicit direction encoding.
- Rasterized strict mini ownership, matching drainage, and a dense mini index.

Vector inspection, schema mapping, Strahler filtering, topology selection, and
selected-geometry reprojection belong to Stage 1. Unit lengths and areas are
provider fields in km and km²; only upstream length is derived. COG is the
prepared raster interface.

### 2. Shared vector and raster execution layers

Develop common execution infrastructure before refactoring the processing
stages. The infrastructure will provide resource-aware planning and execution
without embedding ROI, aggregation, terrain, or sampling rules in the executor.

The shared capabilities will include:

- Work planning from prepared metadata and spatial indexes.
- Bounded parallel execution with configurable worker and memory limits.
- Reuse of opened data sources within workers.
- Backpressure so queued and completed work cannot exhaust memory.
- Progress, diagnostics, timing, cancellation, and failure reporting.
- Deterministic result collection and reduction.
- Staged output creation and atomic publication.
- Support for resumable processing where an output can be safely partitioned.

The vector execution layer will support bounded feature reads and partitioned
geometry operations. Basin aggregation will use `water_course` as its primary
chunk unit.

The raster execution layer will use complete unit catchments as terrain work
units. It will use prepared indexes to obtain the necessary raster windows and
will schedule spatially related units in a way that balances memory bounds with
COG block reuse. Raster output will be coordinated through a single controlled
writer or an equivalent mechanism that guarantees exclusive ownership of output
blocks.

The first executor may use local process-based parallelism. The interfaces
should leave room for another scheduler later, but introducing multiple
execution backends is not part of the initial work.

### 3. Refactor vector ROI selection and basin aggregation

ROI selection and aggregation use the shared vector execution and publication
contracts. ROI takes the raster manifest plus raw GeoPackage/FlatGeobuf inputs;
aggregation takes only the versioned ROI directory.

ROI selection will separate topology from geometry:

- Compact topology attributes may be held in memory when appropriate.
- Upstream selection will not require loading complete source geometry.
- Selected geometry and derived attributes will be processed incrementally.
- Traversal will support continental networks without recursion limits.

Aggregation will be reorganized around water-course work units:

- Group assignment and topology calculations will avoid repeated whole-table
  reconstruction and scanning.
- Geometry aggregation will run on bounded partitions and may execute in
  parallel.
- Cross-water-course output relationships will be resolved deterministically
  after partition-local work.
- Large intermediate GeoDataFrames will not be required.

The published mapping is geometry-free `source_to_mini.csv`; it includes every
selected source, including sources below `uparea_min`, with projected-centroid
coordinates transformed to EPSG:4326.

### 4. Refactor terrain processing

Terrain processing depends on the shared raster execution layer and prepared
rasters. Aggregated minis are rasterized during preparation; `source_to_mini.csv`
and mini vectors are not Stage 4 inputs.

Before terrain calculations, preparation rasterizes mini catchments and matching
mini segments on the DEM grid. This post-aggregation pass produces strict
ownership and drainage COGs through the shared execution layer and does not
expand ownership.

Each terrain task processes one complete mini catchment, or a bounded packet of
complete minis, from local COG windows. Catchment and drainage geometry is not
rasterized during terrain execution. A deterministic dense-label Parquet index
preserves arbitrary mini ID types.

The terrain stage supports two explicit direction sources behind one processing
contract:

- Direction derived from the DEM using the terrain-conditioning workflow.
- A prepared D8 raster that passes grid, encoding, confinement, termination, and
  cycle validation.

Both paths produce the same versioned terrain-dataset interface. Shared
traversal order, cached COG handles, spatial packets, and compact array dtypes
reduce memory and I/O.

Results are assembled into one final COG per terrain product without concurrent
worker writes. Domain and terrain checkpoints remain resumable until the
validated directory is atomically published.

### 5. Refactor mini-basin sampling

Refactor mini-basin sampling to depend on the shared raster execution layer and
the post-aggregation rasterized domain and terrain products.

Sampling will avoid repeated arbitrary polygon-window reads where blockwise or
unit-based reductions are possible. Unit-catchment labels and the aggregation
mini index will associate raster cells with mini basins. Per-partition
statistics will use deterministic, mergeable accumulators where the statistic
permits it.

Statistics that cannot be reduced directly, including exact quantiles, will use
a bounded strategy chosen during implementation. Any change from exact to
approximate statistics would require an explicit new product definition; it
will not be introduced implicitly as an optimization.

The sampling stage will consume terrain and categorical rasters from the
canonical grid and will not perform implicit reprojection or alignment.

## Dependencies and sequencing

The expected dependency order is:

1. Define and implement the pre-conditioned input-data contract.
2. Implement the shared vector and raster execution layers.
3. Refactor ROI selection and aggregation using the vector execution layer.
4. Rasterize the resulting domain and refactor terrain processing using the
   raster execution layer.
5. Refactor mini-basin sampling using the raster execution layer, mini index,
   and terrain products.

Work areas 3, 4, and 5 must not introduce independent chunking, scheduling,
worker-management, or output-publication implementations. Missing capabilities
must be added to the shared execution layers instead.

## Validation and performance gates

Each work area will add tests appropriate to its new contract. At minimum, the
completed system must demonstrate:

- Bounded peak memory as input extent grows.
- Deterministic serial and parallel results.
- Exact grid alignment across all prepared and derived rasters.
- Strict catchment ownership with no implicit buffering.
- Clear rejection of incompatible or incomplete prepared inputs.
- Recovery without publishing partial final products.
- Measured scaling across increasing ROI, feature, and raster sizes.
- Separate reporting of input reads, rasterization, computation, coordination,
  compression, and output writes.

The workspace includes integration-test data under `scratch`. The
first validation round will use the parameters recorded in the
`scratch/bhae/stage_*.sh` scripts and evaluate the newly generated products
against the reference data in `scratch/bhae/expected_output`. These comparisons
will cover each implemented stage before testing broader extents and parallel
scaling. Any intentional difference introduced by the new processing contract
must be identified and accepted explicitly; it must not be hidden behind a
fallback to the previous implementation.

The BHAE gates are 791 source pairs, 267 minis, ROI at or below 26.2 seconds,
aggregation at or below 15.8 seconds and 374 MB peak RSS, and a warm-cache
terrain target at or below 60 seconds. Defaults are 512 MB, 10,000-row scans,
no more than four workers, and two I/O slots.

## Completion condition

The change is complete when all downstream stages consume only the new prepared
data and shared execution contracts, continental jobs remain within configured
memory limits, and the obsolete eager and buffered processing paths have been
removed from the codebase, tests, and documentation.
