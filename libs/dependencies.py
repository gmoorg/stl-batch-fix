"""The libraries the batch needs, checked once when the run starts.

Library modules import their packages plainly, so a missing required package
already fails when `batch_repair` loads them. This check then reports every
library in one place, one line each: a debug line when it is available or an
optional one is missing, an error line when a required one is missing (the
run then stops). Nothing later in the run handles a missing library: a step
never reports one (owner, 2026-10-05).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable


def importable(*modules: str) -> Callable[[], None]:
    """A probe that imports each module; an import error is the answer."""
    def probe() -> None:
        for name in modules:
            importlib.import_module(name)
    return probe


#: (name, required, probe). CGAL is optional: only alpha-wrap uses it, and a
#: run uses alpha-wrap only when started with `--alpha`; that run passes
#: 'CGAL' in `check_all`'s `also_required`. manifold3d is optional for the
#: batch: only `support_3mf.py --layout grid` uses it, and checks it itself
#: at start (`check_one`).
LIBRARIES: tuple[tuple[str, bool, Callable[[], None]], ...] = (
    ('NumPy', True, importable('numpy')),
    ('SciPy', True, importable('scipy')),
    ('libigl', True, importable('igl')),
    ('PyMeshLab', True, importable('pymeshlab')),
    ('PyMeshFix', True, importable('pymeshfix')),
    ('CGAL', False, importable('CGAL.CGAL_Alpha_wrap_3', 'CGAL.CGAL_Kernel',
                               'CGAL.CGAL_Polyhedron_3')),
    ('manifold3d', False, importable('manifold3d')),
)


def check_library(name: str, required: bool, probe: Callable[[], None],
                  log: Callable[[str], None]) -> bool:
    """Run `probe`, log the outcome, and return whether `name` is available.

    Only the probe's exceptions mean "unavailable"; a failing `log` is not
    caught.
    """
    try:
        probe()
    except Exception as exc:
        problem = f'{type(exc).__name__}: {exc}'
    else:
        log(f'[debug] {name}: available')
        return True
    if required:
        log(f'[error] {name} is required but unavailable: {problem}')
    else:
        log(f'[debug] {name} (optional) unavailable: {problem}')
    return False


def check_one(name: str, log: Callable[[str], None]) -> bool:
    """Check one library from `LIBRARIES` as required; return whether available."""
    for known, _, probe in LIBRARIES:
        if known == name:
            return check_library(name, True, probe, log)
    raise ValueError(f'unknown library: {name}')


def check_all(log: Callable[[str], None],
              also_required: tuple[str, ...] = ()) -> list[str]:
    """Check every library in `LIBRARIES`; return the missing required ones.

    `also_required` names optional libraries this run needs (e.g. CGAL for
    alpha-wrap); they are checked and reported as required. A name not in
    `LIBRARIES` raises ValueError: it would otherwise never be checked.
    """
    unknown = set(also_required) - {name for name, _, _ in LIBRARIES}
    if unknown:
        raise ValueError(f'unknown libraries: {", ".join(sorted(unknown))}')
    missing = []
    for name, required, probe in LIBRARIES:
        required = required or name in also_required
        if not check_library(name, required, probe, log) and required:
            missing.append(name)
    return missing
