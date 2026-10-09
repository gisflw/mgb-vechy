# Stage 2: aggregate mini-basins

Implemented by `mgb::prepro::aggregation::aggregate_roi_dataset` and
`mgb prepro aggregate`, following the frozen scientific reference.

Current stage name: `aggregate`.

Group normalized ROI units into mini catchments and reaches, with a complete
source-to-mini mapping and deterministic mini topology. See the
[shared data contracts](shared_data_contracts.md).

## Inputs and parameters

| Input or parameter | Meaning |
| --- | --- |
| `roi_catchments.fgb` | Normalized catchments; authoritative CRS and source areas. |
| `roi_segments.fgb` | Normalized network; matching CRS and source IDs, reach lengths, and topology. |
| `uparea_min` | Required non-negative minimum eligible upstream area, in km². |
| `lmin` | Required non-negative minimum evolving mini reach length, in km. |

Both inputs use the [ROI schema](stage1_roi_cli.md#outputs). IDs must be unique
and match between catchments and segments.

## Scientific behavior

- Reaches with `upstream_area >= uparea_min` are eligible. Eligible topology
  reconnects across excluded segments. Maximal linear chains collapse when
  the downstream eligible segment has one surviving upstream contributor and
  both share `sub` and `water_course`. Surviving confluences are boundaries.
- A group's representative is the member with greatest upstream area, then
  greatest unit length, then greatest string ID.
- Merge short groups within their shared `sub`/`water_course` domain using
  evolving group lengths. The first short group by ascending string ID joins
  its adjacent group with smallest length, then smallest string ID. Reconsider
  lengths and neighbors after each merge. Groups still below `lmin` without a
  merge target cease to supply reaches.
- Assign excluded sources and removed short-group catchments to surviving
  minis connected through unassigned sources in the same `sub`/`water_course`;
  then use the same-`sub` fallback. Choose the candidate with smallest evolving
  length, then smallest string ID. Every ROI catchment must have one target.
- Mini catchment geometry is the union of assigned catchments; mini reach
  geometry is the union of surviving reach members. Reach length is the sum
  of member lengths; catchment area is the sum of assigned source areas.
  Upstream length and area retain their representative source metrics.
- On final mini topology, head minis have `p_order = 1`; a downstream mini has
  one plus the greatest order of its direct upstream minis.
- Sort minis by ascending `sub`, `p_order`, and `upstream_area`, then ascending
  representative string ID. Assign dense IDs `1..N` in that order, remap
  downstream references, and encode mouths as `-1`.

## Outputs

| Filename | Content |
| --- | --- |
| `mini_catchments.fgb` | Unindexed mini polygons in final processing order. |
| `mini_segments.fgb` | Unindexed mini reaches in the same order. |
| `source_to_mini.csv` | Every ROI source ID mapped exactly once. |
| `manifest-aggregate.json` | Input paths and processing parameters. |

Both vector files have the ordered schema:

`id`, `id_down`, `sub`, `p_order`, `unit_length`, `upstream_length`,
`unit_area`, `upstream_area`, `geometry`.

They retain the authoritative CRS and km/km² metric units. `strahler_order`
and `water_course` guide aggregation but are not output columns.
`id`, `id_down`, `sub`, and `p_order` are `int64`; length and area attributes
are `float64`.

The mapping CSV has exactly `id`, `mini_id`, `sub`, `longitude`, `latitude`,
in that order. Rows are sorted by source ID interpreted as a string.
Coordinates are source-catchment centroids transformed to EPSG:4326;
`mini_id` is the final dense ID.

## Invalid inputs

Reject missing required attributes, mismatched CRS or source IDs, duplicate
IDs, invalid numeric attributes or geometries, cycles, and negative thresholds.
Fail when no reach satisfies the area threshold or any catchment has no
surviving aggregation target in its `sub`.

## Rust usage

```bash
mgb prepro aggregate --roi-catchments output/roi_catchments.fgb \
  --roi-segments output/roi_segments.fgb --uparea-min 60 --lmin 6 \
  --output-dir output
```

Both thresholds are required. `--workers` and `--memory-limit-mb` default to
4 and 4096. Topology decisions are sequential; complete geometric unions run
in bounded threads. Topology and source WKB stay resident. Inputs or individual
union groups that exceed conservative allocation estimates fail explicitly;
the application budget is not a hard RSS ceiling. Existing stage products are
never overwritten, and failed publication rolls back new files.
