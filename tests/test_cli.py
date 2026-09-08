from click.testing import CliRunner

from mgb_vec_hydro.cli import main


def test_public_stage_commands_expose_their_primary_inputs():
    expected = {
        "define-roi": ("--catchments", "--segments", "--outlet-id"),
        "aggregate": ("--roi-catchments", "--roi-segments", "--uparea-min", "--lmin"),
        "prepare": (
            "--dem",
            "--mini-catchments",
            "--mini-segments",
            "--workers",
            "--io-slots",
            "--output-dir",
        ),
        "terrain-products": (
            "--dem",
            "--mini-ownership",
            "--drainage",
            "--mini-index",
            "--direction-source",
        ),
        "sample-minis": (
            "--mini-catchments",
            "--mini-segments",
            "--mini-index",
            "--dem",
            "--mini-ownership",
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
