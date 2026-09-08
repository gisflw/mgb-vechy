# Stage 5: sample mini-basin attributes

`mgb-vec-hydro sample-minis` consumes explicit mini vectors, the shared mini
index, prepared domain rasters, terrain products, the prepared DEM, and the
categorical HRU raster.

```bash
mgb-vec-hydro sample-minis \
  --mini-catchments minis/mini_catchments.fgb \
  --mini-segments minis/mini_segments.fgb \
  --mini-index prepared/mini_index.parquet \
  --dem prepared/dem.tif \
  --mini-ownership prepared/mini_ownership.tif \
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
| `--mini-index` | Required | Existing Parquet path | Shared six-column mini index whose IDs and labels must match the mini vectors. |
| `--dem` | Required | Existing raster path | Prepared DEM and authoritative canonical grid for sampling. |
| `--mini-ownership` | Required | Existing raster path | Dense ownership labels used to select catchment cells for each mini. |
| `--drainage` | Required | Existing raster path | Drainage labels used to select reach cells for each mini. |
| `--hand` | Required | Existing raster path | Terrain height-above-drainage raster used for reach and tributary statistics. |
| `--ltnd` | Required | Existing raster path | Local terrain-to-drainage distance raster used for tributary statistics. |
| `--hru` | Required | Existing raster path | Integer categorical HRU raster; sampled classes must be in `1..100`. |
| `--output-dir` | Required | Directory path | New directory where `sampled_minis.csv` is published. |
| `--workers` | Optional | Positive integer; default `4` | Number of worker processes used for bounded sampling packets. There is no upper limit imposed by the CLI or stage validator. |
| `--memory-limit-mb` | Optional | Positive integer MB; default `512` | Memory budget used to size bounded raster sampling packets. |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent raster reads. |
| `--batch-size` | Optional | Positive integer rows; default `10000` | Batch size used when reading vector metadata. |
| `--checkpoint-dir` | Optional | Directory path | Scratch location for resumable sampling packets; it must be outside `--output-dir`. |

Click also provides `--help` to display the command’s generated option list.

The DEM is authoritative for CRS and canonical grid. Both mini vectors must
declare that CRS, use the exact aggregation schema, and contain the same IDs
as the six-column `mini_index.parquet`. All six raster inputs must be
single-band COGs with the exact DEM grid, matching CRS, and internal masks.
The HRU raster must be integer-valued; sampled class IDs must be in `1..100`.
Missing or mismatched files, CRS, grid, masks, schemas, or IDs are rejected.

Sampling uses dense ownership labels rather than polygon masks. Catchment
statistics use cells owned by each mini; reach elevation uses matching
drainage cells. Exact percentiles and deterministic accumulators are reduced
over complete mini packets, while each distinct canonical COG block is read
once per raster in a packet. No raster is reprojected and no terrain or index
file is republished.

The output directory is staged privately and contains exactly one root-level
file:

```text
sampled/
└── sampled_minis.csv
```

Rows preserve the aggregation attributes (`id`, `id_down`, `sub`, `p_order`,
`unit_length`, `upstream_length`, `unit_area`, and `upstream_area`) without
geometry. The output includes longitude/latitude, reach slope, tributary length
and slope, and sorted `hru_<id>_pct` columns summing to 100% for every mini. Optional
`--checkpoint-dir` is operational scratch outside `--output-dir`; it is
removed after successful publication. The CLI prints the concrete CSV path.
Execution defaults are four workers, 512 MB of admitted task memory, two I/O
slots, and 10,000-row batches. Worker counts may be any positive integer.
