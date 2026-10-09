# Remaining capabilities

The project implements [ROI selection](../../docs/stage1_roi_cli.md),
[mini-basin aggregation](../../docs/stage2_aggregation_cli.md),
[raster preparation](../../docs/stage3_prepare_data.md),
[terrain products](../../docs/stage4_terrain_cli.md), and
[mini-basin sampling](../../docs/stage5_mini_sampling_cli.md).

Two capabilities remain unimplemented:

| Capability | Intended result |
| --- | --- |
| [HRU mapping](hru_mapping.md) | HRU classes derived from terrain and land-cover inputs. |
| [MGB file generation](mgb_files.md) | Final simulation files from attributed mini-basins and model parameters. |

These documents describe intended capabilities, not frozen implemented
contracts. Unsettled scientific definitions and file details are identified
in each document. Active implementation plans belong in `.dev/` and are
removed once applied.
