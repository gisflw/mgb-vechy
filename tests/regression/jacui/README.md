# Jacui regression reference

The frozen scientific reference covers **Jacui / BHAE** and **Jacui / TDXHydro**,
each with three ordered outlets. Large assets remain local and gitignored;
the tracked configuration, inventory, audit provenance, and historical timing
records stay unchanged during candidate work.

The Rust package currently provides regression tooling, not scientific stages.
The developer utility is separate from the production `mgb prepro` CLI:

```bash
cargo run --release --example jacui -- --help
cargo run --release --example jacui -- verify
```

`verify` checks existence, byte length, and SHA-256 for every inventory entry.
A checkout without local assets must obtain a copy of the original capture;
missing assets fail explicit verification and candidate runs.

## Contents and provenance

| Path | Purpose |
| --- | --- |
| `config.json` | Scientific settings, ordered outlets, source fields, and CRS overrides. |
| `inventory.json` | Captured file sizes, SHA-256 checksums, and source location. |
| `input/` | Shared source DEM/HRU crops and raw basin-only vectors. |
| `expected/bhae/`, `expected/tdxhydro/` | Frozen five-stage products, audit manifests, and diagnostics. |
| `benchmarks/` | Historical three-outlet timing records. |
| `runs/` | Disposable candidate products, logs, and measurements. |

The capture was generated with the historical scientific implementation.
Source and tests are available at commit
`0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0`. The baseline measurements identify
the production revision used for their runs; they are historical evidence,
including their original executable names and absolute paths.

| Case | Ordered outlets | Source units | Minis |
| --- | --- | --- | --- |
| BHAE | 171984, 420329, 178658 | 4,071 | 527 |
| TDXHydro, region 610 | 640538827, 640543432, 640538824 | 4,351 | 546 |

Outlet order maps to `sub` 3, 2, 1; later outlets take precedence in overlaps.
Both cases use EPSG:4326, area threshold 60 km², length threshold 6 km,
DEM scale 0.01, and AGREE sharp/smooth/buffer values 80/8/4.

## Candidate runs and comparisons

Once an external candidate implements scientific stages, run one stage against
captured upstream products:

```bash
cargo run --release --example jacui -- run --network bhae \
  --stage sample-minis --command target/release/mgb \
  --output-dir tests/regression/jacui/runs/bhae-sampling
cargo run --release --example jacui -- compare --network bhae \
  --stage sample-minis --output-dir tests/regression/jacui/runs/bhae-sampling
```

The command above will fail against the current skeleton because scientific
commands are pending. `--stage all` (the default) runs all five stages, using
candidate upstream products. Invoke both networks for full scientific validation.
Candidate commands take the form `<executable prefix> prepro <stage> ...`.
Repeat `--command-arg` to supply prefix arguments without shell evaluation,
for example `--command cargo --command-arg=run --command-arg=--release
--command-arg=--`. The small scientific-option adapter is provisional and
must evolve with stage implementations.

`run` accepts `--workers` and `--memory-limit-mb`, defaulting to 4 and 4096.
Outputs must go to a fresh, empty directory. Inputs and expected products,
including other networks and symlink aliases, are protected from candidate
writes. Candidate runs never read or write scratch. `--fixture PATH` globally
selects another local capture directory.

`compare` checks the exact stage product set and audit `step`/`parameters`
envelopes. It checks vector schemas, IDs, attributes, topologically equivalent
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

Scientific candidate regression is an ignored Rust integration test. Enable it
explicitly once the candidate is implemented:

```bash
MGB_REGRESSION_COMMAND=target/release/mgb JACUI_STAGE=sample-minis \
  cargo test --test regression jacui_candidate_scientific_regression -- --ignored
```

`MGB_REGRESSION_COMMAND` is an executable path, not shell text; use the developer
utility for executable-prefix arguments. Explicit runs fail on missing fixtures.
The dataset capture omits flow-direction products; the
[synthetic terrain cases](../synthetic/README.md) preserve focused examples.

## Performance evidence

Each candidate stage writes a log. `benchmark.json` records wall time, exit
status, revision, platform, resource settings, executable arguments, and timing
of failed invocations too. On Linux, `max_process_rss_kib` is `wait4`'s maximum
individual-process RSS including completed descendants, not summed concurrent
RSS. Other platforms record null for this Linux-specific metric.

Historical benchmark records are single-run observations, not thresholds.
Use release builds, the same local inputs/settings/machine, controlled cache
conditions, repeated runs, and medians for performance conclusions. Timing does
not determine scientific pass/fail. Broader scaling comes after both Jacui cases
are scientifically useful.

## Historical baseline refresh

Current Rust tools deliberately do not recapture or regenerate expected data.
For an intentional historical refresh, create a separate checkout:

```bash
git worktree add --detach ../mgb-python-reference \
  0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0
```

Follow that checkout's regression README and dependency manifest to use its
historical `capture.py`, explicitly pointing it at the original scratch data.
Review regenerated products, provenance, inventory, and benchmark records
before replacing this capture. This is a separate reference operation, never
part of candidate regression. `scratch/analysis` stays reserved for the user's
broader manual testing.
