"""Opt-in Stage 5 throughput and block-reuse measurements.

Set RUN_SAMPLING_BENCHMARKS=1 and provide the explicit BHAE mini vector,
index, raster, and terrain-product file paths.
"""

import os
from pathlib import Path

import pytest

from mgb_vec_hydro.sampling import MiniSamplingSpec, sample_minibasins

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SAMPLING_BENCHMARKS") != "1",
    reason="sampling performance benchmarks are opt-in",
)


def _input(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for the sampling benchmark")
    path = Path(value)
    if not path.is_file():
        pytest.skip(f"{name} does not identify a local file")
    return path


def test_sampling_reports_serial_and_parallel_throughput(tmp_path, record_property):
    paths = {name: _input(name) for name in (
        "BHAE_MINI_CATCHMENTS",
        "BHAE_MINI_SEGMENTS",
        "BHAE_MINI_INDEX",
        "BHAE_DEM",
        "BHAE_MINI_OWNERSHIP",
        "BHAE_DRAINAGE",
        "BHAE_HAND",
        "BHAE_LTND",
        "BHAE_HRU",
    )}
    reports = {}
    for workers in (1, 4):
        reports[workers] = sample_minibasins(
            MiniSamplingSpec(
                mini_catchments=paths["BHAE_MINI_CATCHMENTS"],
                mini_segments=paths["BHAE_MINI_SEGMENTS"],
                mini_index=paths["BHAE_MINI_INDEX"],
                dem=paths["BHAE_DEM"],
                mini_ownership=paths["BHAE_MINI_OWNERSHIP"],
                drainage=paths["BHAE_DRAINAGE"],
                hand=paths["BHAE_HAND"],
                ltnd=paths["BHAE_LTND"],
                hru=paths["BHAE_HRU"],
                output_dir=tmp_path / f"workers-{workers}",
                workers=workers,
            )
        )
        report = reports[workers]
        diagnostics = report.execution.worker_diagnostics
        blocks_read = sum(int(value.get("blocks_read", 0)) for value in diagnostics)
        record_property(f"workers_{workers}_seconds", report.timings["total"])
        record_property(
            f"workers_{workers}_raster_seconds", report.timings["raster_reads"]
        )
        record_property(f"workers_{workers}_blocks_read", blocks_read)
        record_property(
            f"workers_{workers}_minis_per_second",
            report.mini_count / max(report.timings["total"], 1e-9),
        )
        record_property(
            f"workers_{workers}_peak_admitted_bytes",
            report.execution.peak_admitted_bytes,
        )

    assert (
        reports[1].sampled_minis.read_bytes() == reports[4].sampled_minis.read_bytes()
    )
