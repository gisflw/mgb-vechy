# Jacui regression reference

Use these two datasets for automated scientific regression and performance
work: **Jacui / BHAE** and **Jacui / TDXHydro**, each using three ordered outlets. Small synthetic scientific
tests elsewhere remain useful for isolated edge cases.

The existing runs in `scratch/analysis/results/jacui` supply the source data. The initial source networks confirm that the requested
three-outlet unions are contained in the captured basin IDs. `scratch/analysis` is reserved for
broader manual testing when the tool is more mature. Candidate regression
runs use the local capture and do not read or write scratch.

## Contents

| Path | Purpose |
| --- | --- |
| `config.json` | Scientific settings, outlets, source field names, and CRS overrides. |
| `inventory.json` | Source location, captured file sizes, and SHA-256 checksums. |
| `input/` | Shared source DEM/HRU crops and raw basin-only catchments/segments for each network. |
| `expected/bhae/`, `expected/tdxhydro/` | Captured reference products for all five stages, audit manifests, and diagnostics. |
| `benchmarks/` | Current three-outlet timing records: `bhae.json` and `tdxhydro.json`. |
| `runs/` | Disposable candidate outputs, logs, and measurements. |
| `capture.py` | Explicitly capture reference assets from the existing scratch runs. |
| `benchmark.py` | Exercise one stage or all stages, recording complete-invocation wall time and memory evidence. |

Large FGB/TIFF assets and sampled CSVs are gitignored. Configuration,
checksums, source-to-mini mapping, diagnostic CSVs, audit provenance, and
benchmark records are versioned. A checkout without local assets can capture
them from the original scratch data:

```bash
python tests/regression/jacui/capture.py --scratch /workspace/scratch
```

Capture refreshes the raw basin-only inputs, regenerates all five expected
stages with the current reference implementation, records fixture-local
performance, and updates checksums. It reads the original source networks and
raster paths from their existing ROI manifests. Run it only when intentionally
refreshing the scientific baseline. Source DEM values are centimetres;
preparation applies the configured `dem_scale=0.01`.

| Case | Outlet | Source units | Minis |
| --- | --- | --- | --- |
| BHAE | 171984, 420329, 178658 | 4,071 | 527 |
| TDXHydro, region 610 | 640538827, 640543432, 640538824 | 4,351 | 546 |

In each outlet list, `sub` is assigned 3, 2, 1 in order, with later outlets
taking precedence in overlapping upstream areas. Both cases use EPSG:4326, area threshold 60 km², length threshold 6 km,
and AGREE sharp/smooth/buffer values 80/8/4.

## Scientific regression

From the repository root, run both full pipelines and compare their products:

```bash
RUN_JACUI_REGRESSION=1 pytest -q tests/regression/test_jacui.py
```

The default suite skips these dataset runs. Explicitly enabled runs fail if
local fixtures are missing. Select one stage to compare a partial
implementation against captured upstream products:

```bash
RUN_JACUI_REGRESSION=1 JACUI_STAGE=sample-minis pytest -q tests/regression/test_jacui.py
```

`MGB_REGRESSION_COMMAND` selects an executable prefix for a candidate CLI.
The runner currently expresses the reference CLI's options; adapt this small
command adapter when the Rust CLI is defined. These scripts are development
tools, not production architecture or frozen CLI contracts.

Comparisons require exact column names/types, mini IDs, integer ownership,
validity masks, required metadata, and specified feature order. Geometry is
compared topologically. Sampling rows are matched by mini ID. CSV numbers use
`rtol=1e-10, atol=1e-10`; float raster cells use `rtol=1e-6, atol=1e-6`.
Affine coordinates and embedded mini bounds allow `atol=1e-12` coordinate
units (`rtol=0`) for source-crop rounding; raster dimensions, integer ownership,
and masks remain exact. These tolerances cover representation differences and do not authorize
different scientific routing or assignment decisions. The captured dataset
products do not include flow-direction rasters; focused routing tests supply
that reference.

## Performance measurements

Run a full case into a fresh output folder:

```bash
python tests/regression/jacui/benchmark.py --network bhae \
  --output-dir tests/regression/jacui/runs/bhae
python tests/regression/jacui/benchmark.py --network tdxhydro \
  --output-dir tests/regression/jacui/runs/tdxhydro
```

Use `--stage` for an individual stage, `--command` for a candidate executable,
and `--workers`/`--memory-limit-mb` to select reference-runtime sizing.
The runner defaults to four workers and 4096 MB. Each stage writes a log;
`benchmark.json` records wall time, exit status, revision, platform, and sizing.
`max_process_rss_kib` is Linux wait4's maximum individual-process RSS, including
completed descendants, not the sum of concurrently resident workers.

`benchmarks/bhae.json` and `benchmarks/tdxhydro.json` record the current
three-outlet cases. These are single-run observations on basin-only inputs,
not speed thresholds or statistically established improvements.
Compare release Rust builds on the same inputs and machine, control cache
conditions, and use repeated runs and medians for performance conclusions.
Distinguish cold startup/JIT from warmed execution. Timing does not determine
scientific test success.
