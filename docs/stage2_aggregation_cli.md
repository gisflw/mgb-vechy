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

## Options

| Option | Status | Type/default | Meaning |
| --- | --- | --- | --- |
| `--roi-catchments` | Required | Existing FlatGeobuf path | Normalized Stage 1 catchment file used as the authoritative CRS and source-unit input. |
| `--roi-segments` | Required | Existing FlatGeobuf path | Normalized Stage 1 segment file containing topology and reach attributes. |
| `--uparea-min` | Required | Non-negative number | Minimum `upstream_area` threshold for a segment to be eligible as a mini-basin reach; uses normalized area units (km²). |
| `--lmin` | Required | Non-negative number | Minimum evolving mini length used when short chains are iteratively merged; uses normalized length units (km). |
| `--output-dir` | Required | Directory path | New directory where the mini vectors and `source_to_mini.csv` are published. |
| `--workers` | Optional | Positive integer; default `4` | Number of worker processes used for bounded vector work. There is no upper limit imposed by the CLI or stage validator. |
| `--memory-limit-mb` | Optional | Positive integer MB; default `512` | Memory budget used to size bounded processing packets. |
| `--io-slots` | Optional | Positive integer; default `2` | Maximum number of concurrent vector-I/O operations. |
| `--batch-size` | Optional | Positive integer rows; default `10000` | Number of rows processed per bounded vector batch. |
| `--checkpoint-dir` | Optional | Directory path | Scratch location for resumable aggregation work; it must be outside `--output-dir`. |

Click also provides `--help` to display the command’s generated option list.

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
are 512 MB, four workers, two I/O operations, and 10,000-row batches. Worker
counts may be any positive integer.
`--checkpoint-dir` is optional scratch state outside `--output-dir` and is
removed after successful publication.
