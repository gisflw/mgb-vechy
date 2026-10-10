# MGB

MGB is a Rust library and command-line toolset for hydrography and model inputs.
Its first module, `mgb::prepro`, prepares vector networks, mini-basins, terrain
products, and model attributes using explicit segment and downstream IDs.

All five preprocessing stages are implemented in Rust, with library APIs, CLI
commands, and Jacui regression tools. The stage contracts define their scientific
behavior.

## Build and check

Install stable Rust and the GDAL (with ZSTD support), GEOS, and PROJ development
libraries. On
Debian/Ubuntu, the native packages are `build-essential`, `pkg-config`,
`libgdal-dev`, `libgeos-dev`, and `libproj-dev`. The repository devcontainer
provides these dependencies and Rust tooling.

```bash
cargo build --locked
cargo run -- --help
cargo run -- prepro --help
cargo fmt --all -- --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --all-targets --locked
```

The executable is `mgb`; scientific commands use `mgb prepro <stage>`.
The public library namespace is `mgb::prepro`. There is one Cargo package;
each scientific stage has its own module (`roi`, `aggregation`, `preparation`,
`terrain`, and `sampling` under `mgb::prepro`).

## Frozen preprocessing workflow

| Stage | Capability | Main products |
| --- | --- | --- |
| 1. [Define ROI](docs/stage1_roi_cli.md) | Implemented: upstream outlet union and normalized hydrography. | ROI catchments and segments. |
| 2. [Aggregate mini-basins](docs/stage2_aggregation_cli.md) | Implemented: group source units using area, length, and network rules. | Mini catchments, reaches, and source-to-mini mapping. |
| 3. [Prepare raster data](docs/stage3_prepare_data.md) | Implemented: aligned inputs, raster ownership, and matching drainage. | Prepared DEM, optional rasters, and mini-ID grids. |
| 4. [Terrain products](docs/stage4_terrain_cli.md) | Implemented: confined routing, HAND, and terrain-to-drainage distance. | HAND, LTND, diagnostics, and optional directions. |
| 5. [Sample mini-basins](docs/stage5_mini_sampling_cli.md) | Implemented: terrain, HRU, and flooded-area summaries. | Mini attribute CSV and missing-data reports. |

Stage names remain `define-roi`, `aggregate`, `prepare`, `terrain-products`, and
`sample-minis`, under the `prepro` CLI namespace. The
[shared data contracts](docs/shared_data_contracts.md) freeze output schemas,
filenames, units, and audit stage identifiers.

## Scientific references

The [Jacui reference](tests/regression/jacui/README.md) covers BHAE and TDXHydro.
Large captured assets stay local. Rust tools verify checksums, run an external
candidate, and compare decoded products; stage manifests record runtime and
product checksums:

```bash
cargo run --release --example jacui -- verify
cargo run --release --example jacui -- --help
```

[Synthetic terrain fixtures](tests/regression/synthetic/README.md) preserve
focused routing examples exercised against the Rust terrain kernels. The historical
reference source and tests remain available at commit
`0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0`.

The [Rust architecture plan](.dev/pr/rust-native-architecture.md) defines the
implementation order. [Remaining capabilities](.dev/plan/README.md) describe
HRU construction and final MGB simulation-file generation.
