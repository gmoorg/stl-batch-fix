#!/usr/bin/env python3
"""Experiment: pass a winding output that fails `_check` (NM edges) on
through the default post-reconstruction steps, and report topology and
volume. See docs/errors/winding-non-manifold.md.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/winding_post_steps.py FILE [--meshfix-only]

Default: winding (with `_check` disabled) -> decimate to the part's own face
count (the pipeline's target, `repairer.py` faceCount) -> MeshFix if the
decimated mesh still has defects. The batch's skip_clean gate is not applied;
pass a part that would fail it. `--meshfix-only` runs MeshFix directly
on the winding output, skipping decimation. Uses shell 1 of FILE, the batch's
grid spacing (whole-model diagonal) and the 10 GB plan. For a model decimated
in the batch, pass its `stl-decimated/...900000.stl` cache.

Read-only on inputs. Not wired into the pipeline.
"""

import sys
import time

from libs import decimator, mesh_io, meshfix, scanner, splitter, winding


def report(tag, m, extra=''):
    s = scanner.scan(m)
    print(f"{tag:12} faces={s.faces:>9} nm={s.non_manifold} open={s.open_edges} "
          f"degenerate={s.degenerate} volume={scanner.component_volume(m):.3f} {extra}",
          flush=True)


def main():
    path = sys.argv[1]
    mesh = mesh_io.load(mesh_io.probe(path, '/nonexistent/out.stl'))
    diag = scanner.diagonal(mesh)
    part = splitter.by_shells(mesh, min_faces=100)[0]
    h = winding.grid_spacing(diag)
    plan = winding.plan(part, h, 10 * 10**9)
    winding._check = lambda m: None
    rec = winding.reconstruct(part, h, plan.blocks_per_axis)
    report('input', part)
    report('winding', rec)
    if '--meshfix-only' in sys.argv:
        t = time.monotonic()
        r = meshfix.repair(rec)
        report('meshfix-only', r.mesh, f"ok={r.ok} {time.monotonic() - t:.1f}s {r.problem or ''}")
        return
    d = decimator.decimate(rec, len(part.geometry.faces))
    report('decimated', d.mesh, d.rung.value)
    if scanner.has_defects(scanner.scan(d.mesh)):
        t = time.monotonic()
        r = meshfix.repair(d.mesh)
        report('meshfix', r.mesh, f"ok={r.ok} {time.monotonic() - t:.1f}s {r.problem or ''}")
    else:
        print('meshfix      skipped: no defects after decimation')


if __name__ == '__main__':
    main()
