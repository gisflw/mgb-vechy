# Repository guidance

## Project purpose

MGB-Vec-Hydro is a standalone Python library and command-line interface for
preparing vector hydrography inputs for MGB workflows. Production code lives in
`src/mgb_vec_hydro`; the target product is the reusable package and CLI.

The computational core must remain independent of QGIS and desktop GIS APIs.
Do not import `qgis`, `processing`, `PyQt5`, `QApplication`, or QGIS
project/layer/task APIs, and do not add QGIS integration to this repository.

## Architecture and compatibility

- Operate on generic vector networks with explicit segment ID and downstream ID
  columns. BHO is the first regression target, not the product boundary.
- Never replace topology traversal with BHO-specific ordering or prefix rules
  such as `cobacia >= ...` or `cocursodag.startswith(...)`.
- Treat null downstream IDs as sinks, detect missing columns before processing,
  and reject cycles rather than allowing unbounded traversal.
- Keep CRS handling explicit. Spatial operations must use an appropriate
  projected CRS, and inputs with absent or incompatible CRS metadata must be
  rejected unless the relevant interface explicitly supports an override.
- Prefer FlatGeobuf for indexed vector workflows and GeoPackage when a
  container is useful. Do not introduce Shapefile as a new workflow format.
- Use `pathlib.Path`; do not change the process working directory, assume a
  platform-specific path separator, or write temporary products into the
  caller's current directory.
- Publish multi-file outputs only after successful completion. Preserve the
  existing staging, validation, and atomic-publication behavior when changing
  a stage.
- Validate public inputs early and raise exceptions from
  `mgb_vec_hydro.exceptions` instead of exposing low-level GDAL, GeoPandas, or
  Pandas failures directly.
- Preserve deterministic results and stable tie-breaking. Add focused tests
  before changing scientific or topology behavior.

Before architectural work, read
`docs/changes/larger-than-memory-processing.md` and the documentation for the
stage being changed. Shared execution contracts are documented in
`docs/shared_execution.md`. Do not add stage-specific scheduling, worker
management, checkpointing, or output-publication implementations when the
capability belongs in the shared execution layer.

## Repository map

- `src/mgb_vec_hydro/`: production package and CLI.
- `tests/`: unit and CLI tests. `tests/execution/` covers shared bounded
  execution contracts.
- `tests/regression/` and `tests/carinhanha/`: BHO regression coverage and
  reference fixtures. Compatibility here protects established scientific
  behavior without making BHO schemas the generic API.
- `tests/benchmark/`: opt-in memory, I/O, and scaling checks; these are skipped
  during the normal test run.
- `legacy/`: copied QGIS-era source for understanding algorithms and workflow
  order only. Do not import it from production code or preserve its QGIS
  orchestration patterns.
- `docs/stage*.md`: implemented CLI and data contracts.
- `docs/plan/`: design notes for unimplemented workflow stages.

Keep user-facing command examples in `README.md` concise and keep detailed
contracts in the stage guides. Update documentation when a command, schema, or
output contract changes; do not present implemented behavior as future work.

## Development and verification

Use an isolated Python environment and install the package with test
dependencies:

```bash
python -m pip install -e ".[test]"
pytest
```

Run the smallest relevant test module while iterating, followed by the full
suite before handoff. The scaling suites are opt-in:

```bash
RUN_PREPARATION_BENCHMARKS=1 pytest tests/benchmark/test_preparation_scaling.py
RUN_EXECUTION_BENCHMARKS=1 pytest tests/benchmark/execution
RUN_TERRAIN_BENCHMARKS=1 pytest tests/benchmark/test_terrain_scaling.py
```

Some terrain benchmark cases also require `BHAE_ROUTING_INPUT` to identify a
local integration dataset.
