# Stage 2: aggregate mini-basins

`mgb-vec-hydro aggregate` consumes the two explicit normalized ROI files. The
catchment file is authoritative for CRS; the segment file must declare the
same CRS and normalized schema.

```bash
mgb-vec-hydro aggregate \
  --roi-catchments roi/roi_catchments.fgb \
  --roi-segments roi/roi_segments.fgb \
  --uparea-min 30 \
  --lmin 6 \
  --output-dir minis
```

Segments with `upstream_area >= uparea_min` are eligible reaches. Every source
catchment remains in processing: below-threshold sources are mapped to an
eligible mini using the same-water-course rule, then the same-`sub` fallback.
After the area filter, eligible links are reconnected across filtered
segments. Maximal linear chains are collapsed wherever the downstream segment
has exactly one surviving upstream contributor and both segments share `sub`
and `water_course`. True surviving confluences remain boundaries. `lmin` then
operates iteratively on the evolving chain lengths with stable ID tie-breaking.

The representative segment with the greatest upstream area, then unit length,
then string ID supplies the mini ID. That ID is used by both vector outputs,
the source mapping, and downstream mini references. `source_to_mini.csv`
contains exactly `id`, `mini_id`, `sub`, `longitude`, and `latitude`; every ROI
source ID occurs once.

The published directory contains exactly these root-level files:

```text
minis/
├── mini_catchments.fgb
├── mini_segments.fgb
└── source_to_mini.csv
```

There is no `manifest.json` and no nested output directory. Both FlatGeobuf
files preserve the normalized ten-column schema and the authoritative CRS.
The files are staged privately, validated, and atomically published. Defaults
are 512 MB, four workers, two I/O operations, and 10,000-row batches.
`--checkpoint-dir` is optional scratch state outside `--output-dir` and is
removed after successful publication.
