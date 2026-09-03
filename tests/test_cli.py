from click.testing import CliRunner

from mgb_vec_hydro.cli import main


def test_stage_command_contracts():
    runner = CliRunner()
    prepare = runner.invoke(main, ["prepare", "--help"])
    roi = runner.invoke(main, ["define-roi", "--help"])
    aggregate = runner.invoke(main, ["aggregate", "--help"])
    assert prepare.exit_code == roi.exit_code == aggregate.exit_code == 0
    assert "--catchments" not in prepare.output and "--segments" not in prepare.output
    for option in (
        "--prepared", "--catchments-layer", "--segments-layer",
        "--catchments-source-crs", "--segments-source-crs", "--upstream-area-col",
    ):
        assert option in roi.output
    assert "--crs" not in roi.output and "--output-format" not in roi.output
    assert "--roi" in aggregate.output
    assert "--roi-catchments" not in aggregate.output
    assert "--crs" not in aggregate.output and "--output-format" not in aggregate.output
    for output in (roi.output, aggregate.output):
        assert "default: 512" in output
        assert "default: 10000" in output
        assert "default: 4" in output
        assert "default: 2" in output


def test_terrain_products_cli_exposes_agree_controls():
    result = CliRunner().invoke(main, ["terrain-products", "--help"])
    assert result.exit_code == 0
    assert "--agree-sharp" in result.output
    assert "--agree-smooth" in result.output
    assert "--agree-buffer" in result.output
