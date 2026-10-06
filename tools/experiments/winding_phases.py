#!/usr/bin/env python3
"""Experiment: where does `winding.reconstruct`'s memory peak go?

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/winding_phases.py MODEL BLOCKS [--plain]

MODEL is an STL path (the whole file is one part, as in the 2026-10-03
calibration) or `sphereR` for an icosphere of radius R (subdivisions 4,
5,120 faces: a small input that rebuilds to a large output). The grid
spacing is the batch's, from the model's diagonal.

Default: a copy of `reconstruct` / `_weld` / `_check` with probes at phase
boundaries. Each probe prints the phase's peak RSS (VmHWM, then reset via
/proc/self/clear_refs) and the RSS left when it ends, plus wall time. The
copy keeps production's array lifetimes; it refuses to run if the module's
source no longer matches the copy (SOURCE_SHA). Per-block phases are
reported as the maximum over blocks; the libigl distance and winding-number
calls are timed by wrapping them (no memory effect).

`--plain`: the unmodified `winding.reconstruct`, and the process's own peak
(VmHWM never reset) — the acceptance figure. Run each configuration in a
fresh process.

Read-only on inputs. Not wired into the pipeline. Phase peaks overlap with
arrays still alive from earlier phases and with allocator retention, so they
are not additive costs.
"""

import hashlib
import inspect
import math
import sys
import time

import numpy as np

from libs import mesh_io, scanner, winding
from libs.mesh_io import Geometry, Mesh, Kind

#: sha256 of inspect.getsource of reconstruct + _weld + _check this copy follows.
SOURCE_SHA = 'b509e4d0c82629ee60f1fa33d3a39c5a1293e61c2bd8b8a71ad08bf45d51f9e1'


def status():
    out = {}
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith(('VmHWM', 'VmRSS')):
                key, value = line.split(':')
                out[key] = int(value.split()[0]) * 1024
    return out


class Probe:
    def __init__(self):
        self.t = time.monotonic()
        self.rows = {}
        self.order = []
        self.reset()

    def reset(self):
        with open('/proc/self/clear_refs', 'w') as f:
            f.write('5')

    def __call__(self, phase):
        s = status()
        now = time.monotonic()
        dt = now - self.t
        if phase not in self.rows:
            self.order.append(phase)
            self.rows[phase] = [0, 0, 0.0, 0]
        row = self.rows[phase]
        row[0] = max(row[0], s['VmHWM'])
        row[1] = s['VmRSS']            # RSS after the last occurrence
        row[2] += dt
        row[3] += 1
        self.reset()
        self.t = time.monotonic()

    def report(self):
        print(f"{'phase':24} {'n':>5} {'peak GB':>8} {'rss after':>9} {'time s':>8}")
        for p in self.order:
            peak, rss, dt, n = self.rows[p]
            print(f"{p:24} {n:>5} {peak / 1e9:>8.3f} {rss / 1e9:>9.3f} {dt:>8.1f}")


TIMES = {'fwn': 0.0, 'fwn_calls': 0, 'distance': 0.0}


def wrap_igl():
    real_fwn = winding._igl.fast_winding_number

    def fwn(*a, **k):
        t = time.monotonic()
        try:
            return real_fwn(*a, **k)
        finally:
            TIMES['fwn'] += time.monotonic() - t
            TIMES['fwn_calls'] += 1

    winding._igl.fast_winding_number = fwn
    real_aabb = winding._igl.AABB

    class Timed(real_aabb):
        def squared_distance(self, *a, **k):
            t = time.monotonic()
            try:
                return super().squared_distance(*a, **k)
            finally:
                TIMES['distance'] += time.monotonic() - t

    winding._igl.AABB = Timed


def source_sha():
    src = ''.join(inspect.getsource(f) for f in (winding.reconstruct, winding._weld, winding._check))
    return hashlib.sha256(src.encode()).hexdigest()


def probed_reconstruct(mesh, h, blocks_per_axis, probe):
    """`winding.reconstruct` with probes; same statements and lifetimes."""
    w = winding
    w._check_spacing(h)
    V, F = w._validated(mesh)
    lo, shape = w._grid(V, h)
    if isinstance(blocks_per_axis, bool) or not (isinstance(blocks_per_axis, int) and 1 <= blocks_per_axis <= w.max_blocks_per_axis(shape)):
        raise ValueError(f'blocks_per_axis must be 1..{w.max_blocks_per_axis(shape)}')
    ny, nz = shape[1], shape[2]
    probe('validate+grid')

    A, B, C, _, L, H = w._triangle_frames(V, F)
    n1, n2, bound = w._sample_counts(L, H, h)
    keys = w._surface_keys(A, B, C, n1, n2, bound, lo, h, shape)
    del A, B, C, L, H, n1, n2, bound
    probe('surface keys')

    F = w._oriented(V, F)
    probe('orient')
    tree = w._igl.AABB()
    tree.init(V, F)
    probe('distance tree')
    cuts = [w._cuts(n, blocks_per_axis) for n in shape]
    far_value = (w.BAND + 1) * h
    pieces_v, pieces_f, pieces_e, offset = [], [], [], 0
    for bx in range(blocks_per_axis):
        for by in range(blocks_per_axis):
            for bz in range(blocks_per_axis):
                r0 = np.array([cuts[0][bx], cuts[1][by], cuts[2][bz]])
                r1 = np.array([cuts[0][bx + 1], cuts[1][by + 1], cuts[2][bz + 1]])
                block_shape = tuple(int(n) for n in r1 - r0 + 1)
                if math.prod(block_shape) >= 2 ** 32:
                    raise RuntimeError(f'block {block_shape} has too many grid points '
                                       'for marching cubes edge keys')
                field = w._block_field(V, F, tree, keys, r0, r1, lo, h, ny, nz, far_value)
                probe('block: field')
                axes = [lo[a] + h * np.arange(r0[a], r1[a] + 1) for a in range(3)]
                grid = np.column_stack([g.ravel('F') for g in np.meshgrid(*axes, indexing='ij')])
                mv, mf, e2v = w._igl.marching_cubes(field.ravel('F'), grid, *field.shape, 0.0)
                del grid, field
                if len(mf):
                    pieces_v.append(mv)
                    pieces_f.append(mf + offset)
                    pieces_e.append(w._edge_ids(e2v, len(mv), block_shape, r0))
                    offset += len(mv)
                probe('block: mc+edge ids')  # mv, mf, e2v live on into the next block, as in production
    if not pieces_f:
        w._raise_empty(mesh, V, F)
    Vo, Fo, Eo = np.vstack(pieces_v), np.vstack(pieces_f), np.vstack(pieces_e)
    held = (Vo, Fo, Eo)        # production: the vstacked arguments stay referenced
    probe('vstack (lists alive)')  # by the caller until _weld returns
    # _weld's body. The lists stay alive during the weld and the check, as in
    # production (reconstruct holds them until it returns).
    # `_weld`'s own `_` is a separate local; reconstruct's `_` (triangle
    # areas) stays alive until reconstruct returns.
    _unique, first, inv = np.unique(Eo, axis=0, return_index=True, return_inverse=True)
    probe('weld: unique')
    Vo, Fo = Vo[first], inv.ravel()[Fo]
    Fo = Fo[(Fo[:, 0] != Fo[:, 1]) & (Fo[:, 1] != Fo[:, 2]) & (Fo[:, 0] != Fo[:, 2])]
    geometry = Geometry(Vo, Fo.astype(np.int64, copy=False))
    del Vo, Fo, Eo, first, inv, _unique, held  # _weld's locals and arguments die when it returns
    probe('weld: reindex')
    out = mesh.with_geometry(geometry)
    del geometry
    probe('with_geometry')
    # `_check`'s body.
    if len(out.geometry.faces) == 0:
        raise RuntimeError('reconstruction produced no faces')
    if not np.isfinite(out.geometry.verts).all():
        raise RuntimeError('reconstruction produced non-finite coordinates')
    scan = scanner.scan(out)
    if scan.open_edges or scan.degenerate:
        raise RuntimeError(
            f'reconstruction is not closed: open={scan.open_edges}, '
            f'degenerate={scan.degenerate}, non_manifold={scan.non_manifold}')
    probe('check: scan')
    # production: freed when reconstruct returns
    del pieces_v, pieces_f, pieces_e, mv, mf, e2v, _
    print(f"open={scan.open_edges} degenerate={scan.degenerate} nm={scan.non_manifold}")
    return out


def load_model(name):
    if name.startswith('sphere'):
        from tests.tests.test_decimator import _sphere
        r = float(name[len('sphere'):])
        v, f = _sphere(subdivisions=4)
        v = v / np.linalg.norm(v, axis=1)[:, None] * r
        g = Geometry(np.asarray(v, np.float64), np.asarray(f, np.int64))
        return Mesh('/in/s.stl', '/out/s.stl', Kind.BINARY_STL, len(g.faces), True, None, g)
    return mesh_io.load(mesh_io.probe(name, '/nonexistent/out.stl'))


def main():
    name, blocks = sys.argv[1], int(sys.argv[2])
    plain = '--plain' in sys.argv
    mesh = load_model(name)
    h = winding.grid_spacing(scanner.diagonal(mesh))
    p = winding.plan(mesh, h, 10 ** 13)          # only for the estimate at this block count
    est = winding.estimate_bytes(len(mesh.geometry.faces),
                                 float(sum(winding._triangle_frames(*winding._validated(mesh))[3])),
                                 h, p.samples_bound,
                                 winding._largest_block(p.shape, blocks))
    print(f"model={name} faces_in={len(mesh.geometry.faces)} h={h:g} shape={p.shape} "
          f"blocks={blocks}^3 estimate={est / 1e9:.2f} GB", flush=True)
    base = status()['VmRSS']
    print(f"rss before reconstruct {base / 1e9:.3f} GB")
    t = time.monotonic()
    if plain:
        out = winding.reconstruct(mesh, h, blocks)
        print(f"PLAIN peak={status()['VmHWM'] / 1e9:.3f} GB time={time.monotonic() - t:.1f}s "
              f"faces_out={len(out.geometry.faces)}")
        return
    if source_sha() != SOURCE_SHA:
        sys.exit(f'winding.py changed since this copy (sha {source_sha()}); update the copy')
    wrap_igl()
    probe = Probe()
    out = probed_reconstruct(mesh, h, blocks, probe)
    probe.report()
    print(f"total {time.monotonic() - t:.1f}s faces_out={len(out.geometry.faces)} "
          f"verts_out={len(out.geometry.verts)} distance {TIMES['distance']:.1f}s "
          f"fwn {TIMES['fwn']:.1f}s in {TIMES['fwn_calls']} calls")


if __name__ == '__main__':
    main()
