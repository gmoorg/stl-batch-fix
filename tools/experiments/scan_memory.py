#!/usr/bin/env python3
"""Experiment: peak memory and time of `scanner.scan`'s edge count, current
row-wise `np.unique(axis=0)` against one int64 key per edge.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/scan_memory.py MESH.ply VARIANT
    PYTHONPATH=. tools/project_python.sh -u tools/experiments/scan_memory.py --make sphereR OUT.ply BLOCKS
    PYTHONPATH=. tools/project_python.sh -u tools/experiments/scan_memory.py --agree

VARIANT: `current` (scanner.scan — since 2026-10-06 the key sort below),
`row_unique` (scanner.scan before that: row-wise `np.unique(axis=0)` over the
edge pairs), `key_unique` (np.unique on int64 keys, return_counts) or
`key_sort` (keys sorted in place, run lengths from the boundaries). Peak =
VmHWM reset after loading, minus RSS after loading: the memory the count
itself adds. Run each variant in a fresh process.

`--make` rebuilds an icosphere of radius R with `winding.reconstruct` and
writes it as PLY (a realistic large output). `--agree` checks every variant
returns the same Scan as `row_unique`, the independent reference, on meshes
with open, non-manifold and degenerate edges, on sparse indices and on
random soups.

Read-only on inputs. Not wired into the pipeline.
"""

import sys
import time

import numpy as np

from libs import mesh_io, scanner, winding
from libs.mesh_io import Geometry, Mesh, Kind


def status(key):
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith(key):
                return int(line.split()[1]) * 1024


def reset_peak():
    with open('/proc/self/clear_refs', 'w') as f:
        f.write('5')


def edge_keys(faces, n):
    """low * n + high for every face edge, built one edge column at a time."""
    keys = np.empty(3 * len(faces), np.int64)
    m = len(faces)
    for k, (i, j) in enumerate(((0, 1), (1, 2), (2, 0))):
        a, b = faces[:, i], faces[:, j]
        out = keys[k * m:(k + 1) * m]
        np.minimum(a, b, out=out)
        out *= n
        out += np.maximum(a, b)
    return keys


def counts_key_unique(faces, n):
    _, counts = np.unique(edge_keys(faces, n), return_counts=True)
    return counts


def counts_key_sort(faces, n):
    keys = edge_keys(faces, n)
    keys.sort()
    change = np.empty(len(keys) + 1, bool)
    change[0] = change[-1] = True
    np.not_equal(keys[1:], keys[:-1], out=change[1:-1])
    del keys
    return np.diff(np.flatnonzero(change))


def counts_row_unique(faces, n):
    _, counts = np.unique(scanner.face_edges(faces), axis=0, return_counts=True)
    return counts


def scan_with(counter, mesh):
    faces = mesh.geometry.faces
    if len(faces) == 0:
        return scanner.Scan(0, 0, 0, 0)
    # Same index domain as scanner._edge_counts: radix from the faces.
    assert faces.min() >= 0, 'negative face index'
    n = int(faces.max()) + 1
    assert n <= 3_037_000_499, 'keys would overflow int64'
    degenerate = int(scanner.degenerate_mask(faces).sum())
    counts = counter(faces, n)
    return scanner.Scan(open_edges=int((counts == 1).sum()),
                        non_manifold=int((counts > 2).sum()),
                        faces=len(faces), degenerate=degenerate)


VARIANTS = {'current': scanner.scan,
            'row_unique': lambda m: scan_with(counts_row_unique, m),
            'key_unique': lambda m: scan_with(counts_key_unique, m),
            'key_sort': lambda m: scan_with(counts_key_sort, m)}


def mesh_of(v, f):
    g = Geometry(np.asarray(v, np.float64), np.asarray(f, np.int64).reshape(-1, 3))
    return Mesh('/in/m.stl', '/out/m.stl', Kind.BINARY_STL, len(g.faces), True, None, g)


def sphere_mesh(r, subdivisions=4):
    from tests.tests.test_decimator import _sphere
    v, f = _sphere(subdivisions=subdivisions)
    return mesh_of(v / np.linalg.norm(v, axis=1)[:, None] * r, f)


def agree():
    rng = np.random.default_rng(0)
    s = sphere_mesh(10, 3)
    v, f = s.geometry.verts, s.geometry.faces
    cases = {
        'closed sphere': s,
        'open (one face removed)': mesh_of(v, f[1:]),
        'non-manifold (a face duplicated)': mesh_of(v, np.vstack([f, f[:1]])),
        'degenerate face added': mesh_of(v, np.vstack([f, [[0, 0, 1]]])),
        'flipped duplicate (NM, opposite winding)': mesh_of(v, np.vstack([f, f[:5, ::-1]])),
        'no faces': mesh_of(v[:3], np.zeros((0, 3), np.int64)),
        'single triangle': mesh_of(v[:3], [[0, 1, 2]]),
        'single degenerate triangle (two corners)': mesh_of(v[:3], [[0, 0, 1]]),
        'single point triangle (one distinct edge)': mesh_of(v[:3], [[1, 1, 1]]),
        # Keyed by the 4-vertex table length, (0, 5) and (1, 1) would collide.
        'sparse indices past the vertex table': mesh_of(v[:4], [[0, 5, 9], [1, 1, 2]]),
    }
    for seed in range(5):
        nv = int(rng.integers(5, 200))
        cases[f'random soup {seed}'] = mesh_of(rng.random((nv, 3)),
                                               rng.integers(0, nv, (int(rng.integers(1, 2000)), 3)))
    ok = True
    for name, m in cases.items():
        ref = VARIANTS['row_unique'](m)
        for variant, fn in VARIANTS.items():
            got = fn(m)
            if got != ref:
                ok = False
                print(f'MISMATCH {name} {variant}: {got} != {ref}')
        print(f'{name:42} {ref}')
    print('all variants agree' if ok else 'DISAGREEMENT')
    sys.exit(0 if ok else 1)


def main():
    if sys.argv[1] == '--agree':
        agree()
    if sys.argv[1] == '--make':
        name, out, blocks = sys.argv[2], sys.argv[3], int(sys.argv[4])
        m = sphere_mesh(float(name[len('sphere'):]))
        rec = winding.reconstruct(m, winding.grid_spacing(scanner.diagonal(m)), blocks)
        mesh_io.write_ply(rec, out)
        print(f'wrote {out}: {len(rec.geometry.faces)} faces')
        return
    path, variant = sys.argv[1], sys.argv[2]
    mesh = mesh_io.read_ply(path, sphere_mesh(1, 1))   # template; its geometry is replaced
    base = status('VmRSS')
    reset_peak()
    t = time.monotonic()
    result = VARIANTS[variant](mesh)
    dt = time.monotonic() - t
    added = status('VmHWM') - base
    print(f'{variant:10} faces={len(mesh.geometry.faces):>10} added_peak={added / 1e9:.3f} GB '
          f'({added / len(mesh.geometry.faces):.0f} B/face) time={dt:.1f}s '
          f'open={result.open_edges} nm={result.non_manifold} degenerate={result.degenerate}')


if __name__ == '__main__':
    main()
