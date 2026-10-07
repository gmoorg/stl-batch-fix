#!/usr/bin/env python3
"""Experiment: what does a fixed glibc mmap threshold do to the real child?

    tools/project_python.sh -u tools/experiments/malloc_child.py run WORKDIR \
        --rounds N --log FILE [--model NAME=PATH[,clean] ...] [--sphere]
        [--variant NAME[=BYTES] ...]

The 2026-10-07 weld investigation (reconstruction.md, "The spread follows
glibc malloc's policy") measured `GLIBC_TUNABLES=glibc.malloc.mmap_threshold`
on `reconstruct` alone. This runs the whole per-file child the batch spawns
(`batch_repair._spawn_child`, unchanged argv): a prepare child, then a repair
child loading what prepare handed off, each a fresh process, the variants
interleaved round by round and rotated (A B C, B C A, ...), never two
children at once. Before each prepare the model's decimation cache is
deleted, so every prepare decimates.

Variants: `default` (no allocator setting) or `NAME=BYTES` (the threshold
fixed at BYTES). The harness strips every `glibc.malloc.*` entry from
`GLIBC_TUNABLES` and every `MALLOC_*_` variable first, keeping other
tunables, then sets its variant in `os.environ`, which `_spawn_child`'s
`Popen` inherits.

Models: `NAME=PATH` runs with production settings (`skip_clean`); suffix
`,clean` runs with `skip_clean` off. `--sphere` adds the sphere r 132 seg 56
of test_jobmemory (written to WORKDIR) with `skip_clean` off: it is clean,
so the gate would otherwise skip reconstruction and the 13.9 GB
post-reconstruction decimation it is here for; a sphere run whose log lacks
`winding` or a post-reconstruction `decimate` is invalid. Each source is
copied into WORKDIR/in once; outputs go to WORKDIR/run-N.

Per child, one JSON line in FILE: model, mode, variant, allocator env,
config; `maxrss_kib` from `os.wait4` (lifetime maximum, KiB, as Linux
reports it); `sampled_peak` and `t_peak` from VmRSS read every 2 ms from
outside (bytes, seconds since spawn — misses peaks between samples);
`wall`; `exit`; the validated result (category, indicator, reason; a child
whose result fails `childresult.read_and_validate` is `valid: false`);
`steps`: every `end` and `info` row of its step log as (event, step, part,
seconds, detail);
`sha256` of the prepared PLY (prepare, when it decimated) or the written
output (repair); `swap`: pswpin/pswpout deltas (`valid: false` on any).

A failed child is kept and retried once; a second failure stops that
(model, variant). A valid child's geometry hash that differs from the
model's first valid one of the same mode stops the whole run, keeping that
run's directory (and the prepared cache) for inspection; successful runs'
directories are deleted.

Read-only on sources. Not wired into the pipeline.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PROJECT)

import batch_repair                                             # noqa: E402
from libs import childresult, mesh_io, runconfig                # noqa: E402

MAX_FACES = 900_000                       # batch_repair.toml, 2026-10-07
BUDGET_GB = 10.0
MIN_SHELL_FACES = 100
CHILD = os.path.join(PROJECT, 'batch_repair_child.py')
POST_RECONSTRUCTION = ('winding', 'decimate')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def swap():
    out = {}
    with open('/proc/vmstat') as f:
        for line in f:
            key, value = line.split()
            if key in ('pswpin', 'pswpout'):
                out[key] = int(value)
    return out


def allocator_env(threshold):
    """Strip allocator settings from os.environ, then set `threshold`
    (None: glibc's default). Returns what the child gets."""
    for key in [k for k in os.environ if k.startswith('MALLOC_') and k.endswith('_')]:
        del os.environ[key]
    kept = [t for t in os.environ.get('GLIBC_TUNABLES', '').split(':')
            if t and not t.startswith('glibc.malloc.')]
    if threshold is not None:
        kept.append(f'glibc.malloc.mmap_threshold={threshold}')
    if kept:
        os.environ['GLIBC_TUNABLES'] = ':'.join(kept)
    else:
        os.environ.pop('GLIBC_TUNABLES', None)
    return {'GLIBC_TUNABLES': os.environ.get('GLIBC_TUNABLES')}


class Sampler(threading.Thread):
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid, self.t0 = pid, time.monotonic()
        self.stop = threading.Event()
        self.peak, self.t_peak = 0, 0.0

    def run(self):
        path = f'/proc/{self.pid}/status'
        while not self.stop.is_set():
            try:
                with open(path) as f:
                    for line in f:
                        if line.startswith('VmRSS'):
                            rss = int(line.split()[1]) * 1024
                            if rss > self.peak:
                                self.peak, self.t_peak = rss, time.monotonic() - self.t0
                            break
            except (OSError, ValueError):
                pass                    # gone or a zombie: VmRSS absent
            time.sleep(0.002)


def write_sphere(path):
    sys.path.insert(0, PROJECT)
    from tests.tests import test_jobmemory as tj
    mesh = tj.mesh_of(tj.sphere(132.0, seg=56))
    mesh_io.write(mesh.with_destination(path))


def steps_of(log_file, offset):
    """`end` and `info` rows (`nm_fast_path` reports as `info`) appended to
    the step log since `offset`: (event, step, part, seconds, detail)."""
    out = []
    with open(log_file) as f:
        f.seek(offset)
        for line in f:
            cols = line.rstrip('\n').split('\t')
            if len(cols) >= 7 and cols[2] in ('end', 'info'):
                out.append([cols[2], cols[3], cols[4],
                            float(cols[5]) if cols[5] else None, cols[6]])
    return out


def run_child(mesh, mode, *, skip_clean, run_dir, input_root, load_from=None):
    """Spawn one real child, sample it, reap it with wait4 on this thread."""
    log_file = os.path.join(run_dir, 'steps.log')
    offset = os.path.getsize(log_file) if os.path.exists(log_file) else 0
    result_file = os.path.join(run_dir, f'{mode}.result.json')
    cache_path = (batch_repair.decimated_path(input_root, mesh.path, MAX_FACES)
                  if mode == 'prepare' else None)
    before = swap()
    with open(os.path.join(run_dir, 'child.log'), 'ab') as output_log:
        t0 = time.monotonic()
        proc = batch_repair._spawn_child(
            sys.executable, CHILD, mesh, MAX_FACES if mode == 'prepare' else 0,
            result_file, log_file, skip_clean=skip_clean, output_log=output_log,
            reconstruct_budget_bytes=runconfig.budget_bytes(BUDGET_GB),
            min_shell_faces=MIN_SHELL_FACES, mode=mode,
            cache_path=cache_path, load_from=load_from)
    sampler = Sampler(proc.pid)
    sampler.start()
    _, status, usage = os.wait4(proc.pid, 0)
    wall = time.monotonic() - t0
    sampler.stop.set()
    sampler.join()
    proc.returncode = os.waitstatus_to_exitcode(status)
    after = swap()
    expected = (batch_repair.expected_prepared_path(mesh, cache_path, MAX_FACES)
                if mode == 'prepare' else None)
    result = childresult.read_and_validate(result_file, mesh.path, mode=mode,
                                           expected_prepared=expected)
    rec = {'mode': mode, 'exit': proc.returncode, 'wall': round(wall, 3),
           'maxrss_kib': usage.ru_maxrss, 'sampled_peak': sampler.peak,
           't_peak': round(sampler.t_peak, 3),
           'swap': {k: after[k] - before[k] for k in after},
           'steps': steps_of(log_file, offset) if os.path.exists(log_file) else []}
    rec['valid'] = result is not None and not any(rec['swap'].values())
    if result is not None:
        rec['result'] = {'category': result.category, 'indicator': result.indicator,
                         'reason': result.reason}
        if mode == 'prepare' and result.is_handoff:
            rec['prepared_path'] = result.prepared_path
            if result.prepared_path != mesh.path:
                rec['sha256'] = sha256(result.prepared_path)
        elif mode == 'prepare':
            rec['valid'] = False        # ended the job before repair
        elif result.written_path:
            rec['sha256'] = sha256(result.written_path)
        if mode == 'repair' and result.category != 'published':
            rec['valid'] = False
    return rec


def run_pair(name, source, skip_clean, input_root, run_dir):
    """Prepare then repair one model; the records of both children."""
    os.makedirs(run_dir)
    shutil.rmtree(input_root + '.decimated', ignore_errors=True)
    dest = os.path.join(run_dir, 'out', os.path.basename(source))
    os.makedirs(os.path.dirname(dest))
    mesh = mesh_io.probe(source, dest)
    prep = run_child(mesh, 'prepare', skip_clean=skip_clean,
                     run_dir=run_dir, input_root=input_root)
    records = [prep]
    if prep['valid']:
        records.append(run_child(mesh, 'repair', skip_clean=skip_clean, run_dir=run_dir,
                                 input_root=input_root, load_from=prep['prepared_path']))
    return records


def parse_variant(text):
    name, _, value = text.partition('=')
    return name, (int(value) if value else None)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('workdir')
    r.add_argument('--rounds', type=int, required=True)
    r.add_argument('--log', required=True)
    r.add_argument('--model', action='append', default=[])
    r.add_argument('--sphere', action='store_true')
    r.add_argument('--variant', action='append', default=[])
    args = p.parse_args(argv)

    variants = [parse_variant(v) for v in args.variant] or [
        ('default', None), ('m128k', 131072), ('m4m', 4194304)]
    input_root = os.path.join(os.path.abspath(args.workdir), 'in')
    os.makedirs(input_root, exist_ok=True)
    models = []                          # (name, source copy, skip_clean)
    if args.sphere:
        path = os.path.join(input_root, 'sphere_r132.stl')
        if not os.path.exists(path):
            write_sphere(path)
        models.append(('sphere_r132', path, False))
    for spec in args.model:
        name, _, rest = spec.partition('=')
        src, _, flag = rest.partition(',')
        copy = os.path.join(input_root, name + '.stl')
        if not os.path.exists(copy):
            shutil.copyfile(src, copy)
        models.append((name, copy, flag != 'clean'))

    first = {}                           # (model, mode) -> first valid sha256
    stopped = set()                      # (model, variant)
    run_no = 0
    for rnd in range(args.rounds):
        order = variants[rnd % len(variants):] + variants[:rnd % len(variants)]
        for name, source, skip_clean in models:
            for vname, threshold in order:
                if (name, vname) in stopped:
                    continue
                env = allocator_env(threshold)
                for attempt in (1, 2):
                    run_no += 1
                    run_dir = os.path.join(os.path.abspath(args.workdir), f'run-{run_no}')
                    records = run_pair(name, source, skip_clean, input_root, run_dir)
                    ok = len(records) == 2 and all(r['valid'] for r in records)
                    mismatch = False
                    for rec in records:
                        if not skip_clean and name == 'sphere_r132' and rec['mode'] == 'repair':
                            seen = [s[1] for s in rec['steps'] if s[0] == 'end']
                            if 'winding' not in seen or 'decimate' not in seen[seen.index('winding'):]:
                                rec['valid'] = ok = False
                                rec['invalid_why'] = 'sphere did not reconstruct'
                        if rec['valid'] and 'sha256' in rec:
                            key = (name, rec['mode'])
                            first.setdefault(key, rec['sha256'])
                            rec['hash_matches'] = first[key] == rec['sha256']
                            mismatch |= not rec['hash_matches']
                        rec.update({'model': name, 'variant': vname, 'env': env,
                                    'round': rnd, 'attempt': attempt, 'run': run_no,
                                    'config': {'max_faces': MAX_FACES, 'skip_clean': skip_clean,
                                               'budget_gb': BUDGET_GB,
                                               'min_shell_faces': MIN_SHELL_FACES}})
                        with open(args.log, 'a') as f:
                            f.write(json.dumps(rec) + '\n')
                        print(f"{name} {vname} r{rnd} a{attempt} {rec['mode']}: "
                              f"wall {rec['wall']:.1f}s maxrss {rec['maxrss_kib'] / 2**20:.3f} GiB "
                              f"valid {rec['valid']}", flush=True)
                    if mismatch:
                        print(f'STOP: {name} {vname} geometry differs; kept {run_dir}', flush=True)
                        return 3
                    if ok:
                        shutil.rmtree(run_dir)
                        break
                else:
                    stopped.add((name, vname))
                    print(f'stopped {name} {vname}: failed twice', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
