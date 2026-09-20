# CLI, TUI, and configuration

Preserve the legacy user interface while replacing its internals with the shared runner. The legacy script and its potentially stale design document must not drive geometry implementation.

## CLI

- Default: recursively process an input folder into sibling `Fixed/`, preserving relative paths.
- Options: input, suffix, recursive mode, workers, file/part timeouts, face budget, merge distance, minimum layer, bounds tolerance, and Blender reserve.
- Modes: batch, statistics-only, and supported single-file diagnostics. `--is-part` and `--result-fd` are internal child details.
- Behavior: live progress, one final status per source, marker-based restart, nonzero exit for actionable failures, and Ctrl+C cleanup.

## TUI and `.fixcfg`

`run.sh` launches `stl_batch_fix_tui.py`. The TUI finds `*.fixcfg` beside the script, creates `default.fixcfg` when absent, lets the user edit it, then displays progress. Multiple files prompt for selection.

| Key | Meaning |
|---|---|
| `INPUT_FOLDER` | Source STL/OBJ tree |
| `OUTPUT_SUFFIX` | Text appended to output stems |
| `MERGE_DIST` | Legacy merge setting; do not reuse for scale-sensitive repair without evidence |
| `MIN_LAYER` | Open-loop printability threshold; policy remains open |
| `BBOX_TOLERANCE_PCT` | Bounds warning relative to model diagonal |
| `WORKERS` | Parallel file workers; `0` means automatic |
| `TIMEOUT_PART` | Budget for one mesh/part |
| `TIMEOUT` | Whole split-file ceiling; `0` means practical 24-hour cap |
| `BLENDER_RESERVE_PCT` | Part budget reserved for work after Blender |
| `MAX_FACES` | Final per-file slicer budget; `0` disables decimation |
| `RECURSIVE` | Walk subdirectories |

The target has one `RunConfig` shared by CLI, TUI, parent, and child. The TUI observes structured events; it must not create another pool or parse logs as state.
