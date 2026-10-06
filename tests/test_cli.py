from click.testing import CliRunner
from types import SimpleNamespace

import pytest

from mgb_vec_hydro.cli import main


def test_public_stage_commands_expose_their_primary_inputs():
    expected = {
        "define-roi": ("--catchments", "--segments", "--outlet-id"),
        "aggregate": ("--roi-catchments", "--roi-segments", "--uparea-min", "--lmin"),
        "prepare": (
            "--dem",
            "--dem-scale",
            "--mini-catchments",
            "--mini-segments",
            "--workers",
            "--io-slots",
            "--output-dir",
        ),
        "terrain-products": (
            "--dem",
            "--cells",
            "--drainage",
            "--direction-source",
        ),
        "sample-minis": (
            "--mini-catchments",
            "--mini-segments",
            "--dem",
            "--cells",
            "--drainage",
            "--hand",
            "--ltnd",
            "--hru",
        ),
    }
    runner = CliRunner()
    for command, options in expected.items():
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0
        assert all(option in result.output for option in options)
        assert "--mini-index" not in result.output
        assert "--checkpoint-dir" not in result.output


def test_worker_options_have_no_artificial_upper_bound():
    runner = CliRunner()
    for command in (
        "define-roi",
        "aggregate",
        "prepare",
        "terrain-products",
        "sample-minis",
    ):
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0
        assert "x>=1" in result.output
        assert "x<=4" not in result.output


def test_manifest_backed_directory_options_are_removed():
    expected = {
        "prepare": ("--minis",),
        "aggregate": ("--roi",),
        "terrain-products": ("--prepared",),
        "sample-minis": ("--minis", "--prepared", "--terrain", "--hru-name"),
    }
    runner = CliRunner()
    for command, options in expected.items():
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0
        option_lines = {
            line.strip().split()[0]
            for line in result.output.splitlines()
            if line.strip().startswith("--")
        }
        assert all(option not in option_lines for option in options)


@pytest.mark.parametrize("dem_scale", [None, "0.01"])
def test_prepare_cli_forwards_dem_scale(tmp_path, monkeypatch, dem_scale):
    seen = []

    def prepare(spec):
        seen.append(spec)
        return SimpleNamespace(files=(), raster_count=0, timings={})

    monkeypatch.setattr("mgb_vec_hydro.cli.prepare_dataset", prepare)
    inputs = []
    for option in ("dem", "mini-catchments", "mini-segments"):
        path = tmp_path / option
        path.touch()
        inputs.extend([f"--{option}", str(path)])
    args = ["prepare", *inputs, "--output-dir", str(tmp_path / "out")]
    if dem_scale is not None:
        args.extend(["--dem-scale", dem_scale])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert seen[0].dem_scale == (1.0 if dem_scale is None else 0.01)


def test_agree_cli_defaults_are_unchanged():
    defaults = {param.name: param.default for param in main.commands["terrain-products"].params}
    assert defaults["agree_sharp"] == 80.0
    assert defaults["agree_smooth"] == 8.0
    assert defaults["agree_buffer"] == 4
