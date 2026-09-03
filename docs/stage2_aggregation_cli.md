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
`lmin` operates iteratively on evolving aggregated reach lengths and retains
stable ID tie-breaking.

The output directory is validated and published with one rename:

```text
minis/
├── mini_catchments.fgb
├── mini_segments.fgb
└── source_to_mini.csv
```

The FlatGeobuf products retain the normalized ROI schema and CRS.
`source_to_mini.csv` contains exactly `id`, `mini_id`, `sub`, `longitude`, and
`latitude`. Every ROI source ID occurs once. Coordinates come from each source
catchment's centroid calculated in the projected ROI CRS and then transformed
to EPSG:4326.

Execution defaults are 512 MB, four workers, two I/O operations, and 10,000-row
batches. Optional checkpoints survive failures/cancellation and are removed
only after successful validation and publication.
