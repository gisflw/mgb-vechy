# Stage 4: terrain products

Implemented by `mgb::prepro::terrain` and `mgb prepro terrain-products`.

Current stage name: `terrain-products`.

Determine drainage routes confined to each mini and derive height above
matching drainage (HAND) and local terrain-to-drainage distance (LTND).
See the [shared data contracts](shared_data_contracts.md).

## Inputs and parameters

| Input or parameter | Meaning/default |
| --- | --- |
| `dem.tif` | Prepared DEM in metres; authoritative CRS and canonical grid. |
| `grid_catchments.tif` | Mini ownership with embedded `mini_index` bounds. |
| `grid_segments.tif` | Matching drainage IDs; positive IDs equal ownership. |
| Direction source | `dem` by default; alternatively an explicit canonical D8 raster. |
| D8 raster | Required in D8 mode; matches the canonical grid. |
| Optional flow-direction output | Disabled by default. |
| AGREE sharp incision | Additional drainage-cell lowering, default `80.0` m. |
| AGREE smooth depth | Ramp depth per pixel toward drainage, default `8.0` m. |
| AGREE buffer | Conditioning radius, default `4` pixels. |

AGREE depths must be finite and non-negative; the buffer must be a
non-negative integer. Parameters apply in DEM mode and are not automatically
rescaled by the preparation conversion factor.

## Usage

```bash
mgb prepro terrain-products --dem prepared/dem.tif \
  --grid-catchments prepared/grid_catchments.tif \
  --grid-segments prepared/grid_segments.tif --output-dir terrain
```

Use `--direction-source d8 --d8 prepared/d8.tif` for explicit directions and
`--write-flow-direction` to publish the selected direction raster. DEM mode
accepts `--agree-sharp`, `--agree-smooth`, and `--agree-buffer`.

The library exposes `TerrainSpec`, `DirectionSource`, `TerrainReport`, and
`create_terrain_dataset(&TerrainSpec)` through `mgb::prepro`.
`--workers` defaults to 4; `--memory-limit-mb` defaults to 4096. Actual
concurrency may be reduced so each admitted task can route one complete mini in
memory. A mini that cannot fit the managed working budget fails with its
estimated requirement. Dataset size does not require loading every mini at once;
raster inputs and completed output patches are handled with bounded buffers.
Routing always covers each complete mini, so output window edges never become
routing boundaries. GDAL/GEOS allocations and process overhead are outside the
managed working-memory estimate.

COGs use lossless ZSTD compression through the installed GDAL driver.

Products are staged and published after processing and COG validation;
publication failures restore replacements. Other stage files may share the
output directory. See [execution controls](execution.md).

## Scientific behavior

Matching drainage is `grid_segments == mini_id` within owned cells. Routing
stays inside each mini without buffering or crossing ownership boundaries.

In DEM mode, AGREE lowers finite cells within the same mini and within the
buffer's Euclidean pixel distance. At distance `d`, add
`smooth * (d - buffer)` to the raw elevation, and subtract `sharp` additionally
at drainage cells. Cells beyond the buffer retain their elevation.

Use steepest downhill D8 descent on the conditioned surface, accounting for
rectangular pixel dimensions. Equal slopes choose the destination with the
lowest row-major cell index. Natural downhill directions are retained;
flats prefer their lowest natural outlet, with deterministic neighbor ordering
N, NE, E, SE, S, SW, W, NW. Closed flats use the first row-major cell as their
initial terminal. Shallow-breach routes connect trapped basins to matching
drainage by minimizing, in order, maximum cut depth, cumulative excavation,
and route length; equal costs retain deterministic cell/basin ordering.
Scientific routing distances for these choices use raster pixel dimensions;
LTND's reported distances use geodesic measurements.

In D8 mode, matching drainage is terminal regardless of its input direction.
Every other drainage-connected owned cell must have a valid direction, remain
inside its mini, avoid cycles, and terminate at matching drainage.

HAND is raw DEM elevation minus raw DEM elevation at the selected matching
drainage terminal. Conditioning changes routing, not HAND's elevation source;
negative HAND values are retained. LTND is the sum of geodesic
cell-centre-to-parent steps along the selected route on the source CRS
ellipsoid, in metres. Drainage has HAND and LTND zero.

## Nodata policy

Every owned cell requires valid DEM and segment-grid data. Owned 8-connected
components without matching drainage are invalid in HAND and LTND; they remain
owned in the prepared grid and are counted in the undrained report. In D8
mode, valid direction coverage and path checks apply within components
connected to matching drainage. Output masks retain the resulting validity.

## Outputs

| Filename | Content |
| --- | --- |
| `hand.tif` | Canonical-grid `float32` HAND, with `units=m`. |
| `ltnd.tif` | Canonical-grid `float32` LTND, with `units=m` and `distance_method=geodesic`. |
| `undrained_cells.csv` | Counts and percentages for affected minis. |
| `flow_direction.tif` | Optional canonical-grid `uint8` selected directions: drainage 0, directions 1–8. |
| `manifest-terrain-products.json` | Input paths and processing parameters. |

Rasters are internally masked COGs. Dataset metadata includes `role`,
`routing_source`, and `ownership`, whose value is
`strict aggregated mini catchments; no buffer`. Roles are
`height above matching drainage`, `along-route distance to matching drainage`,
and `canonical clockwise D8 direction`, respectively. DEM-mode outputs also
record `agree_sharp`, `agree_smooth`, and `agree_buffer_pixels`. Flow-direction
metadata includes `direction_codes=0 drainage, 1-8 N NE E SE S SW W NW`.

The undrained CSV has the ordered columns `mini_id`, `undrained_cells`,
`total_cells`, `percentage_undrained`. Rows contain only affected minis in
ascending ID order; the file contains only its header if all cells drain.
The percentage is `100 * undrained_cells / total_cells`.

## Invalid inputs

Reject missing or inconsistent canonical grids, units, masks or mini metadata,
mismatched positive segment IDs, invalid AGREE parameters, missing D8 in D8
mode, and invalid drainage-connected D8 coverage, codes, paths, or cycles.
