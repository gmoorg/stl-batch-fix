#!/usr/bin/env python3
"""Regenerate the single-defect probe meshes used to survey repair behaviour.

    tools/project_python.sh tools/make_probe_meshes.py [outdir]

Default outdir is /mnt/sda2/STL/_validate, which is inside the collection and
therefore **deletable** — that is why this generator exists. The meshes it
writes are disposable; this file is the durable artefact.

Distinct from `tests/tests/make_fixtures.py`, which builds the committed fixtures
`tests/tests/test_pipeline.py` depends on. These are for answering "does anything
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


def sphere(r=10.0, seg=20):
    """A closed UV sphere: watertight, manifold, single-vertex poles."""
    verts = [(0, 0, r)]
    for i in range(1, seg):
        lat = np.pi * i / seg
        for j in range(seg):
            lon = 2 * np.pi * j / seg
            verts.append((r * np.sin(lat) * np.cos(lon),
                          r * np.sin(lat) * np.sin(lon),
                          r * np.cos(lat)))
    verts.append((0, 0, -r))
    south = len(verts) - 1
    faces = []
    for j in range(seg):
        faces.append([0, 1 + j, 1 + (j + 1) % seg])
    for i in range(seg - 2):
        a = 1 + i * seg
        b = 1 + (i + 1) * seg
        for j in range(seg):
            j2 = (j + 1) % seg
            faces.append([a + j, b + j, b + j2])
            faces.append([a + j, b + j2, a + j2])
    a = 1 + (seg - 2) * seg
    for j in range(seg):
        faces.append([a + j, south, a + (j + 1) % seg])
    return np.array(verts, np.float32), np.array(faces, np.int64)


def build_correct(v, f):
    """The control. Every other mesh differs from this by exactly one defect."""
    return v, f


def build_inverted(v, f):
    """Every face reversed: inside-out, and NOTHING detects it.

    Settled 2026-09-16: scanner blind, PyMeshFix a no-op, a commercial repair
    service reports "0 Inverted normals", and Bambu both renders and slices it
    normally. Only signed volume tells it apart (-4094.9 vs +4094.9).
    """
    return v, f[:, ::-1].copy()


def build_seam(v, f):
    """The northern cap reversed: a closed winding-seam loop.

    The partial case, and unlike `inverted` it IS detected — by us (40 seam
    edges, 1 closed loop) and by the online service (40 inverted normals).
    """
    cap = v[f].mean(axis=1)[:, 2] > 5.0
    f = f.copy()
    f[cap] = f[cap][:, ::-1]
    return v, f


def build_inverted_third(v, f):
    """Exactly one third of the faces reversed, chosen by index rather than
    by position.

    Distinct from `seam`, whose cap is defined geometrically (z > 5.0) and
    happens to be 32% — a contiguous region bounded by one closed loop.  This
    one reverses every third face, so the reversed set is **scattered** and its
    boundary is many short loops rather than one ring.

    It exists to separate two things the orientation guard conflates: a mesh
    whose total volume stays positive because the reversed part is a minority
    (both cases), and a reversed region that `by_seams` can isolate as a
    connected component (only `seam`).  Scattered faces cannot be split out,
    so this is the harder case and the one a per-region guard will not catch.
    """
    f = f.copy()
    f[::3] = f[::3][:, ::-1]
    return v, f


def build_two_shells(v, f):
    """Two separate spheres, both correctly wound.

    The plain multi-shell case.  Every other probe is a single sphere, so
    `by_shells` returns them unsplit and step 4's per-part path never runs —
    this is the fixture that makes the split, the per-part loop and `merge`
    reachable at all.

    Volume is the sum of both: +8189.7.
    """
    second = (v + np.float32([40, 0, 0])).astype(np.float32)
    return np.vstack([v, second]), np.vstack([f, f + len(v)])


def build_shell_inverted(v, f):
    """A large correct sphere beside a small inverted one.

    **The fixture for the per-part orientation guard**, and the sizes are the
    point.  The small sphere is half the radius, so it contributes an eighth of
    the volume and the signed total stays **positive** (+3583.0) — step 2's
    guard correctly skips, and only the per-part guard after the split can see
    that one component is inside-out (-511.9 against the host's +4094.9).

    Two equal spheres would not do: their volumes cancel to -0.0, step 2 fires
    on that, and the per-part guard is never reached.  That was measured, not
    assumed.

    Correct result: both spheres outward, +4094.9 + 511.9 = **+4606.8**.
    """
    second = (v * 0.5 + np.float32([40, 0, 0])).astype(np.float32)
    return np.vstack([v, second]), np.vstack([f, f[:, ::-1] + len(v)])


def build_allbad(v, f):
    """Every defect at once, on one sphere.

    Built by composing the single-defect builders in a fixed order, so the
    result is reproducible and each ingredient is traceable — the original
    `sphere_allbad.stl` came from a scratch script and survived only as a file.

    Order matters: the index-based defects (`tjunction_many`, `degenerate`)
    run before `doubles` duplicates the whole thing, and `inverted` runs last
    so the mesh is wholly inside-out, which is what the orientation guard is
    meant to catch.
    """
    v, f = build_tjunction_many(v, f, n=80)
    v, f = build_degenerate(v, f)
    v, f = build_fin(v, f)
    v, f = build_doubles(v, f)
    return build_inverted(v, f)


def build_tjunction(v, f):
    """One vertex at an edge midpoint the neighbouring face does not use.

    Flush but unjoined — no vertex merge closes it, since nothing is
    coincident. scanner sees 3 open edges; PyMeshFix undoes it exactly.
    """
    a, b, c = f[200]
    v = np.vstack([v, ((v[a] + v[b]) / 2.0).astype(np.float32)])
    m = len(v) - 1
    f = f.tolist()
    f[200] = [a, m, c]
    f.append([m, b, c])
    return v, np.array(f, np.int64)


def build_tjunction_many(v, f, n=150, seed=0):
    """150 T-junctions at once, to test the Blender splitter's MAX_ITER=200.

    It is not a real limit: PyMeshFix closed all 450 resulting open edges.
    """
    verts, faces = v.tolist(), f.tolist()
    for t in np.random.default_rng(seed).choice(len(f), n, replace=False):
        a, b, c = faces[t]
        verts.append(((np.array(verts[a]) + np.array(verts[b])) / 2).tolist())
        m = len(verts) - 1
        faces[t] = [a, m, c]
        faces.append([m, b, c])
    return np.array(verts, np.float32), np.array(faces, np.int64)


def build_fin(v, f):
    """A triangle attached along one edge, sticking out into space."""
    v = np.vstack([v, np.array([[18, 0, 0]], np.float32)])
    a, b = f[200][0], f[200][1]
    return v, np.vstack([f, [[a, b, len(v) - 1]]])


def build_degenerate(v, f):
    """A zero-area face: two identical corners. PyMeshFix fixes it exactly."""
    return v, np.vstack([f, [[f[100][0], f[100][1], f[100][1]]]])


def build_doubles(v, f):
    """The same sphere twice, 1e-5 mm apart — the MERGE_DIST case.

    **The one real gap.** scanner reads it completely clean (nm=0, open=0,
    degenerate=0); only the shell count is 2, and two shells is legitimate on a
    real model. PyMeshFix is destructive: 1520 faces become 418 and the volume
    goes from +8189.7 to +2047.4 — half of ONE sphere. It discarded one shell
    and ate half the other.
    """
    return (np.vstack([v, v + np.float32([1e-5, 0, 0])]),
            np.vstack([f, f + len(v)]))


BUILDERS = {
    'correct': build_correct,
    'inverted': build_inverted,
    'inverted_third': build_inverted_third,
    'seam': build_seam,
    'tjunction': build_tjunction,
    'tjunction_many': build_tjunction_many,
    'fin': build_fin,
    'degenerate': build_degenerate,
    'doubles': build_doubles,
    'two_shells': build_two_shells,
    'shell_inverted': build_shell_inverted,
    'allbad': build_allbad,
}


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
