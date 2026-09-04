from __future__ import annotations

from pathlib import Path

import click

from mgb_vec_hydro.aggregation import AggregationSpec, aggregate_roi_dataset
from mgb_vec_hydro.crs_utils import DEFAULT_CRS
from mgb_vec_hydro.exceptions import MgbVecHydroError
from mgb_vec_hydro.io import read_vector
from mgb_vec_hydro.preparation import NamedRaster, PreparationSpec, prepare_dataset
from mgb_vec_hydro.roi import RoiSpec, define_roi_dataset
from mgb_vec_hydro.sampling import sample_minibasins
from mgb_vec_hydro.terrain import TerrainSpec, create_terrain_dataset


@click.group()
def main() -> None:
    """MGB vector hydrography preprocessing tools."""


_NAMED_RASTER = click.Tuple(
    [click.STRING, click.Path(exists=True, dir_okay=False, path_type=Path)]
)


@main.command("prepare")
@click.option(
    "--dem",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--minis",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
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
    default=512,
    show_default=True,
)
@click.option("--buffer-cells", type=click.IntRange(min=0), default=1, show_default=True)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
)
def prepare_command(
    dem: Path,
    minis: Path,
    continuous_raster: tuple[tuple[str, Path], ...],
    categorical_raster: tuple[tuple[str, Path], ...],
    d8: Path | None,
    d8_encoding: str | None,
    memory_limit_mb: int,
    buffer_cells: int,
    output_dir: Path,
) -> None:
    """Stage a canonical grid and COG raster inputs."""
    rasters = tuple(
        [NamedRaster(name, path, "continuous") for name, path in continuous_raster]
        + [NamedRaster(name, path, "categorical") for name, path in categorical_raster]
    )
    try:
        report = prepare_dataset(
            PreparationSpec(
                dem=dem,
                minis=minis,
                rasters=rasters,
                d8=d8,
                d8_encoding=d8_encoding,
                memory_limit_mb=memory_limit_mb,
                buffer_cells=buffer_cells,
                output_dir=output_dir,
            )
        )
    except MgbVecHydroError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Wrote {report.manifest}")
    click.echo(f"Prepared {report.raster_count} raster(s)")


@main.command("define-roi")
@click.option("--crs", required=True, help="Output CRS; geographic and projected CRSs are supported.")
@click.option(
    "--catchments",
    "catchments_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--catchments-layer")
@click.option("--catchments-source-crs")
@click.option(
    "--segments",
    "segments_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
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
    "--workers", type=click.IntRange(min=1, max=4), default=4, show_default=True
)
@click.option(
    "--memory-limit-mb", type=click.IntRange(min=1), default=512, show_default=True
)
@click.option("--io-slots", type=click.IntRange(min=1), default=2, show_default=True)
@click.option(
    "--batch-size", type=click.IntRange(min=1), default=10_000, show_default=True
)
@click.option("--checkpoint-dir", type=click.Path(file_okay=False, path_type=Path))
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
    checkpoint_dir: Path | None,
) -> None:
    """Select and normalize an ROI from raw vector providers."""

    try:
        report = define_roi_dataset(
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
                workers=workers,
                memory_limit_mb=memory_limit_mb,
                io_slots=io_slots,
                batch_size=batch_size,
                checkpoint_dir=checkpoint_dir,
            )
        )
    except MgbVecHydroError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Wrote {report.manifest}")
    click.echo(f"Selected {report.segment_count} source pairs")


@main.command("aggregate")
@click.option(
    "--roi",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
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
    "--workers", type=click.IntRange(min=1, max=4), default=4, show_default=True
)
@click.option(
    "--memory-limit-mb", type=click.IntRange(min=1), default=512, show_default=True
)
@click.option("--io-slots", type=click.IntRange(min=1), default=2, show_default=True)
@click.option(
    "--batch-size", type=click.IntRange(min=1), default=10_000, show_default=True
)
@click.option("--checkpoint-dir", type=click.Path(file_okay=False, path_type=Path))
def aggregate_command(
    roi: Path,
    uparea_min: float,
    lmin: float,
    output_dir: Path,
    workers: int,
    memory_limit_mb: int,
    io_slots: int,
    batch_size: int,
    checkpoint_dir: Path | None,
) -> None:
    """Aggregate a versioned ROI into mini-basins."""

    try:
        report = aggregate_roi_dataset(
            AggregationSpec(
                roi=roi,
                uparea_min=uparea_min,
                lmin=lmin,
                output_dir=output_dir,
                workers=workers,
                memory_limit_mb=memory_limit_mb,
                io_slots=io_slots,
                batch_size=batch_size,
                checkpoint_dir=checkpoint_dir,
            )
        )
    except MgbVecHydroError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Wrote {report.output_dir / 'mini_catchments.fgb'}")
    click.echo(f"Wrote {report.output_dir / 'mini_segments.fgb'}")
    click.echo(f"Wrote {report.output_dir / 'source_to_mini.csv'}")


@main.command("terrain-products")
@click.option(
    "--prepared",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
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
    "--workers", type=click.IntRange(min=1, max=4), default=4, show_default=True
)
@click.option(
    "--memory-limit-mb", type=click.IntRange(min=1), default=512, show_default=True
)
@click.option("--io-slots", type=click.IntRange(min=1), default=2, show_default=True)
@click.option(
    "--batch-size", type=click.IntRange(min=1), default=10_000, show_default=True
)
@click.option("--checkpoint-dir", type=click.Path(file_okay=False, path_type=Path))
def terrain_products_command(
    prepared: Path,
    output_dir: Path,
    direction_source: str,
    write_flow_direction: bool,
    agree_sharp: float,
    agree_smooth: float,
    agree_buffer: int,
    workers: int,
    memory_limit_mb: int,
    io_slots: int,
    batch_size: int,
    checkpoint_dir: Path | None,
) -> None:
    """Generate bounded mini-confined terrain products."""

    try:
        report = create_terrain_dataset(
            TerrainSpec(
                prepared=prepared,
                output_dir=output_dir,
                direction_source=direction_source.lower(),
                write_flow_direction=write_flow_direction,
                agree_sharp=agree_sharp,
                agree_smooth=agree_smooth,
                agree_buffer=agree_buffer,
                workers=workers,
                memory_limit_mb=memory_limit_mb,
                io_slots=io_slots,
                batch_size=batch_size,
                checkpoint_dir=checkpoint_dir,
            )
        )
    except MgbVecHydroError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Wrote {report.manifest}")
    click.echo(f"Processed {report.mini_count} complete minis")
    click.echo(f"Cells: {report.owned_cells} owned, {report.drainage_cells} drainage")
    click.echo(
        "Timing: "
        + ", ".join(
            f"{name} {seconds:.3f}s" for name, seconds in report.timings.items()
        )
    )
    if report.negative_hand_cells:
        click.echo(
            f"Negative HAND: {report.negative_hand_cells} cells, "
            f"range {report.negative_hand_min:g} to {report.negative_hand_max:g} m"
        )
    else:
        click.echo("Negative HAND: 0 cells")


@main.command("sample-minis")
@click.option(
    "--catchments",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--segments",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--dem", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True
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
    "--hru", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True
)
@click.option(
    "--output-dir", type=click.Path(file_okay=False, path_type=Path), required=True
)
@click.option("--crs", default=DEFAULT_CRS, show_default=True)
def sample_minis_command(
    catchments: Path,
    segments: Path,
    dem: Path,
    hand: Path,
    ltnd: Path,
    hru: Path,
    output_dir: Path,
    crs: str,
) -> None:
    """Sample terrain and HRU attributes onto mini-basins."""
    try:
        result = sample_minibasins(
            read_vector(catchments),
            read_vector(segments),
            dem,
            hand,
            ltnd,
            hru,
            crs=crs,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "sampled_minis.csv"
        result.sampled_minis.to_csv(output, index=False)
    except MgbVecHydroError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Wrote {output}")
    click.echo(
        f"Sampled {result.diagnostics['minis']} minis; "
        f"{result.diagnostics['catchment_cells']} catchment cells and "
        f"{result.diagnostics['reach_cells']} reach cells"
    )
    click.echo(
        f"HRU classes ({result.diagnostics['hru_class_count']}): "
        + ", ".join(str(value) for value in result.diagnostics["hru_class_ids"])
    )


if __name__ == "__main__":
    main()
