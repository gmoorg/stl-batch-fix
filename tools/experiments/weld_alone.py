#!/usr/bin/env python3
"""Experiment: `winding._weld` alone, on inputs saved from a real run.

    PYTHONPATH=.:tools/experiments tools/project_python.sh -u \
        tools/experiments/weld_alone.py save MODEL BLOCKS DIR
    PYTHONPATH=. tools/project_python.sh tools/experiments/weld_alone.py run DIR [LABEL]

`save` runs `reconstruct` once (after `winding_phases.prepare`) with `_weld`
wrapped to write its three arguments to DIR as .npy and print their digests
(dtype, shape, sha256) and the output's; that run's peak is not comparable.
`run` calls `_weld(np.load(...), np.load(...), np.load(...))` with the
arguments as call temporaries, as in production, and prints one JSON line:
VmHWM, the RSS before loading, the RSS after, and the time. Run it in a
fresh process per measurement, wrapped as needed (`setarch -R`, env vars).
The 2026-10-07 runs are in weld_alone_2026-10-07.jsonl.

Read-only on inputs. Not wired into the pipeline.
"""

import json
import sys
import time

import numpy as np

from libs import winding


def status():
    out = {}
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith(('VmHWM', 'VmRSS')):
                key, value = line.split(':')
                out[key] = int(value.split()[0]) * 1024
    return out


def save(name, blocks, out):
    import weld_spread
    import winding_phases
    mesh, h = winding_phases.prepare(name, blocks)
    real = winding._weld

    def weld(Vo, Fo, keys):
        print('WELD_INPUTS ' + json.dumps([weld_spread.digest(a) for a in (Vo, Fo, keys)]),
              flush=True)
        for n, a in zip(('Vo', 'Fo', 'keys'), (Vo, Fo, keys)):
            np.save(f'{out}/{n}.npy', a)
        return real(Vo, Fo, keys)

    winding._weld = weld
    g = winding.reconstruct(mesh, h, blocks).geometry
    print('OUT ' + json.dumps({'verts': weld_spread.digest(g.verts),
                               'faces': weld_spread.digest(g.faces)}), flush=True)


def run(d, label):
    base = status()['VmRSS']
    t = time.monotonic()
    g = winding._weld(np.load(f'{d}/Vo.npy'), np.load(f'{d}/Fo.npy'), np.load(f'{d}/keys.npy'))
    s = status()
    print(json.dumps({'label': label, 'hwm': s['VmHWM'], 'base': base, 'after': s['VmRSS'],
                      't': round(time.monotonic() - t, 2), 'faces': len(g.faces)}))


if __name__ == '__main__':
    if sys.argv[1] == 'save':
        save(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        run(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else '')
