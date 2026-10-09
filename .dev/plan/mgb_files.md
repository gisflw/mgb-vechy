# MGB file generation

Status: unimplemented. This is an intended capability, outside the frozen
reference tool contracts.

## Purpose and inputs

Produce final MGB simulation inputs from mini vectors and the attributed
mini-basin table, including HRU percentages and flooded-area columns.
Scientific parameters include geomorphological relation coefficients `a`,
`b`, `c`, and `d`, slope limits `smin` and `smax`, and Manning coefficient
`nman`.

## Intended results

| Product | Meaning |
| --- | --- |
| `MINI.gtp` | Simulation mini attributes, ordering, and downstream references. |
| `COTA_AREA.flp` | Flood elevation-area relationships. |
| `minis_mgb.<ext>` | Mini-basin vectors carrying final simulation attributes. |

Final attributes include channel width and depth from geomorphological
relations and slopes limited by the configured bounds. Simulation ordering,
references, and text formatting must be consistent and deterministic.
Exact formulas, parameter defaults, text layouts, and the final vector format
remain to be specified; the filenames above describe intended products.

Terrain processing, HRU classification, and raster sampling are upstream
capabilities. This step consumes their results to produce simulation inputs.
