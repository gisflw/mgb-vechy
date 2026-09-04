# Stage 2: aggregate mini-basins

`mgb-vec-hydro aggregate` consumes one versioned ROI directory. It accepts no
CRS or source-schema options.

```bash
mgb-vec-hydro aggregate \
  --roi roi \
  --uparea-min 30 \
  --lmin 6 \
  --output-dir minis
```

Segments with `upstream_area >= uparea_min` are eligible reaches. Every source
catchment remains in processing: below-threshold sources are mapped to an
eligible mini using the same-water-course rule, then the same-`sub` fallback.
After the area filter, eligible links are reconnected across filtered segments.
Maximal linear chains are collapsed wherever the downstream segment has exactly
one surviving upstream contributor and both segments share `sub` and
`water_course`. True surviving confluences remain boundaries. `lmin` then
operates iteratively on these evolving chain lengths and retains stable ID
tie-breaking. Below-threshold reach lengths never contribute to `lmin`.

The representative segment with the greatest upstream area (then unit length,
then string ID) supplies the mini ID. That same ID is used by the vector output,
the source mapping, and downstream mini references.

The output directory is validated and published with one rename:

```text
minis/
├── manifest.json
├── mini_catchments.fgb
├── mini_segments.fgb
└── source_to_mini.csv
```

The version-2 aggregation contract retains the normalized ROI schema and CRS.
Input is read through Pyogrio Arrow, while grouped polygon and line geometry is
dissolved by GDAL's SQLite `ST_Union` implementation and streamed to FlatGeobuf.
`source_to_mini.csv` contains exactly `id`, `mini_id`, `sub`, `longitude`, and
`latitude`. Every ROI source ID occurs once. Coordinates come from each source
catchment's centroid calculated in the projected ROI CRS and then transformed
to EPSG:4326.

Execution defaults are 512 MB, four workers, two I/O operations, and 10,000-row
batches. Optional checkpoints survive failures/cancellation and are removed
only after successful validation and publication.
On successful completion, the command also prints runtime timings for ROI input
loading, mini-basin aggregation, bounded geometry execution, output publication,
and total time.
