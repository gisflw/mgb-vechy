# Jacui regression reference

The scientific reference covers **Jacui / BHAE** and **Jacui / TDXHydro**,
each with three ordered outlets. Large assets remain local and gitignored;
the stage manifests carry the scientific parameters, input/output checksums,
and runtime measurements.

The Rust package implements all five preprocessing stages and provides
regression tooling.
The developer utility is separate from the production `mgb prepro` CLI:

```bash
cargo run --release --example jacui -- --help
cargo run --release --example jacui -- verify
```

`verify` checks every input and output recorded by the stage manifests against
its SHA-256 checksum. A checkout without local assets must obtain a copy of the
original capture; missing assets fail explicit verification and candidate runs.

## Contents and provenance

| Path | Purpose |
| --- | --- |
| `input/` | Shared source DEM/HRU crops and raw basin-only vectors. |
| `expected/bhae/`, `expected/tdxhydro/` | Frozen five-stage products and manifests with parameters, checksums, and diagnostics. |
| `runs/` | Disposable candidate products, logs, and manifests. |

The capture was generated with the historical scientific implementation.
Source and tests are available at commit
`0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0`.

| Case | Ordered outlets | Source units | Minis |
| --- | --- | --- | --- |
| BHAE | 171984, 420329, 178658 | 4,071 | 527 |
| TDXHydro, region 610 | 640538827, 640543432, 640538824 | 4,351 | 546 |

Outlet order maps to `sub` 3, 2, 1; later outlets take precedence in overlaps.
Both cases use EPSG:4326, area threshold 60 km², length threshold 6 km,
DEM scale 0.01, and AGREE sharp/smooth/buffer values 80/8/4.

## Fixture correction

On 2026-10-09, an explicitly authorized correction changed `sub` from float64
to int64 in both networks' mini catchments, mini segments, and sampled CSVs.
Values are unchanged. Other vector attributes, geometry bytes, and mini feature
order were checked unchanged. ROI vectors and source-to-mini CSVs already use
integer `sub`; source rasters were not changed. The stage manifests record
checksums for the captured inputs and regenerated outputs. Local copies of the
capture must include this correction to pass verification.

On 2026-10-10, both expected product sets were regenerated with the Rust
implementation and compared against the previous references. Decoded products
matched; manifests now record input and output checksums and runtime measurements.

## Candidate runs and comparisons

Build the candidate with `cargo build --release --locked --bin mgb`, then run
sampling against captured upstream products:

```bash
cargo run --release --example jacui -- run --network bhae \
  --stage sample-minis --command target/release/mgb \
  --output-dir tests/regression/jacui/runs/bhae-sampling
cargo run --release --example jacui -- compare --network bhae \
  --stage sample-minis --output-dir tests/regression/jacui/runs/bhae-sampling
```

All stages work against the current Rust executable. `--stage all` (the default)
uses candidate upstream products throughout the full workflow. Use
`--stage prepare` for preparation against captured mini vectors, or run the full
pipeline for both networks:

```bash
cargo run --release --example jacui -- run --network bhae \
  --stage all --command target/release/mgb \
  --output-dir tests/regression/jacui/runs/bhae-pipeline
cargo run --release --example jacui -- compare --network bhae \
  --stage all --output-dir tests/regression/jacui/runs/bhae-pipeline
```

Repeat with `--network tdxhydro` and a fresh output directory.
Candidate commands take the form `<executable prefix> prepro <stage> ...`.
Repeat `--command-arg` to supply prefix arguments without shell evaluation,
for example `--command cargo --command-arg=run --command-arg=--release
--command-arg=--`. All stage adapters match the Rust CLI.

`run` accepts `--workers` and `--memory-limit-mb`, defaulting to 4 and 4096.
Optional `--io-slots` is passed to the candidate executable; omit it when
testing the CLI default. The historical Rust binary has no I/O-slot option, so
set it to the worker count when matching its effective maximum I/O concurrency.
Outputs must go to a fresh, empty directory. Inputs and expected products,
including other networks and symlink aliases, are protected from candidate
writes. Candidate runs never read or write scratch. `--fixture PATH` globally
selects another local capture directory.

`compare` checks the exact stage product set, checksummed manifest outputs,
runtime measurements, and audit `step`/`parameters` envelopes. It checks vector
schemas, IDs, attributes, topologically equivalent
geometry, and mini feature ordering. ROI rows and sampled CSV rows are matched
by ID. Integer values and masks are exact. Numeric CSV/vector attributes use
`rtol=1e-10, atol=1e-10`; continuous rasters use `rtol=1e-6, atol=1e-6`.
Grid transforms and mini bounds use `rtol=0, atol=1e-12`. Raster dimensions,
CRS, dtypes, validity masks, COG layout, units, and metadata are checked using
bounded windows. These tolerances do not authorize changed scientific decisions.

For tool validation only, compare frozen products against themselves:

```bash
cargo run --release --example jacui -- compare --network bhae \
  --output-dir tests/regression/jacui/expected/bhae
cargo run --release --example jacui -- compare --network tdxhydro \
  --output-dir tests/regression/jacui/expected/tdxhydro
```

Terrain runs use the same commands with `--stage terrain-products`. Scientific
candidate regression is an ignored Rust integration test. Enable terrain
regression explicitly:

```bash
MGB_REGRESSION_COMMAND=target/release/mgb JACUI_STAGE=terrain-products \
  cargo test --test regression jacui_candidate_scientific_regression -- --ignored
```

Use `JACUI_STAGE=sample-minis`, `JACUI_STAGE=define-roi`, or
`JACUI_STAGE=aggregate`, or `JACUI_STAGE=prepare` to test individual stages.
Omit `JACUI_STAGE` or set it to `all` for the full workflow.

`MGB_REGRESSION_COMMAND` is an executable path, not shell text; use the developer
utility for executable-prefix arguments. Explicit runs fail on missing fixtures.
The dataset capture omits flow-direction products; the
[synthetic terrain cases](../synthetic/README.md) preserve focused examples.

## Historical baseline refresh

The Rust tools do not recapture the historical input data. For an intentional
historical refresh, create a separate checkout:

```bash
git worktree add --detach ../mgb-python-reference \
  0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0
```

Follow that checkout's regression README and dependency manifest to use its
historical `capture.py`, explicitly pointing it at the original scratch data.
Review regenerated products and stage manifests before replacing this capture.
This is a separate reference operation, never
part of candidate regression. `scratch/analysis` stays reserved for the user's
broader manual testing.

For the vector stages, use the same `run` and `compare` commands with
`--stage define-roi` or `--stage aggregate`. Separate aggregation runs consume
captured ROI products. To check candidate ROI integration, invoke
`mgb prepro aggregate` directly with candidate ROI paths and compare the
result using `--stage aggregate`. Keep the stage output directories separate
so the comparator can verify each exact product set.
