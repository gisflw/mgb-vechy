"""Refresh the three-outlet Jacui inputs and reference products."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pyogrio
import rasterio
from rasterio.windows import Window, from_bounds

from benchmark import run as run_benchmark

HERE = Path(__file__).resolve().parent
OUTLETS = {
    "bhae": ("171984", "420329", "178658"),
    "tdxhydro": ("640538827", "640543432", "640538824"),
}


def capture(scratch):
    references = scratch / "analysis/results/jacui"
    config = {
        "crs": "EPSG:4326", "uparea_min": 60.0, "lmin": 6.0,
        "dem_scale": 0.01, "agree_sharp": 80.0, "agree_smooth": 8.0,
        "agree_buffer": 4, "networks": {},
    }
    bounds = []
    for network, outlets in OUTLETS.items():
        source = references / network
        roi = json.loads((source / "manifest-define-roi.json").read_text())["parameters"]
        config["networks"][network] = {
            "outlet_ids": list(outlets),
            **{key: roi[key] for key in (
                "id_col", "id_down_col", "strahler_order_col",
                "catchments_source_crs", "segments_source_crs",
            )},
            "tdx_region": "610" if network == "tdxhydro" else None,
        }
        benchmark_dir = HERE / "benchmarks"
        benchmark_dir.mkdir(exist_ok=True)
        input_dir = HERE / "input" / network
        input_dir.mkdir(parents=True, exist_ok=True)
        ids = pyogrio.read_arrow(
            source / "roi_segments.fgb", columns=["id"], read_geometry=False
        )[1]["id"].to_pylist()
        id_set = set(ids)
        missing_outlets = set(map(int, outlets)) - id_set
        if missing_outlets:
            raise ValueError(
                f"Captured {network} ROI omits requested outlet(s): {sorted(missing_outlets)}"
            )
        # These requested outlets' upstream domains are contained in this captured
        # union. The full-source ROI check established this for the initial capture.
        for kind in ("catchments", "segments"):
            columns = [roi["id_col"]]
            if kind == "segments":
                columns.extend((roi["id_down_col"], roi["strahler_order_col"]))
            original = Path(roi[kind])
            original = scratch / original.relative_to(original.parents[1])
            source_fields = list(pyogrio.read_info(original)["fields"])
            resolved = {
                name: next(field for field in source_fields if field.casefold() == name.casefold())
                for name in columns
            }
            actual_columns = [resolved[name] for name in columns]
            where = f'"{resolved[roi["id_col"]]}" IN ({",".join(map(str, ids))})'
            metadata, table = pyogrio.read_arrow(original, columns=actual_columns, where=where)
            if len(table) != len(ids):
                raise ValueError(f"{network} {kind} source IDs do not match the ROI")
            pyogrio.write_arrow(
                table, input_dir / f"{kind}.fgb", driver="FlatGeobuf",
                geometry_name=metadata["geometry_name"] or "wkb_geometry",
                geometry_type=metadata["geometry_type"],
                crs=roi[f"{kind}_source_crs"] or metadata["crs"],
            )
        with rasterio.open(source / "dem.tif") as dem:
            bounds.append(dem.bounds)

    # Both cases share aligned DEM and HRU crops. Retain source values and masks.
    union = (
        min(bound.left for bound in bounds), min(bound.bottom for bound in bounds),
        max(bound.right for bound in bounds), max(bound.top for bound in bounds),
    )
    for name, source in (
        ("dem", scratch / "dem_sa.tif"),
        ("hru", scratch / "mapbiomas/lc_sa_partial.tif"),
    ):
        with rasterio.open(source) as src:
            raw = from_bounds(*union, transform=src.transform)
            col0, row0 = int(np.floor(raw.col_off)) - 1, int(np.floor(raw.row_off)) - 1
            col1 = int(np.ceil(raw.col_off + raw.width)) + 1
            row1 = int(np.ceil(raw.row_off + raw.height)) + 1
            window = Window(col0, row0, col1 - col0, row1 - row0)
            profile = src.profile.copy()
            profile.update(
                driver="GTiff", width=int(window.width), height=int(window.height),
                transform=src.window_transform(window), tiled=True,
                blockxsize=512, blockysize=512, compress="deflate", BIGTIFF="IF_SAFER",
            )
            destination = HERE / "input" / f"{name}.tif"
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                with rasterio.open(destination, "w", **profile) as dst:
                    for _, block in dst.block_windows():
                        selected = Window(
                            window.col_off + block.col_off, window.row_off + block.row_off,
                            block.width, block.height,
                        )
                        dst.write(src.read(window=selected), window=block)
                        dst.write_mask(src.dataset_mask(window=selected), window=block)
        print(f"Captured shared {name} crop", flush=True)

    (HERE / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    for network in OUTLETS:
        run_dir = HERE / "runs" / f"capture-{network}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        report = run_benchmark(network, run_dir, workers=4, memory_limit_mb=4096)
        report["outlet_ids"] = list(OUTLETS[network])
        report["dataset"] = "three-outlet Jacui basin-only fixture"
        report["observation"] = "single regenerated baseline; not a performance threshold"
        (HERE / "benchmarks" / f"{network}.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        expected = HERE / "expected" / network
        expected.mkdir(parents=True, exist_ok=True)
        for old in expected.iterdir():
            if old.is_dir():
                raise ValueError(f"Unexpected directory in expected products: {old}")
            old.unlink()
        for product in run_dir.iterdir():
            if product.suffix not in {".fgb", ".tif", ".csv", ".json"}:
                continue
            if product.name == "benchmark.json":
                continue
            if product.name.startswith("manifest-"):
                manifest = json.loads(product.read_text())
                manifest["parameters"]["output_dir"] = str(expected.resolve())
                (expected / product.name).write_text(json.dumps(manifest, indent=2) + "\n")
            else:
                shutil.copy2(product, expected / product.name)
        shutil.rmtree(run_dir)
        print(f"Regenerated three-outlet {network} reference products", flush=True)

    inventory = {"source": str(references), "files": {}}
    for folder in (HERE / "input", HERE / "expected"):
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                        digest.update(chunk)
                inventory["files"][str(path.relative_to(HERE))] = {
                    "bytes": path.stat().st_size,
                    "sha256": digest.hexdigest(),
                }
    (HERE / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    print("Captured inputs, regenerated outputs, and recorded benchmark provenance.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, default=Path("/workspace/scratch"))
    capture(parser.parse_args().scratch)
