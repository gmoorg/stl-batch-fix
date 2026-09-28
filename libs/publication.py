"""Every path a job could publish to, and whether that set is safe to use."""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import indicators
from .mesh_io import Mesh


def expected_paths(destination: str) -> frozenset[str]:
    """The destination plus every marker path this project could write for it.

    Used both to detect a collision between two jobs before either is
    dispatched, and to detect — after a crash — whether exactly one of
    these paths newly exists, which is what makes a recovered outcome
    trustworthy without guessing.
    """
    base, _ = os.path.splitext(destination)
    markers = {base + suffix for suffix, _ in indicators._OUTPUT_MARKERS}
    return frozenset(markers | {destination})


def find_collisions(meshes: list[Mesh]) -> dict[str, list[Mesh]]:
    """Group meshes whose expected-path sets intersect another's.

    Returns `{path: [owning meshes]}` for every path claimed by more than
    one mesh.  A caller rejects every mesh appearing in any group's value
    list — no "first seen wins", since walk order is not guaranteed
    deterministic (`converter.prepare`'s `os.walk` makes no such promise).
    """
    owners: dict[str, list[Mesh]] = {}
    for mesh in meshes:
        for path in expected_paths(mesh.destination):
            owners.setdefault(path, []).append(mesh)
    return {path: group for path, group in owners.items() if len(group) > 1}


def preexisting_paths(mesh: Mesh) -> frozenset[str]:
    """Which of `mesh`'s expected paths already exist on disk, right now."""
    return frozenset(p for p in expected_paths(mesh.destination) if os.path.exists(p))


@dataclass(frozen=True)
class Reconciliation:
    """What the filesystem shows for a job whose child produced no trusted result.

    kind    'RECOVERED' (exactly one new path — trust it), 'INCONSISTENT'
            (two or more new paths — do not add a synthetic marker to an
            already-inconsistent set), or 'NOTHING' (zero new paths —
            genuinely nothing was published)
    path    the one new path, only when kind == 'RECOVERED'
    detail  every new path, only when kind == 'INCONSISTENT'
    """

    kind: str
    path: str | None = None
    detail: tuple[str, ...] = ()


def reconcile(mesh: Mesh, baseline: frozenset[str]) -> Reconciliation:
    """Compare `mesh`'s current expected paths against a pre-dispatch snapshot.

    `baseline` is the set of `mesh`'s expected paths that already existed
    *before* this run dispatched it — normally empty, since preflight
    rejects any job with a pre-existing path.  Only paths absent from
    `baseline` but present now count as "new" — evidence this run's own
    child produced them, not stale leftovers from before the run started.
    """
    now = preexisting_paths(mesh)
    new = now - baseline
    if not new:
        return Reconciliation('NOTHING')
    if len(new) > 1:
        return Reconciliation('INCONSISTENT', detail=tuple(sorted(new)))
    return Reconciliation('RECOVERED', path=next(iter(new)))


def marker_indicator_for_path(destination: str, path: str) -> indicators.Indicator:
    """The `Indicator` a recovered `path` implies, given `mesh.destination`.

    `path == destination` means the plain repaired output — `PROCESS`.
    Anything else is one of the marker suffixes.
    """
    if path == destination:
        return indicators.Indicator.PROCESS
    base, _ = os.path.splitext(destination)
    for suffix, indicator in indicators._OUTPUT_MARKERS:
        if path == base + suffix:
            return indicator
    raise ValueError(f'{path!r} is not an expected path for {destination!r}')
