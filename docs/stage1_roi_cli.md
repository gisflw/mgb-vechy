# Stage 1 ROI CLI

`mgb-vec-hydro define-roi` selects catchment and segment features upstream of one or more outlet IDs.

The command expects prepared vector inputs. It does not clip to a DEM extent and it does not call QGIS.

## Generic Schema

```bash
mgb-vec-hydro define-roi \
  --catchments path/to/catchments.gpkg \
  --segments path/to/segments.gpkg \
  --outlet-id 123 \
  --outlet-id 456 \
  --id-col id \
  --id-down-col id_down \
  --strahler-order-col strahler_order \
  --destine-crs ESRI:102033 \
  --output-dir output \
  --output-format fgb
```

Supply repeated `--outlet-id` values in downstream-to-upstream order. The first outlet gets the highest `sub` value, and later upstream outlet selections overwrite overlapping `sub` assignments.

Outputs are normalized to exactly these columns:

- `id`
- `id_down`
- `sub`
- `strahler_order`
- `unit_length`
- `upstream_length`
- `unit_area`
- `upstream_area`
- `water_course`
- `geometry`

Both inputs must share the configured `--id-col`. The downstream topology column is read from the segment input and copied into both outputs.
The Strahler order column is required on the segment input and copied into both outputs as `strahler_order`.
Input column matching is case-insensitive, so `--id-col linkno` can match a source column named `LINKNO`.
The command computes geometry metrics after resolving input CRS and transforming to `--destine-crs`, which is also the output CRS. If `--source-crs` is supplied, it overrides the CRS metadata on both input layers. If it is omitted, both input layers must already declare a CRS.
After `upstream_area` is computed, `water_course` is derived independently inside each `sub`: at each confluence, the upstream branch with the greatest `upstream_area` continues the downstream course, with ties resolved by greater `unit_length` and then stable `id` string order. Other upstream branches start a new `water_course` from their own segment ID.

## Custom Schema

```bash
mgb-vec-hydro define-roi \
  --catchments data/areas.gpkg \
  --segments data/trecs.gpkg \
  --outlet-id 90497 \
  --outlet-id 416 \
  --outlet-id 159713 \
  --id-col cotrecho \
  --id-down-col nutrjus \
  --strahler-order-col ordem \
  --destine-crs ESRI:102033 \
  --output-dir output \
  --output-format gpkg
```

Outputs use legacy Stage 1 names:

- `roi_areas.<ext>`
- `roi_trecs.<ext>`

Supported formats are `fgb` and `gpkg`. New workflows should prefer `fgb`.
