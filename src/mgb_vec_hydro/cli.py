from __future__ import annotations

from pathlib import Path

import click

from mgb_vec_hydro.exceptions import MgbVecHydroError
from mgb_vec_hydro.io import output_paths, read_vector, write_vector
from mgb_vec_hydro.roi import define_roi
from mgb_vec_hydro.topology import resolve_column_name


@click.group()
def main() -> None:
    """MGB vector hydrography preprocessing tools."""


@main.command("define-roi")
@click.option(
    "--catchments",
    "catchments_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--segments",
    "segments_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--outlet-id", "outlet_ids", multiple=True, required=True)
@click.option("--id-col", default="id", show_default=True)
@click.option("--id-down-col", default="id_down", show_default=True)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
)
@click.option("--output-format", default="fgb", show_default=True)
def define_roi_command(
    catchments_path: Path,
    segments_path: Path,
    outlet_ids: tuple[str, ...],
    id_col: str,
    id_down_col: str,
    output_dir: Path,
    output_format: str,
) -> None:
    """Define ROI catchments and segments from explicit network topology."""

    try:
        paths = output_paths(output_dir, output_format)
        catchments = read_vector(catchments_path)
        segments = read_vector(segments_path)
        segment_id_col = resolve_column_name(segments, id_col)
        coerced_outlet_ids = _coerce_outlet_ids(outlet_ids, segments[segment_id_col])
        roi = define_roi(
            catchments,
            segments,
            outlet_ids=coerced_outlet_ids,
            id_col=id_col,
            id_down_col=id_down_col,
        )
        write_vector(roi.catchments, paths.catchments, output_format=output_format)
        write_vector(roi.segments, paths.segments, output_format=output_format)
    except MgbVecHydroError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Wrote {paths.catchments}")
    click.echo(f"Wrote {paths.segments}")


def _coerce_outlet_ids(outlet_ids: tuple[str, ...], segment_id_series):
    """Convert CLI outlet strings to the dtype used by the segment ID column."""

    dtype = segment_id_series.dtype
    if dtype.kind in {"i", "u"}:
        return [int(value) for value in outlet_ids]
    if dtype.kind == "f":
        return [float(value) for value in outlet_ids]
    return list(outlet_ids)


if __name__ == "__main__":
    main()
