#!/usr/bin/env python3
"""Experiment: memory peak of post-reconstruction decimation, and of MeshFix
on its output, with and without vertices thrown off the surface. See
docs/errors/post-wrap-meshfix-timeout.md.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/placement_memory_probe.py MODE ...

Modes (one process each, so every peak is that step's own):

    rebuild FILE OUT.npz       winding on shell 1 of FILE (batch grid spacing,
                               10 GB plan); saves verts, faces, target (the
                               part's face count, the pipeline's decimation
                               target) and the source path
    decimate IN.npz VARIANT OUT.npz
                               quadric decimation to the target; VARIANT is
                               defaults (the batch), placement-off
                               (optimalplacement=False), planarquadric or
                               preservetopology (each =True, else defaults);
                               `on` / `off` are accepted for the first two.
                               Saves the result
    meshfix IN.npz             meshfix.repair on IN; cap it with `timeout`

Each mode prints RSS before the step, RSS every INTERVAL seconds while it
runs (env INTERVAL, default 10), and VmHWM (process peak) at the end. The
peak includes loading IN.npz; the RSS before the step is the baseline.

Writes only the OUT paths given. Not wired into the pipeline.
"""

import os
import sys
import threading
import time

import numpy as np

from libs import meshfix, meshlab, mesh_io, scanner, splitter, winding
from libs.mesh_io import Geometry


def status_gib(key):
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith(key + ':'):
                return int(line.split()[1]) / 1024 / 1024
    return float('nan')


def sample(stop, t0, interval):
    while not stop.wait(interval):
        print(f"  [{time.monotonic() - t0:6.0f}s] rss={status_gib('VmRSS'):.2f} GiB", flush=True)


def measured(tag, fn):
    """Run fn, printing RSS before, while and after, and the process peak."""
    print(f"{tag}: before rss={status_gib('VmRSS'):.2f} GiB", flush=True)
    stop, t0 = threading.Event(), time.monotonic()
    th = threading.Thread(target=sample,
                          args=(stop, t0, float(os.environ.get('INTERVAL', 10))), daemon=True)
    th.start()
    try:
        return fn()
    finally:
        stop.set()
        print(f"{tag}: {time.monotonic() - t0:.1f}s after rss={status_gib('VmRSS'):.2f} GiB "
              f"peak(VmHWM)={status_gib('VmHWM'):.2f} GiB", flush=True)


#: Extra parameters per variant, on top of `targetfacenum`.
VARIANTS = {'defaults': {}, 'on': {},
            'placement-off': {'optimalplacement': False}, 'off': {'optimalplacement': False},
            'planarquadric': {'planarquadric': True},
            'preservetopology': {'preservetopology': True}}


def load_npz(path):
    z = np.load(path)
    src = str(z['source'])
    mesh = mesh_io.probe(src, '/nonexistent/out.stl').with_geometry(
        Geometry(z['verts'], z['faces']))
    return mesh, int(z['target']), src


def save_npz(path, G, target, src):
    np.savez(path, verts=G.verts, faces=G.faces, target=target, source=src)


def report(tag, mesh):
    s = scanner.scan(mesh)
    print(f"{tag}: faces={s.faces} nm={s.non_manifold} open={s.open_edges} "
          f"degenerate={s.degenerate}", flush=True)


def main():
    mode = sys.argv[1]
    if mode == 'rebuild':
        src, out = sys.argv[2], sys.argv[3]
        mesh = mesh_io.load(mesh_io.probe(src, '/nonexistent/out.stl'))
        part = splitter.by_shells(mesh, min_faces=100)[0]
        h = winding.grid_spacing(scanner.diagonal(mesh))
        rec = measured('winding', lambda: winding.reconstruct(
            part, h, winding.plan(part, h, 10 * 10**9).blocks_per_axis))
        report('winding', rec)
        save_npz(out, rec.geometry, len(part.geometry.faces), src)
    elif mode == 'decimate':
        inp, variant, out = sys.argv[2], sys.argv[3], sys.argv[4]
        mesh, target, src = load_npz(inp)
        G = measured(f"decimate {variant}", lambda: meshlab.apply_filters(
            mesh, (('meshing_decimation_quadric_edge_collapse',
                    {'targetfacenum': target, **VARIANTS[variant]}),)).geometry)
        report('decimated', mesh.with_geometry(G))
        save_npz(out, G, target, src)
    elif mode == 'meshfix':
        mesh, _, _ = load_npz(sys.argv[2])
        report('input', mesh)
        r = measured('meshfix', lambda: meshfix.repair(mesh))
        report(f"meshfix ok={r.ok} {r.problem or ''}", r.mesh)
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
