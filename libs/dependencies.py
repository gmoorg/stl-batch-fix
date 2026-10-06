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


#: (name, required, probe). CGAL is optional because alpha-wrap is
#: explicit-use only; making it required changes this policy, and putting
#: alpha-wrap back into the default sequence is a separate pipeline change.
LIBRARIES: tuple[tuple[str, bool, Callable[[], None]], ...] = (
    ('NumPy', True, importable('numpy')),
    ('SciPy', True, importable('scipy')),
    ('libigl', True, importable('igl')),
    ('PyMeshLab', True, importable('pymeshlab')),
    ('PyMeshFix', True, importable('pymeshfix')),
    ('CGAL', False, importable('CGAL.CGAL_Alpha_wrap_3', 'CGAL.CGAL_Kernel',
                               'CGAL.CGAL_Polyhedron_3')),
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


def check_all(log: Callable[[str], None]) -> list[str]:
    """Check every library in `LIBRARIES`; return the missing required ones."""
    return [name for name, required, probe in LIBRARIES
            if not check_library(name, required, probe, log) and required]
