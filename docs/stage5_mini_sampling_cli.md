# Stage 5: sample mini-basin attributes

Frozen scientific reference for `mgb::prepro`; Rust implementation is pending.

Current stage name: `sample-minis`.

Summarize terrain and existing HRU classes into one geometry-free attribute
row per mini. See the [shared data contracts](shared_data_contracts.md).

## Inputs

Mini catchments and segments use the [aggregation schema](stage2_aggregation_cli.md#outputs)
and contain the same IDs as the prepared `mini_index`. Their attributes agree
except that segment `unit_length` is independently used for reach slope.
Both vectors declare the DEM CRS.

The six raster inputs are DEM, catchment IDs, segment IDs, HAND, LTND, and HRU.
All are single-band COGs matching the DEM's canonical grid, CRS, and internal
masks. DEM, HAND, and LTND declare `units=m`. HRU values are integer classes
in `1..100`; no new HRU classes are constructed by this stage.

## Scientific behavior

Use ownership IDs to select catchment cells. Reach cells have matching segment
IDs (`grid_segments == mini_id`). Positive segment IDs must match ownership.
Longitude and latitude are a representative point on the mini segment,
transformed to EPSG:4326.

| Attribute | Definition | Unit |
| --- | --- | --- |
| `reach_elevation` | Median valid DEM elevation on matching reach cells. | m |
| `reach_slope` | `(P85 - P10) / (0.75 * segment unit_length)` for those reach elevations. | m/km |
| `tributary_length` | Maximum usable LTND divided by 1000. | km |
| `tributary_slope` | Mean HAND of cells tied for maximum usable LTND, divided by tributary length. | m/km |
| `hru_<id>` | `100 * valid class-cell count / valid HRU-cell count`. | percent |
| `flooded_area_<stage>` | Sum of geodesic cell areas with valid HAND at or below the stage. | km² |

Percentiles are exact with linear interpolation: position `(n - 1) * p` in
sorted samples, interpolating between adjacent values. Tributary statistics
use only paired valid HAND/LTND cells. Maximum-LTND ties satisfy
`abs(value - maximum) <= 1e-8 + 1e-5 * abs(maximum)`.

Flood stages are integer metres from 1 through 100, inclusive. Negative HAND
contributes at every stage; valid HAND above 100 contributes at none. Cell
areas come from geographic pixel corners on the source CRS ellipsoid.
Vector `unit_area` is retained as an attribute and does not scale flooded area.
HRU percentages use valid HRU cells as their denominator and sum to 100%.

## Outputs

| Filename | Content |
| --- | --- |
| `sampled_minis.csv` | One row per mini, without geometry. |
| `nodata_<raster>.csv` | Missing-cell report for each affected input raster. |
| `manifest-sample-minis.json` | Input paths and processing parameters. |

The sampled CSV has these ordered column groups:

1. Aggregation attributes: `id`, `id_down`, `sub`, `p_order`, `unit_length`,
   `upstream_length`, `unit_area`, `upstream_area`.
2. `longitude`, `latitude`, `reach_slope`, `reach_elevation`,
   `tributary_length`, `tributary_slope`.
3. `hru_<id>` columns for classes present across the sampled domain, ordered
   by ascending numeric class ID; absent classes in a mini receive zero.
4. `flooded_area_1` through `flooded_area_100`, in ascending stage order.

Aggregation attributes retain their values and km/km² units. Coordinates
are degrees; sampled elevation, tributary length, slopes, and flooded areas
use the units specified above. The sampled table has deterministic row order;
ascending mini-ID order is not an established contract.

## Nodata policy and invalid inputs

Exclude cells outside ownership entirely. Within each mini, masked cells and
NaNs are excluded from the corresponding DEM, HAND, LTND, HRU, or segment-grid
statistics. Infinities and invalid HRU classes fail.

Report partial missing coverage with one warning per run identifying the
affected raster inputs. Each `nodata_<raster>.csv` has ordered columns
`mini_id`, `nodata_cells`, `total_cells`, `percentage_nodata`. Include only
affected minis, in ascending mini-ID order, with
`percentage_nodata = 100 * nodata_cells / total_cells`. Raster names are
`dem`, `grid_segments`, `hand`, `ltnd`, and `hru`.

A mini fails if it has no valid HRU cells, valid HAND cells, valid DEM reach
cells, or paired HAND/LTND cells, or if maximum usable LTND is not positive.
Reports are retained when a completed scan fails for missing required
statistics. Also reject inconsistent vector schemas/attributes, mini IDs,
CRS, grids, masks, units, positive segment ownership, or zero/invalid reach
lengths.
