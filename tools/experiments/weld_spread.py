#!/usr/bin/env python3
"""Experiment: why does `winding.reconstruct`'s peak vary between runs?

    tools/project_python.sh -u tools/experiments/weld_spread.py run MODEL BLOCKS \
        --rounds N --log FILE --variant SPEC [--variant SPEC ...]

Runs `winding.reconstruct` unmodified (as `winding_phases.py --plain`, with
the same work before it) in a fresh child process per run, the variants
interleaved round by round (A B A B ...), never two children at once. Each
variant SPEC is `name[,KEY=VALUE...][,setarch-R][,tree=PATH][,weld-inputs]`:
environment variables for the child, ASLR off (`setarch -R`, which execs, so
the PID stays the python process), `libs` imported from another checkout
(e.g. a `git worktree` of an older commit), or a diagnostic run that hashes
`_weld`'s arguments (its wrapper keeps them referenced, so that run's peak is
not comparable; it is marked `diagnostic`).

Per run, one JSON line in FILE:
- `hwm`: the child's VmHWM, read right after `reconstruct` returns;
- `sampled_peak`, `t_peak`, `t_end`: max VmRSS from /proc/PID/status read
  every 2 ms from this process, its time and the time `reconstruct` returned
  (seconds since the child started);
- `ahp_at_peak`, `ahp_max`: AnonHugePages from /proc/PID/smaps_rollup, read
  every 200 ms in a second thread (at the sample nearest the peak, and the
  maximum), with the read's own mean and max duration;
- `vmstat`: deltas of swap and THP counters over the child's life
  (system-wide context, not attributable to the child); `valid` is false on
  any swap I/O or a failed child;
- `out`: dtype, shape and sha256 of the output vertices and faces, hashed
  after sampling stopped; `weld_inputs` likewise for diagnostic runs, and
  `mallinfo`: glibc's `mallinfo2()` plus VmRSS/VmHWM on entering `_weld`.
The 2026-10-07 runs (reconstruction.md, "The spread is glibc malloc's") are
in weld_spread_2026-10-07.jsonl.
The 100 ms timeline (t, VmRSS, last AnonHugePages) goes to FILE.<run>.tsv.

Read-only on inputs. Not wired into the pipeline.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
VMSTAT = ('pswpin', 'pswpout', 'thp_fault_alloc', 'thp_fault_fallback',
          'thp_collapse_alloc', 'thp_split_page')
SMAPS_EVERY = 0.2


def digest(a):
    """dtype, shape and sha256 of an array's bytes, without copying a
    contiguous array."""
    import numpy as np
    a = np.ascontiguousarray(a)
    return {'dtype': str(a.dtype), 'shape': list(a.shape),
            'sha256': hashlib.sha256(a.data).hexdigest()}


def mallinfo():
    """glibc's mallinfo2(), summed over all arenas: `arena` bytes obtained
    by sbrk/heaps, `hblkhd` in mmapped chunks, `uordblks` in use,
    `fordblks` free but kept by the allocator, `keepcost` trimmable top."""
    import ctypes

    class Mallinfo2(ctypes.Structure):
        _fields_ = [(n, ctypes.c_size_t) for n in (
            'arena', 'ordblks', 'smblks', 'hblks', 'hblkhd', 'usmblks', 'fsmblks',
            'uordblks', 'fordblks', 'keepcost')]

    libc = ctypes.CDLL('libc.so.6')
    libc.mallinfo2.restype = Mallinfo2
    m = libc.mallinfo2()
    return {n: getattr(m, n) for n, _ in m._fields_}


def child(name, blocks, weld_inputs):
    import winding_phases
    from libs import winding
    mesh, h = winding_phases.prepare(name, blocks)
    if weld_inputs:
        real = winding._weld

        def weld(Vo, Fo, keys):
            print('MALLINFO ' + json.dumps(dict(mallinfo(), **winding_phases.status())), flush=True)
            print('WELD_INPUTS ' + json.dumps([digest(a) for a in (Vo, Fo, keys)]), flush=True)
            return real(Vo, Fo, keys)

        winding._weld = weld
    print('START', flush=True)
    t = time.monotonic()
    out = winding.reconstruct(mesh, h, blocks)
    hwm = winding_phases.status()['VmHWM']
    print('PEAK ' + json.dumps({'hwm': hwm, 'time': time.monotonic() - t,
                                'libs': os.path.dirname(winding.__file__)}), flush=True)
    sys.stdin.readline()                # the parent stops sampling first
    g = out.geometry
    print('OUT ' + json.dumps({'verts': digest(g.verts), 'faces': digest(g.faces)}), flush=True)


def vmstat():
    out = {}
    with open('/proc/vmstat') as f:
        for line in f:
            key, value = line.split()
            if key in VMSTAT:
                out[key] = int(value)
    return out


class Sampler(threading.Thread):
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid, self.t0 = pid, time.monotonic()
        self.stop = threading.Event()
        self.peak, self.t_peak = 0, 0.0
        self.smaps = []                 # (t, AnonHugePages)
        self.read_times = []
        self.timeline = []

    def read_status(self):
        with open(f'/proc/{self.pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS'):
                    return int(line.split()[1]) * 1024
        return 0

    def read_ahp(self):
        t = time.monotonic()
        with open(f'/proc/{self.pid}/smaps_rollup') as f:
            for line in f:
                if line.startswith('AnonHugePages'):
                    self.read_times.append(time.monotonic() - t)
                    return int(line.split()[1]) * 1024
        return 0

    def run(self):
        smaps = threading.Thread(target=self.run_smaps, daemon=True)
        smaps.start()
        next_line = 0.0
        while not self.stop.is_set():
            now = time.monotonic() - self.t0
            try:
                rss = self.read_status()
            except (FileNotFoundError, ProcessLookupError):
                break
            if rss > self.peak:
                self.peak, self.t_peak = rss, now
            if now >= next_line:
                self.timeline.append((now, rss, self.smaps[-1][1] if self.smaps else 0))
                next_line = now + 0.1
            time.sleep(0.002)
        smaps.join()

    def run_smaps(self):
        """AnonHugePages every SMAPS_EVERY s, in its own thread: one read
        walks the child's page tables (6–28 ms on a 1 GB process), which
        would stall the 2 ms VmRSS sampling."""
        while not self.stop.is_set():
            now = time.monotonic() - self.t0
            try:
                self.smaps.append((now, self.read_ahp()))
            except (FileNotFoundError, ProcessLookupError):
                return
            self.stop.wait(SMAPS_EVERY)

    def summary(self):
        near = min(self.smaps, key=lambda s: abs(s[0] - self.t_peak), default=(0, 0))
        rt = self.read_times or [0.0]
        return {'sampled_peak': self.peak, 't_peak': round(self.t_peak, 3),
                'ahp_at_peak': near[1], 'ahp_at_peak_dt': round(near[0] - self.t_peak, 3),
                'ahp_max': max((s[1] for s in self.smaps), default=0),
                'smaps_read_ms': [round(1e3 * sum(rt) / len(rt), 3), round(1e3 * max(rt), 3)]}


def parse_variant(spec):
    name, *parts = spec.split(',')
    v = {'name': name, 'env': {}, 'setarch': False, 'tree': PROJECT, 'weld_inputs': False}
    for p in parts:
        if p == 'setarch-R':
            v['setarch'] = True
        elif p == 'weld-inputs':
            v['weld_inputs'] = True
        elif p.startswith('tree='):
            v['tree'] = os.path.abspath(p[len('tree='):])
        elif '=' in p:
            key, value = p.split('=', 1)
            v['env'][key] = value
        else:
            sys.exit(f'bad variant part {p!r} in {spec!r}')
    return v


def one_run(v, model, blocks, log, index):
    cmd = [os.path.join(PROJECT, 'tools', 'project_python.sh'), '-u', os.path.abspath(__file__),
           'child', model, str(blocks)] + (['--weld-inputs'] if v['weld_inputs'] else [])
    if v['setarch']:
        cmd = ['setarch', '-R'] + cmd
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([v['tree'], HERE]), **v['env'])
    before = vmstat()
    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, bufsize=1)
    sampler = Sampler(proc.pid)
    sampler.start()
    rec = {'run': index, 'variant': v['name'], 'env': v['env'], 'setarch': v['setarch'],
           'tree': v['tree'], 'diagnostic': v['weld_inputs'], 'pid': proc.pid}
    for line in proc.stdout:
        if line.startswith('PEAK '):
            sampler.stop.set()
            sampler.join()
            rec['t_end'] = round(time.monotonic() - sampler.t0, 3)
            rec.update(json.loads(line[5:]))
            rec.update(sampler.summary())
            proc.stdin.write('\n')
            proc.stdin.flush()
        elif line.startswith('OUT '):
            rec['out'] = json.loads(line[4:])
        elif line.startswith('MALLINFO '):
            rec['mallinfo'] = json.loads(line[9:])
        elif line.startswith('WELD_INPUTS '):
            rec['weld_inputs'] = json.loads(line[12:])
        elif line.startswith('model='):
            rec['header'] = line.strip()
    code = proc.wait()
    sampler.stop.set()
    after = vmstat()
    rec['vmstat'] = {k: after.get(k, 0) - before.get(k, 0) for k in VMSTAT}
    rec['exit'] = code
    rec['valid'] = (code == 0 and 'hwm' in rec and 'out' in rec
                    and rec['vmstat']['pswpin'] == 0 and rec['vmstat']['pswpout'] == 0)
    with open(log, 'a') as f:
        f.write(json.dumps(rec) + '\n')
    with open(f'{log}.{index}.tsv', 'w') as f:
        f.writelines(f'{t:.3f}\t{rss}\t{ahp}\n' for t, rss, ahp in sampler.timeline)
    print(f"run {index} {v['name']}: hwm={rec.get('hwm', 0) / 1e9:.3f} "
          f"sampled={rec.get('sampled_peak', 0) / 1e9:.3f} "
          f"ahp@peak={rec.get('ahp_at_peak', 0) / 1e9:.3f} GB valid={rec['valid']} "
          f"swap={rec['vmstat']['pswpin']}/{rec['vmstat']['pswpout']} "
          f"thp_fault_alloc={rec['vmstat']['thp_fault_alloc']} "
          f"fallback={rec['vmstat']['thp_fault_fallback']}", flush=True)


def main():
    if sys.argv[1] == 'child':
        child(sys.argv[2], int(sys.argv[3]), '--weld-inputs' in sys.argv)
        return
    args = sys.argv[2:]
    model, blocks = args[0], int(args[1])
    rounds, log, variants = 1, None, []
    i = 2
    while i < len(args):
        if args[i] == '--rounds':
            rounds = int(args[i + 1])
        elif args[i] == '--log':
            log = args[i + 1]
        elif args[i] == '--variant':
            variants.append(parse_variant(args[i + 1]))
        else:
            sys.exit(f'unknown argument {args[i]!r}')
        i += 2
    if not (log and variants):
        sys.exit('--log and at least one --variant are required')
    start = 0
    if os.path.exists(log):
        with open(log) as f:
            start = sum(1 for _ in f)
    index = start
    for _ in range(rounds):
        for v in variants:
            one_run(v, model, blocks, log, index)
            index += 1


if __name__ == '__main__':
    main()
