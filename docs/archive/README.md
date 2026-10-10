# Archive — evidence, not specification

Moved here on 2026-10-10 because they no longer describe the current
architecture. Current behavior is in [modules](../refactor/modules.md) and
the [pipeline](../refactor/orchestration.md); open work is in
[TODO](../refactor/TODO.md). Where an archived statement disagrees with the
code or those references, they win. Read a file here only to answer an
evidence question; code comments and TODO link to the sections they rely on.

| File | What it holds |
|---|---|
| [reconstruction.md](reconstruction.md) | Winding-number reconstruction: the experiment, measurements against alpha-wrap, block count, the module memory model's calibration (2026-10-03), the decimator switch to PyMeshLab (2026-10-04), memory-floor and malloc investigations (2026-10-06/07). Its open items moved to TODO. |
| [errors/](errors/README.md) | Errors from the 2026-10-04 full-collection run, one file per kind, with causes, fixes and experiment scripts (`tools/experiments/`). Open items (volume guard on open shells, MeshFix time/NM guard, side issues) moved to TODO. |
| [REFACTOR_DECISIONS.md](REFACTOR_DECISIONS.md) | Former root pointer to the D1–D27 decision records (those are in the outside archive). |
| [root-TODO.md](root-TODO.md) | Former root pointer to the live TODO. |

Older history (designs, handoffs, reviews before 2026-09-26) stays in the
[outside archive](../../../stl-batch-fix.old/archive/README.md).
