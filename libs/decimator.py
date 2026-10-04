"""Reduce a loaded mesh to a face budget with PyMeshLab quadric edge collapse.

PyMeshLab's `meshing_decimation_quadric_edge_collapse` with its default
parameters (owner decision 2026-10-04): fast_simplification destroyed thin
features on reconstructed output, turned spheres oblong and stalled far
above extreme targets; evidence in docs/refactor/reconstruction.md,
"Decimation after reconstruction"."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from . import meshlab
from .mesh_io import Geometry, Mesh, require_geometry

class Rung(Enum):
    """Whether the required decimator ran, was unnecessary, or failed."""

    MESHLAB = 'meshlab'
    NOT_NEEDED = 'not_needed'        # already within budget; nothing ran
    FAILED = 'failed'                # the decimator failed


@dataclass(frozen=True)
class Result:
    """What decimation did, beside the mesh itself.

    mesh        the decimated mesh — or the input unchanged, when nothing ran
    rung        which implementation produced it
    faces_in    face count before
    faces_out   face count after
    attempts    the required decimator failure, when one occurred
    """

    mesh: Mesh
    rung: Rung
    faces_in: int
    faces_out: int
    attempts: tuple[tuple[Rung, str], ...] = ()

    @property
    def ran(self) -> bool:
        """True when a decimator actually changed the mesh."""
        return self.rung not in (Rung.NOT_NEEDED, Rung.FAILED)


def is_available() -> bool:
    """Whether the required decimator is installed.

    For the startup check.  Decimation is a deliverable rather than an
    optimisation — a run that cannot decimate produces meshes the printer will
    re-decimate on its own, reintroducing the defects this tool exists to
    remove — so a false here is a reason not to start, not a reason to skip.
    """
    return meshlab.is_available()


def available_rungs() -> tuple[Rung, ...]:
    """Report whether the required decimator is available."""
    return (Rung.MESHLAB,) if meshlab.is_available() else ()


def _decimate_meshlab(mesh: Mesh, max_faces: int) -> Geometry:
    """PyMeshLab quadric edge collapse to `max_faces`, every other parameter
    at its default (optimal vertex placement, quality threshold 0.3, no
    topology/boundary constraints, autoclean)."""
    return meshlab.apply_filters(
        mesh, (('meshing_decimation_quadric_edge_collapse',
                {'targetfacenum': int(max_faces)}),)).geometry


def decimate(mesh: Mesh, max_faces: int) -> Result:
    """Reduce `mesh` to at most `max_faces` faces.

    Takes a loaded mesh and returns a loaded mesh, without touching the disk.
    The selected rung or failure is reported in the result.

    `max_faces <= 0` disables decimation, matching the old `MAX_FACES = 0`.
    A mesh already within budget is returned untouched rather than round
    tripped through a decimator that would have nothing to do.

    Raises `ValueError` if the mesh is not loaded — that is a programming error
    at the call site, not a property of the data.
    """
    require_geometry(mesh)

    faces_in = len(mesh.geometry.faces)
    if max_faces <= 0 or faces_in <= max_faces:
        return Result(mesh, Rung.NOT_NEEDED, faces_in, faces_in)

    if not meshlab.is_available():
        return Result(mesh, Rung.FAILED, faces_in, faces_in,
                      ((Rung.MESHLAB,
                        'PyMeshLab is not installed'),))

    try:
        geometry = _decimate_meshlab(mesh, max_faces)
    except Exception as exc:
        return Result(mesh, Rung.FAILED, faces_in, faces_in,
                      ((Rung.MESHLAB,
                        f"{type(exc).__name__}: {exc}"),))
    return Result(mesh.with_geometry(geometry), Rung.MESHLAB,
                  faces_in, len(geometry.faces))


def _detail_for(result: Result) -> str:
    """The uniform step's own detail string for one `decimate()` result."""
    if result.rung is Rung.FAILED:
        attempts = '; '.join(f'{rung.value}: {why}' for rung, why in result.attempts)
        return f'decimate to {result.faces_in}f failed ({attempts})'
    return f'{result.rung.value}, {result.faces_out} faces out'


def make_step(sink: list[Result] | None = None
             ) -> Callable[[Mesh, object], tuple[bool, Mesh, str]]:
    """Build the uniform step `(mesh, config) -> (ok, mesh, detail)` for
    decimation, reading its target from `config.faceCount`.

    Every place decimation runs in the pipeline — the CLI's initial
    whole-mesh pass in `processor.process` and each part's post-reconstruction
    `decimate` entry in `repairer.DEFAULT_PART_STEPS` — uses this same
    function, so every pass guards, decimates and logs identically. The guard
    is `decimate`'s own: a mesh already within `faceCount` (or a target of 0)
    is returned unchanged as `not_needed` without calling the library.

    `sink`, when given, receives the rich `decimator.Result` for this call
    (exactly one `append` per call) — for a caller that needs more than the
    step's own `(ok, mesh, detail)` triple, such as `processor.process`
    building its `Outcome.decimation` field. `sink=None` (the default)
    retains nothing anywhere: no module state, no per-call list a caller
    forgot to drain — the closure created by one call to `make_step()` is
    the only place any state could live, and by default there is none. Two
    independent calls to `make_step()` never share anything, whether or not
    either is given a sink.
    """
    def step(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
        face_count = getattr(config, 'faceCount', 0) if config is not None else 0
        result = decimate(mesh, face_count)
        if sink is not None:
            sink.append(result)
        if result.rung is Rung.FAILED:
            return False, mesh, _detail_for(result)
        return True, result.mesh, _detail_for(result)
    return step

