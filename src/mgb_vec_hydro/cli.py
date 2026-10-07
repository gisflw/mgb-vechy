from __future__ import annotations

import sys
import warnings
from pathlib import Path

import click

from mgb_vec_hydro.aggregation import AggregationSpec, aggregate_roi_dataset
from mgb_vec_hydro.execution.progress import StageProgress
from mgb_vec_hydro.preparation import NamedRaster, PreparationSpec, prepare_dataset
from mgb_vec_hydro.roi import RoiSpec, define_roi_dataset
from mgb_vec_hydro.sampling import (
    NODATA_REPORT_FILENAMES,
    MiniSamplingSpec,
    _SamplingNodataWarning,
    sample_minibasins,
)
from mgb_vec_hydro.terrain import TerrainSpec, create_terrain_dataset


@click.group()
def main() -> None:
    """MGB vector hydrography preprocessing tools."""


_NAMED_RASTER = click.Tuple(
    [click.STRING, click.Path(exists=True, dir_okay=False, path_type=Path)]
)


def _confirm_replacement(output_dir: Path, names: tuple[str, ...]) -> bool:
    existing = [output_dir / name for name in names if (output_dir / name).exists()]
    if existing:
        click.confirm(
            "Replace existing output files?\n" + "\n".join(str(path) for path in existing),
            default=False,
            abort=True,
        )
    return bool(existing)


def _echo_timings(timings: dict[str, float]) -> None:
    labels = (
        ("preparing_wall", "preparing"),
        ("processing_wall", "processing"),
        ("finalizing_wall", "finalizing"),
        ("total", "total"),
    )
    click.echo("Elapsed: " + ", ".join(
        f"{label} {timings[key]:.1f}s" for key, label in labels if key in timings
    ))


def _run_stage(stage, spec):
    labels = {
        "preparing": "Preparing inputs",
        "processing": "Processing batches",
        "finalizing": "Finalizing outputs",
    }
    with click.progressbar(
        length=1, label=labels["preparing"], file=sys.stderr,
        show_eta=False, show_percent=False, show_pos=False,
    ) as bar:
        phase = "preparing"

        def progress(update: StageProgress) -> None:
            nonlocal phase
            if update.phase != phase:
                phase = update.phase
                bar.label = labels[phase]
                bar.length = update.total if update.total is not None else 1
                bar.pos = 0
                bar.finished = False
                bar.show_percent = bar.show_pos = phase == "processing"
                bar.render_progress()
            if phase == "processing":
                bar.update(update.completed - bar.pos)

        report = stage(spec, progress=progress)
        bar.update(max(0, (bar.length or 0) - bar.pos))
        return report


@main.command("prepare")
@click.option(
    "--dem-scale", type=float, default=1.0, show_default=True,
    help="Multiply stored DEM values by this factor to convert elevations to metres.",
)
@click.option(
    "--dem",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--mini-catchments",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--mini-segments",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--continuous-raster",
    type=_NAMED_RASTER,
    multiple=True,
    metavar="NAME PATH",
)
@click.option(
    "--categorical-raster",
    type=_NAMED_RASTER,
    multiple=True,
    metavar="NAME PATH",
)
@click.option("--d8", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--d8-encoding", type=click.Choice(["canonical", "esri"]))
@click.option(
    "--memory-limit-mb",
    type=click.IntRange(min=1),
    default=4096,
    show_default=True,
)
@click.option(
    "--workers", type=click.IntRange(min=1), default=4, show_default=True
)
@click.option("--io-slots", type=click.IntRange(min=1), default=2, show_default=True)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
)
def prepare_command(
    dem: Path,
    dem_scale: float,
    mini_catchments: Path,
    mini_segments: Path,
    continuous_raster: tuple[tuple[str, Path], ...],
    categorical_raster: tuple[tuple[str, Path], ...],
    d8: Path | None,
    d8_encoding: str | None,
    memory_limit_mb: int,
    workers: int,
    io_slots: int,
    output_dir: Path,
) -> None:
    """Prepare explicit mini vectors and aligned raster files as flat COGs."""
    rasters = tuple(
        [NamedRaster(name, path, "continuous") for name, path in continuous_raster]
        + [NamedRaster(name, path, "categorical") for name, path in categorical_raster]
    )
    overwrite = _confirm_replacement(
        output_dir,
        ("dem.tif", "cells.tif", "drainage.tif", "manifest-prepare.json",
         *(f"{raster.name}.tif" for raster in rasters),
         *(("d8.tif",) if d8 is not None else ())),
    )
    report = _run_stage(
        prepare_dataset,
        PreparationSpec(
            dem=dem,
            dem_scale=dem_scale,
            mini_catchments=mini_catchments,
            mini_segments=mini_segments,
            rasters=rasters,
            d8=d8,
            d8_encoding=d8_encoding,
            workers=workers,
            memory_limit_mb=memory_limit_mb,
            io_slots=io_slots,
            output_dir=output_dir,
            overwrite=overwrite,
        )
    )
    for path in report.files:
        click.echo(f"Wrote {path}")
    click.echo(f"Wrote {report.output_dir / 'manifest-prepare.json'}")
    click.echo(f"Prepared {report.raster_count} raster(s)")
    _echo_timings(report.timings)


@main.command("define-roi")
@click.option(
    "--crs",
    required=True,
    help="Output CRS; geographic and projected CRSs are supported.",
)
@click.option(
    "--catchments",
    "catchments_path",
    type=click.Path(exists=True, path_type=Path),
    required=True,
)
@click.option("--catchments-layer")
@click.option("--catchments-source-crs")
@click.option(
    "--segments",
    "segments_path",
    type=click.Path(exists=True, path_type=Path),
    required=True,
)
@click.option("--segments-layer")
@click.option("--segments-source-crs")
@click.option("--outlet-id", "outlet_ids", multiple=True, required=True)
@click.option("--id-col", required=True)
@click.option("--id-down-col", required=True)
@click.option("--strahler-order-col", required=True)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--workers", type=click.IntRange(min=1), default=4, show_default=True
)
@click.option(
    "--memory-limit-mb", type=click.IntRange(min=1), default=4096, show_default=True
)
@click.option("--io-slots", type=click.IntRange(min=1), default=2, show_default=True)
@click.option(
    "--batch-size", type=click.IntRange(min=1), default=10_000, show_default=True
)
def define_roi_command(
    crs: str,
    catchments_path: Path,
    catchments_layer: str | None,
    catchments_source_crs: str | None,
    segments_path: Path,
    segments_layer: str | None,
    segments_source_crs: str | None,
    outlet_ids: tuple[str, ...],
    id_col: str,
    id_down_col: str,
    strahler_order_col: str,
    output_dir: Path,
    workers: int,
    memory_limit_mb: int,
    io_slots: int,
    batch_size: int,
) -> None:
    """Select and normalize an ROI from raw vector providers."""

    overwrite = _confirm_replacement(
        output_dir,
        ("roi_catchments.fgb", "roi_segments.fgb", "manifest-define-roi.json"),
    )
    report = _run_stage(
        define_roi_dataset,
        RoiSpec(
            crs=crs,
            catchments=catchments_path,
            catchments_layer=catchments_layer,
            catchments_source_crs=catchments_source_crs,
            segments=segments_path,
            segments_layer=segments_layer,
            segments_source_crs=segments_source_crs,
            outlet_ids=outlet_ids,
            id_col=id_col,
            id_down_col=id_down_col,
            strahler_order_col=strahler_order_col,
            output_dir=output_dir,
            overwrite=overwrite,
            workers=workers,
            memory_limit_mb=memory_limit_mb,
            io_slots=io_slots,
            batch_size=batch_size,
        )
    )

    click.echo(f"Wrote {report.catchments}")
    click.echo(f"Wrote {report.segments}")
    click.echo(f"Wrote {report.output_dir / 'manifest-define-roi.json'}")
    click.echo(f"Selected {report.segment_count} source pairs")
    _echo_timings(report.timings)


@main.command("aggregate")
@click.option(
    "--roi-catchments",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--roi-segments",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--uparea-min", type=float, required=True)
@click.option("--lmin", type=float, required=True)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--workers", type=click.IntRange(min=1), default=4, show_default=True
)
@click.option(
    "--memory-limit-mb", type=click.IntRange(min=1), default=4096, show_default=True
)
@click.option("--io-slots", type=click.IntRange(min=1), default=2, show_default=True)
@click.option(
    "--batch-size", type=click.IntRange(min=1), default=10_000, show_default=True
)
def aggregate_command(
    roi_catchments: Path,
    roi_segments: Path,
    uparea_min: float,
    lmin: float,
    output_dir: Path,
    workers: int,
    memory_limit_mb: int,
    io_slots: int,
    batch_size: int,
) -> None:
    """Aggregate explicit ROI files into mini-basins."""

    overwrite = _confirm_replacement(
        output_dir,
        ("mini_catchments.fgb", "mini_segments.fgb", "source_to_mini.csv", "manifest-aggregate.json"),
    )
    report = _run_stage(
        aggregate_roi_dataset,
        AggregationSpec(
            roi_catchments=roi_catchments,
            roi_segments=roi_segments,
            uparea_min=uparea_min,
            lmin=lmin,
            output_dir=output_dir,
            overwrite=overwrite,
            workers=workers,
            memory_limit_mb=memory_limit_mb,
            io_slots=io_slots,
            batch_size=batch_size,
        )
    )

    click.echo(f"Wrote {report.mini_catchments}")
    click.echo(f"Wrote {report.mini_segments}")
    click.echo(f"Wrote {report.source_to_mini}")
    click.echo(f"Wrote {report.output_dir / 'manifest-aggregate.json'}")
    _echo_timings(report.timings)


@main.command("terrain-products")
@click.option(
    "--dem",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--cells",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--drainage",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--d8",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Canonical D8 raster; required when --direction-source=d8.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--direction-source",
    type=click.Choice(["dem", "d8"], case_sensitive=False),
    default="dem",
    show_default=True,
)
@click.option("--write-flow-direction", is_flag=True)
@click.option(
    "--agree-sharp",
    type=click.FloatRange(min=0),
    default=80.0,
    show_default=True,
    help="Additional stream-cell incision in DEM elevation units.",
)
@click.option(
    "--agree-smooth",
    type=click.FloatRange(min=0),
    default=8.0,
    show_default=True,
    help="AGREE ramp depth per pixel toward the stream.",
)
@click.option(
    "--agree-buffer",
    type=click.IntRange(min=0),
    default=4,
    show_default=True,
    help="AGREE conditioning radius in raster pixels.",
)
@click.option(
    "--workers", type=click.IntRange(min=1), default=4, show_default=True
)
@click.option(
    "--memory-limit-mb", type=click.IntRange(min=1), default=4096, show_default=True
)
@click.option("--io-slots", type=click.IntRange(min=1), default=2, show_default=True)
def terrain_products_command(
    dem: Path,
    cells: Path,
    drainage: Path,
    d8: Path | None,
    output_dir: Path,
    direction_source: str,
    write_flow_direction: bool,
    agree_sharp: float,
    agree_smooth: float,
    agree_buffer: int,
    workers: int,
    memory_limit_mb: int,
    io_slots: int,
) -> None:
    """Generate flat bounded terrain COG files from explicit inputs."""

    overwrite = _confirm_replacement(
        output_dir,
        ("hand.tif", "ltnd.tif", "manifest-terrain-products.json",
         *(("flow_direction.tif",) if write_flow_direction else ())),
    )
    report = _run_stage(
        create_terrain_dataset,
        TerrainSpec(
            dem=dem,
            mini_ownership=cells,
            drainage=drainage,
            d8=d8,
            output_dir=output_dir,
            overwrite=overwrite,
            direction_source=direction_source.lower(),
            write_flow_direction=write_flow_direction,
            agree_sharp=agree_sharp,
            agree_smooth=agree_smooth,
            agree_buffer=agree_buffer,
            workers=workers,
            memory_limit_mb=memory_limit_mb,
            io_slots=io_slots,
        )
    )

    click.echo(f"Wrote {report.hand}")
    click.echo(f"Wrote {report.ltnd}")
    if report.flow_direction is not None:
        click.echo(f"Wrote {report.flow_direction}")
    click.echo(f"Wrote {report.output_dir / 'manifest-terrain-products.json'}")
    click.echo(f"Processed {report.mini_count} complete minis")
    click.echo(f"Cells: {report.owned_cells} owned, {report.drainage_cells} drainage")
    _echo_timings(report.timings)
    if report.negative_hand_cells:
        click.echo(
            f"Negative HAND: {report.negative_hand_cells} cells, "
            f"range {report.negative_hand_min:g} to {report.negative_hand_max:g} m"
        )
    else:
        click.echo("Negative HAND: 0 cells")


@main.command("sample-minis")
@click.option(
    "--mini-catchments",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--mini-segments",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--dem",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--cells",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--drainage",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--hand",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--ltnd",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--hru",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--output-dir", type=click.Path(file_okay=False, path_type=Path), required=True
)
@click.option(
    "--workers", type=click.IntRange(min=1), default=4, show_default=True
)
@click.option(
    "--memory-limit-mb", type=click.IntRange(min=1), default=4096, show_default=True
)
@click.option("--io-slots", type=click.IntRange(min=1), default=2, show_default=True)
@click.option(
    "--batch-size", type=click.IntRange(min=1), default=10_000, show_default=True
)
def sample_minis_command(
    mini_catchments: Path,
    mini_segments: Path,
    dem: Path,
    cells: Path,
    drainage: Path,
    hand: Path,
    ltnd: Path,
    hru: Path,
    output_dir: Path,
    workers: int,
    memory_limit_mb: int,
    io_slots: int,
    batch_size: int,
) -> None:
    """Sample explicit canonical rasters and mini vectors into one CSV."""
    overwrite = _confirm_replacement(
        output_dir,
        (
            "sampled_minis.csv",
            "manifest-sample-minis.json",
            *NODATA_REPORT_FILENAMES,
        ),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", _SamplingNodataWarning)
        try:
            report = _run_stage(
                sample_minibasins,
                MiniSamplingSpec(
                    mini_catchments=mini_catchments,
                    mini_segments=mini_segments,
                    dem=dem,
                    mini_ownership=cells,
                    drainage=drainage,
                    hand=hand,
                    ltnd=ltnd,
                    hru=hru,
                    output_dir=output_dir,
                    overwrite=overwrite,
                    workers=workers,
                    memory_limit_mb=memory_limit_mb,
                    io_slots=io_slots,
                    batch_size=batch_size,
                )
            )
        finally:
            for value in caught:
                if isinstance(value.message, _SamplingNodataWarning):
                    click.echo(f"RuntimeWarning: {value.message}", err=True)
                else:
                    stream = value.file or sys.stderr
                    stream.write(
                        warnings.formatwarning(
                            value.message,
                            value.category,
                            value.filename,
                            value.lineno,
                            value.line,
                        )
                    )
    click.echo(f"Wrote {report.sampled_minis}")
    click.echo(f"Wrote {report.output_dir / 'manifest-sample-minis.json'}")
    for path in report.nodata_reports:
        click.echo(f"Wrote {path}")
    click.echo(
        f"Sampled {report.mini_count} minis; "
        f"{report.catchment_cells} catchment cells and "
        f"{report.reach_cells} reach cells"
    )
    click.echo(
        f"HRU classes ({len(report.hru_class_ids)}): "
        + ", ".join(str(value) for value in report.hru_class_ids)
    )
    _echo_timings(report.timings)


if __name__ == "__main__":
    main()
