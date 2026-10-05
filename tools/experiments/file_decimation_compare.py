#!/usr/bin/env python3
"""Experiment: the initial decimation from file (PyMeshLab reads the source,
saves a PLY) against the array path (mesh_io.load + decimate). The comparison
that decided against the file path (2026-10-05): same output, but PyMeshLab's
STL reader holds ~4.5x the mesh's own memory. See
docs/errors/decimation-memory-path.md.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/file_decimation_compare.py array FILE OUT.npz [TARGET]
    PYTHONPATH=. tools/project_python.sh -u tools/experiments/file_decimation_compare.py file FILE OUT.ply [TARGET]
    PYTHONPATH=. tools/project_python.sh -u tools/experiments/file_decimation_compare.py compare FILE ARRAY.npz FILE.ply

Run `array` and `file` in separate processes (PyMeshLab keeps filter
parameters between calls; VmHWM is per process); each prints time and peak
RSS (VmHWM, GiB). TARGET defaults to 900000.

`compare` measures both results: vertices/faces, NM/open, component volume
ratio out/in (vs the source), and point-to-triangle distances in model
units, p99 and max, from ALL vertices of one mesh to the other's surface:
source -> output and output -> source for each, and file -> array, array ->
file. Gates agreed for the change (diag = source diagonal): the file
output's distances to/from the source within max(1.25 x the array output's,
1e-4 x diag) at p99 and max(1.25 x, 1e-3 x diag) at max; volume ratios within
0.005; face counts within 1 %. Gates for this check, not general guarantees.

Writes only OUT. Not wired into the pipeline.
"""

import sys
import time

import igl
import numpy as np
import pymeshlab

from libs import decimator, mesh_io, scanner
from libs.mesh_io import Geometry


def peak_gib():
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith('VmHWM:'):
                return int(line.split()[1]) / 1024 / 1024
    return float('nan')


def probe(path):
    return mesh_io.probe(path, '/nonexistent/out.stl')


def dist(points, G):
    return np.sqrt(igl.point_mesh_squared_distance(
        points.astype(np.float64), G.verts.astype(np.float64), G.faces.astype(np.int64))[0])


def stats(d):
    return float(np.percentile(d, 99)), float(d.max())


def main():
    mode, path = sys.argv[1], sys.argv[2]
    target = int(sys.argv[4]) if len(sys.argv) > 4 and mode != 'compare' else 900000
    if mode == 'array':
        t = time.monotonic()
        r = decimator.decimate(mesh_io.load(probe(path)), target)
        g = r.mesh.geometry
        print(f"array: {r.rung.value} {r.faces_in} -> {r.faces_out} "
              f"{time.monotonic() - t:.1f}s peak={peak_gib():.2f} GiB", flush=True)
        np.savez(sys.argv[3], verts=g.verts, faces=g.faces)
    elif mode == 'file':
        # Self-contained: PyMeshLab reads the STL, decimates with the
        # pipeline's parameters and saves a bare PLY.
        t = time.monotonic()
        ms = pymeshlab.MeshSet()
        ms.load_new_mesh(path)
        faces_in = ms.current_mesh().face_number()
        ms.apply_filter('meshing_decimation_quadric_edge_collapse',
                        targetfacenum=target, **decimator.QUADRIC_PARAMS)
        ms.save_current_mesh(sys.argv[3], binary=True, save_vertex_normal=False,
                             save_vertex_color=False, save_vertex_quality=False,
                             save_face_color=False, save_face_quality=False,
                             save_wedge_texcoord=False, save_wedge_normal=False)
        back = mesh_io.read_ply(sys.argv[3], probe(path))
        print(f"file: {faces_in} -> {ms.current_mesh().face_number()} (read {back.triangles}) "
              f"{time.monotonic() - t:.1f}s peak={peak_gib():.2f} GiB", flush=True)
    elif mode == 'compare':
        source = mesh_io.load(probe(path))
        S = source.geometry
        diag = scanner.diagonal(source)
        z = np.load(sys.argv[3])
        A = Geometry(z['verts'], z['faces'])
        F = mesh_io.read_ply(sys.argv[4], probe(path)).geometry
        vol_in = scanner.component_volume(source)
        res = {}
        for name, G in (('array', A), ('file', F)):
            s = scanner.scan(source.with_geometry(G))
            res[name] = dict(v=len(G.verts), f=len(G.faces), nm=s.non_manifold,
                             open=s.open_edges,
                             vol=scanner.component_volume(source.with_geometry(G)) / vol_in,
                             src_to=stats(dist(S.verts, G)), to_src=stats(dist(G.verts, S)))
            r = res[name]
            print(f"{name:5} v/f={r['v']}/{r['f']} nm={r['nm']} open={r['open']} "
                  f"volume ratio={r['vol']:.5f} source->out p99/max={r['src_to'][0]:.4g}/"
                  f"{r['src_to'][1]:.4g} out->source p99/max={r['to_src'][0]:.4g}/"
                  f"{r['to_src'][1]:.4g}", flush=True)
        fa, af = stats(dist(F.verts, A)), stats(dist(A.verts, F))
        print(f"file->array p99/max={fa[0]:.4g}/{fa[1]:.4g}  "
              f"array->file p99/max={af[0]:.4g}/{af[1]:.4g}", flush=True)
        a, f = res['array'], res['file']
        checks = []
        for key in ('src_to', 'to_src'):
            checks.append((f'{key} p99', f[key][0] <= max(1.25 * a[key][0], 1e-4 * diag)))
            checks.append((f'{key} max', f[key][1] <= max(1.25 * a[key][1], 1e-3 * diag)))
        checks.append(('volume', abs(f['vol'] - a['vol']) <= 0.005))
        checks.append(('faces', abs(f['f'] - a['f']) <= 0.01 * a['f']))
        print(f"diag={diag:.4g}  " + '  '.join(f"{n}: {'ok' if ok else 'FAIL'}"
                                               for n, ok in checks), flush=True)
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
