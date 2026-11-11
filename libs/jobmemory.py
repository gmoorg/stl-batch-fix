"""Memory a repair job will need, for batch admission.

A job runs in two child processes (docs/refactor/orchestration.md):

- **prepare** loads the model, runs the initial decimation, saves the result
  and plans every part. Its own reservation (`prepare_bytes`) can only use
  what the parent knows before it runs: the source triangle count.
- **repair** rebuilds every part. Its reservation (`repair_bytes`) is
  computed by the prepare child on the mesh repair will actually load — the
  reloaded cache file, split exactly as `repairer.repair` splits it — so it
  sees the real parts, the real grid spacing and the real block plans.

Both are calibrated sizing estimates fitted to measured runs, not proven
upper bounds; the measurements are in docs/refactor/orchestration.md,
"Job memory". What drives a repair job's peak, from those measurements:

- the winding reconstruction of one part, which `winding.plan` already
  estimates and keeps within `reconstruct_memory_budget_gb` by choosing a
  block count — so it can be anywhere up to that budget;
- the PyMeshLab decimation of that part's rebuilt surface, which nothing
  else bounds: about 3 rebuilt faces per A/h² (A the part's area, h the grid
  spacing) and a few hundred bytes per rebuilt face. A 6,160-face sphere of
  radius 132 rebuilt to 29 M faces and peaked at 13.9 GB here;
- MeshFix on the decimated part, counted on the same rebuilt-face bound
  because decimation is best-effort and can stop above its target.

With `--alpha`, alpha-wrap replaces the winding reconstruction: no block
budget bounds it, and its peak and output size follow both A/alpha² and
the part's own face count (constants below).

Phases run one after another, so a part's peak is the largest of them, plus
what is resident at that moment (the whole model and every part finished so
far); the job's peak is the largest part peak, never a sum over parts.
"""

from __future__ import annotations

import numpy as np

from . import pipeconfig, scanner, splitter, winding
from .mesh_io import Mesh, require_geometry

# Constants: see docs/refactor/orchestration.md, "Job memory", for the runs
# they were fitted to and the margin each keeps.

#: Interpreter, NumPy/SciPy/PyMeshLab/igl imports and a loaded process.
CHILD_BASE_BYTES = 400_000_000
#: One face of a loaded, indexed mesh kept resident (float64 vertices, int64
#: faces, scans and copies made around it). Kept after the float64 move:
#: re-measured peaks rose 0-4 % (orchestration.md, "Job memory").
RESIDENT_BYTES_PER_FACE = 120
#: Rebuilt faces per unit of A/h². Measured 2.6-3.2 on closed parts.
REBUILT_FACES_PER_UNIT = 3.5
#: PyMeshLab quadric decimation, per input face.
DECIMATE_BYTES_PER_FACE = 650
#: MeshFix, per input face.
MESHFIX_BYTES_PER_FACE = 700
#: The prepare child, per source face: load, PyMeshLab initial decimation,
#: cache write and reload, split and per-part planning.
PREPARE_BYTES_PER_FACE = 700

# Alpha-wrap (`batch_repair.py --alpha`). It cannot be split into blocks, so
# no budget bounds it. Both its peak and its output follow two things: the
# wrap's size, A/alpha^2 ("units"), and the part's own face count. The peak
# was in the wrap step on every model measured (orchestration.md, "Job
# memory", alpha-wrap table, 2026-10-10).
#: Wrapped faces per unit of A/alpha^2 (measured ~1.38) ...
ALPHA_FACES_PER_UNIT = 1.6
#: ... plus per input face of the part (measured ~1.0).
ALPHA_FACES_PER_INPUT_FACE = 1.2
#: The wrap step, per unit of A/alpha^2 (measured 1.58-1.66 KB).
ALPHA_BYTES_PER_UNIT = 2_150
#: The wrap step, per input face of the part: an empirical allowance (the
#: fit gives ~390 B/face for the whole process, ~270 B beyond the resident
#: model); 500 keeps a mostly fixed-cost model (sword) above 1.25x.
ALPHA_BYTES_PER_INPUT_FACE = 500


def prepare_bytes(source_faces: int) -> int:
    """Reservation for the prepare child of a model with `source_faces`."""
    return int(CHILD_BASE_BYTES + PREPARE_BYTES_PER_FACE * max(int(source_faces), 0))


def _area(mesh: Mesh) -> float:
    V = mesh.geometry.verts
    F = mesh.geometry.faces
    if len(F) == 0:
        return 0.0
    cross = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    return float(np.linalg.norm(cross, axis=1).sum() / 2)


def _reconstruction_bytes(part: Mesh, h: float, budget_bytes: int,
                          ceiling_bytes: int | None) -> int:
    """`winding.plan`'s own estimate; for a part that fits no block count,
    the budget it will fail against (it still plans, then fails)."""
    try:
        return winding.plan(part, h, budget_bytes, ceiling_bytes).estimate_bytes
    except winding.BudgetError as error:
        return error.budget
    except (ValueError, winding.EmptyResult):
        return 0


def _alpha_wrap(part: Mesh, alpha: float) -> tuple[int, int]:
    """(the wrap step's bytes, faces it outputs) for one part."""
    units = _area(part) / alpha ** 2
    faces = int(part.triangles)
    return (int(ALPHA_BYTES_PER_INPUT_FACE * faces + ALPHA_BYTES_PER_UNIT * units),
            int(ALPHA_FACES_PER_UNIT * units + ALPHA_FACES_PER_INPUT_FACE * faces))


def repair_bytes(mesh: Mesh, min_shell_faces: int, reconstruct_budget_bytes: int,
                 reconstruction: str = 'winding',
                 reconstruct_ceiling_bytes: int | None = None) -> int:
    """Reservation for the repair child that will load `mesh`.

    Mirrors `repairer.repair`'s path for `reconstruction`: diagonal of the
    whole mesh, shell split with the same floor, then the reconstruction
    (winding, planned with the same budget and ceiling as the step, or
    alpha-wrap, which ignores both),
    decimation and MeshFix per part. `skip_clean` is ignored: a gated mesh
    needs less, never more.
    """
    if reconstruction not in ('winding', 'alpha-wrap'):
        raise ValueError(f'unknown reconstruction {reconstruction!r}')
    require_geometry(mesh)
    faces = int(mesh.triangles)
    resident = RESIDENT_BYTES_PER_FACE * faces
    peak = CHILD_BASE_BYTES + resident                    # load, scan, split
    diag = scanner.diagonal(mesh)
    if not np.isfinite(diag) or diag <= 0:
        return int(peak)
    h = winding.grid_spacing(diag)
    alpha, _ = pipeconfig.alpha_wrap_parameters(diag)
    finished = 0
    for part in splitter.by_shells(mesh, min_faces=min_shell_faces):
        if reconstruction == 'alpha-wrap':
            rebuild_bytes, rebuilt = _alpha_wrap(part, alpha)
        else:
            rebuilt = int(REBUILT_FACES_PER_UNIT * _area(part) / h ** 2)
            rebuild_bytes = _reconstruction_bytes(part, h, reconstruct_budget_bytes,
                                                  reconstruct_ceiling_bytes)
        part_peak = max(rebuild_bytes,
                        DECIMATE_BYTES_PER_FACE * rebuilt,
                        MESHFIX_BYTES_PER_FACE * rebuilt)
        peak = max(peak, CHILD_BASE_BYTES + resident
                   + RESIDENT_BYTES_PER_FACE * finished + part_peak)
        finished += rebuilt
    # Merge and the closing scans hold the model and every finished part.
    peak = max(peak, CHILD_BASE_BYTES + resident + 2 * RESIDENT_BYTES_PER_FACE * finished)
    return int(peak)
