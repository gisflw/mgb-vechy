# Stage 5: sample mini-basin attributes

`mgb-vec-hydro sample-minis` consumes explicit mini vectors, prepared domain
rasters with embedded mini bounds, terrain products, the prepared DEM, and the
categorical HRU raster.

```bash
mgb-vec-hydro sample-minis \
  --mini-catchments minis/mini_catchments.fgb \
  --mini-segments minis/mini_segments.fgb \
  --dem prepared/dem.tif \
  --cells prepared/cells.tif \
  --drainage prepared/drainage.tif \
  --hand terrain/hand.tif \
  --ltnd terrain/ltnd.tif \
  --hru prepared/hru.tif \
  --output-dir sampled
```

## Options

| Option | Status | Type/default | Meaning |
| --- | --- | --- | --- |
| `--mini-catchments` | Required | Existing vector path | Aggregated mini-catchment polygons and normalized mini attributes. |
| `--mini-segments` | Required | Existing vector path | Aggregated mini-segment lines and normalized reach attributes. |
| `--dem` | Required | Existing raster path | Prepared DEM and authoritative canonical grid for sampling. |
| `--cells` | Required | Existing raster path | Dense integer mini IDs and embedded bounds used to select catchment cells. |
| `--drainage` | Required | Existing raster path | Drainage mask used to select reach cells within each mini. |
| `--hand` | Required | Existing raster path | Terrain height-above-drainage raster used for reach and tributary statistics. |
| `--ltnd` | Required | Existing raster path | Local terrain-to-drainage distance raster used for tributary statistics. |
| `--hru` | Required | Existing raster path | Integer categorical HRU raster; sampled classes must be in `1..100`. |
| `--output-dir` | Required | Directory path | Directory where `sampled_minis.csv` and any nodata reports are published. |
| `--workers` | Optional | Positive integer; default `4` | Number of worker processes used for bounded sampling packets. There is no upper limit imposed by the CLI or stage validator. |
| `--memory-limit-mb` | Optional | Positive integer MB; default `512` | Soft memory sizing hint for sampling packets and retained statistics. |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent raster reads. |
| `--batch-size` | Optional | Positive integer rows; default `10000` | Batch size used when reading vector metadata. |

Click also provides `--help` to display the command’s generated option list.

The DEM is authoritative for CRS and canonical grid. Both mini vectors must
declare that CRS, use the exact aggregation schema, and contain the same IDs
as the mini IDs stored in `cells.tif`. All six raster inputs must be
single-band COGs with the exact DEM grid, matching CRS, and internal masks.
The HRU raster must be integer-valued; sampled class IDs must be in `1..100`.
CRS, grid, masks, and mini IDs must match. Missing fields and unreadable
inputs fail directly in the underlying libraries.

DEM, HAND, and LTND must declare `units=m` for their stored values.
In particular, degree-valued LTND cannot be accurately converted
with one scale factor after route directions have been discarded. For a DEM
stored in centimetres, run preparation with `--dem-scale 0.01`, then regenerate
terrain and sampling into new directories.

Sampling uses mini IDs in cells rather than polygon masks. Catchment
statistics use cells owned by each mini; reach elevation uses matching
drainage cells. Longitude and latitude use a representative point on each mini
segment. Exact percentiles and deterministic accumulators are reduced
over complete mini packets, while each distinct canonical COG block is read
once per raster in a packet. No raster is reprojected or republished.

The output directory is staged privately and contains the CSV and an audit
manifest with the input paths and processing parameters:

```text
sampled/
├── manifest-sample-minis.json
└── sampled_minis.csv
```

The output also contains `nodata_<raster>.csv` for each affected raster.

Rows preserve the aggregation attributes (`id`, `id_down`, `sub`, `p_order`,
`unit_length`, `upstream_length`, `unit_area`, and `upstream_area`) without
geometry. The output includes longitude/latitude, `reach_slope`,
`reach_elevation`, `tributary_length`, and `tributary_slope`; sorted `hru_<id>`
percentage columns summing to 100%; and `flooded_area_<stage>` columns for
stages 1 through 100, in that order. Column names omit units; lengths and
elevations are metres, slopes are metres per kilometre, and areas are km².
The CLI prints the concrete CSV path.
Execution defaults are four workers, 512 MB as a soft memory hint, two I/O
slots, and 10,000-row batches. Worker counts may be any positive integer.

Reach elevation is the median DEM elevation of cells labeled for each
mini and marked as drainage, in metres. Reach slope is the difference between
the 85th and 10th percentiles of those same reach elevations (metres), divided
by `0.75 * unit_length` (kilometres). Each flooded-area column is the cumulative
fraction of valid HAND cells in each mini at or below its stage in metres,
multiplied by the vector catchment area; negative HAND is included at every
stage. Stage 1 computes
`unit_length` geodesically and aggregation preserves those kilometre metrics.
Tributary length is maximum LTND divided by 1000; tributary slope is
mean HAND at cells tied for that maximum divided by tributary length. Tributary
statistics use only cells where both HAND and LTND are valid.

## Nodata policy

The ownership mask defines each mini's domain; masked cells outside ownership
are excluded. Within a mini, masked cells and NaNs in DEM, HAND, LTND, HRU, and
drainage are excluded from the corresponding statistics. Sampling emits one
warning per run listing only affected flags, such as `--hand` and `--hru`. For
each affected raster, it writes a `nodata_<raster>.csv` with columns `mini_id`,
`nodata_cells`, `total_cells`, and `percentage_nodata`; rows include only
affected minis and are ordered by mini ID. Reports are saved when a completed
scan fails because a required statistic has no valid data. Infinities and
invalid HRU classes still fail. A mini fails sampling if it has no valid HRU
cells, HAND cells, DEM reach cells, or paired HAND/LTND cells, or if its
maximum usable LTND is not positive.

HRU percentages divide each class count by the mini's valid HRU-cell count, so
the emitted class percentages sum to 100%. Flooded-area columns remain HAND
thresholds in metres. Each column multiplies the fraction of valid HAND cells
at or below its threshold by vector `unit_area`; missing HAND cells are omitted
from the fraction's denominator, while values above 100 metres remain in it.
Therefore, when all valid HAND cells are flooded, the area equals `unit_area`.
This count-based calculation avoids per-cell geodesic area work. For pipeline
behavior beyond sampling, see the [shared raster nodata policy](shared_execution.md#nodata-policy).

Measured preparation, terrain, and sampling costs and reproduction commands
are in [the unit-correction performance report](sampling_units_performance.md).

`--memory-limit-mb` is a soft sizing hint for task packets and retained
intermediates, without separate quotas. Workers and a small queue bound
concurrency; library caches have explicit sizes. Actual RSS can exceed the
hint. See [shared memory sizing](shared_execution.md#local-execution).
