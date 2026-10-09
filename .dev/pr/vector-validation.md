# ROI and aggregation validation and performance

Validated on 2026-10-09 against the frozen, corrected Jacui capture.

## Scientific and integration checks

- ROI comparisons pass for BHAE (4,071 source units) and TDXHydro (4,351).
  Aggregation comparisons pass for BHAE (527 minis) and TDXHydro (546), for
  every measured run.
- Aggregation using candidate ROI products passes for both networks. Sampling
  using those candidate mini vectors and captured raster products also passes,
  including the sampled CSV and HAND/LTND nodata diagnostics.
- Focused tests cover source ID types, exact large integer IDs, field resolution,
  outlet precedence, Strahler filtering, selected-only validation, cycles,
  geodesic metrics on projected/native-ellipsoid/geographic-grad CRSs, polygon
  holes, confluence boundaries, evolving length and string-ID ties, excluded
  connectors, domain fallback, metric provenance, geometric unions, centroids,
  dense processing order, memory admission, worker determinism, and collisions.
- Missing FlatGeobuf properties are checked as unset or null before conversion;
  GDAL's default numeric zero is not accepted as a missing required attribute.
- Formatting, Clippy with warnings denied, and all-target tests pass. The opt-in
  dataset test remains ignored in the ordinary suite; explicit developer-tool
  comparisons exercise both vector stages and downstream integration.
- All 46 inventory entries pass. Captures, historical benchmarks, and comparison
  tolerances remain unchanged. Vector geometry is compared semantically.

## Release measurements

Three sequential measurements per stage/network, alternating BHAE and TDXHydro,
with four requested workers and a 4096 MiB application budget. ROI measurements
preceded aggregation measurements; each invocation used a fresh output directory.
Every run reports four geometry workers in its manifest and benchmark record,
and passes its scientific comparison. Earlier development runs warmed inputs;
caches were not cleared. Candidate-chain and sampling checks ran afterward,
outside these timed invocations.

| Stage | Network | Run | Wall time (s) | Maximum process RSS (MiB) |
| --- | --- | --- | ---: | ---: |
| ROI | BHAE | 1 | 1.057 | 360.6 |
| ROI | BHAE | 2 | 0.994 | 361.1 |
| ROI | BHAE | 3 | 1.006 | 360.5 |
| ROI | BHAE | Median | 1.006 | 360.6 |
| ROI | TDXHydro | 1 | 1.154 | 416.8 |
| ROI | TDXHydro | 2 | 1.213 | 416.8 |
| ROI | TDXHydro | 3 | 1.191 | 414.7 |
| ROI | TDXHydro | Median | 1.191 | 416.8 |
| Aggregation | BHAE | 1 | 1.389 | 224.6 |
| Aggregation | BHAE | 2 | 1.397 | 223.0 |
| Aggregation | BHAE | 3 | 1.403 | 222.9 |
| Aggregation | BHAE | Median | 1.397 | 223.0 |
| Aggregation | TDXHydro | 1 | 4.263 | 268.3 |
| Aggregation | TDXHydro | 2 | 4.279 | 264.7 |
| Aggregation | TDXHydro | 3 | 4.243 | 264.9 |
| Aggregation | TDXHydro | Median | 4.263 | 264.9 |

The medians meet the provisional local historical targets: ROI 3.248/3.793
seconds and aggregation 4.848/7.310 seconds for BHAE/TDXHydro. Historical records
are single observations from a different native-library environment; these
results do not establish a controlled historical speedup or universal runtime
guarantee. Linux RSS is the maximum individual-process RSS, not summed workers.

Raw products, logs, and measurements are in
`tests/regression/jacui/runs/rust-vector-accepted-<roi|aggregate>-<network>-<1..3>/`.
Candidate ROI integration products are in `rust-vector-accepted-chain-<network>/`
and downstream sampling products in `rust-vector-accepted-sampling-<network>/`,
under the same gitignored runs directory.

## Implementation limits

Topology and source WKB remain resident. Conservative allocation estimates
limit geometry workers or reject inputs/individual union groups that cannot
fit; the memory budget is not a hard RSS ceiling. Geometry workers use their
own GIS transforms and contexts. Graph decisions and output reductions are
deterministic; no general executor, spill framework, or dependency was added.

Integer source IDs normalize to int64, matching the captured ROI schema.
ROI layer geometry types retain the source declaration; mini layers declare
unknown geometry type, matching the frozen union products. Actual geometries
remain validated polygons/multipolygons or lines/multilines.

Executable SHA-256: `68a98c36702eca05bcaad843f2280d2c7044f0872017dd8529915c45b33d90dc`.
Repository HEAD: `a8583c424898621db50e532f236259dc15903504`; measurements use the
uncommitted vector implementation, not that terrain commit alone.
Rust/Cargo 1.99.0; Linux x86_64; GDAL 3.10.3, PROJ 9.6.0, GEOS 3.13.1;
runner reports 20 available CPUs.
