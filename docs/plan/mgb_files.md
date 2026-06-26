# MGB Files

This step will write the final files needed to run an MGB simulation.

## Purpose

Final MGB file generation should be a formatting and consistency step. It should consume attributed mini-basin data from the sampling workflow, apply required ordering and parameter formulas, and write the simulation input files.

## Planned Inputs

- Attributed mini-basin catchments and reaches from mini sampling.
- HRU percentage attributes.
- Geomorphological relation parameters:
  - `a`
  - `b`
  - `c`
  - `d`
- Slope limits:
  - `smin`
  - `smax`
- Manning coefficient:
  - `nman`

## Planned Outputs

- `MINI.gtp`
- `COTA_AREA.flp`
- `minis_mgb.<ext>`

## Planned Responsibilities

- Compute final MGB mini ordering and downstream mini references.
- Apply geomorphological width and depth formulas.
- Clamp slope attributes to configured limits.
- Format `MINI.gtp` and `COTA_AREA.flp`.
- Write a vector mini-basin product with the final simulation attributes.

This step should not perform raster terrain processing, generate HRU classes, or sample raster attributes.

## Legacy Mapping

Legacy responsibilities that belong here include:

- final ordering and text formatting from `write_mini`.
- flood elevation-area text generation from `write_cota_area`.
- final `minis_mgb` vector writing.

The new implementation should keep file formatting deterministic and testable, while allowing earlier steps to evolve independently.
