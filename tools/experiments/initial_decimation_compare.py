#!/usr/bin/env python3
"""Experiment: the initial (whole-source) decimation with PyMeshLab defaults
against `planarquadric=True`. See docs/errors/post-wrap-meshfix-timeout.md.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/initial_decimation_compare.py run FILE VARIANT OUT.npz [TARGET]
    PYTHONPATH=. tools/project_python.sh -u tools/experiments/initial_decimation_compare.py compare FILE DEFAULTS.npz PLANAR.npz

`run` (one process per variant: PyMeshLab keeps filter parameters between
calls) loads FILE with mesh_io.load and decimates it through
`decimator.decimate` to TARGET (default 900000), VARIANT `defaults`
(QUADRIC_PARAMS with planarquadric=False) or `planarquadric` (QUADRIC_PARAMS
as is); prints time and peak RSS (VmHWM, GiB) and saves the result.

`compare` measures both results against the source and applies the
criteria agreed for the change (diag = source bbox diagonal):
  - surface distance both ways (source vertices -> decimated surface,
    decimated vertices -> source surface): p99 <= max(1.25 x defaults',
    1e-4 x diag), max <= max(1.25 x defaults', 1e-3 x diag)
  - component volume ratio out/in within 0.005 of defaults'
  - every source shell with >= 100 faces: max distance of its vertices to
    the decimated surface <= max(1.25 x defaults', 1e-3 x diag) (sampled
    coverage: does not prove components stay topologically distinct)
  - the two extreme source vertices along the bbox's longest axis (tips):
    distance <= max(1.25 x defaults', 1e-3 x diag)
NM/open counts are informational.

Writes only OUT.npz. Not wired into the pipeline.
"""

import sys
import time
from unittest import mock

import igl
import numpy as np

from libs import decimator, mesh_io, scanner
from libs.mesh_io import Geometry


def peak_gib():
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith('VmHWM:'):
                return int(line.split()[1]) / 1024 / 1024
    return float('nan')


def load(path):
    return mesh_io.load(mesh_io.probe(path, '/nonexistent/out.stl'))


def run(path, variant, out, target):
    mesh = load(path)
    params = dict(decimator.QUADRIC_PARAMS, planarquadric=(variant == 'planarquadric'))
    t = time.monotonic()
    with mock.patch.dict(decimator.QUADRIC_PARAMS, params):
        r = decimator.decimate(mesh, target)
    print(f"{variant}: rung={r.rung.value} faces {r.faces_in} -> {r.faces_out} "
          f"{time.monotonic() - t:.1f}s peak={peak_gib():.2f} GiB", flush=True)
    g = r.mesh.geometry
    np.savez(out, verts=g.verts, faces=g.faces)


def distance(points, V, F):
    return np.sqrt(igl.point_mesh_squared_distance(
        points.astype(np.float64), V.astype(np.float64), F.astype(np.int64))[0])


def measure(source, G):
    SV, SF = source.geometry.verts, source.geometry.faces
    m = source.with_geometry(G)
    s = scanner.scan(m)
    to_dec = distance(SV, G.verts, G.faces)
    to_src = distance(G.verts, SV, SF)
    shells = [sh for sh in scanner.shells(source) if len(sh) >= 100]
    shell_max = [float(to_dec[np.unique(SF[sh])].max()) for sh in shells]
    axis = int(np.argmax(np.ptp(SV, axis=0)))
    tips = [int(np.argmin(SV[:, axis])), int(np.argmax(SV[:, axis]))]
    return dict(nm=s.non_manifold, open=s.open_edges,
                volume=scanner.component_volume(m) / scanner.component_volume(source),
                to_dec=(float(np.percentile(to_dec, 99)), float(to_dec.max())),
                to_src=(float(np.percentile(to_src, 99)), float(to_src.max())),
                shells=shell_max, tips=[float(to_dec[i]) for i in tips])


def compare(path, a_path, b_path):
    source = load(path)
    diag = scanner.diagonal(source)
    res = {}
    for name, p in (('defaults', a_path), ('planarquadric', b_path)):
        z = np.load(p)
        res[name] = measure(source, Geometry(z['verts'], z['faces']))
        r = res[name]
        print(f"{name:13} nm={r['nm']} open={r['open']} volume ratio={r['volume']:.5f} "
              f"src->dec p99/max={r['to_dec'][0]:.4g}/{r['to_dec'][1]:.4g} "
              f"dec->src p99/max={r['to_src'][0]:.4g}/{r['to_src'][1]:.4g} "
              f"shells(>=100f)={len(r['shells'])} worst shell max={max(r['shells'], default=0):.4g} "
              f"tips={[round(t, 4) for t in r['tips']]}", flush=True)
    d, q = res['defaults'], res['planarquadric']
    checks = []
    for key in ('to_dec', 'to_src'):
        checks.append((f'{key} p99', q[key][0] <= max(1.25 * d[key][0], 1e-4 * diag)))
        checks.append((f'{key} max', q[key][1] <= max(1.25 * d[key][1], 1e-3 * diag)))
    checks.append(('volume ratio', abs(q['volume'] - d['volume']) <= 0.005))
    checks.append(('every shell', all(qs <= max(1.25 * ds, 1e-3 * diag)
                                      for qs, ds in zip(q['shells'], d['shells']))))
    checks.append(('tips', all(qt <= max(1.25 * dt, 1e-3 * diag)
                               for qt, dt in zip(q['tips'], d['tips']))))
    print(f"diag={diag:.4g}  " + '  '.join(f"{n}: {'ok' if ok else 'FAIL'}" for n, ok in checks),
          flush=True)


def main():
    mode = sys.argv[1]
    if mode == 'run':
        run(sys.argv[2], sys.argv[3], sys.argv[4],
            int(sys.argv[5]) if len(sys.argv) > 5 else 900000)
    elif mode == 'compare':
        compare(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
