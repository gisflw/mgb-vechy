from click.testing import CliRunner

from mgb_vec_hydro.cli import main


def test_public_stage_commands_expose_their_primary_inputs():
    expected = {
        "define-roi": ("--catchments", "--segments", "--outlet-id"),
        "aggregate": ("--roi", "--uparea-min", "--lmin"),
        "prepare": ("--dem", "--minis", "--output-dir"),
        "terrain-products": ("--prepared", "--direction-source"),
        "sample-minis": ("--catchments", "--segments", "--hru"),
    }
    runner = CliRunner()
    for command, options in expected.items():
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0
        assert all(option in result.output for option in options)
