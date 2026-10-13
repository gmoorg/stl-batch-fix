#!/usr/bin/env python3
"""Generate the regression probe meshes in `tests/probes/`.

    tools/project_python.sh tests/tests/make_fixtures.py

Small, committed, and independent of any STL collection. Each reproduces the
*property* a real model-loss bug depended on, not the model itself:

    small_valid_shell          meaningful shell below the debris face floor
    opposite_volume_shells     signed volumes cancel while one shell is small
    decimation_lost_appendage  thin feature shortened by face reduction
    reversed_tjunction_chain   open-edge path runs backward along its span

Verify with `--check`, which reports each probe's measured properties so a
generator change that stops reproducing a defect is visible immediately.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(TEST_ROOT)
sys.path.insert(0, PROJECT_ROOT)
from libs import mesh_io, scanner                       # noqa: E402
from libs.mesh_io import Geometry, Kind, Mesh           # noqa: E402

PROBE_DIR = os.path.join(TEST_ROOT, 'probes')


# ── primitives ───────────────────────────────────────────────────────────────

def sphere(cx, cy, cz, r, seg=16):
    """A closed UV sphere: watertight, manifold, no boundary, no degenerates.

    The poles get ONE vertex each, not `seg` coincident ones.  Emitting a full
    ring at each pole is the obvious way to write this and it is wrong: the
    ring collapses to a point, every triangle in the top and bottom bands has
    zero area, and the mesh scans as non-manifold.  A first attempt at these
    fixtures did exactly that and produced a "clean" sphere with nm=29."""
    verts = [(cx, cy, cz + r)]                       # north pole
    for i in range(1, seg):                          # interior latitude rings
        lat = np.pi * i / seg
        for j in range(seg):
            lon = 2 * np.pi * j / seg
            verts.append((cx + r * np.sin(lat) * np.cos(lon),
                          cy + r * np.sin(lat) * np.sin(lon),
                          cz + r * np.cos(lat)))
    verts.append((cx, cy, cz - r))                   # south pole
    south = len(verts) - 1

    def ring(i, j):                                  # index into ring i (1-based)
        return 1 + (i - 1) * seg + (j % seg)

    faces = []
    for j in range(seg):                             # north cap
        faces.append((0, ring(1, j), ring(1, j + 1)))
    for i in range(1, seg - 1):                      # quad bands
        for j in range(seg):
            a, b = ring(i, j), ring(i, j + 1)
            c, d = ring(i + 1, j), ring(i + 1, j + 1)
            faces.append((a, c, b))
            faces.append((b, c, d))
    for j in range(seg):                             # south cap
        faces.append((south, ring(seg - 1, j + 1), ring(seg - 1, j)))
    return np.array(verts, dtype=np.float32), np.array(faces, dtype=np.int64)


def combine(*parts):
    """Concatenate meshes, offsetting face indices — shells stay separate."""
    verts, faces, base = [], [], 0
    for v, f in parts:
        verts.append(v)
        faces.append(f + base)
        base += len(v)
    return np.vstack(verts), np.vstack(faces)


def tetrahedron(origin, edge, inverted=False):
    """Closed four-face shell; useful because it falls below the debris floor."""
    x, y, z = origin
    verts = np.array([
        (x, y, z), (x + edge, y, z),
        (x, y + edge, z), (x, y, z + edge),
    ], dtype=np.float32)
    faces = np.array([(0, 2, 1), (0, 1, 3),
                      (0, 3, 2), (1, 2, 3)], dtype=np.int64)
    if inverted:
        faces = faces[:, ::-1].copy()
    return verts, faces


def _quads(*rings):
    """Triangulate ordered four-corner surface patches."""
    faces = []
    for a, b, c, d in rings:
        faces.extend(((a, b, c), (a, c, d)))
    return np.asarray(faces, dtype=np.int64)


# ── probes ───────────────────────────────────────────────────────────────────

def build_small_valid_shell():
    """A sound four-face part beside a large shell.

    The tetrahedron is deliberately below ``MIN_SHELL_FACES``. It represents
    a meaningful button, pin, or tooth, so a clean pipeline result must retain
    both components rather than silently classify the small one as debris.
    """
    return combine(sphere(0, 0, 0, 10, seg=20),
                   tetrahedron((15, 0, 0), 4))


def build_opposite_volume_shells():
    """A sphere plus a four-face shell with equal, opposite signed volume.

    The small-face shell is geometrically large. Dropping it while computing
    retention from the signed total makes the denominator approximately zero,
    which previously allowed a partial model to be reported as clean.
    """
    body_v, body_f = sphere(0, 0, 0, 10, seg=20)
    tri = body_v[body_f].astype(np.float64)
    body_volume = float(np.einsum(
        'ij,ij->i', tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)
    edge = (6.0 * abs(body_volume)) ** (1.0 / 3.0)
    return combine((body_v, body_f),
                   tetrahedron((30, -edge / 2, -edge / 2), edge,
                               inverted=True))


def build_decimation_lost_appendage():
    """One watertight body with a thin 0.25-unit appendage.

    At a 20-face budget the installed fast decimator shortens the protrusion
    from 0.25 to about 0.10 units while returning a clean closed mesh. The
    pipeline must compare that result with this source, rather than beginning
    its preservation check after the loss occurred.
    """
    w = 0.25
    verts = np.asarray([
        (-10, -10, -10), (-10, 10, -10),
        (-10, 10, 10), (-10, -10, 10),
        (10, -10, -10), (10, 10, -10),
        (10, 10, 10), (10, -10, 10),
        (10, -w, -w), (10, w, -w), (10, w, w), (10, -w, w),
        (10.25, -w, -w), (10.25, w, -w),
        (10.25, w, w), (10.25, -w, w),
    ], dtype=np.float32)
    faces = _quads(
        # Five outer faces of the box.
        (0, 3, 2, 1), (0, 1, 5, 4), (3, 7, 6, 2),
        (0, 4, 7, 3), (1, 2, 6, 5),
        # The +X face is an annulus around the appendage opening.
        (4, 5, 9, 8), (5, 6, 10, 9),
        (6, 7, 11, 10), (7, 4, 8, 11),
        # Appendage sides and end cap; its start is the box opening.
        (8, 9, 13, 12), (11, 15, 14, 10),
        (8, 12, 15, 11), (9, 10, 14, 13), (12, 13, 14, 15),
    )
    return verts, faces


def build_reversed_tjunction_chain():
    """Two vertices whose open-edge path runs backward along a spanning edge.

    Topology orders the path ``a -> M(2/3) -> M(1/3) -> b``. Sorting those
    vertices by projected position before splitting creates faces that do not
    match the actual path, so the welder must reject or handle it explicitly.
    """
    verts, faces = sphere(0, 0, 0, 10, seg=20)
    verts, changed = verts.tolist(), faces.tolist()
    a, b, c = changed[200]
    along = np.asarray(verts[b]) - np.asarray(verts[a])
    verts.append((np.asarray(verts[a]) + 2.0 * along / 3.0).tolist())
    verts.append((np.asarray(verts[a]) + along / 3.0).tolist())
    first, second = len(verts) - 2, len(verts) - 1
    changed[200] = [a, first, c]
    changed.extend(([first, second, c], [second, b, c]))
    return (np.asarray(verts, dtype=np.float32),
            np.asarray(changed, dtype=np.int64))


PROBE_BUILDERS = {
    'small_valid_shell': build_small_valid_shell,
    'opposite_volume_shells': build_opposite_volume_shells,
    'decimation_lost_appendage': build_decimation_lost_appendage,
    'reversed_tjunction_chain': build_reversed_tjunction_chain,
}


REGRESSION_SHAPES = {
    # faces, non-manifold edges, open edges, edge-connected shells
    'small_valid_shell': (764, 0, 0, 2),
    'opposite_volume_shells': (764, 0, 0, 2),
    'decimation_lost_appendage': (28, 0, 0, 1),
    'reversed_tjunction_chain': (762, 0, 4, 1),
}


def generate():
    os.makedirs(PROBE_DIR, exist_ok=True)
    for name, build in PROBE_BUILDERS.items():
        v, f = build()
        path = os.path.join(PROBE_DIR, f'{name}.stl')
        mesh_io.write(Mesh(path, path, Kind.BINARY_STL, len(f), True, None,
                           Geometry(np.asarray(v, np.float64), np.asarray(f, np.int64))))
        print(f'  {name:28} {len(f):>9,} tris  '
              f'{os.path.getsize(path)/1048576:6.2f} MB', flush=True)


def check():
    """Report what each probe actually is, so a generator change that stops
    reproducing a defect shows up here rather than as a confusing test pass."""
    print(f'{"name":28} {"tris":>10} {"nm":>8} {"open":>6} {"shells":>7} {"volume":>12}')
    for name in PROBE_BUILDERS:
        path = os.path.join(PROBE_DIR, f'{name}.stl')
        if not os.path.exists(path):
            print(f'{name:28}  MISSING — run without --check first')
            continue
        mesh = mesh_io.load(mesh_io.probe(path, path))
        scan = scanner.scan(mesh)
        volume = scanner.volume(mesh)
        observed = (mesh.triangles, scan.non_manifold, scan.open_edges, len(scanner.shells(mesh)))
        if observed != REGRESSION_SHAPES[name]:
            raise AssertionError(f'{name}: expected {REGRESSION_SHAPES[name]}, got {observed}')
        if name == 'opposite_volume_shells' and abs(volume) > 0.01:
            raise AssertionError(f'{name}: signed volumes no longer cancel ({volume:+.6f})')
        if name == 'decimation_lost_appendage':
            maximum_x = float(mesh.geometry.verts[:, 0].max())
            if not np.isclose(maximum_x, 10.25):
                raise AssertionError(f'{name}: appendage tip moved to x={maximum_x}')
        print(f'{name:28} {observed[0]:>10,} {observed[1]:>8} {observed[2]:>6} '
              f'{observed[3]:>7} {volume:>+12,.1f}')


if __name__ == '__main__':
    if '--check' in sys.argv:
        check()
    else:
        print(f'writing {PROBE_DIR}')
        generate()
        print()
        check()
