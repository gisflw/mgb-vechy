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
  --output-dir output \
  --output-format fgb
```

Supply repeated `--outlet-id` values in downstream-to-upstream order. The first outlet gets the highest `sub` value, and later upstream outlet selections overwrite overlapping `sub` assignments.

Outputs are normalized to exactly these columns:

- `id`
- `id_down`
- `sub`
- `geometry`

Both inputs must share the configured `--id-col`. The downstream topology column is read from the segment input and copied into both outputs.
Input column matching is case-insensitive, so `--id-col linkno` can match a source column named `LINKNO`.

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
  --output-dir output \
  --output-format shp
```

Outputs use legacy Stage 1 names:

- `roi_areas.<ext>`
- `roi_trecs.<ext>`

Supported formats are `fgb`, `gpkg`, and `shp`. New workflows should prefer `fgb` or `gpkg`; Shapefile is kept for compatibility checks.
