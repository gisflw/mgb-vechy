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

Rows preserve the normalized attributes without geometry. The output includes
longitude/latitude, reach slope, tributary length and slope, and sorted
`hru_<id>_pct` columns summing to 100% for every mini. Optional
`--checkpoint-dir` is operational scratch outside `--output-dir`; it is
removed after successful publication. The CLI prints the concrete CSV path.
