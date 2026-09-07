# Stage 5: sample mini-basin attributes

`mgb-vec-hydro sample-minis` consumes the published aggregation, prepared,
and terrain datasets. It samples only the strict rasterized mini domain on the
canonical grid; it does not reproject rasters or rasterize vector geometry.

```bash
mgb-vec-hydro sample-minis \
  --minis output/minis \
  --prepared output/prepared \
  --terrain output/terrain \
  --hru-name hru \
  --output-dir output/sampled
```

The prepared dataset must contain the named categorical raster (default
`hru`). Its data type must be integer. Valid sampled values are class IDs in
the inclusive range `1..100`. Classes outside mini-owned cells are irrelevant
and are not scanned or added to the output schema.

The aggregation vectors, prepared mini index, and terrain mini index must
contain identical mini IDs. Prepared DEM and HRU COGs and terrain ownership,
drainage, HAND, and LTND COGs must share the exact canonical grid. Inputs with
incompatible contracts, grids, masks, or IDs are rejected.

## Sampling behavior

Sampling uses dense ownership labels rather than polygon masks:

- catchment statistics use cells owned by the mini label;
- reach elevation uses matching drainage cells owned by the mini;
- `reach_slope_m_per_km` is the exact DEM 85th percentile minus the exact
  10th percentile, divided by 75% of `unit_length`;
- `tributary_length_km` is maximum LTND converted from metres;
- `tributary_slope_m_per_km` is mean HAND at cells `isclose` to maximum
  LTND, divided by tributary length;
- `hru_<id>_pct` is the percentage of owned cells in each discovered class.

Catchment centroids are calculated while aggregation geometry is streamed in
bounded batches, then transformed from the projected working CRS to EPSG:4326.
The normalized aggregation attributes are preserved without geometry. Null
outlet links are allowed; other numeric output values must be finite.

Workers process spatial packets of complete minis. Every distinct 512-pixel COG
block is read once per raster within a packet and opened raster handles are
reused. Exact percentile values remain bounded by the complete-mini task.
A mini that cannot fit the configured memory budget is rejected rather than
approximated.

## Execution and output

Execution defaults to four workers, 512 MB, two I/O slots, 10,000-row vector
batches, and at most four workers. `--checkpoint-dir` enables resumable packet
results. Checkpoints are removed only after successful output publication.

The output directory is staged privately and published with one rename:

```text
sampled/
└── sampled_minis.csv
```

Rows follow deterministic spatial packet order. HRU columns are sorted by class
ID and sum to 100% for every row. The command reports mini and cell counts,
discovered classes, and timings for planning, raster reads, computation,
coordination, checkpointing, packet staging, CSV assembly, publication, and
total runtime.
