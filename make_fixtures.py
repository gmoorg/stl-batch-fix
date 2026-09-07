#!/usr/bin/env python3
"""Generate the synthetic test meshes used by test_pipeline.py.

    .venv/bin/python make_fixtures.py

Writes tests/fixtures/*.stl — a few hundred KB in total, committable, and
independent of the STL collection, which is meant to be deletable.

Each fixture reproduces the *property* that its real counterpart's bug depended
on, not the model itself:

    body     many separate shells, over the scan limit  (head deletion)
    arms     several shells, under the scan limit        (normal split path)
    leg      one real shell plus far-away stray specks   (benign bbox change)
    foot1    one shell with a real boundary hole         (open-edge repair)
    foot2    one clean closed shell                      (passthrough)
    falcon   heavy non-manifold edges                    (blender fallback)

Verify with `--check`, which reports each fixture's measured properties so a
generator change that stops reproducing a defect is visible immediately.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stl_batch_fix as fix

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'tests', 'fixtures')


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


def tube_open_ends(cx, cy, cz, r, h, seg=24):
    """A cylinder wall with NO end caps — two genuine boundary loops.

    This is foot1's defect: real open edges that a repair will try to close,
    and closing them by pulling the boundary shut is what destroyed the caps."""
    verts, faces = [], []
    for k, z in enumerate((cz, cz + h)):
        for j in range(seg):
            a = 2 * np.pi * j / seg
            verts.append((cx + r * np.cos(a), cy + r * np.sin(a), z))
    for j in range(seg):
        a = j
        b = (j + 1) % seg
        c = seg + j
        d = seg + (j + 1) % seg
        faces.append((a, c, b))
        faces.append((b, c, d))
    return np.array(verts, dtype=np.float32), np.array(faces, dtype=np.int64)


def combine(*parts):
    """Concatenate meshes, offsetting face indices — shells stay separate."""
    verts, faces, base = [], [], 0
    for v, f in parts:
        verts.append(v)
        faces.append(f + base)
        base += len(v)
    return np.vstack(verts), np.vstack(faces)


def subdivide(verts, faces, times=1):
    """Split every triangle into four.  Used only to push a fixture over the
    2M-triangle scan limit, which is the whole point of the body fixture."""
    for _ in range(times):
        v = list(map(tuple, verts))
        index = {p: i for i, p in enumerate(v)}
        new_faces = []

        def mid(i, j):
            p = tuple((verts[i] + verts[j]) / 2.0)
            if p not in index:
                index[p] = len(v)
                v.append(p)
            return index[p]

        for a, b, c in faces:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            new_faces += [(a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)]
        verts = np.array(v, dtype=np.float32)
        faces = np.array(new_faces, dtype=np.int64)
    return verts, faces


def non_manifold_fan(cx, cy, cz, n_edges=400):
    """Edges shared by three faces — the defect falcon has 147,448 of.

    Three triangles hinged on one shared edge is the textbook non-manifold
    edge: no orientation makes it a surface boundary."""
    verts, faces = [], []
    for k in range(n_edges):
        o = k * 3.0
        base = len(verts)
        verts += [(cx + o, cy, cz), (cx + o, cy + 1, cz),          # shared edge
                  (cx + o + 1, cy, cz), (cx + o - 1, cy, cz),
                  (cx + o, cy, cz + 1)]
        faces += [(base, base + 1, base + 2),
                  (base, base + 1, base + 3),
                  (base, base + 1, base + 4)]     # third face on the same edge
    return np.array(verts, dtype=np.float32), np.array(faces, dtype=np.int64)


# ── fixtures ─────────────────────────────────────────────────────────────────

def build_body():
    """Many real shells, one much larger than the rest.

    The head-deletion bug needed two things: the mesh over the scan limit (so
    the split was skipped) and several genuine shells (so there was something
    to delete).  The test supplies the first by lowering
    _LARGE_MESH_TRI_LIMIT and MAX_FACES rather than by shipping a 2M-triangle
    file — the code path is what matters, not the triangle count.  Building
    the real size took 119 MB, larger than the model it stood in for.

    The size spread matters: pymeshfix kept the largest shell and dropped the
    others, so a fixture of equal-sized blobs would not show the same loss."""
    parts = [sphere(0, 0, 0, 20, seg=24)]           # the "body"
    for i in range(8):                              # eight smaller shells
        parts.append(sphere(45 + i * 14, 0, 0, 5, seg=12))
    return combine(*parts)


def build_arms():
    """Several real shells, deliberately SMALLER than body.

    body and arms are a matched pair either side of the scan limit: the test
    sets that limit between them, so arms takes the normal step B split and
    body takes the deferred step B2.  Keep arms the smaller of the two."""
    return combine(sphere(0, 0, 0, 10, seg=12),
                   sphere(30, 0, 0, 8, seg=12),
                   sphere(-30, 0, 0, 8, seg=12))


def build_leg():
    """One real shell plus stray specks far away.

    The specks stretch the bounding box; removing them shrinks it a long way
    while the model is untouched.  That is leg's 89mm 'change' being correct."""
    parts = [sphere(0, 0, 0, 10, seg=20)]
    # Small enough that split_shells() drops them as debris (under its
    # max(100, largest/1000) face floor), far enough out that removing them
    # moves the bounding box a long way — which is the point: a large bbox
    # change that is CORRECT.
    for i in range(6):
        parts.append(sphere(200 + i * 30, 150, -120, 0.4, seg=4))
    return combine(*parts)


def build_foot1():
    """A closed body with a genuine boundary hole (open edges, nm=0)."""
    return combine(sphere(0, 0, 0, 10, seg=24),
                   tube_open_ends(0, 0, 20, 4, 6, seg=24))


def build_foot2():
    """Clean, closed, defect-free: must pass through untouched."""
    return sphere(0, 0, 0, 10, seg=28)


def build_falcon():
    """Heavy non-manifold edges: pymeshfix's failure case."""
    return combine(sphere(0, 0, 0, 10, seg=20),
                   non_manifold_fan(40, 0, 0, n_edges=600))


BUILDERS = {
    'body': build_body, 'arms': build_arms, 'leg': build_leg,
    'foot1': build_foot1, 'foot2': build_foot2, 'falcon': build_falcon,
}


def generate():
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    for name, build in BUILDERS.items():
        v, f = build()
        path = os.path.join(FIXTURE_DIR, f'{name}.stl')
        fix._write_binary_stl(path, v, f)
        print(f'  {name:8} {len(f):>9,} tris  '
              f'{os.path.getsize(path)/1048576:6.2f} MB', flush=True)


def check():
    """Report what each fixture actually is, so a generator change that stops
    reproducing a defect shows up here rather than as a confusing test pass."""
    print(f'{"name":8} {"tris":>10} {"nm":>8} {"open":>6} {"shells":>7}  note')
    for name in BUILDERS:
        path = os.path.join(FIXTURE_DIR, f'{name}.stl')
        if not os.path.exists(path):
            print(f'{name:8}  MISSING — run without --check first')
            continue
        n, err = fix._read_stl_header(path)
        nm, op, _ = fix.scan_mesh_errors(path, n)
        v, f = fix._weld_binary_stl(path)
        p = np.arange(len(v), dtype=np.int64)

        def find(x):
            while p[x] != x:
                p[x] = p[p[x]]
                x = p[x]
            return x
        e = np.unique(np.sort(np.vstack([f[:, [0, 1]], f[:, [1, 2]],
                                         f[:, [2, 0]]]), axis=1), axis=0)
        for a, b in e:
            ra, rb = find(a), find(b)
            if ra != rb:
                p[ra] = rb
        shells = len(np.unique([find(i) for i in range(len(v))]))
        over = ' OVER SCAN LIMIT' if n > fix._LARGE_MESH_TRI_LIMIT else ''
        print(f'{name:8} {n:>10,} {nm:>8} {op:>6} {shells:>7}{over}')
        del v, f


if __name__ == '__main__':
    if '--check' in sys.argv:
        check()
    else:
        print(f'writing {FIXTURE_DIR}')
        generate()
        print()
        check()
