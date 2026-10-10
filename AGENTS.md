# Repository guide

## Purpose and current capabilities

MGB is a standalone Rust library and command-line toolset for preparing
hydrography and model inputs for MGB workflows. It works with generic vector
networks using explicit segment and downstream IDs; a particular dataset's
naming or numbering conventions do not define the product.

The package and executable are `mgb`. Preprocessing is the first public module,
`mgb::prepro`; scientific CLI commands start with `mgb prepro <stage>`.
All five preprocessing stages and Jacui regression tools are implemented.
HRU construction and final simulation-file generation remain future capabilities.
Keep each scientific stage in its own module;
do not collect stages in a shared `science` module.

## Repository map

- `src/`: root library/CLI and preprocessing modules under `src/prepro/`.
- `examples/jacui.rs`: developer entry point for fixture verification,
  candidate runs, and comparisons.
- `tests/`: automated checks and scientific regression references.
  `tests/regression/jacui/` holds the BHAE and TDXHydro dataset references.
- `docs/`: stable descriptions of frozen scientific behavior and data-file
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
Use captured Jacui products, synthetic terrain fixtures, and the scientific
source/tests at reference commit `0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0` for
results, including units, topology, ownership, numerical conventions, and ties.
Implementation structure and management machinery are free to change.

Keep the computational product independent of desktop GIS applications.
Verify changes with checks appropriate to the affected behavior. Keep durable
documentation focused on what the tools do; keep active implementation plans
in `.dev/`. Distinguish implemented capabilities from remaining work.

Run `cargo fmt --all -- --check`, `cargo clippy --all-targets --locked -- -D warnings`,
and `cargo test --all-targets --locked`. Scientific candidate regression is opt-in;
missing local fixtures must fail explicit runs. Never write candidate outputs to
scratch, captured inputs, or expected products during candidate runs. Refresh
expected products only for explicit corrections; stage manifests record their
input/output checksums and runtime.
