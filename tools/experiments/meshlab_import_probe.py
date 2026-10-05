#!/usr/bin/env python3
"""Experiment: what PyMeshLab's STL **import** and PLY round trip do with
duplicate, degenerate and near-duplicate geometry. See
docs/errors/decimation-memory-path.md ("PLY dialect" and the defect table).

    PYTHONPATH=. tools/project_python.sh tools/experiments/meshlab_import_probe.py OUTDIR [FILE]

Without FILE: writes six synthetic binary STLs to OUTDIR (clean tetrahedron,
+ duplicate face, + reversed duplicate face, + coincident duplicate shell,
+ degenerate face, + vertex 1e-6 from another) and prints vertex/face counts
in the file, after `load_new_mesh`, and after a bare binary PLY save and
re-read (counts only; coordinates aren't compared). With FILE: the same for that file, plus the PLY header, file size,
and whether vertices/faces re-read bit-identical (the "saves PLY even if not
decimated" check).

Writes only into OUTDIR. Not wired into the pipeline.
"""

import os
import sys
import time

import numpy as np
import pymeshlab

BARE = dict(binary=True, save_vertex_normal=False, save_vertex_color=False,
            save_vertex_quality=False, save_face_color=False, save_face_quality=False,
            save_wedge_texcoord=False, save_wedge_normal=False)


def write_stl(path, tris):
    tris = np.array(tris, np.float32)
    rec = np.zeros(len(tris), dtype=[('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')])
    rec['v'] = tris
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)
        f.write(np.uint32(len(tris)).tobytes())
        f.write(rec.tobytes())


def roundtrip(stl, ply, verbose=False):
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(stl)
    m = ms.current_mesh()
    v0, f0 = m.vertex_matrix().copy(), m.face_matrix().copy()
    t = time.monotonic()
    ms.save_current_mesh(ply, **BARE)
    saved = time.monotonic() - t
    ms2 = pymeshlab.MeshSet()
    ms2.load_new_mesh(ply)
    m2 = ms2.current_mesh()
    if verbose:
        print(f"saved {os.path.getsize(ply) / 1e6:.1f} MB in {saved:.2f}s")
        print(open(ply, 'rb').read(400).split(b'end_header')[0].decode())
        # Bytes, not array_equal: -0.0 == 0.0 numerically but not bitwise.
        v2, f2 = m2.vertex_matrix(), m2.face_matrix()
        print(f"bit-identical: verts {v0.dtype == v2.dtype and v0.tobytes() == v2.tobytes()} "
              f"faces {f0.dtype == f2.dtype and f0.tobytes() == f2.tobytes()}")
    return (m.vertex_number(), m.face_number(), m2.vertex_number(), m2.face_number())


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    ply = os.path.join(out, 'probe.ply')
    if len(sys.argv) > 2:
        iv, iff, pv, pf = roundtrip(sys.argv[2], ply, verbose=True)
        print(f"import v={iv} f={iff} | ply v={pv} f={pf}")
        return
    P = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]], np.float32)
    T = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]

    def tri(idx):
        return [P[list(f)] for f in idx]
    base = tri(T)
    cases = {
        'clean tetrahedron': base,
        'duplicate face': base + tri([T[0]]),
        'duplicate face reversed': base + tri([T[0][::-1]]),
        'duplicate shell': base + tri(T),
        'degenerate face': base + [np.array([P[0], P[0], P[1]])],
        'vertex 1e-6 apart': base + [np.array([P[1] + np.float32(1e-6), P[2],
                                               P[0] + np.float32([5, 5, 5])])],
    }
    print(f"{'case':26} {'file f':>6} | {'import v':>8} {'f':>3} | {'ply v':>5} {'f':>3}")
    for name, tris in cases.items():
        stl = os.path.join(out, 'probe.stl')
        write_stl(stl, tris)
        iv, iff, pv, pf = roundtrip(stl, ply)
        print(f"{name:26} {len(tris):6} | {iv:8} {iff:3} | {pv:5} {pf:3}")


if __name__ == '__main__':
    main()
