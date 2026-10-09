# Stage 1: define ROI

Frozen scientific reference for `mgb::prepro`; Rust implementation is pending.

Current stage name: `define-roi`.

Select the union of the network upstream of one or more outlets, normalize
catchments and segments, and establish the output CRS for vector stages.
See the [shared data contracts](shared_data_contracts.md).

## Inputs and parameters

| Input or parameter | Meaning |
| --- | --- |
| Catchment polygons and network segments | GeoPackage, FlatGeobuf, or ESRI FileGDB, with a layer selected where needed. |
| ID field | Source identifier shared by catchments and segments. |
| Downstream-ID field | Segment field expressing network connections; null downstream IDs represent sinks. |
| Strahler-order field | Segment field used to filter the source network. |
| Ordered outlet IDs | One or more source segment IDs whose upstream union defines the ROI. |
| Target CRS | Explicit geographic or projected CRS for both output vectors. |
| Source-CRS overrides | Optional metadata overrides for each source. |

Required field names resolve by exact match first, then an unambiguous
case-insensitive match. Identifiers are interpreted in the source ID type.

## Scientific behavior

- Remove rows with null, non-finite, or below-one Strahler order before outlet
  selection. Selected Strahler orders must be integral.
- Select outlets and all upstream contributors using explicit topology.
  Outlet IDs must exist after filtering. Selected non-outlet segments must
  connect toward a selected outlet, and the selected topology must be acyclic.
- For `K` ordered outlets, assign `sub = K - outlet_index`, using zero-based
  outlet indices. Where upstream domains overlap, the later outlet's `sub`
  takes precedence. The selected union retains each source unit once.
- Calculate geodesic segment lengths in km and catchment areas in km² on the
  source CRS ellipsoid. Upstream metrics include the current unit and its
  upstream contributors.
- Within each `sub`, continue a water course through the upstream branch with
  greatest upstream area, then greatest unit length, then greatest string ID.
  Other branches start their own water courses. `water_course` is the source
  ID identifying the resulting course.
- Transform selected geometries to the explicit target CRS. Catchment and
  segment attributes share the source unit's topology and metrics.

## Outputs

| Filename | Content |
| --- | --- |
| `roi_catchments.fgb` | Spatially indexed normalized catchment polygons. |
| `roi_segments.fgb` | Spatially indexed normalized network lines. |
| `manifest-define-roi.json` | Input paths and processing parameters. |

Both vector files use this ordered schema:

`id`, `id_down`, `sub`, `strahler_order`, `unit_length`, `upstream_length`,
`unit_area`, `upstream_area`, `water_course`, `geometry`.

Source IDs and downstream references are preserved; `id`, `id_down`, and
`water_course` use the source ID type. `sub` and `strahler_order` are `int64`;
length and area attributes are `float64`. Lengths are km and areas are km²,
independent of output CRS coordinate units. Spatial-index ordering does not
define scientific processing order.

## Invalid inputs

Reject missing or ambiguous required fields, unusable CRS metadata, missing
outlets, an empty filtered network, duplicate retained segment IDs, duplicate
selected catchment IDs, missing selected source pairs, selected cycles,
non-integral selected Strahler orders, and invalid, empty, null, or wrongly
typed selected polygon/line geometries. Catchment duplicates outside the
selected IDs do not affect the ROI.
