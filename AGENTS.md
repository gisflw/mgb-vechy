# Repository guide

## Purpose and current capabilities

MGB-Vec-Hydro is a standalone library and command-line toolset for preparing
hydrography and model inputs for MGB workflows. It works with generic vector
networks using explicit segment and downstream IDs; a particular dataset's
naming or numbering conventions do not define the product.

Five capabilities are implemented: upstream region-of-interest selection,
mini-basin aggregation, aligned raster and mini-domain preparation, HAND and
local terrain-to-drainage products, and mini-basin attribute sampling from
terrain and existing HRU classes. HRU class construction and final MGB
simulation-file generation remain unimplemented.

## Repository map

- `src/`: library and command-line implementation.
- `tests/`: automated checks, scientific regression references, and benchmarks.
  `tests/regression/jacui/` holds the BHAE and TDXHydro dataset references.
- `docs/`: stable descriptions of implemented scientific behavior and data-file
  contracts.
- `.dev/`: development documents for work being applied. Every document here
  is intended to be removed once its implementation is complete.
  - `plan` is for future plans.
  - `pr` is for current implementation planning.
- `README.md`: project overview and entry point to the tool documentation.
- `../scratch/`: workspace datasets, scripts, experiments, and outputs used to
  manually test this project's implementations. `scratch/analysis` is reserved
  for broader manual testing when the tool is more mature.

## Working on the project

Read the relevant tool contract and the
[shared data contracts](docs/shared_data_contracts.md) before changing behavior.
Use the current scientific source and regression tests as references for
results, including units, topology, ownership, numerical conventions, and ties.
Implementation structure and management machinery are free to change.

Keep the computational product independent of desktop GIS applications.
Verify changes with checks appropriate to the affected behavior. Keep durable
documentation focused on what the tools do; keep active implementation plans
in `.dev/`. Distinguish implemented capabilities from remaining work.
