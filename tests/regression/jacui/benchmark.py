"""Run one Jacui stage or the complete pipeline and record wall time and RSS."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
STAGES = ("define-roi", "aggregate", "prepare", "terrain-products", "sample-minis")


def run(network, output, *, stage="all", command=None, workers=4, memory_limit_mb=4096):
    config = json.loads((HERE / "config.json").read_text())
    fields = config["networks"][network]
    inputs = HERE / "input"
    expected = HERE / "expected" / network
    output = Path(output).resolve()
    if output == expected.resolve() or expected.resolve() in output.parents or output in expected.resolve().parents:
        raise ValueError("Candidate runs must not replace expected outputs")
    output.mkdir(parents=True, exist_ok=True)
    upstream = output if stage == "all" else expected
    commands = {
        "define-roi": ["--crs", config["crs"], "--catchments", inputs / network / "catchments.fgb",
                       "--segments", inputs / network / "segments.fgb", "--id-col", fields["id_col"],
                       "--id-down-col", fields["id_down_col"],
                       "--strahler-order-col", fields["strahler_order_col"]],
        "aggregate": ["--roi-catchments", upstream / "roi_catchments.fgb",
                      "--roi-segments", upstream / "roi_segments.fgb",
                      "--uparea-min", config["uparea_min"], "--lmin", config["lmin"]],
        "prepare": ["--dem", inputs / "dem.tif", "--dem-scale", config["dem_scale"],
                    "--mini-catchments", upstream / "mini_catchments.fgb",
                    "--mini-segments", upstream / "mini_segments.fgb",
                    "--categorical-raster", "hru", inputs / "hru.tif"],
        "terrain-products": ["--dem", upstream / "dem.tif",
                             "--grid-catchments", upstream / "grid_catchments.tif",
                             "--grid-segments", upstream / "grid_segments.tif",
                             "--direction-source", "dem", "--agree-sharp", config["agree_sharp"],
                             "--agree-smooth", config["agree_smooth"],
                             "--agree-buffer", config["agree_buffer"]],
        "sample-minis": ["--mini-catchments", upstream / "mini_catchments.fgb",
                         "--mini-segments", upstream / "mini_segments.fgb",
                         *[arg for name in ("dem", "grid_catchments", "grid_segments", "hand", "ltnd", "hru")
                           for arg in ("--" + name.replace("_", "-"), upstream / f"{name}.tif")]],
    }
    for outlet in fields["outlet_ids"]:
        commands["define-roi"] += ["--outlet-id", outlet]
    for kind in ("catchments", "segments"):
        if fields[f"{kind}_source_crs"]:
            commands["define-roi"] += [f"--{kind}-source-crs", fields[f"{kind}_source_crs"]]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    prefix = shlex.split(command) if command else [sys.executable, "-m", "mgb_vec_hydro.cli"]
    measurements = []
    for name in STAGES if stage == "all" else (stage,):
        argv = [*prefix, name, *map(str, commands[name]), "--output-dir", str(output),
                "--workers", str(workers), "--memory-limit-mb", str(memory_limit_mb)]
        started = time.perf_counter()
        with (output / f"{name}.log").open("w") as log:
            process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=log)
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
        measurements.append({"stage": name, "wall_seconds": time.perf_counter() - started,
                             "max_process_rss_kib": usage.ru_maxrss,
                             "exit_code": process.returncode})
        print(f'{network}/{name}: {measurements[-1]["wall_seconds"]:.3f}s', flush=True)
        if process.returncode:
            raise RuntimeError(f"{name} failed; see {output / (name + '.log')}")
    report = {"network": network, "command": prefix, "workers": workers,
              "memory_limit_mb": memory_limit_mb, "platform": platform.platform(),
              "cpu_count": os.cpu_count(), "recorded_at": datetime.now(timezone.utc).isoformat(),
              "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "measurements": measurements}
    (output / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=("bhae", "tdxhydro"), required=True)
    parser.add_argument("--stage", choices=("all", *STAGES), default="all")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--command", help="Executable prefix; defaults to the current reference CLI")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--memory-limit-mb", type=int, default=4096)
    args = parser.parse_args()
    run(args.network, args.output_dir, stage=args.stage, command=args.command,
        workers=args.workers, memory_limit_mb=args.memory_limit_mb)
