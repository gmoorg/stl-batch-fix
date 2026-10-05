#!/usr/bin/env python3
"""Experiment: load a mesh file straight into PyMeshLab, decimate it, and
report memory and time. It measures the planned "decimate from file" path; see
docs/errors/decimation-memory-path.md.

    tools/project_python.sh -X faulthandler -u tools/experiments/meshlab_file_decimate.py IN [TARGET]

TARGET is the face target (default 900000, the batch `max_faces`). Prints
RSS / high-water mark after load and after decimation, vertex and face counts,
phase times, and non-finite vertex counts in PyMeshLab's mesh. For a binary
STL it also counts the faces with NaN/inf coordinates in the raw file, so a
silent drop by the importer shows up as a face-count difference.

A sampler thread prints RSS every 15 s, so a run killed by the OOM killer
still shows how far it got. For inputs that may not fit, cap it so the
desktop survives:

    systemd-run --user --scope -q -p MemoryMax=22800M -p MemorySwapMax=0 \
        tools/project_python.sh -u tools/experiments/meshlab_file_decimate.py IN > out.txt 2>&1

Read-only: the input is only read and the result is never written. Not wired
into the pipeline.
"""

import os
import sys
import threading
import time

import numpy as np
import pymeshlab

T0 = time.monotonic()
STL_HEADER = 84
STL_RECORD = 50


def mem() -> str:
    out = {}
    with open('/proc/self/status') as f:
        for line in f:
            if line.startswith(('VmRSS', 'VmHWM')):
                key, val = line.split(':')
                out[key] = int(val.split()[0]) / 1024 / 1024   # GiB
    return f"rss={out['VmRSS']:.2f}G peak={out['VmHWM']:.2f}G"


def sampler() -> None:
    while True:
        time.sleep(15)
        print(f"  [{time.monotonic() - T0:6.0f}s] {mem()}", flush=True)


def raw_stl_nonfinite(path: str) -> str | None:
    """Faces with a non-finite coordinate in a binary STL, read straight from
    its records; None when the file is not a size-consistent binary STL."""
    size = os.path.getsize(path)
    if not path.lower().endswith('.stl') or size < STL_HEADER:
        return None
    with open(path, 'rb') as f:
        f.seek(80)
        count = int.from_bytes(f.read(4), 'little')
    if STL_HEADER + count * STL_RECORD != size:
        return None
    # Read in chunks so this scan stays small next to the import: VmHWM
    # keeps the process peak, and the scan runs before it.
    bad = nan = inf = 0
    chunk = 1 << 20
    for start in range(0, count, chunk):
        n = min(chunk, count - start)
        raw = np.fromfile(path, dtype=np.uint8, count=n * STL_RECORD,
                          offset=STL_HEADER + start * STL_RECORD).reshape(-1, STL_RECORD)
        v = raw[:, 12:48].copy().view(np.float32).reshape(-1, 9)
        bad += int((~np.isfinite(v).all(axis=1)).sum())
        nan += int(np.isnan(v).sum())
        inf += int(np.isinf(v).sum())
    return (f"raw stl: triangles={count} non-finite faces={bad} "
            f"nan={nan} inf={inf}")


def nonfinite_vertices(m) -> int:
    return int((~np.isfinite(m.vertex_matrix())).any(axis=1).sum())


def main() -> None:
    path = sys.argv[1]
    target = int(sys.argv[2]) if len(sys.argv) > 2 else 900000
    raw = raw_stl_nonfinite(path)
    if raw is not None:
        print(raw, flush=True)

    threading.Thread(target=sampler, daemon=True).start()
    print(f"start   {mem()}", flush=True)
    t = time.monotonic()
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(path)
    m = ms.current_mesh()
    print(f"loaded  {mem()}  v={m.vertex_number()} f={m.face_number()} "
          f"{time.monotonic() - t:.1f}s", flush=True)
    print(f"  non-finite vertices after load: {nonfinite_vertices(m)}", flush=True)

    t = time.monotonic()
    ms.apply_filter('meshing_decimation_quadric_edge_collapse',
                    targetfacenum=target)
    m = ms.current_mesh()
    print(f"decim   {mem()}  v={m.vertex_number()} f={m.face_number()} "
          f"{time.monotonic() - t:.1f}s", flush=True)
    print(f"  non-finite vertices after decimate: {nonfinite_vertices(m)}", flush=True)


if __name__ == '__main__':
    main()
