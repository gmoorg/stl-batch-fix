#!/usr/bin/env python3
"""Experiment: degenerate faces crash PyMeshLab quadric decimation on the
array path. See docs/errors/decimation-segfault.md.

    PYTHONPATH=. tools/project_python.sh -X faulthandler tools/experiments/segfault_probe.py MODE ...

Modes (run each in its own process; a segfault exits 139 and kills only it,
a caught decimation failure exits 1):

    count FILE                 mesh_io.load, count degenerate faces (repeated
                               vertex index) and unreferenced vertices
    array FILE [TARGET]        mesh_io.load -> decimator.decimate (the batch path)
    clean FILE [TARGET]        as array, with degenerate faces removed first
    synth CASE OUT [TARGET]    write a synthetic STL to OUT and decimate it via
                               the array path. CASE: clean, edge, bridge-near,
                               bridge-far, isolated, dup-pair, isolated+dup,
                               tetra-isolated
    crop FILE FACE R OUT [--no-degenerate]
                               crop FILE to faces whose centroid is within R
                               of face index FACE (kept even if outside),
                               write OUT, decimate it to half via the array path

TARGET defaults to 900000 (synth: 10000; tetra-isolated: 2).
`tools/experiments/data/segv_min16.stl` is the 16-face real crop of
Base_Pillar_R (crop around face 330092, R = 0.1); decimating it to 8 faces
through the array path segfaults.

Read-only on inputs. Not wired into the pipeline.
"""

import sys
import time

import numpy as np

from libs import decimator, mesh_io
from libs.mesh_io import Geometry


def load(path):
    mesh = mesh_io.load(mesh_io.probe(path, '/nonexistent/out.stl'))
    if not mesh.is_valid:
        sys.exit(f"load failed: {mesh.problem}")
    return mesh


def degenerate_mask(F):
    return (F[:, 0] == F[:, 1]) | (F[:, 1] == F[:, 2]) | (F[:, 0] == F[:, 2])


def write_stl(path, tris):
    tris = np.asarray(tris, np.float32)
    rec = np.zeros(len(tris), dtype=[('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')])
    rec['v'] = tris
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)
        f.write(np.uint32(len(tris)).tobytes())
        f.write(rec.tobytes())


def decimate(mesh, target, tag):
    t = time.monotonic()
    print(f"{tag}: faces_in={len(mesh.geometry.faces)} target={target}", flush=True)
    r = decimator.decimate(mesh, target)
    print(f"{tag}: rung={r.rung.value} faces_out={r.faces_out} "
          f"{time.monotonic() - t:.1f}s", flush=True)
    # decimate() catches filter exceptions and reports Rung.FAILED; exit
    # nonzero so the exit status alone tells success from failure.
    if r.rung is decimator.Rung.FAILED:
        sys.exit(1)


def icosphere(level):
    t = (1 + 5 ** 0.5) / 2
    V = [[-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0], [0, -1, t], [0, 1, t],
         [0, -1, -t], [0, 1, -t], [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1]]
    F = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9],
         [5, 11, 4], [11, 10, 2], [10, 7, 6], [7, 1, 8], [3, 9, 4], [3, 4, 2],
         [3, 2, 6], [3, 6, 8], [3, 8, 9], [4, 9, 5], [2, 4, 11], [6, 2, 10],
         [8, 6, 7], [9, 8, 1]]
    V = [np.array(v, float) / np.linalg.norm(v) for v in V]
    for _ in range(level):
        cache, nf = {}, []

        def mid(a, b):
            k = (min(a, b), max(a, b))
            if k not in cache:
                m = V[a] + V[b]
                V.append(m / np.linalg.norm(m))
                cache[k] = len(V) - 1
            return cache[k]
        for a, b, c in F:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            nf += [[a, ab, ca], [b, bc, ab], [c, ca, bc], [ab, bc, ca]]
        F = nf
    return np.array(V, np.float32) * 50, np.array(F)


def synth(case, out, target):
    if case == 'tetra-isolated':
        T = np.float32([[10, 0, 0], [11, 0, 0], [10, 1, 0], [10, 0, 1]])
        P, Q = np.float32([0, 0, 0]), np.float32([0.05, 0, 0])
        tris = [T[[0, 2, 1]], T[[0, 1, 3]], T[[0, 3, 2]], T[[1, 2, 3]], [P, Q, P]]
        write_stl(out, tris)
        decimate(load(out), target or 2, case)
        return
    V, F = icosphere(6)                                   # 81,920 faces
    adj = {}
    for f in F:
        for i in range(3):
            adj.setdefault(f[i], set()).update((f[(i + 1) % 3], f[(i + 2) % 3]))
    P, Q = np.float32([0, 0, 0]), np.float32([0.05, 0, 0])   # inside the sphere
    extra = []
    if case == 'edge':                                    # on an existing edge
        extra.append([V[0], V[0], V[1]])
    elif case in ('bridge-near', 'bridge-far'):           # between non-adjacent vertices
        if case == 'bridge-far':
            b = int(np.argmax(np.linalg.norm(V - V[0], axis=1)))
        else:
            b = int(sorted(set().union(*(adj[n] for n in adj[0])) - adj[0] - {0})[0])
        extra.append([V[0], V[b], V[0]])
    if case in ('isolated', 'isolated+dup'):              # on two new vertices
        extra.append([P, Q, P])
    if case in ('dup-pair', 'isolated+dup'):              # reversed duplicate pair
        extra += [V[F[0]], V[F[0]][::-1]]
    if case not in ('clean', 'edge', 'bridge-near', 'bridge-far', 'isolated',
                    'dup-pair', 'isolated+dup'):
        sys.exit(f"unknown case {case}")
    tris = V[F] if not extra else np.concatenate([V[F], np.array(extra, np.float32)])
    write_stl(out, tris)
    decimate(load(out), target or 10000, case)


def crop(path, face, radius, out, keep_degenerate):
    m = load(path)
    V, F = m.geometry.verts, m.geometry.faces
    c = V[F].mean(axis=1)
    keep = np.linalg.norm(c - c[face], axis=1) <= radius
    keep[face] = keep_degenerate
    used, inv = np.unique(F[keep], return_inverse=True)
    sf = inv.reshape(-1, 3)
    write_stl(out, V[used][sf])
    sub = load(out)
    decimate(sub, max(4, len(sub.geometry.faces) // 2), f"crop R={radius}")


def main():
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == 'count':
        m = load(args[0])
        F = m.geometry.faces
        unref = len(m.geometry.verts) - len(np.unique(F))
        print(f"faces={len(F)} degenerate={int(degenerate_mask(F).sum())} "
              f"verts={len(m.geometry.verts)} unreferenced={unref}")
    elif mode in ('array', 'clean'):
        m = load(args[0])
        target = int(args[1]) if len(args) > 1 else 900000
        if mode == 'clean':
            F = m.geometry.faces
            m = m.with_geometry(Geometry(m.geometry.verts, F[~degenerate_mask(F)]))
        decimate(m, target, mode)
    elif mode == 'synth':
        synth(args[0], args[1], int(args[2]) if len(args) > 2 else None)
    elif mode == 'crop':
        crop(args[0], int(args[1]), float(args[2]), args[3], '--no-degenerate' not in args)
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
