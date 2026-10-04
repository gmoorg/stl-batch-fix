#!/usr/bin/env python3
"""Regenerate the single-defect probe meshes used to survey repair behaviour.

    tools/project_python.sh tools/make_probe_meshes.py [outdir]

Default outdir is /mnt/sda2/STL/_validate, which is inside the collection and
therefore **deletable** — that is why this generator exists. The meshes it
writes are disposable; this file is the durable artefact.

Distinct from `tests/tests/make_fixtures.py`, which builds the committed
regression probes in `tests/probes/`. These are for answering "does anything
downstream actually care about defect X" by hand: load them in a slicer, upload
them to an online repair service, compare against the control.

Each mesh is one defect on an otherwise perfect 760-face sphere, so any
difference in a tool's response is attributable. That was the lesson of trying
to use a real file first: `platform_supported.stl` had 4,526 shells, 22.6% of
faces with skewed normals, and printed supports — nothing about its repair
could be attributed to anything.

Findings from the 2026-09-16 survey are in REFACTOR_DECISIONS.md; in short,
four of five defects needed no new capability and `doubles` is the one gap.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from libs import scanner                                   # noqa: E402
from libs.mesh_io import Geometry, Kind, Mesh, write        # noqa: E402


# The builders live in tests/tests/defect_spheres.py — one copy, shared with
# the test fixtures. Re-exported here so `from make_probe_meshes import
# sphere, build_*` and `tools.make_probe_meshes` imports keep working.
from tests.tests.defect_spheres import (                       # noqa: E402,F401
    BUILDERS, build_allbad, build_correct, build_degenerate, build_doubles,
    build_fin, build_inverted, build_inverted_third, build_seam,
    build_shell_inverted, build_tjunction, build_tjunction_many,
    build_two_shells, sphere)




def volume(geometry):
    """Signed volume — the only check that sees an inside-out mesh, and the
    only one that saw PyMeshFix destroy the doubles case."""
    tri = geometry.verts[geometry.faces].astype(np.float64)
    return float(np.einsum('ij,ij->i', tri[:, 0],
                           np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    base_v, base_f = sphere()
    print(f'{"mesh":18} {"faces":>6} {"verts":>6} {"nm":>4} {"open":>5} '
          f'{"deg":>4} {"shells":>7} {"volume":>11}')
    for name, build in BUILDERS.items():
        v, f = build(base_v.copy(), base_f.copy())
        path = os.path.join(outdir, f'sphere_{name}.stl')
        mesh = Mesh('/generated', path, Kind.BINARY_STL, len(f), True, None,
                    Geometry(v, f))
        write(mesh)
        s = scanner.scan(mesh)
        print(f'{name:18} {len(f):>6} {len(v):>6} {s.non_manifold:>4} '
              f'{s.open_edges:>5} {s.degenerate:>4} '
              f'{len(scanner.shells(mesh)):>7} '
              f'{volume(mesh.geometry):>+11.1f}')
    print(f'\nwritten to {outdir}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/mnt/sda2/STL/_validate')
