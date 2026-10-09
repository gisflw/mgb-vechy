# Shared data contracts

These contracts and the stage guides freeze the historical preprocessing
behavior for the `mgb::prepro` implementation.
They are the stable behavioral and data-file reference across implementation
changes. The scientific source/tests at reference commit
`0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0` resolve details; runtime mechanisms and
language-specific interfaces are not part of this reference.

## Explicit inputs and CRS

Stages consume explicit paths to the data they need. Audit manifests record
provenance; they are not required to discover downstream inputs.

ROI selection accepts GeoPackage, FlatGeobuf, and ESRI FileGDB sources, with
layer selection for containers. Source-CRS overrides can supply or replace
metadata. Its explicit target CRS becomes the vector workflow CRS; both
geographic and projected CRSs are supported.

Aggregation and preparation use their catchment vector as the authoritative
CRS. Terrain and sampling use the DEM as the authoritative raster grid and CRS.
Other inputs must declare matching CRS metadata. Geodesic measurements use
the source CRS ellipsoid; projected coordinate units are not a substitute for
geodesic lengths or areas.

## Vector identifiers and geometry

Networks have explicit segment and downstream IDs. ROI preserves source IDs;
aggregation assigns dense integer mini IDs `1..N`, with mouths encoded as `-1`.
Prepared grids store those mini IDs directly. Positive segment-grid IDs must
match catchment ownership at the same cell.

Catchments are polygons or multipolygons; reaches are lines or multilines.
Aggregation represents the geometric union of assigned source catchments and
surviving reach members. Line union is a geometric union, not concatenation.
ROI FlatGeobuf files are spatially indexed. Mini FlatGeobuf files are
unindexed, preserving the specified processing order.

## Canonical raster grid

Preparation retains the DEM resolution and pixel orientation, clipping to the
combined mini polygon/segment domain plus a one-cell halo. Source rasters must
already be aligned and cover this extent; preparation does not implicitly
reproject or resample them. Raster stages require north-up, unrotated grids.

Prepared and terrain rasters are single-band Cloud Optimized GeoTIFFs (COGs)
with 512-pixel tiles and internal per-dataset validity masks, with no numeric
nodata sentinel. Terrain and sampling inputs must match the canonical CRS,
affine transform, dimensions, and internal-mask contract exactly. Continuous
prepared values and HAND/LTND are `float32`; categorical prepared values and
mini grids are `int32`; D8 and flow-direction values are `uint8`.

`grid_catchments.tif` includes the dataset tag `mini_index`, a JSON array of
records `[mini_id, minx, miny, maxx, maxy]`, ordered by mini ID. Bounds are tight
pixel-edge rectangles in the raster CRS, enclosing final ownership after
segment overlays. Terrain and sampling use this metadata; no index sidecar
is an output.

Canonical direction codes are 0 for a terminal and 1–8 for N, NE, E, SE, S,
SW, W, and NW. Terrain terminal cells are matching drainage cells. Preparation
also accepts ESRI codes and normalizes them to this convention.

## Units and validity

| Quantity | Unit |
| --- | --- |
| Vector unit/upstream length; sampled tributary length | km |
| Vector unit/upstream area; sampled flooded area | km² |
| Prepared DEM; HAND; LTND; sampled reach elevation | m |
| Sampled reach and tributary slope | m/km |
| Longitude and latitude | degrees in EPSG:4326 |
| HRU percentages; diagnostic percentages | percent |

Prepared DEM, HAND, and LTND declare `units=m` in dataset metadata and band
units. DEM also records `dem_scale`; its stored values already include that
conversion. LTND declares `distance_method=geodesic`.

Preparation preserves source validity within final mini ownership and masks
cells outside it. Non-finite continuous source values become invalid. The two
ID grids have identical ownership masks; zero is valid segment background.
Terrain requires valid DEM and segment-grid data for owned cells and masks
components without matching drainage. Sampling excludes missing cells from
the relevant statistics and denominators, reports affected minis, and fails
when required statistics have no usable data. Stage guides define the
remaining stage-specific rules.

## Output files and audit records

Each stage produces its documented root-level files, without nested product
directories. Multiple stages may use the same output folder.

| Stage identifier | Audit filename |
| --- | --- |
| `define-roi` | `manifest-define-roi.json` |
| `aggregate` | `manifest-aggregate.json` |
| `prepare` | `manifest-prepare.json` |
| `terrain-products` | `manifest-terrain-products.json` |
| `sample-minis` | `manifest-sample-minis.json` |

Each manifest is a JSON object containing `step` (the stage identifier) and
`parameters` (an object recording input paths and processing parameters).
Paths are recorded as absolute paths. Runtime-specific parameter fields are
not frozen. Manifest serialization, overwrite behavior, and publication
mechanisms do not define the scientific products.
