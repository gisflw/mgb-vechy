# MGB-Vec-Hydro

MGB-Vec-Hydro prepares vector hydrography, mini-basins, raster terrain products,
and attributes for MGB model inputs. The product is a standalone library and
command-line toolset. Networks use explicit segment and downstream identifiers;
BHO is a regression dataset rather than a required source schema.

## Implemented workflow

| Stage | Capability | Main products |
| --- | --- | --- |
| 1. [Define ROI](docs/stage1_roi_cli.md) | Select the upstream union of one or more outlets and normalize hydrography in a chosen CRS. | ROI catchments and segments with topology and geodesic metrics. |
| 2. [Aggregate mini-basins](docs/stage2_aggregation_cli.md) | Group source units according to area, length, and network rules. | Mini catchments, mini reaches, and source-to-mini mapping. |
| 3. [Prepare raster data](docs/stage3_prepare_data.md) | Clip aligned inputs and establish raster ownership and matching drainage. | Prepared DEM, optional rasters, and mini-ID grids. |
| 4. [Terrain products](docs/stage4_terrain_cli.md) | Determine confined drainage routes and derive HAND and local terrain-to-drainage distance. | HAND, LTND, undrained-cell report, and optional flow directions. |
| 5. [Sample mini-basins](docs/stage5_mini_sampling_cli.md) | Summarize terrain, existing HRU classes, and flooded areas for each mini. | Geometry-free mini attribute CSV and missing-data reports. |

### Planned [remaining capabilities](.dev/plan/README.md).

### Shared [data contracts](docs/shared_data_contracts.md)