# MGB-Vec-Hydro

MGB-Vec-Hydro prepares vector hydrography, mini-basins, raster terrain products,
and attributes for MGB model inputs. The product is a standalone library and
command-line toolset. Networks use explicit segment and downstream identifiers;
BHO is a regression dataset rather than a required source schema.

## Implemented workflow

| Stage | Capability | Main products |
| --- | --- | --- |
| [1. Define ROI](docs/stage1_roi_cli.md) | Select the upstream union of one or more outlets and normalize hydrography in a chosen CRS. | ROI catchments and segments with topology and geodesic metrics. |
| [2. Aggregate mini-basins](docs/stage2_aggregation_cli.md) | Group source units according to area, length, and network rules. | Mini catchments, mini reaches, and source-to-mini mapping. |
| [3. Prepare raster data](docs/stage3_prepare_data.md) | Clip aligned inputs and establish raster ownership and matching drainage. | Prepared DEM, optional rasters, and mini-ID grids. |
| [4. Terrain products](docs/stage4_terrain_cli.md) | Determine confined drainage routes and derive HAND and local terrain-to-drainage distance. | HAND, LTND, undrained-cell report, and optional flow directions. |
| [5. Sample mini-basins](docs/stage5_mini_sampling_cli.md) | Summarize terrain, existing HRU classes, and flooded areas for each mini. | Geometry-free mini attribute CSV and missing-data reports. |

Each stage consumes explicit inputs and produces flat data files. The stages
can share an output folder. HRU class construction and final simulation-file
generation are [remaining capabilities](docs/plan/README.md).

## Behavioral reference

The stage guides and [shared data contracts](docs/shared_data_contracts.md)
are the stable reference for implemented capabilities, scientific rules, and
output file contracts. These contracts stay fixed when implementation changes.
Current source and scientific tests provide the reference where details need
verification. A scientific-contract change is separate from an architectural
change and should be documented as such.

The guides describe inputs, scientific parameters, results, and data meanings.
Current command names identify the stages; they do not freeze CLI spelling,
Python APIs, or execution machinery. Audit manifests retain their filenames
and `step`/`parameters` envelope; runtime-specific parameter fields may evolve.
Implementation documentation is deferred.

Contributor guidance is in [AGENTS.md](AGENTS.md). `.dev/` holds temporary
development documents for work being applied; they are removed once their
implementation is complete. Workspace datasets and experiments live in the
sibling `scratch/` folder.
