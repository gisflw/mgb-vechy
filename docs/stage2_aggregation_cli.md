# Stage 2 Aggregation CLI

`mgb-vec-hydro aggregate` builds MGB mini-basin catchments, reaches, and source-to-mini mapping from normalized ROI vector products.

The command expects ROI catchment and segment inputs with the exact input schema listed below. It does not require `cocursodag`; dominant paths are derived from topology and upstream area.

## Basic Usage

```bash
mgb-vec-hydro aggregate \
  --roi-areas output/roi_areas.fgb \
  --roi-trecs output/roi_trecs.fgb \
  --uparea-min 30 \
  --lmin 6 \
  --output-dir output \
  --output-format fgb
```

At confluences, the continuing path is the upstream segment with the greatest `upstream_area`. Ties are resolved by larger `unit_length`, then by the stable string form of `id`.

## Input Schema

Both `--roi-areas` and `--roi-trecs` must have exactly these columns, in this order:

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

Inputs with missing, extra, or reordered columns are rejected before topology or geometry work begins.

`id` and `water_course` may be numeric or string. The metric columns must be numeric, and `geometry` must be the active GeoPandas geometry column.

## Aggregation Parameters

`--uparea-min` selects candidate segments where `upstream_area > uparea_min`, grouped by `sub` and derived topology domain.

`--lmin` merges candidate chains shorter than the minimum length into an adjacent candidate group whose representative segment has the greatest `upstream_area`.

## Outputs

Outputs use legacy Stage 2 names:

- `mareas.<ext>`: aggregated mini-basin catchments
- `mtrecs.<ext>`: aggregated mini-basin reaches
- `bho2mini.<ext>`: original catchment-to-mini mapping

`mareas` and `mtrecs` use the same column order as the input schema. `bho2mini` contains:

- `id`
- `mini_id`
- `sub`
- `geometry`

Supported formats are `fgb` and `gpkg`. New workflows should prefer `fgb`.

## After ROI Definition

```bash
mgb-vec-hydro define-roi \
  --catchments data/areas.gpkg \
  --segments data/trecs.gpkg \
  --outlet-id 90497 \
  --outlet-id 416 \
  --outlet-id 159713 \
  --id-col cotrecho \
  --id-down-col nutrjus \
  --strahler-order-col nustrahler \
  --destine-crs ESRI:102033 \
  --output-dir output \
  --output-format fgb

mgb-vec-hydro aggregate \
  --roi-areas output/roi_areas.fgb \
  --roi-trecs output/roi_trecs.fgb \
  --uparea-min 30 \
  --lmin 6 \
  --output-dir output \
  --output-format fgb
```
