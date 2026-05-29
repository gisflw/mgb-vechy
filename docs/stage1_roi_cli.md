# Stage 1 ROI CLI

`mgb-vec-hydro define-roi` selects catchment and segment features upstream of one or more outlet segment IDs.

The command expects prepared vector inputs. It does not clip to a DEM extent and it does not call QGIS.

## Generic Schema

```bash
mgb-vec-hydro define-roi \
  --catchments path/to/catchments.gpkg \
  --segments path/to/segments.gpkg \
  --outlet-id 123 \
  --outlet-id 456 \
  --seg-id-col seg_id \
  --seg-id-down-col seg_id_down \
  --catch-id-col catch_id \
  --output-dir output \
  --output-format fgb
```

Supply repeated `--outlet-id` values in downstream-to-upstream order. The first outlet gets the highest `sub` value, and later upstream outlet selections overwrite overlapping `sub` assignments.

## BHO Schema

```bash
mgb-vec-hydro define-roi \
  --catchments data/geoft_bhae_area_drenagem.gpkg \
  --segments data/geoft_bhae_trecho_drenagem.gpkg \
  --outlet-id 90497 \
  --outlet-id 416 \
  --outlet-id 159713 \
  --seg-id-col cotrecho \
  --seg-id-down-col nutrjus \
  --catch-id-col cobacia \
  --output-dir output \
  --output-format shp
```

Outputs use legacy Stage 1 names:

- `roi_areas.<ext>`
- `roi_trecs.<ext>`

Supported formats are `fgb`, `gpkg`, and `shp`. New workflows should prefer `fgb` or `gpkg`; Shapefile is kept for compatibility checks.
