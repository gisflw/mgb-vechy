from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from mgb_vec_hydro.cli import main
from mgb_vec_hydro.sampling import NODATA_REPORT_FILENAMES, _SamplingNodataWarning


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
        return SimpleNamespace(files=(), raster_count=0, timings={}, output_dir=tmp_path / "out")

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


@pytest.mark.parametrize("command,function,output_name,extra", [
    ("define-roi", "define_roi_dataset", "roi_segments.fgb", []),
    ("aggregate", "aggregate_roi_dataset", "source_to_mini.csv", []),
    ("prepare", "prepare_dataset", "dem.tif", []),
    ("prepare", "prepare_dataset", "hru.tif", ["--categorical-raster", "hru"]),
    ("prepare", "prepare_dataset", "d8.tif", ["--d8"]),
    ("terrain-products", "create_terrain_dataset", "hand.tif", []),
    ("terrain-products", "create_terrain_dataset", "flow_direction.tif", ["--write-flow-direction"]),
    ("sample-minis", "sample_minibasins", "sampled_minis.csv", []),
    ("sample-minis", "sample_minibasins", "manifest-sample-minis.json", []),
    *[
        ("sample-minis", "sample_minibasins", name, [])
        for name in NODATA_REPORT_FILENAMES
    ],
])
def test_output_confirmation_precedes_execution(tmp_path, monkeypatch, command, function, output_name, extra):
    import click

    calls = []

    def run(spec):
        calls.append(spec.overwrite)
        raise RuntimeError("stage started")

    monkeypatch.setattr(f"mgb_vec_hydro.cli.{function}", run)
    source = tmp_path / "input"
    source.touch()
    output = tmp_path / "output"
    output.mkdir()
    unrelated = output / "other-stage.txt"
    unrelated.write_text("keep")
    args = [command, "--output-dir", str(output)]
    values = {"crs": "EPSG:3857", "uparea_min": "1", "lmin": "1"}
    for param in main.commands[command].params:
        if param.required and param.name != "output_dir":
            value = str(source) if isinstance(param.type, click.Path) else values.get(param.name, "id")
            args.extend([param.opts[0], value])
    args.extend(extra)
    if extra and extra[-1] != "--write-flow-direction":
        args.append(str(source))

    runner = CliRunner()
    result = runner.invoke(main, args)
    assert str(result.exception) == "stage started"
    assert calls == [False]
    assert "Replace existing" not in result.output
    calls.clear()
    existing = output / output_name
    existing.write_text("old")
    for answer in ("n\n", ""):
        result = runner.invoke(main, args, input=answer)
        assert result.exit_code != 0
        assert calls == []
        assert existing.read_text() == "old"
    result = runner.invoke(main, args, input="y\n")
    assert str(result.exception) == "stage started"
    assert calls == [True]
    assert str(existing) in result.output
    assert str(unrelated) not in result.output
    assert unrelated.read_text() == "keep"


def test_sample_cli_renders_one_clean_nodata_warning_and_lists_reports(
    tmp_path, monkeypatch
):
    import warnings

    message = (
        "Nodata cells were found within the domain for raster(s): --hru. "
        "Statistics exclude these cells; substantial missing coverage can produce "
        "unrealistic results. Please verify whether the affected results are suitable."
    )
    report_path = tmp_path / "out" / "nodata_hru.csv"

    def sample(spec):
        warnings.warn(message, _SamplingNodataWarning)
        warnings.warn("ordinary warning", UserWarning)
        return SimpleNamespace(
            sampled_minis=tmp_path / "out" / "sampled_minis.csv",
            output_dir=tmp_path / "out",
            mini_count=1,
            catchment_cells=12,
            reach_cells=3,
            hru_class_ids=(1,),
            timings={},
            nodata_reports=(report_path,),
        )

    monkeypatch.setattr("mgb_vec_hydro.cli.sample_minibasins", sample)
    args = ["sample-minis"]
    for option in (
        "mini-catchments",
        "mini-segments",
        "dem",
        "cells",
        "drainage",
        "hand",
        "ltnd",
        "hru",
    ):
        path = tmp_path / option
        path.touch()
        args.extend([f"--{option}", str(path)])
    args.extend(["--output-dir", str(tmp_path / "out")])

    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert f"RuntimeWarning: {message}" in result.output
    assert "tests/test_cli.py:" in result.output
    assert "UserWarning: ordinary warning" in result.output
    assert "sampling.py:" not in result.output
    assert "Mini 1:" not in result.output
    assert "1 / 12 cells" not in result.output
    assert f"Wrote {report_path}" in result.output
