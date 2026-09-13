#!/usr/bin/env python3
"""
STL Batch Fix
=============
Fixes every STL file in a folder using non-destructive mesh repair:
merge duplicate vertices, fill holes, fix normals — without remeshing.
Original topology and flat surfaces are preserved, so printed flat
contact areas come out clean.

Requires: blender (tested with 4.0)
"""

import argparse
import concurrent.futures
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

try:
    import pymeshfix as _pymeshfix
    _PYMESHFIX_AVAILABLE = True
except ImportError:
    _PYMESHFIX_AVAILABLE = False

try:
    import pymeshlab as _pymeshlab
    _PYMESHLAB_AVAILABLE = True
except ImportError:
    _PYMESHLAB_AVAILABLE = False

try:
    import numpy as _np
    import fast_simplification as _fastsimp
    _FASTSIMP_AVAILABLE = True
except ImportError:
    _FASTSIMP_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INPUT_FOLDER   = os.environ.get('INPUT_FOLDER', "/mnt/sda2/STL/Fixing/")
OUTPUT_SUFFIX  = ""
MERGE_DIST     = 0.01 # mm — merge vertices closer than this (T-junction fix)
# mm — the finest layer this collection is printed at.  An open boundary loop
# smaller than one layer cannot be expressed by the slicer: it produces no
# toolpath, so closing it changes nothing that reaches the plate.  Repairs that
# chase such holes are not free, and on one model the chase was catastrophic —
# see _open_loops_are_printable() for the measurements.  0 disables the
# tolerance and restores the old "open edges must be exactly zero" rule.
MIN_LAYER      = 0.6
# run.sh and install.sh both document BLENDER_BIN as the way to point at a
# non-PATH Blender, and install.sh probes it — but this module ignored it, so a
# custom path passed the installer's check and then failed here as "not found".
BLENDER        = os.environ.get("BLENDER_BIN", "blender")
RECURSIVE      = True
WORKERS        = 0      # parallel workers; 0 = auto from cores and memory budget
# seconds — the budget for one mesh, whether that mesh is a whole unsplit model
# or a single shell part.  This is the limit almost every file is judged by.
# A six-shell file used to do six repairs under one shared budget and was killed
# for being multi-part rather than slow; each part now gets its own.
TIMEOUT_PART   = 600
# percent of TIMEOUT_PART held back from Blender for the steps that follow it
# (post-verify scan, a possible post-blender PyMeshFix pass, writing output).
# Measured over 17 Blender invocations across two runs: 10 needed no post-work,
# 2 took ~9s, and 5 took 100.7-186.4s — p95 was 152.6s, which is 25.4% of a
# 600s budget.  30% covers that with headroom.  Blender gets
#     TIMEOUT_PART - elapsed - (TIMEOUT_PART * BLENDER_RESERVE_PCT / 100)
BLENDER_RESERVE_PCT = 30
# below this many seconds a Blender run is not worth starting: it would be
# killed before it could finish and the mesh would pay the time for nothing.
_BLENDER_MIN_RUN = 30.0
# seconds — ceiling for a file that splits, and nothing else.  An unsplit model
# is capped by TIMEOUT_PART alone and never reaches this.  A split file's cap is
#     min(TIMEOUT, TIMEOUT_PART * n_parts)
# so the ceiling only binds when a file has enough parts to exceed it: a 2-part
# file gets 1200s, a 40-part file gets TIMEOUT rather than 24000s.
TIMEOUT        = 3_600
MAX_FACES      = 900_000    # decimate if face count exceeds this (0 = disabled)
LOG_FILE       = "/mnt/sda2/STL/Fixed/repair_log.tsv"
# Files decimated by at least this factor are listed in REVIEW_FILE.  Heavy
# decimation is where thin features and small connector holes (magnet sockets,
# pin holes) are most likely to have been distorted or lost, so those outputs
# are worth checking before printing.
REVIEW_RATIO   = 2.0
REVIEW_FILE    = "/mnt/sda2/STL/Fixed/review_decimated.tsv"

# How many previous runs to keep alongside each log, as <name>.1 … <name>.N.
# The logs are a diagnostic instrument for the run that just finished, so the
# live file is always truncated; the point of keeping a few generations is that
# a bad run can be restarted before its evidence has been read.  Bounded so the
# folder cannot grow without limit.
LOG_KEEP       = 5

# ---------------------------------------------------------------------------
# CLI overrides
#
# Only parsed when run as a script.  Importing this module must never consume
# the importer's own sys.argv — the TUI imports it, as does anything else that
# reuses these helpers, and argparse would abort the host program on any
# argument it doesn't recognise.
# ---------------------------------------------------------------------------

if __name__ == '__main__' and len(sys.argv) > 1:
    parser = argparse.ArgumentParser(description="Batch fix STL files (non-destructive repair)")
    parser.add_argument('--input',       default=None,  help=f"Input folder (default: {INPUT_FOLDER})")
    parser.add_argument('--suffix',      default=None,  help=f"Output filename suffix (default: {OUTPUT_SUFFIX!r})")
    parser.add_argument('--merge-dist',  type=float, default=None,
                        help=f"Vertex merge distance in mm (default: {MERGE_DIST})")
    parser.add_argument('--blender-reserve-pct', type=int, default=None,
                        help=f"Percent of TIMEOUT_PART held back from Blender "
                             f"for the steps after it (default: "
                             f"{BLENDER_RESERVE_PCT})")
    parser.add_argument('--min-layer',   type=float, default=None,
                        help=f"Finest print layer in mm; open boundaries smaller "
                             f"than this are accepted as-is (default: {MIN_LAYER}, "
                             f"0=require zero open edges)")
    parser.add_argument('--recursive',    dest='recursive', action='store_true',  default=None,
                        help=f"Search input folder recursively (default: {RECURSIVE})")
    parser.add_argument('--no-recursive', dest='recursive', action='store_false',
                        help="Search input folder non-recursively")
    parser.add_argument('--workers',      type=int, default=None,
                        help=f"Parallel Blender processes (default: {WORKERS})")
    parser.add_argument('--timeout',      type=int, default=None,
                        help=f"Ceiling for split files only; cap is "
                             f"min(TIMEOUT, TIMEOUT_PART * n_parts) "
                             f"(default: {TIMEOUT})")
    parser.add_argument('--timeout-part', type=int, default=None,
                        help=f"Seconds allowed for any single shell part "
                             f"(default: {TIMEOUT_PART}, 0=one whole-file budget)")
    parser.add_argument('--max-faces',    type=int, default=None,
                        help=f"Decimate mesh if face count exceeds this (default: {MAX_FACES}, 0=disabled)")
    parser.add_argument('--one-file',     default=None,
                        help="Repair exactly this one file and exit. Used by the "
                             "worker to run each file in its own process, and "
                             "usable by hand to debug a single mesh.")
    parser.add_argument('--is-part',      action='store_true',
                        help="With --one-file: treat the path as a split shell part.")
    parser.add_argument('--result-fd',    type=int, default=None,
                        help="With --one-file: write the result dict as JSON to "
                             "this file descriptor.")
    args = parser.parse_args()
    if args.input      is not None: INPUT_FOLDER  = args.input
    if args.suffix     is not None: OUTPUT_SUFFIX = args.suffix
    if args.merge_dist is not None: MERGE_DIST    = args.merge_dist
    if args.min_layer  is not None: MIN_LAYER     = args.min_layer
    if args.blender_reserve_pct is not None:
        BLENDER_RESERVE_PCT = args.blender_reserve_pct
    if args.recursive  is not None: RECURSIVE     = args.recursive
    if args.workers    is not None: WORKERS       = args.workers
    if args.timeout    is not None: TIMEOUT       = args.timeout
    if args.timeout_part is not None: TIMEOUT_PART = args.timeout_part
    if args.max_faces  is not None: MAX_FACES     = args.max_faces
    _ONE_FILE   = args.one_file
    _ONE_IS_PART = args.is_part
    _RESULT_FD  = args.result_fd
else:
    _ONE_FILE = _RESULT_FD = None
    _ONE_IS_PART = False

# ---------------------------------------------------------------------------
# Blender script template — loaded from the companion file at startup.
# ---------------------------------------------------------------------------

_BLENDER_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'stl_batch_fix.blender')
with open(_BLENDER_SCRIPT_PATH, 'r') as _f:
    BLENDER_SCRIPT = _f.read()

_BLENDER_DECIMATE_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              'stl_batch_fix.decimate.blender')
with open(_BLENDER_DECIMATE_SCRIPT_PATH, 'r') as _f:
    BLENDER_DECIMATE_SCRIPT = _f.read()

if False:  # never executed — sentinel so editors know what variables get injected
    src        = ""
    dst        = ""
    merge_dist = 0.0
    is_ascii   = False
    is_obj     = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COMPANION_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif', '.svg',
    '.pdf', '.txt', '.md', '.readme',
    '.zip', '.7z', '.rar',
}

# Scratch subfolder under the output tree holding per-shell split parts.
# Removed after a successful merge; never walked when collecting inputs.
PARTS_DIRNAME = '~parts'

# Prefix marking a part that has NOT been repaired yet.  A part is written as
# "~<name>" and renamed to "<name>" only once its repair succeeds, so the
# filename itself is the state: a bare name means finished, a ~ name means
# in progress or abandoned.  Previously the state lived in sibling signal files
# (<part>.failed.stl), which survived across runs and made a part report 'skip'
# -- counted as success by the merge -- while its geometry was never repaired.
_PART_PENDING = '~'


def _parts_dir_for(dst, max_faces=None):
    """Scratch dir for one mesh's split parts: ~parts/<mesh>.<max_faces>/.

    Keyed by MAX_FACES because part indices are assigned by face-count rank
    AFTER decimation (see split_shells), so the same index means a different
    shell at a different decimation target.  Keeping each setting's parts in
    its own folder makes a stale part structurally unreachable rather than
    something that has to be detected.

    MAX_FACES is not the only input to that ranking -- _MIN_SHELL_FACES and
    which decimator ran (fast_simplification / pymeshlab / blender) also shift
    face counts.  It is the one that changes in practice; the ~ rename protocol
    is what actually guarantees correctness, this just avoids needless work."""
    if max_faces is None:
        max_faces = MAX_FACES
    base = os.path.splitext(os.path.basename(dst))[0]
    return os.path.join(os.path.dirname(os.path.abspath(dst)),
                        PARTS_DIRNAME, f"{base}.{int(max_faces)}")


def _clear_stale_parts_dirs(dst, keep_dir):
    """Delete this mesh's parts dirs from other MAX_FACES settings.

    Safe to rmtree: the folder holds one mesh's parts and nothing else, which
    is why the parts root is per-mesh rather than shared.  Returns how many
    directories were removed."""
    base = os.path.splitext(os.path.basename(dst))[0]
    root = os.path.join(os.path.dirname(os.path.abspath(dst)), PARTS_DIRNAME)
    keep = os.path.abspath(keep_dir)
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        path = os.path.join(root, name)
        if not os.path.isdir(path) or os.path.abspath(path) == keep:
            continue
        # "<mesh>.<digits>" -- this mesh, a different setting.
        stem, _, tail = name.rpartition('.')
        if stem == base and tail.isdigit():
            try:
                shutil.rmtree(path)
                removed += 1
            except OSError:
                pass
    return removed


def _pending_part_path(part_path):
    """The "~<name>" form of a committed part path."""
    d, n = os.path.split(part_path)
    return os.path.join(d, _PART_PENDING + n) if not n.startswith(_PART_PENDING) \
        else part_path


def _repair_part(part_path, L=None, label='part'):
    """Repair one split part under the pending/commit protocol.

    split_shells writes each part as "~<name>"; a bare "<name>" means an
    earlier run already repaired it.  So: reuse the committed file if it is
    there, otherwise repair the pending one and rename it on success.  The
    rename is what makes 'finished' durable -- without it the only record was
    a sibling signal file, which outlived the part it described."""
    if os.path.exists(part_path):
        return {'rel': os.path.basename(part_path), 'status': 'skip',
                'reason': 'already repaired in an earlier run'}
    pending = _pending_part_path(part_path)
    if not os.path.exists(pending):
        return {'rel': os.path.basename(part_path), 'status': 'failed',
                'is_mesh_bad': False,
                'stdout': f'{label} file missing: {os.path.basename(pending)}',
                'stderr': ''}
    r = process_file(pending, is_part=True)
    if r.get('status') in ('ok', 'skip'):
        if _commit_part(part_path) and L is not None:
            L(f"  {label} {os.path.basename(part_path)}: committed")
    return r


def _commit_part(part_path):
    """Rename ~<name> -> <name>, marking this part repaired.  Idempotent."""
    pending = _pending_part_path(part_path)
    if pending == part_path or not os.path.exists(pending):
        return False
    try:
        os.replace(pending, part_path)
        return True
    except OSError:
        return False

# Shells smaller than this (in faces) are treated as debris by split_shells()
# and dropped rather than repaired as parts.
#
# Flat, not a fraction of the largest shell.  The old rule was
# max(100, largest // 1000), and the ratio is what went wrong: it discards more
# the bigger the model gets.  On a 2M-face figure it set the floor at 1,315
# faces, and 562 after decimation — a magnet peg or locating pin is smaller
# than that and is a part, not debris.
#
# Measured on the collection, this is a wide gap rather than a fine judgement:
#   Mandy_Body_Dinamuuu3D  39 real shells, smallest 750 faces after decimation
#   whole-costume01        444 shells, of which 443 are under 100 faces
# So 100 keeps every real part with 7.5x margin and still rejects the specks.
# Lower is not free: at a floor of 10, whole-costume01 splits into 381 parts,
# each one a separate repair and merge.
_MIN_SHELL_FACES = 100

# Above this limit all in-process Python work (edge scan, PyMeshLab split/decimate/NM repair,
# PyMeshFix pre-scan) is skipped — the file goes straight to Blender, which streams from disk.
# Keeps per-worker RAM within bounds when running multiple workers in parallel.
_LARGE_MESH_TRI_LIMIT = 2_000_000

# Peak resident memory the full pipeline needs, per triangle of *input*.
# Measured end to end on a 7,000,034-triangle mesh that peaked at 5.8 GB
# (decimation 2.5 GB, then NM repair on the decimated result on top of it).
# Scaling is close to linear across the collection, so this predicts cost well
# enough to decide whether a file can be attempted at all.
_BYTES_PER_TRIANGLE = 5.8 * 1024**3 / 7_000_034      # ~890 bytes/triangle

# Total memory the whole run may use, in bytes.  run.sh derives this from
# installed RAM and exports it; 0 means unknown, in which case no admission
# control is applied and behaviour matches the old unconditional attempt.
def _run_memory_budget():
    raw = os.environ.get('WORKER_MEM_MAX', '').strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def estimate_peak_bytes(n_tris):
    """Predicted peak RSS for putting a mesh of n_tris through the pipeline."""
    return int(n_tris * _BYTES_PER_TRIANGLE)


def mesh_is_too_large(n_tris, budget=None, share=0.8):
    """True if this mesh cannot be processed within the run's memory budget.

    A file whose own projected peak exceeds `share` of the entire budget cannot
    be made to fit by reducing worker count — it would OOM even running alone.
    Around 30M triangles that is true of a 23 GB budget, and AI-generated meshes
    reach that routinely, so the size has to be checked before the attempt
    rather than discovered by having a worker killed.
    Returns False when the budget is unknown, preserving the old behaviour."""
    if budget is None:
        budget = _run_memory_budget()
    if budget <= 0:
        return False
    return estimate_peak_bytes(n_tris) > budget * share


def _read_stl_header(path):
    """Return (n_tris, error_str) from a binary STL header, cross-checked against
    the actual file size.  n_tris is authoritative — never trust a caller-supplied
    count, which may come from a different tool's face counter (see todo M5).
    Returns (-1, message) if the file cannot be read or the count is inconsistent."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(84)
        if len(head) < 84:
            return -1, "file shorter than STL header"
        n_tris = struct.unpack_from('<I', head, 80)[0]
        if size < 84 + n_tris * 50:
            return -1, (f"header claims {n_tris:,} tris "
                        f"({84 + n_tris * 50:,} bytes) but file is {size:,} bytes")
        return n_tris, None
    except OSError as _e:
        return -1, f"{type(_e).__name__}: {_e}"


# Bounding-box changes below this (mm) are not worth reporting.  Calibrated
# against inspected results from a 141-file run:
#
#   0.0005 mm  decimation drift          invisible
#   0.033  mm  Goblin/Poni1              inspected: no visible difference
#   1.077  mm  Zelda NSFW/Chair_foot1    inspected: end caps destroyed
#  89.190  mm  Transhuman_Girl/Leg1      inspected: stray artifact removed, fine
#
# 0.1 mm sits in the empty gap between the noise and the real changes, and is
# below one layer height, so nothing printable hides under it.  Note the top of
# that table: magnitude alone does not say whether a change is damage or
# cleanup — only that it is worth a look.
_BBOX_TOLERANCE = 0.1


def stl_bounds(path):
    """Return ((minx,miny,minz), (maxx,maxy,maxz)) for a binary STL, or None.

    Streams the vertex block in chunks rather than welding the mesh: the extents
    need no connectivity, and welding a 900k-face mesh to answer this would cost
    hundreds of MB inside a worker that is already near its budget."""
    n_tris, err = _read_stl_header(path)
    if err or n_tris <= 0:
        return None
    try:
        lo = _np.full(3, _np.inf, dtype=_np.float64)
        hi = _np.full(3, -_np.inf, dtype=_np.float64)
        CHUNK = 200_000                      # triangles per pass (~10 MB)
        with open(path, 'rb') as f:
            f.seek(84)
            remaining = n_tris
            while remaining > 0:
                take = min(CHUNK, remaining)
                buf = f.read(take * 50)
                if len(buf) < take * 50:
                    return None
                block = _np.frombuffer(buf, dtype=_np.uint8).reshape(take, 50)
                # Bytes 12:48 are the three vertices; 0:12 is the normal, which
                # must not be included in the extents.
                verts = block[:, 12:48].copy().view(_np.float32).reshape(-1, 3)
                _np.minimum(lo, verts.min(axis=0), out=lo)
                _np.maximum(hi, verts.max(axis=0), out=hi)
                remaining -= take
        if not _np.all(_np.isfinite(lo)) or not _np.all(_np.isfinite(hi)):
            return None
        return tuple(lo), tuple(hi)
    except (OSError, ValueError):
        return None


def compare_bounds(before, after, tol=_BBOX_TOLERANCE):
    """Describe how a repair changed a mesh's extents, or None if unchanged.

    A repair — filling holes, resolving non-manifold edges — adds or adjusts
    triangles inside an existing boundary, so it should never move the model's
    extents.  A shrink means geometry was deleted; growth means geometry was
    invented.  Both are worth knowing about: pymeshlab's NM repair once deleted
    17% of a mesh and 4.9 mm off its base while reporting a successful repair.

    Returns a short human-readable string naming the axes that moved."""
    if not before or not after:
        return None
    (lo0, hi0), (lo1, hi1) = before, after
    parts = []
    for i, axis in enumerate('xyz'):
        d_lo = lo1[i] - lo0[i]      # positive = min rose = geometry lost
        d_hi = hi1[i] - hi0[i]      # negative = max fell = geometry lost
        if abs(d_lo) > tol:
            parts.append(f"{axis} min {lo0[i]:.3f}->{lo1[i]:.3f} ({d_lo:+.3f})")
        if abs(d_hi) > tol:
            parts.append(f"{axis} max {hi0[i]:.3f}->{hi1[i]:.3f} ({d_hi:+.3f})")
    return "  ".join(parts) if parts else None


def _build_edge_counts(raw, n_tris):
    """Count how many faces use each undirected edge of a binary STL triangle block.

    Edge keys are packed integers rather than tuples of float tuples: each vertex
    is its 12 raw bytes read as one int, and an edge is the two vertex ints
    combined into a single 192-bit int (smaller one first, so direction doesn't
    matter).  One int object per edge instead of seven tuple/float objects cuts
    peak RSS roughly five-fold on large meshes (see todo M6) — the difference
    between fitting in the cgroup memory budget and OOM-killing the run.

    Packing is byte-level, but the bytes are not taken raw: negative zero must
    first be folded to positive zero.  -0.0 and 0.0 have different bit patterns
    yet compare equal as floats, and exporters emit -0.0 freely on mirrored or
    negated geometry.  Comparing raw bytes would split such a vertex in two and
    report phantom open edges, so each coordinate is unpacked, normalised, and
    repacked.  With that done the comparison is exact and matches float equality:
    STL stores float32, so two vertices are shared iff their normalised bytes match.
    Returns a dict of packed_edge_key -> use count."""
    ec = {}
    ec_get   = ec.get
    unpack   = struct.Struct('<3f').unpack_from
    pack     = struct.Struct('<3f').pack
    frombytes = int.from_bytes

    def _vkey(off):
        x, y, z = unpack(raw, off)
        # `+ 0.0` maps -0.0 to 0.0 and leaves every other value untouched.
        return frombytes(pack(x + 0.0, y + 0.0, z + 0.0), 'little')

    for i in range(n_tris):
        base = i * 50 + 12
        a = _vkey(base)
        b = _vkey(base + 12)
        c = _vkey(base + 24)
        for v0, v1 in ((a, b), (b, c), (c, a)):
            key = (v0 << 96) | v1 if v0 < v1 else (v1 << 96) | v0
            ec[key] = ec_get(key, 0) + 1
    return ec


def scan_mesh_errors(path, n_tris=None, return_edges=False):
    """Return (nm_edges, open_edges, error_str) from a binary STL edge scan.

    The triangle count is always read from the file header, not from the caller —
    the n_tris argument is accepted only so existing call sites keep working and
    is used solely for the oversize pre-check.

    Returns (-1, -1, None) for oversized meshes — callers treat that as 'needs processing'.
    Returns (-1, -1, message) on read/parse error.
    With return_edges=True returns (nm, open_e, error_str, edge_counts) so callers
    that need the edge map (e.g. _find_paired_open_vertices) can reuse it instead
    of rescanning the file."""
    def _out(nm, open_e, err, ec=None):
        return (nm, open_e, err, ec) if return_edges else (nm, open_e, err)

    # Cheap pre-check using the caller's estimate, so an obviously huge mesh
    # doesn't even get a stat/open.
    if n_tris is not None and n_tris > _LARGE_MESH_TRI_LIMIT:
        return _out(-1, -1, None)
    real_tris, err = _read_stl_header(path)
    if err:
        return _out(-1, -1, err)
    if real_tris > _LARGE_MESH_TRI_LIMIT:
        return _out(-1, -1, None)
    try:
        with open(path, 'rb') as f:
            f.seek(84)
            raw = f.read(real_tris * 50)
        if len(raw) < real_tris * 50:
            return _out(-1, -1, f"short read: expected {real_tris * 50:,} bytes, got {len(raw):,}")
        ec = _build_edge_counts(raw, real_tris)
        del raw
        nm     = 0
        open_e = 0
        for c in ec.values():
            if c == 1:
                open_e += 1
            elif c > 2:
                nm += 1
        return _out(nm, open_e, None, ec)
    except Exception as _e:
        return _out(-1, -1, f"{type(_e).__name__}: {_e}")


def _blender_budget(elapsed=0.0):
    """Seconds this Blender invocation may take, given time already spent.

    Blender must finish before the mesh's own budget runs out, not merely be
    given the whole of it: a mesh that has already spent 450s of a 600s budget
    handing Blender a fresh 600s reaches 1050s, and the cap does not cap.  Worse,
    an over-running Blender is killed by the worker's SIGKILL of the whole
    process tree rather than timing out on its own, so the pipeline never gets
    the clean 'TIMEOUT after Ns' it knows how to handle.

    So Blender gets   budget - elapsed - reserve,   where the reserve is what
    the steps AFTER Blender need.  Measured across two runs (n=17 blender
    invocations): 10 needed no post-Blender work at all, 2 took ~9s, and 5 took
    100.7-186.4s — every one of those a post-blender PyMeshFix pass on a ~900k
    face mesh.  p95 was 152.6s.  BLENDER_RESERVE_PCT of 30% covers that with
    headroom at the default 600s budget.

    Returns 0 when there is not enough time left to be worth starting; callers
    must treat that as 'skip Blender and fail the mesh on time'."""
    budget = TIMEOUT_PART or TIMEOUT
    reserve = budget * (BLENDER_RESERVE_PCT / 100.0)
    left = budget - elapsed - reserve
    return left if left >= _BLENDER_MIN_RUN else 0.0


def _arm_mesh_alarm(seconds, why=''):
    """Cap this process's own wall time with SIGALRM.  No-op outside the child.

    The parent spawns every whole file with TIMEOUT (the 3600s ceiling) because
    at spawn time nobody knows whether the mesh will split, or into how many
    parts.  Nothing narrowed that back down afterwards, so a mesh that stayed
    whole kept the ceiling instead of TIMEOUT_PART: a 1.3M-tri single shell sat
    in PyMeshFix for the full hour before the parent's communicate() killed it,
    when its real budget was 600s.

    A deadline check cannot fix that.  The pipeline spends its time inside
    library calls that do not return to Python until they are done, so by the
    time any `if monotonic() >= deadline` is reached the overrun has already
    happened.  SIGALRM interrupts the blocking call itself, which is the whole
    reason for using it here.

    Raising TimeoutExpired (not exiting) lets the existing handler write the
    .timeout.stl marker and report a normal timeout, so the outcome is the same
    shape the pipeline already knows how to report."""
    if _ONE_FILE is None or not hasattr(signal, 'SIGALRM'):
        return
    seconds = int(max(1, seconds))

    def _fire(_sig, _frame):
        global _blender_proc
        if _blender_proc is not None:
            try:
                _blender_proc.kill()
            except Exception:
                pass
        raise subprocess.TimeoutExpired(cmd='mesh', timeout=seconds)

    try:
        signal.signal(signal.SIGALRM, _fire)
        signal.alarm(seconds)
    except (ValueError, OSError):
        # Not the main thread, or no SIGALRM: the parent watchdog still applies.
        pass


def _part_cap(n_parts):
    """Seconds allowed for a file that split into `n_parts` shells.

    min(TIMEOUT, TIMEOUT_PART * n_parts): the per-part budget times the number
    of parts, but never more than the ceiling.  A 2-part file gets 1200s, a
    40-part file gets TIMEOUT rather than 24000s.  With TIMEOUT_PART disabled
    (0) there is no per-part budget to multiply, so the ceiling is the cap."""
    if not TIMEOUT_PART:
        return TIMEOUT
    return min(TIMEOUT, TIMEOUT_PART * max(1, int(n_parts)))


def _open_loops_are_printable(edge_counts, limit=None):
    """True when every open boundary in the mesh is smaller than one layer.

    Returns (printable, n_loops, largest_mm).  printable is False if there are
    no open edges to judge (callers already handle open==0), if the limit is
    disabled, or if any loop is at least `limit` across.

    Why this exists.  The pipeline's success condition was `nm == 0 and
    open == 0` — a mathematical standard, not a manufacturing one.  On
    1st-body.stl that cost the model its head and torso:

        after decimate      900,000 faces   volume 100.00%
        after pymeshfix     879,332 faces   volume  99.99%   open=4
          the 4 open edges spanned 0.01mm, all at one point
        -> blender called to clear them (180s)
        after blender       874,236 faces   volume 100.04%   open=142
          blender closed the pinhole and punched 28 new holes,
          every one between 0.023mm and 0.162mm across
        -> pymeshfix called again to clear those
        final               356,392 faces   volume  44.94%   open=0
          head and torso gone, cut ragged at the waist

    Every hole in that cascade was smaller than a third of the finest layer
    this collection prints at, so none of them could reach the plate.  The
    repair chasing them destroyed 55% of the model to fix nothing.

    Size is measured as the diameter of each boundary loop — the span of its
    vertices — not edge length or edge count.  A loop of many short edges can
    still be a large hole, and a single long edge is not a hole at all."""
    if limit is None:
        limit = MIN_LAYER
    if not limit or limit <= 0:
        return False, 0, 0.0
    open_keys = [k for k, c in edge_counts.items() if c == 1]
    if not open_keys:
        return False, 0, 0.0

    # Group the open edges into connected chains, keyed by vertex position.
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ends = []
    for k in open_keys:
        a, b = _unpack_edge_key(k)
        ends.append((a, b))
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups = {}
    for a, b in ends:
        groups.setdefault(find(a), []).extend((a, b))

    largest = 0.0
    for pts in groups.values():
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        zs = [p[2] for p in pts]
        span = (((max(xs) - min(xs)) ** 2
                 + (max(ys) - min(ys)) ** 2
                 + (max(zs) - min(zs)) ** 2) ** 0.5)
        if span > largest:
            largest = span
    return largest < limit, len(groups), largest


def _merge_parts(parts, dst, dst_dir, L, stats, failed_copy, unrepaired_copy,
                 open_copy, rel):
    """Merge repaired shell parts back into one output file.

    Shared by step B (split before decimation) and step B2 (split deferred until
    after it).  Returns a result dict on success, or None to let the caller fall
    through to whole-mesh repair — the parts are left in place for that."""
    try:
        ms_merge = _pymeshlab.MeshSet()
        for part_path in parts:
            if os.path.exists(part_path):
                ms_merge.load_new_mesh(part_path)
        # generate_by_merging_visible_meshes was added in PyMeshLab 2022.2;
        # fall back to flatten_visible_layers on older builds.
        if hasattr(ms_merge, 'generate_by_merging_visible_meshes'):
            ms_merge.generate_by_merging_visible_meshes()
        else:
            ms_merge.flatten_visible_layers(mergevisible=True)
        _ensure_parent(dst)
        ms_merge.save_current_mesh(dst, binary=True)
        size = os.path.getsize(dst)
        _clear_stale(failed_copy, unrepaired_copy, open_copy)
        _n_removed = _cleanup_parts(dst_dir, parts)
        L(f"merged {len(parts)} parts → {os.path.basename(dst)}  ({size:,} bytes)"
          f"; removed {_n_removed} part file(s)")
        stats['path'].append(f'split{len(parts)}+merge')
        return {'rel': rel, 'status': 'ok', 'dst': os.path.basename(dst),
                'size': size, 'is_ascii': False, 'split': len(parts)}
    except Exception as _merge_err:
        L(f"merge failed: {_merge_err}")
        return None


def split_shells(src, dst_dir, L=None):
    """Split a mesh file into per-shell part files using PyMeshLab's connected
    component analysis. Parts are written into dst_dir (caller supplies the ~parts subfolder).
    Source folder is never modified.
    Returns list of part paths in dst_dir (largest shell first), or [] if
    single shell or error."""
    if not _PYMESHLAB_AVAILABLE:
        return []
    try:
        ms = _pymeshlab.MeshSet()
        ms.load_new_mesh(src)
        count_before = len(ms)  # typically 1; components are added after this index
        ms.generate_splitting_by_connected_components()
        count_after = len(ms)
        n_components = count_after - count_before
        if n_components <= 1:
            return []
        # Only collect the newly added component meshes (indices count_before..count_after-1).
        # The original combined mesh (index 0..count_before-1) is left in place by PyMeshLab
        # and must be excluded — it is a duplicate of the full mesh, not a shell.
        meshes = []
        for i in range(count_before, count_after):
            ms.set_current_mesh(i)
            n = ms.current_mesh().face_number()
            meshes.append((n, i))
        meshes.sort(reverse=True)
        # Minimum shell size, in faces.  A flat floor, deliberately: the old
        # rule was max(100, largest // 1000), which scales with the biggest
        # shell and so discards more as the model grows.  On a 2M-face figure
        # that floor was 1,315 faces at full size and 562 after decimation —
        # large enough to silently drop a magnet peg, a locating pin or a small
        # accessory, which are parts, not debris.
        #
        # 10 is below anything printable (a cube is 12 triangles) and still
        # excludes the stray specks that make split_shells decline: 443 of
        # whole-costume01's 444 shells are 3-to-100 vertex fragments.
        min_faces = _MIN_SHELL_FACES
        meshes = [(n, i) for n, i in meshes if n >= min_faces]
        if len(meshes) <= 1:
            return []
        os.makedirs(dst_dir, exist_ok=True)
        base_no_ext = os.path.splitext(os.path.basename(src))[0]
        results = []
        for idx, (_, mesh_idx) in enumerate(meshes):
            out_path = os.path.join(dst_dir, f"{base_no_ext}.part.{idx}.stl")
            # A bare name from an earlier run means that part was repaired and
            # committed.  The dir is keyed by MAX_FACES, so it belongs to this
            # same split -- reuse it instead of redoing the work.
            if os.path.exists(out_path):
                if L is not None:
                    L(f"part {idx}: already repaired — reusing")
                results.append(out_path)
                continue
            ms.set_current_mesh(mesh_idx)
            # Written pending; the repair renames it on success.
            ms.save_current_mesh(_pending_part_path(out_path), binary=True)
            results.append(out_path)
        return results
    except Exception as _e:
        # Never print to stdout — under the TUI that is the alternate screen
        # buffer, where the output is swallowed or corrupts the layout (todo L6).
        import traceback
        if L is not None:
            L(f"split failed: {type(_e).__name__}: {_e}")
            for _tl in traceback.format_exc().splitlines():
                L(f"  {_tl}")
        else:
            log_step(os.path.basename(src), f"split failed: {type(_e).__name__}: {_e}")
        return []


def retarget_logs(input_folder=None):
    """Point the log files at the output tree for `input_folder`.

    The three log paths default to this machine's own folders, so a run given a
    different --input wrote its meshes to that folder's Fixed/ while its logs
    went on landing in /mnt/sda2/STL/Fixed — the diagnostics for a run ended up
    somewhere unrelated to its results.  Called once at run start, after the
    config is settled and before anything is written."""
    global LOG_FILE, REVIEW_FILE, SUMMARY_FILE
    if input_folder is None:
        input_folder = INPUT_FOLDER
    try:
        root = os.path.join(
            os.path.dirname(os.path.abspath(input_folder)), 'Fixed')
    except (OSError, TypeError):
        return
    LOG_FILE     = os.path.join(root, os.path.basename(LOG_FILE))
    REVIEW_FILE  = os.path.join(root, os.path.basename(REVIEW_FILE))
    SUMMARY_FILE = os.path.join(root, os.path.basename(SUMMARY_FILE))


def _ensure_parent(path):
    """Create the directory holding `path`.  Absolute-ises first so a bare
    filename yields '.' rather than an empty dirname."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def _append_locked(path, line, attempts=1, label=None):
    """Append one line to `path` under an exclusive lock.

    Every run log is written concurrently by all workers, so each append takes
    flock(LOCK_EX) for the duration of the write.  Failure policy is the
    caller's: `attempts` > 1 retries with a short sleep and, once exhausted,
    reports to stderr under `label` — that is for the step log, where a missing
    line is what makes a crash impossible to attribute.  The default single
    attempt fails silently, which is right for the summary and review lists: an
    aid, never a reason to fail a repair that otherwise worked.

    Returns True if the line was written.
    """
    import fcntl, time
    last_err = None
    for _ in range(max(1, attempts)):
        try:
            _ensure_parent(path)
            with open(path, 'a') as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(line)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return True
        except OSError as _e:
            last_err = _e
            if attempts > 1:
                time.sleep(0.05)
    if label:
        # A log line vanishing without a trace is exactly what makes a crash
        # impossible to attribute — surface it on stderr instead (todo L7).
        try:
            sys.stderr.write(
                f"{label}: giving up after {attempts} attempts ({last_err}): {line}")
            sys.stderr.flush()
        except Exception:
            pass
    return False


def log_step(rel, msg):
    """Write one log line immediately. Safe for concurrent workers via file locking."""
    _append_locked(LOG_FILE, f"{rel}\t{msg}\n", attempts=10, label='log_step')


def _weld_binary_stl(path):
    """Read a binary STL and return (verts, faces) as numpy arrays.

    A binary STL stores no vertex sharing — each triangle carries its own three
    coordinate triples, so a vertex touched by six faces appears six times
    (measured: exactly 6.0x on real models).  Quadric edge collapse works on
    edges, so it needs to know which faces meet at each vertex; recovering that
    sharing is a precondition of the algorithm, not an implementation detail.

    The sort is done on the raw coordinate *bits* viewed as three uint32
    columns rather than on float rows.  Identical float32 values have identical
    bit patterns, so np.lexsort over the integer columns is exact and about 4x
    faster than np.unique(axis=0) while allocating less (measured 6.9s/461 MB
    -> 1.75s/383 MB on a 2.55M-triangle mesh).

    Negative zero is normalised first: -0.0 and 0.0 compare equal as floats but
    have different bits, so without this a shared vertex would split in two and
    leave a crack that QEC cannot collapse.  Same hazard as in _build_edge_counts."""
    # Each intermediate is released the moment it is no longer needed.  On a
    # 7M-triangle mesh holding them all to the end peaks at 1000 MB, versus
    # 667 MB when freed eagerly — and that mesh was OOM-killed at 2.5 GB once
    # fast_simplification's own structures were added on top.
    with open(path, 'rb') as f:
        f.read(80)
        n_tris = struct.unpack('<I', f.read(4))[0]
        raw = _np.frombuffer(f.read(n_tris * 50), dtype=_np.uint8)
    if len(raw) < n_tris * 50:
        raise RuntimeError(f"short read: expected {n_tris * 50:,} bytes, got {len(raw):,}")
    raw = raw.reshape(n_tris, 50)

    # Bytes 12..48 of each 50-byte record are the three vertices (9 float32).
    coords = _np.ascontiguousarray(raw[:, 12:48]).reshape(-1, 12)
    del raw                      # the 50-byte records are no longer needed
    fview  = coords.view(_np.float32).reshape(-1, 3)
    # Fold -0.0 to 0.0 in place (adding 0.0 leaves every other value untouched).
    _np.add(fview, _np.float32(0.0), out=fview)
    del fview

    bits  = coords.view(_np.uint32).reshape(-1, 3)
    order = _np.lexsort((bits[:, 2], bits[:, 1], bits[:, 0]))
    srt   = bits[order]
    new   = _np.empty(len(srt), dtype=bool)
    new[0] = True
    _np.any(srt[1:] != srt[:-1], axis=1, out=new[1:])
    ids   = _np.cumsum(new) - 1
    inv   = _np.empty(len(srt), dtype=_np.int64)
    inv[order] = ids
    del order, ids
    verts = srt[new].view(_np.float32).reshape(-1, 3).copy()
    del srt, new, bits, coords
    faces = inv.reshape(n_tris, 3)
    return verts, faces


def find_winding_seams(verts, faces):
    """Locate edges where two faces disagree about which way the surface faces.

    On a consistently wound surface, the two faces sharing an edge traverse it
    in OPPOSITE directions.  Traversing it the same way means the surface
    reverses there — and when those edges form CLOSED LOOPS, they are the
    boundary between two regions whose winding cannot be reconciled: hair over
    a scalp, cloth over a body, a separately-sculpted part fused to its host.

    Returns (seam_edges, n_closed_loops).  The loop count is what matters, not
    the edge count.  Measured on one model:

        head deleted by PyMeshFix   40 seam edges, 7 closed loops, 0 loose ends
        renders and prints fine      5 seam edges, 0 closed loops, 4 loose ends

    A few edges with dangling ends are local noise that stops on its own.  A
    closed loop encircles something."""
    from collections import defaultdict
    edge_dir = defaultdict(list)
    for a, b, c in faces:
        for u, w in ((a, b), (b, c), (c, a)):
            edge_dir[(min(u, w), max(u, w))].append(u < w)
    seam = [k for k, dirs in edge_dir.items()
            if len(dirs) == 2 and dirs[0] == dirs[1]]
    if not seam:
        return [], 0

    # A closed loop is a connected run of seam edges where every vertex has
    # exactly two of them — no ends, no branches.
    adj = defaultdict(list)
    for a, b in seam:
        adj[a].append(b)
        adj[b].append(a)
    seen = set()
    loops = 0
    for start in list(adj):
        if start in seen:
            continue
        stack, group = [start], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            group.append(x)
            stack.extend(adj[x])
        if all(len(adj[x]) == 2 for x in group):
            loops += 1
    return seam, loops


# A repair that leaves less than this fraction of the enclosed volume has
# deleted geometry rather than fixed it.  Repairs legitimately change volume a
# little — capping a hole adds some, removing a stray artifact takes some — but
# the known destructive cases lost 15% and more, while good repairs on the same
# models stayed within a percent.
_VOLUME_LOSS_LIMIT = 0.95

# Below this enclosed volume (mm^3) the ratio above is not trusted.  A thin
# shell or a flat scrap encloses almost nothing, so the "loss" is a ratio
# between two rounding errors: a full-collection run fired the split on twelve
# parts reading "volume 0 -> 0 (3%)" and similar, none of which had any
# geometry to lose.  Only two of the fourteen were real, and both were well
# over this.
# Below this volume (mm^3) the before/after ratio is noise, not damage.  At 1.0
# the check fired on fragments of 9 mm^3 losing 1 mm^3 ("volume 9 -> 8 (86%)"),
# which is rounding between two welds rather than deleted geometry.  50 silences
# those and still catches every real casualty seen: Stool_Base (16,970),
# Class.stl (183), imp_stand (6,778 and 1,464).
_VOLUME_MIN_MEANINGFUL = 50.0


def _mesh_volume(path):
    """Signed volume enclosed by a mesh, or None if it cannot be read.

    The measure that catches a repair deleting geometry INSIDE the model, where
    the bounding box cannot: the model that lost its head kept its exact bbox
    and lost 15% of its volume."""
    try:
        verts, faces = _weld_binary_stl(path)
    except Exception:
        return None
    tri = verts[faces]
    vol = _np.einsum('ij,ij->i', tri[:, 0],
                     _np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0
    del verts, faces, tri
    return abs(float(vol))


def _repair_by_seam_split(src_mesh, dst, dst_base, temps, stats, L, rel,
                          failed_copy, unrepaired_copy, open_copy, elapsed=0.0):
    """Split at the winding seams, repair each region, merge back.

    The recovery path for a mesh PyMeshFix answered by deleting part of it.
    Each region is internally consistent once separated, so PyMeshFix preserves
    it: measured 100.0% and 100.2% of volume on the two regions of a mesh that
    lost 15% when they were joined.

    Returns a result dict on success, or None to leave the caller's own result
    in place."""
    try:
        verts, faces = _weld_binary_stl(src_mesh)
    except Exception as exc:
        L(f"seam split: cannot read the mesh — {exc}")
        return None
    seam, loops = find_winding_seams(verts, faces)
    L(f"seam split: {len(seam)} seam edges in {loops} closed loop(s)")
    pieces = split_at_seams(verts, faces, seam) if seam else []
    del verts, faces

    if len(pieces) < 2:
        # No boundary to cut on — but Blender's repair creates one.  Measured
        # on the mesh this exists for: straight from decimation it has 5 seam
        # edges in 0 loops and cannot be separated, and after Blender it has 40
        # in 7 loops and splits cleanly into the body and the head.  Blender
        # rebuilds the surface where the two regions meet, which turns an
        # ambiguous join into an explicit boundary.
        #
        # Blender is worth running here on its own merits too: it keeps 99.9%
        # of the geometry where PyMeshFix deleted 15% of it.  What it does not
        # do is resolve the seam, which is why the split follows it.
        L("seam split: nothing to separate — running Blender first, "
          "its repair makes the boundary explicit")
        bl_tmp = dst_base + '.seamblender.stl'
        temps.append(bl_tmp)
        _ensure_parent(bl_tmp)
        ok, _open_only, _unrep, _out, _err = fix_stl(src_mesh, bl_tmp, MERGE_DIST,
                                                     elapsed=elapsed,
                                                     L=L, route='seam-split')
        if not ok or not os.path.exists(bl_tmp):
            L("seam split: Blender did not produce a mesh")
            return None
        try:
            verts, faces = _weld_binary_stl(bl_tmp)
        except Exception as exc:
            L(f"seam split: cannot read Blender's output — {exc}")
            return None
        seam, loops = find_winding_seams(verts, faces)
        L(f"seam split: after Blender, {len(seam)} seam edges in "
          f"{loops} closed loop(s)")
        pieces = split_at_seams(verts, faces, seam) if seam else []
        del verts, faces
        if len(pieces) < 2:
            L("seam split: still nothing to separate")
            return None
        src_mesh = bl_tmp

    seam_dir = _parts_dir_for(dst)
    _ensure_parent(os.path.join(seam_dir, 'x'))
    base = os.path.splitext(os.path.basename(dst))[0]
    parts = []
    for i, (pv, pf) in enumerate(pieces):
        p = os.path.join(seam_dir, f"{base}{_SEAM_MARKER}{i}.stl")
        _write_binary_stl(_pending_part_path(p), pv, pf)
        temps.append(_pending_part_path(p))
        temps.append(p)
        parts.append(p)
    L("split: " + ", ".join(f"{len(pf):,} faces" for _, pf in pieces))

    n_ok = 0
    for p in parts:
        r = _repair_part(p, L=L, label='region')
        L(f"  region {os.path.basename(p)}: {r['status']}")
        if r['status'] in ('ok', 'skip'):
            n_ok += 1
    if n_ok != len(parts):
        L(f"seam split: {n_ok}/{len(parts)} regions repaired")
        return None

    merged = _merge_parts(parts, dst, seam_dir, L, stats, failed_copy,
                          unrepaired_copy, open_copy, rel)
    if merged is not None:
        stats['path'].append(f'seamsplit{len(parts)}')
    return merged


def split_at_seams(verts, faces, seam):
    """Separate a mesh into regions that do not cross the given seam edges.

    Each region comes back internally consistent, which is the whole point:
    PyMeshFix given the joined mesh keeps one region and deletes the rest —
    562,288 faces in, 394,432 out, the model's head gone.  Given the regions
    separately it preserved 100.0% and 100.2% of their volume.

    Returns a list of (verts, faces) with their own vertex numbering, largest
    first, dropping anything under _MIN_SHELL_FACES as debris."""
    from collections import defaultdict, deque
    blocked = set(seam)
    edge_faces = defaultdict(list)
    for i, (a, b, c) in enumerate(faces):
        for u, w in ((a, b), (b, c), (c, a)):
            edge_faces[(min(u, w), max(u, w))].append(i)

    region = _np.full(len(faces), -1, dtype=_np.int64)
    n_regions = 0
    for start in range(len(faces)):
        if region[start] >= 0:
            continue
        region[start] = n_regions
        queue = deque([start])
        while queue:
            fi = queue.popleft()
            a, b, c = faces[fi]
            for u, w in ((a, b), (b, c), (c, a)):
                key = (min(u, w), max(u, w))
                if key in blocked:
                    continue                # the seam is the cut line
                for fj in edge_faces[key]:
                    if region[fj] < 0:
                        region[fj] = n_regions
                        queue.append(fj)
        n_regions += 1

    out = []
    for r in range(n_regions):
        mask = region == r
        if mask.sum() < _MIN_SHELL_FACES:
            continue
        sub = faces[mask]
        used = _np.unique(sub)
        remap = _np.zeros(len(verts), dtype=_np.int64)
        remap[used] = _np.arange(len(used))
        out.append((verts[used], remap[sub]))
    out.sort(key=lambda vf: -len(vf[1]))
    return out


def _write_binary_stl(path, verts, faces):
    """Write an indexed mesh out as a binary STL, with real facet normals.

    The normals used to be left zeroed on the reasoning that slicers recompute
    them from the winding.  Slicers do, but viewers do not all agree: given a
    zero normal some fall back to the winding and some to a guess, so the same
    file could render inside-out in one program and correctly in another.  That
    made a genuine comparison between two outputs impossible — one file with
    normals and one without are not being drawn the same way.

    Computing them is a cross product over the face array; the cost is
    negligible beside the repair that produced the mesh."""
    n = len(faces)
    tv  = verts[faces].astype(_np.float32)          # (n, 3, 3)
    nrm = _np.cross(tv[:, 1] - tv[:, 0], tv[:, 2] - tv[:, 0])
    ln  = _np.linalg.norm(nrm, axis=1)
    # Degenerate faces have no normal to speak of; leave those zeroed rather
    # than dividing by zero and writing NaNs into the file.
    ok = ln > 1e-20
    nrm[ok] /= ln[ok][:, None]
    nrm[~ok] = 0.0
    buf = _np.zeros((n, 50), dtype=_np.uint8)
    buf[:, 0:12]  = nrm.astype(_np.float32).view(_np.uint8)
    buf[:, 12:48] = tv.reshape(n, 9).view(_np.uint8)
    _ensure_parent(path)
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)
        f.write(struct.pack('<I', n))
        f.write(buf.tobytes())


SUMMARY_FILE = "/mnt/sda2/STL/Fixed/repair_summary.tsv"

_SUMMARY_COLUMNS = ('file', 'status', 'secs', 'tris_in', 'tris_out',
                    'nm_in', 'open_in', 'blender_secs', 'path', 'bbox_drift')


def _reset_summary_file():
    """Truncate the per-file summary at the start of a run and write its header."""
    try:
        _ensure_parent(SUMMARY_FILE)
        with open(SUMMARY_FILE, 'w') as f:
            f.write('\t'.join(_SUMMARY_COLUMNS) + '\n')
    except OSError:
        pass


def log_summary_start(rel, pid):
    """Record that a file has been picked up, before any work begins.

    A worker killed mid-file — SIGKILL from the OOM killer, or a segfault inside
    pymeshlab/pymeshfix — never reaches the finally block that writes the real
    summary row, so without this the one file that killed the run is the single
    file missing from the summary.  Reconciliation is by row order: a file whose
    last row is status='started' never finished, and the PID says which worker
    died.  Best-effort, like the other log writers."""
    # Built from _SUMMARY_COLUMNS rather than a fixed field list, so adding a
    # column cannot silently misalign this row against the real ones.
    row = {'file': rel, 'status': 'started', 'path': f'pid={pid}'}
    line = '\t'.join(row.get(c, '') for c in _SUMMARY_COLUMNS) + '\n'
    _append_locked(SUMMARY_FILE, line)


def log_summary(row):
    """Append one machine-readable row per file.

    The step log interleaves lines from every worker and needs 6-10 lines
    stitched back together per file to answer anything; this gives one row that
    sorts and aggregates directly — which files were slowest, how many reached
    Blender, how much total decimation the collection saw.  'path' records which
    route the file took (decimator used, whether Blender ran) so the two open
    questions — Blender's real cost, and which files had open edges filled — can
    be answered without parsing prose."""
    line = '\t'.join(str(row.get(c, '')) for c in _SUMMARY_COLUMNS) + '\n'
    _append_locked(SUMMARY_FILE, line)


def read_summary(path=None):
    """Parse the summary file, collapsing each file's rows to its final state.

    Each file contributes a 'started' row and, if it finished, a completion row.
    Keeping the last row per file therefore yields the real outcome, and any
    entry still reading 'started' is a file whose worker died before it could
    report — the OOM killer or a library segfault.  Returns a list of dicts."""
    rows = {}
    order = []
    try:
        with open(path or SUMMARY_FILE) as f:
            for line in f:
                line = line.rstrip('\n')
                if not line or line.startswith('file\t'):
                    continue
                parts = line.split('\t')
                if len(parts) < len(_SUMMARY_COLUMNS):
                    parts += [''] * (len(_SUMMARY_COLUMNS) - len(parts))
                rec = dict(zip(_SUMMARY_COLUMNS, parts))
                if rec['file'] not in rows:
                    order.append(rec['file'])
                rows[rec['file']] = rec
    except OSError:
        return []
    return [rows[k] for k in order]


def file_started_in_log(rel):
    """True if this file was actually picked up by a worker.

    log_summary_start writes a 'started' row the moment process_file_safe takes
    a file, before any work begins.  So when the pool breaks, a file with such a
    row was in flight and genuinely interrupted, while one without a row never
    left the queue and can be retried safely on a replacement pool."""
    try:
        with open(SUMMARY_FILE) as f:
            prefix = rel + '\t'
            return any(line.startswith(prefix) for line in f)
    except OSError:
        return False


def sweep_orphan_temps(out_root):
    """Delete pipeline intermediates left behind by a killed worker.

    process_file cleans its temps in a finally block, but SIGKILL — the OOM
    killer, which is exactly what happens on the largest meshes — skips finally
    entirely.  A 44 MB .repairnm.stl was found orphaned after one such kill.
    The M4 suffix filter keeps these from ever being mistaken for inputs, so
    they are only wasted space, but they accumulate one per crash.

    Run at startup, before any work: at that point nothing is in flight, so
    every matching file is certainly stale.  Returns (count, bytes_freed)."""
    patterns = ('.decimate.stl', '.repairnm.stl', '.pymeshfix.stl',
                '.merge.stl', '.partial')
    n = freed = 0
    for root, dirs, names in os.walk(out_root):
        for name in names:
            if any(name.endswith(p) for p in patterns):
                p = os.path.join(root, name)
                try:
                    sz = os.path.getsize(p)
                    os.unlink(p)
                    n += 1
                    freed += sz
                except OSError:
                    pass
    return n, freed


def partition_already_done(files, input_folder, suffix=None):
    """Split files into (todo, done) before any worker is started.

    process_file makes the same checks, but only after a file has been handed to
    a worker and pickled back — on a collection that is mostly already repaired
    that is hundreds of pointless dispatches.  Doing it here also lets the run
    report up front how much work is actually left.

    `done` entries are (path, reason) where reason is one of 'fixed', 'broken',
    'failed', 'unrepaired' or 'open', matching the indicator that was found."""
    if suffix is None:
        suffix = OUTPUT_SUFFIX
    todo, done = [], []
    for src in files:
        dst = output_path(src, suffix, input_folder)
        base = os.path.splitext(dst)[0]
        if os.path.exists(base + '.broken.stl'):
            done.append((src, 'broken'))
        elif os.path.exists(base + '.failed.stl'):
            done.append((src, 'failed'))
        elif os.path.exists(base + '.timeout.stl'):
            done.append((src, 'timeout'))
        elif os.path.exists(base + '.unrepaired.stl'):
            done.append((src, 'unrepaired'))
        elif os.path.exists(base + '.open.stl'):
            done.append((src, 'open'))
        elif os.path.exists(dst):
            done.append((src, 'fixed'))
        else:
            todo.append(src)
    return todo, done


def save_original_copy(src, dst_base):
    """Keep the source as <dst_base>.original.stl next to a suspect output.

    Written when a repair moved the model's bounding box.  That signal cannot
    distinguish a correct repair (Transhuman_Girl/Leg1, where a stray artifact
    was removed) from a destructive one (Zelda NSFW/Chair_foot1, where the end
    caps were pulled shut), so the repaired file stays as the normal output and
    the original is kept beside it.  Whichever is right, both are on disk.

    Deliberately not one of the .broken/.failed/.unrepaired markers: those mean
    "this file was not repaired" and stop the pre-filter from retrying it.  This
    one carries no such meaning — the output next to it is a real result.

    Returns the path written, or None."""
    marker = dst_base + '.original.stl'
    try:
        if os.path.exists(marker):
            return marker
        _ensure_parent(marker)
        shutil.copy2(src, marker)
        return marker
    except OSError:
        return None


def mark_timeout(src, input_folder=None, suffix=None):
    """Write <dst_base>.timeout.stl for a file whose worker was killed on time.

    Called from the parent, not the worker: a SIGKILLed worker never reaches the
    code that writes the other indicators, so without this a file that reliably
    times out is silently re-dispatched on every future run and burns the full
    limit again each time.

    A copy of the source, matching .failed.stl — the indicators are named
    <name>.<signal>.stl precisely so they open in any STL viewer, and an empty
    file would not.  Delete it to retry, as with the others.  Returns the marker
    path, or None if it could not be written."""
    if input_folder is None:
        input_folder = INPUT_FOLDER
    if suffix is None:
        suffix = OUTPUT_SUFFIX
    try:
        dst = output_path(src, suffix, input_folder)
        marker = os.path.splitext(dst)[0] + '.timeout.stl'
        if os.path.exists(marker):
            return marker
        _ensure_parent(marker)
        shutil.copy2(src, marker)
        return marker
    except (OSError, ValueError):
        return None


def measure_files(files):
    """Return [(n_tris, path)] with size read from each header, smallest first.

    Files whose size cannot be determined are reported as 0 and sort first —
    they are cheap to attempt and their real cost is discovered on the way."""
    out = []
    for p in files:
        try:
            if p.lower().endswith('.obj'):
                # No triangle count in an OBJ header; approximate from bytes.
                out.append((os.path.getsize(p) // 60, p))
                continue
            n, err = _read_stl_header(p)
            out.append((0 if (err or n <= 0) else n, p))
        except OSError:
            out.append((0, p))
    out.sort(key=lambda t: t[0])
    return out


# Upper bound on the automatically-chosen worker count.  Past a handful of
# workers the run stops being CPU-bound and starts contending on memory
# bandwidth and disk, and each extra worker widens the blast radius when one is
# killed.  A machine with many cores and a large budget gains little from more.
AUTO_WORKERS_CAP = 6


def auto_worker_count(sized=None, budget=None, cores=None):
    """Choose a worker ceiling automatically (the WORKERS=0 setting).

    Three limits apply and the smallest wins:

      CPU     — one worker per core.  Each worker is mostly single-threaded
                (BLAS/MKL are pinned to one thread in _worker_init), so beyond
                one per core they only contend.
      cap     — AUTO_WORKERS_CAP, a flat ceiling regardless of hardware.
      memory  — the budget divided by what the *median* file costs.  A high
                percentile was tried first and is wrong: it lets one outlier
                set the ceiling for the whole run, and with three files left
                whose largest was 7M triangles it gave a single worker for all
                of them.  plan_worker_count() already lowers concurrency when a
                big file actually comes up, and files run smallest-first, so
                this ceiling only has to suit the typical file.

    Falls back to a conservative 2 when nothing can be measured.  The result is
    only a ceiling — per-file admission may run fewer."""
    if cores is None:
        cores = os.cpu_count() or 2
    if budget is None:
        budget = _run_memory_budget()

    by_cpu = max(1, min(cores, AUTO_WORKERS_CAP))
    if budget <= 0:
        return max(1, min(by_cpu, 2))     # unknown budget: stay cautious

    sizes = sorted(n for n, _ in (sized or []) if n > 0)
    if sizes:
        # Size against the median file.  A high percentile lets one outlier set
        # the ceiling for the entire run — with three files left whose largest
        # is 7M triangles, a p90 reference gave a single worker even though the
        # other two need under 1 GB each.  plan_worker_count() already lowers
        # concurrency when a big file actually comes up, and files run
        # smallest-first, so the ceiling only has to suit the typical file.
        ref = sizes[len(sizes) // 2]
    else:
        ref = 2_000_000                   # no measurements: assume a large-ish mesh
    per = estimate_peak_bytes(ref)
    # Target 70% of the budget rather than all of it.  The per-triangle estimate
    # is a linear fit from measured runs, not a guarantee, and filling the budget
    # exactly leaves nothing for a mesh that costs more than predicted — the
    # failure it exists to prevent.  Simulated on a real collection, filling the
    # budget reached 100% of it, while this lands near 60%.
    by_mem = max(1, int(budget * 0.7 // per)) if per > 0 else by_cpu
    return max(1, min(by_cpu, by_mem, AUTO_WORKERS_CAP))


def plan_worker_count(n_tris, n_workers, budget=None):
    """How many workers may run concurrently while a mesh of n_tris is in flight.

    Memory per worker scales with the mesh being processed, so a fixed worker
    count is wrong at both ends: it wastes capacity on small files and
    overcommits on large ones.  Processing smallest-first and reducing the
    worker count as files grow keeps the run inside its budget without refusing
    work — the concurrency adapts to the mesh instead of the mesh having to fit
    a fixed concurrency.

    Returns at least 1: a single worker is the floor, and whether that one file
    fits at all is a separate question answered by mesh_is_too_large()."""
    if budget is None:
        budget = _run_memory_budget()
    if budget <= 0 or n_tris <= 0:
        return max(1, n_workers)
    per = estimate_peak_bytes(n_tris)
    if per <= 0:
        return max(1, n_workers)
    return max(1, min(n_workers, int(budget // per)))


def rotate_log(path, keep=None):
    """Shift <path> to <path>.1, ageing existing generations, before truncation.

    Renames from the oldest backwards so no generation overwrites one that has
    not been shifted yet: .4 -> .5, .3 -> .4, … , path -> .1.  Anything past
    `keep` is dropped.  Best-effort like the other log helpers — a run must not
    fail because its history could not be rotated."""
    if keep is None:
        keep = LOG_KEEP
    try:
        if keep < 1 or not os.path.exists(path):
            return
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            os.unlink(oldest)
        for n in range(keep - 1, 0, -1):
            src = f"{path}.{n}"
            if os.path.exists(src):
                os.replace(src, f"{path}.{n + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        pass


def rotate_all_logs(keep=None):
    """Rotate every run log.  Call once per run, before the reset helpers."""
    for _p in (LOG_FILE, REVIEW_FILE, SUMMARY_FILE):
        rotate_log(_p, keep)


def _reset_review_file():
    """Truncate the review list at the start of a run and write its header."""
    try:
        _ensure_parent(REVIEW_FILE)
        with open(REVIEW_FILE, 'w') as f:
            f.write(f"# Files decimated {REVIEW_RATIO:g}x or more — worth checking that thin\n"
                    f"# walls and connector holes (magnets, pins) survived.\n"
                    f"# file\tfaces_before\tfaces_after\tratio\n")
    except OSError:
        pass


def log_review(rel, n_before, n_after):
    """Record a heavily-decimated file for later inspection.

    Only files reduced by REVIEW_RATIO or more are listed.  That is where thin
    walls and small connector holes (magnet sockets, pin holes) are most likely
    to have been distorted — decimation itself never closes a boundary loop, but
    it can thin the geometry around one, and the later open-edge fill cannot tell
    an intentional opening from a defect.  Writing the file is best-effort: a
    review list is an aid, never a reason to fail a repair that otherwise worked."""
    ratio = (float(n_before) / float(n_after)) if n_after else 0.0
    _append_locked(REVIEW_FILE, f"{rel}\t{n_before}\t{n_after}\t{ratio:.1f}x\n")


def run_fast_decimate(src, dst, target_faces):
    """Decimate src to target_faces with fast_simplification's quadric edge collapse.

    Same Garland-Heckbert algorithm PyMeshLab and Blender use, but operating on
    plain numpy arrays instead of a full mesh database, which is where the cost
    difference comes from.  Measured on a 2.55M-triangle mesh -> 900k:
        Blender      43s     (OOM'd under a worker RLIMIT_AS)
        PyMeshLab    42.0s   1557 MB peak
        this path    ~5.6s   ~915 MB peak

    Decimation may leave non-manifold edges; that is expected and is what the
    later NM-repair and open-edge steps of the pipeline exist to clean up.
    Returns (nm, open_e, n_faces_out)."""
    verts, faces = _weld_binary_stl(src)
    n_in = len(faces)
    if n_in <= target_faces:
        return None  # caller falls through; nothing to do
    # fast_simplification takes the fraction of faces to REMOVE.
    reduction = 1.0 - (float(target_faces) / float(n_in))
    v2, f2 = _fastsimp.simplify(verts, faces.astype(_np.uint32), reduction)
    del verts, faces
    _write_binary_stl(dst, v2, f2)
    n_out = len(f2)
    del v2, f2
    nm, open_e, _ = scan_mesh_errors(dst)
    return nm, open_e, n_out


def run_pymeshlab_decimate(src, dst, target_faces):
    """Decimate src to target_faces using QEC with topology preservation. Returns (nm, open_e)."""
    ms = _pymeshlab.MeshSet()
    ms.load_new_mesh(src)
    ms.meshing_decimation_quadric_edge_collapse(
        targetfacenum=target_faces,
        preservetopology=True,
        preserveboundary=True,
        preservenormal=True,
        autoclean=True,
    )
    ms.save_current_mesh(dst)
    n = ms.current_mesh().face_number()
    nm, open_e, _ = scan_mesh_errors(dst, n)
    return nm, open_e, n


def collect_companion_files(folder, recursive):
    """Return list of (src, dst) pairs for non-STL companion files to copy."""
    input_folder = os.path.abspath(folder)
    out_root     = os.path.join(os.path.dirname(input_folder), 'Fixed')
    pairs = []
    if recursive:
        for root, dirs, names in os.walk(input_folder):
            dirs[:] = [d for d in dirs if d != PARTS_DIRNAME]
            for name in names:
                if os.path.splitext(name)[1].lower() in COMPANION_EXTENSIONS:
                    src = os.path.join(root, name)
                    rel = os.path.relpath(src, input_folder)
                    dst = os.path.join(out_root, rel)
                    pairs.append((src, dst))
    else:
        for name in os.listdir(input_folder):
            if os.path.splitext(name)[1].lower() in COMPANION_EXTENSIONS:
                src = os.path.join(input_folder, name)
                dst = os.path.join(out_root, name)
                pairs.append((src, dst))
    return pairs


# Files the tool itself writes into the output tree.  None of these are ever
# valid inputs — if the output folder is ever scanned (a retry workflow, or Fixed
# nested under the input root) they must not be picked up as source meshes.
_SIGNAL_SUFFIXES = (
    # Result indicators.
    '.unrepaired.stl', '.failed.stl', '.broken.stl', '.open.stl', '.timeout.stl',
    '.original.stl',
    # Pipeline intermediates — left behind if a run is killed mid-file (todo M4).
    '.decimate.stl', '.repairnm.stl', '.pymeshfix.stl', '.merge.stl', '.partial',
)

# Per-shell files written by split_shells() into the ~parts subfolder (todo H2).
_PART_MARKER = '.part.'

# <base>.seam.<N>.stl — a region cut out at a winding seam, repaired on its own
# and merged back.  Like a shell part it lives in the output tree and must never
# be collected as an input; unlike one there can be any number of them, so this
# is matched as a marker rather than enumerated.
_SEAM_MARKER = '.seam.'

def collect_stl_files(folder, recursive):
    files = []
    suffix_lower = OUTPUT_SUFFIX.lower()
    def _keep(name):
        name_lower = name.lower()
        ext = os.path.splitext(name_lower)[1]
        if ext not in ('.stl', '.obj'):
            return False
        # AppleDouble stubs.  macOS writes a "._<name>" sidecar next to every
        # file it zips, carrying resource-fork metadata and no geometry.  They
        # match *.stl, so they used to be collected, decimated past, handed to
        # PyMeshFix, and finally to Blender, which reported "STL triangles: 0,
        # Verts loaded: 0, BLENDER_EMPTY" — one wasted Blender launch each.
        # A 822-file collection contained 61 of them: every one counted as a
        # failure and they made the run look 44% broken.
        if name.startswith('._'):
            return False
        for sig in _SIGNAL_SUFFIXES:
            if name_lower.endswith(sig):
                return False
        # <base>.part.<N>.stl — a split shell, not an input mesh.
        if _PART_MARKER in name_lower or _SEAM_MARKER in name_lower:
            return False
        if suffix_lower and ext == '.stl':
            base = os.path.splitext(name_lower)[0]
            if base.endswith(suffix_lower):
                return False
        return True
    if recursive:
        for root, dirs, names in os.walk(folder):
            # Never descend into split-part scratch folders, or into the
            # __MACOSX tree a macOS zip carries alongside the real files —
            # it holds nothing but AppleDouble sidecars (see _keep).
            dirs[:] = [d for d in dirs
                       if d != PARTS_DIRNAME and d != '__MACOSX']
            for name in names:
                if _keep(name):
                    files.append(os.path.join(root, name))
    else:
        for name in os.listdir(folder):
            if _keep(name):
                files.append(os.path.join(folder, name))
    return sorted(files)


def is_ascii_stl(path):
    """Return True if the file is an ASCII STL (starts with 'solid' and contains 'facet normal').

    Tiebreaker: if the header text looks like ASCII but the declared triangle count
    is consistent with the file size, treat it as binary — some exporters (SolidWorks,
    older Slic3r) write 'solid <name>' headers on binary files.

    Known limit, deliberately left alone: only the first 256 bytes are read, so a
    valid ASCII STL whose solid name runs past ~240 characters before the first
    'facet normal' is misread as binary.  The mis-detection is one-directional —
    such a file is then parsed as binary, where the triangle-count field is
    garbage and _read_stl_header's size cross-check rejects it as corrupt rather
    than repairing the wrong bytes.  So the failure mode is a false 'corrupt'
    report on a file nobody produces, not silent damage.  Reading further would
    cost a larger read on every file in the collection to defend against that.
    Revisit only if a real file is ever reported corrupt with a long solid name.
    """
    try:
        with open(path, 'rb') as f:
            header = f.read(256)
        looks_ascii = (header.lstrip()[:5].lower() == b'solid'
                       and b'facet normal' in header.lower())
        if not looks_ascii:
            return False
        # Tiebreaker: check whether the binary triangle-count field is consistent
        # with the actual file size.  If it is, the file is binary despite the header.
        if len(header) >= 84:
            n = struct.unpack_from('<I', header, 80)[0]
            size = os.path.getsize(path)
            if size >= 84 + n * 50:
                return False  # binary STL with a misleading text header
        return True
    except Exception:
        return False


def check_stl_integrity(path):
    """
    Returns (n_tris, is_ascii, error_string).  error_string is None if file looks valid.
    OBJ files return (0, False, None) — integrity is validated by Blender at import time.
    Only rejects STL files that are genuinely too short to contain the triangles
    they claim — some exporters append extra bytes after the data (color info
    etc.) which is non-standard but readable by most slicers.
    """
    try:
        if path.lower().endswith('.obj'):
            if os.path.getsize(path) == 0:
                return 0, False, "OBJ file is empty"
            return 0, False, None  # let Blender validate at import
        if is_ascii_stl(path):
            return 0, True, None   # ASCII STL — Blender imports natively, skip binary checks
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            header = f.read(80)
            if len(header) < 80:
                return 0, False, "file too short to be a valid STL"
            raw = f.read(4)
            if len(raw) < 4:
                return 0, False, "missing triangle count"
            n = struct.unpack('<I', raw)[0]
        minimum = 80 + 4 + n * 50
        if size < minimum:
            return n, False, f"truncated binary STL: need {minimum:,} bytes, got {size:,}"
        return n, False, None
    except Exception as e:
        return 0, False, str(e)


def output_path(src, suffix, input_folder, makedirs=False):
    # Normalise first so trailing slashes don't affect dirname.
    input_folder = os.path.abspath(input_folder)
    rel          = os.path.relpath(src, input_folder)
    base, ext    = os.path.splitext(rel)
    # OBJ files are always converted to STL output.
    out_ext = '.stl' if ext.lower() == '.obj' else ext
    out_dir = os.path.join(os.path.dirname(input_folder), 'Fixed')
    dst     = os.path.join(out_dir, base + suffix + out_ext)
    if makedirs:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    return dst



def _unpack_edge_key(key):
    """Reverse the packing done by _build_edge_counts: a 192-bit int back into
    two (x, y, z) float32 coordinate tuples."""
    lo = key & ((1 << 96) - 1)
    hi = key >> 96
    return (struct.unpack('<3f', hi.to_bytes(12, 'little')),
            struct.unpack('<3f', lo.to_bytes(12, 'little')))


def _find_paired_open_vertices(path, shared_epsilon=0.001, edge_counts=None):
    """Find vertex pairs that should be merged to close paired open boundary loops.
    For each open edge pair that shares a vertex (within shared_epsilon mm),
    the two free endpoints are a merge candidate.
    Returns a list of (va, vb) coordinate tuples to snap together, or [].
    Uses a spatial bucket (O(n)) instead of O(n²) nested loop.

    edge_counts, when supplied, is a packed edge map from a previous
    scan_mesh_errors(..., return_edges=True) call — reusing it skips a full
    re-read and re-scan of the file (see todo L5)."""
    if edge_counts is None:
        _nm, _open, _err, edge_counts = scan_mesh_errors(path, return_edges=True)
        if _err or edge_counts is None:
            return []

    open_edges = [_unpack_edge_key(k) for k, c in edge_counts.items() if c == 1]
    if not open_edges:
        return []

    # Build a bucket keyed by rounded vertex coordinate so shared-vertex
    # lookup is O(1) per edge rather than O(n).
    inv_eps = 1.0 / shared_epsilon

    def _bucket(v):
        return (int(round(v[0] * inv_eps)),
                int(round(v[1] * inv_eps)),
                int(round(v[2] * inv_eps)))

    # Map each vertex bucket -> list of (free_endpoint, edge_index)
    bucket_map = {}
    for idx, (v0, v1) in enumerate(open_edges):
        for shared, free in ((v0, v1), (v1, v0)):
            b = _bucket(shared)
            bucket_map.setdefault(b, []).append((free, idx))

    pairs = []
    used = set()
    for idx, (v0, v1) in enumerate(open_edges):
        if idx in used:
            continue
        for shared, free_a in ((v0, v1), (v1, v0)):
            b = _bucket(shared)
            for free_b, other_idx in bucket_map.get(b, []):
                if other_idx <= idx or other_idx in used:
                    continue
                pairs.append((free_a, free_b))
                used.add(idx)
                used.add(other_idx)
                break
            if idx in used:
                break

    return pairs


def _merge_paired_vertices(src, dst, pairs):
    """Write a copy of binary STL src to dst with paired vertices snapped together.
    For each (va, vb) pair, any occurrence of vb in the mesh is replaced with va.
    Only the exact listed coordinate pairs are touched — all other geometry is unchanged."""
    # Build replacement map: vb -> va (snap vb to va position).
    # Pairs are non-overlapping (each vertex appears in at most one pair),
    # so single-pass substitution is sufficient — no chaining needed.
    replace = {}
    for va, vb in pairs:
        replace[vb] = va

    # Re-read and rewrite with replacements applied
    with open(src, 'rb') as f:
        header = f.read(80)
        n_tris = struct.unpack('<I', f.read(4))[0]
        raw = f.read(n_tris * 50)

    buf = bytearray(80 + 4 + n_tris * 50)
    buf[:80] = header
    struct.pack_into('<I', buf, 80, n_tris)
    for i in range(n_tris):
        src_base = i * 50
        dst_base = 84 + i * 50
        # copy normal unchanged
        buf[dst_base:dst_base+12] = raw[src_base:src_base+12]
        for j in range(3):
            vo = src_base + 12 + j * 12
            v = struct.unpack_from('<3f', raw, vo)
            v = replace.get(v, v)
            struct.pack_into('<3f', buf, dst_base + 12 + j * 12, *v)
        # attribute bytes
        buf[dst_base+48:dst_base+50] = raw[src_base+48:src_base+50]

    with open(dst, 'wb') as f:
        f.write(buf)


def run_pymeshfix(src, dst, edge_counts=None):
    """Run PyMeshFix on src and write result to dst.
    Surgically snaps only the paired open-boundary vertex pairs together before
    repair — leaves all other geometry untouched to avoid fuzziness.
    edge_counts, if given, is a packed edge map for src from an earlier scan,
    reused to avoid re-reading the file.
    Returns (nm, open_edges) from a pure-Python edge scan, or raises on error."""
    pairs = _find_paired_open_vertices(src, edge_counts=edge_counts)
    _merge_tmp = None
    try:
        if pairs:
            _ensure_parent(dst)
            _merge_tmp = dst + '.merge.stl'
            _merge_paired_vertices(src, _merge_tmp, pairs)
            tin = _pymeshfix.MeshFix(_merge_tmp)
        else:
            tin = _pymeshfix.MeshFix(src)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tin.repair()
            tin.save(dst)
    finally:
        # Always remove the intermediate, including when repair() raises.
        if _merge_tmp and os.path.exists(_merge_tmp):
            os.unlink(_merge_tmp)
    n_tris, _hdr_err = _read_stl_header(dst)
    if _hdr_err:
        raise RuntimeError(f"pymeshfix output unreadable: {_hdr_err}")
    if n_tris == 0:
        raise RuntimeError("pymeshfix produced empty mesh")
    nm, open_e, _ = scan_mesh_errors(dst)
    return nm, open_e


def _run_blender_script(script, elapsed=0.0, L=None, route='blender'):
    """Run `script` in headless Blender.  Returns (rc, stdout, stderr, timed_out).

    `elapsed` is how long this mesh has already taken.  Blender is given the
    time that leaves, less the reserve the post-Blender steps need — see
    _blender_budget().  With too little left it is not started at all and the
    call returns timed_out=True without spending anything, because a run that
    cannot finish costs the mesh its remaining time and produces nothing.

    Centralises what both Blender entry points need to get right: the temp
    script is always unlinked, `_blender_proc` is published so the signal
    handlers can kill a hung child and always cleared afterwards, and
    preexec_fn lifts any RLIMIT_AS inherited from the worker — that limit caps
    *virtual* address space, which Blender reserves far more of than it
    resides, and left in place it killed repairs at ~1.2 GB with 14 GB free.

    On timeout the child is killed and reaped before returning; rc is None and
    timed_out is True.  Callers map the result onto their own return shape."""
    global _blender_proc
    import time as _time
    _bud = _blender_budget(elapsed)
    _t_start = _time.monotonic()
    if not _bud:
        # Not enough of the mesh's budget left to be worth starting.  Reported
        # as a timeout so callers take their existing timeout path; nothing was
        # run, so nothing has to be killed or cleaned up.
        if L:
            L(f"blender: skipped ({route}) — {elapsed:.0f}s of "
              f"{TIMEOUT_PART or TIMEOUT}s spent, under "
              f"{_BLENDER_MIN_RUN:.0f}s left after the "
              f"{BLENDER_RESERVE_PCT}% reserve")
        return None, '', f'skipped: under {_BLENDER_MIN_RUN:.0f}s left', True
    if L:
        L(f"blender: start ({route})  budget {_bud:.0f}s")
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
        tmp.write(script)
        script_path = tmp.name
    try:
        proc = subprocess.Popen(
            [BLENDER, '--background', '--python', script_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            preexec_fn=_unlimit_child_address_space,
        )
        _blender_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=_bud)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            if L:
                L(f"blender: KILLED ({route}) after "
                  f"{_time.monotonic() - _t_start:.1f}s — exceeded its "
                  f"{_bud:.0f}s budget")
            return None, '', '', True
        finally:
            _blender_proc = None
        if L:
            L(f"blender: done  ({route})  "
              f"{_time.monotonic() - _t_start:.1f}s  rc={proc.returncode}")
        return proc.returncode, stdout, stderr, False
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def fix_stl(src, dst, merge_dist, is_ascii=False, is_obj=False, elapsed=0.0,
            L=None, route='blender'):
    script = BLENDER_SCRIPT.format(src=src, dst=dst, merge_dist=merge_dist,
                                   is_ascii=repr(bool(is_ascii)),
                                   is_obj=repr(bool(is_obj)))
    _bud = _blender_budget(elapsed)
    rc, stdout, stderr, timed_out = _run_blender_script(script, elapsed=elapsed,
                                                        L=L, route=route)
    if timed_out:
        return False, False, False, f'TIMEOUT after {_bud:.0f}s', ''
    success    = rc == 0 and 'BLENDER_OK' in stdout
    open_only  = rc == 0 and 'BLENDER_OPEN' in stdout
    unrepaired = 'BLENDER_UNREPAIRED' in stdout
    return success, open_only, unrepaired, stdout, stderr

def blender_decimate(src, dst, max_faces, elapsed=0.0, L=None, route='decimate'):
    """Run stl_batch_fix.decimate.blender on src, writing a decimated binary STL to dst.
    Returns (ok, n_faces_out, stdout, stderr).  n_faces_out is -1 on failure."""
    script = BLENDER_DECIMATE_SCRIPT.format(src=src, dst=dst, max_faces=max_faces)
    _bud = _blender_budget(elapsed)
    rc, stdout, stderr, timed_out = _run_blender_script(script, elapsed=elapsed,
                                                        L=L, route=route)
    if timed_out:
        return False, -1, f'TIMEOUT after {_bud:.0f}s', ''
    ok = rc == 0 and 'BLENDER_DECIMATE_OK' in stdout
    n_out = -1
    for line in stdout.splitlines():
        if line.startswith('Decimate output faces: '):
            try:
                n_out = int(line.split(': ', 1)[1])
            except ValueError:
                pass
        elif line.startswith('Decimate skipped'):
            try:
                n_out = int(line.split('(')[1].split(' ')[0])
            except (ValueError, IndexError):
                pass
    return ok, n_out, stdout, stderr


# ---------------------------------------------------------------------------
# process_file helpers
# ---------------------------------------------------------------------------

def _clear_stale(*paths):
    """Delete any of the given indicator paths that exist."""
    for p in paths:
        if os.path.exists(p):
            os.unlink(p)


def _post_verify(path, L, label="post-verify"):
    """Independently re-scan a mesh that a repair step just claimed was clean.

    PyMeshFix (and Blender) self-report unreliably, so nothing is written out as
    'ok' on a library's word alone.

    Returns (nm, open_e, verified):
      (0, 0, True)   — the file was scanned and is genuinely clean
      (n, m, True)   — scanned, and defects remain
      (0, 0, False)  — the scan could not be performed at all

    The third value exists because the first two cannot express "unknown".
    Every unscannable case — ASCII input, a scan error, a mesh too large to
    scan — used to return a bare (0, 0), which callers test as `nm > 0 or
    open_e > 0` and therefore read as verified-clean.  The file was then written
    out as status 'ok' having been checked by nothing, with the only trace a log
    line no summary column reflects.  That matters most on the largest meshes,
    which are both the ones that fail this scan and the ones most likely to be
    genuinely broken.  Callers must treat verified=False as unproven."""
    if is_ascii_stl(path):
        L(f"{label}: ASCII STL — cannot edge-scan, repair UNVERIFIED")
        return 0, 0, False
    nm, open_e, err = scan_mesh_errors(path)
    if err:
        L(f"{label} scan error — {err}; repair UNVERIFIED")
        return 0, 0, False
    if nm == -1:
        L(f"{label}: mesh too large to scan, repair UNVERIFIED")
        return 0, 0, False
    if nm > 0 or open_e > 0:
        L(f"{label} — reported ok but scan found nm={nm} open={open_e}")
    return nm, open_e, True


def _cleanup_parts(parts_dir, parts):
    """Remove split-part files (and any indicator/temp siblings a part's own
    repair produced) after their merged result has been written.

    Only files belonging to the given parts are touched — the ~parts folder is
    shared by every mesh in the same output directory, so a blanket rmtree would
    destroy a sibling file's in-progress parts.  The directory itself is removed
    only once it is empty.  Returns the number of files deleted."""
    removed = 0
    for part_path in parts:
        stem = os.path.splitext(part_path)[0]
        pending = _pending_part_path(part_path)
        p_stem  = os.path.splitext(pending)[0]
        for candidate in (part_path, pending,
                          *(stem + sfx for sfx in _SIGNAL_SUFFIXES),
                          *(p_stem + sfx for sfx in _SIGNAL_SUFFIXES)):
            try:
                if os.path.exists(candidate):
                    os.unlink(candidate)
                    removed += 1
            except OSError:
                pass
    try:
        os.rmdir(parts_dir)   # only succeeds when nothing else is left in it
        # The per-mesh dir lives under ~parts/; drop that too once the last
        # mesh in this output folder is done with it.
        os.rmdir(os.path.dirname(os.path.abspath(parts_dir)))
    except OSError:
        pass
    return removed


def _save_indicator(indicator_path, src_path, stales):
    """Copy src_path to indicator_path, delete stale indicators, return file size."""
    _ensure_parent(indicator_path)
    shutil.copy2(src_path, indicator_path)
    _clear_stale(*stales)
    return os.path.getsize(indicator_path)


def _try_pymeshfix_after_blender(src_for_fix, dst, open_copy, failed_copy,
                                 unrepaired_copy, is_ascii, is_obj, stdout, stderr,
                                 label, L, _result, temps=None):
    """Attempt a PyMeshFix pass on src_for_fix after Blender left open edges.

    Returns a result dict if PyMeshFix settles the file (ok or open), or None
    if it fails / makes things worse (caller should fall through to its own
    indicator logic)."""
    _pmf_tmp = dst + '.pymeshfix.stl'
    if temps is not None:
        temps.append(_pmf_tmp)
    try:
        _pmf_nm, _pmf_open = run_pymeshfix(src_for_fix, _pmf_tmp)
        L(f"pymeshfix {label}: nm={_pmf_nm}  open={_pmf_open}")
        if _pmf_nm == 0 and _pmf_open == 0:
            # Verify with an independent edge scan — pymeshfix self-report is not reliable.
            _pv_nm, _pv_open, _pv_ok = _post_verify(
                _pmf_tmp, L, label=f"pymeshfix {label} post-verify")
            if _pv_nm > 0 or _pv_open > 0:
                _pmf_open = _pv_open  # fall through to open-edges path below
            if _pmf_open == 0:
                os.replace(_pmf_tmp, dst)
                size = os.path.getsize(dst)
                _clear_stale(failed_copy, unrepaired_copy, open_copy)
                L(f"result: ok ({label})"
                  + ("" if _pv_ok else " — UNVERIFIED, scan could not run"))
                return _result(status='ok', dst=os.path.basename(dst),
                               size=size, is_ascii=is_ascii, is_obj=is_obj,
                               pymeshfix=True, verified=_pv_ok)
        if _pmf_nm == 0 and _pmf_open > 0:
            _ensure_parent(open_copy)
            os.replace(_pmf_tmp, open_copy)
            if os.path.exists(dst):
                os.unlink(dst)
            size = os.path.getsize(open_copy)
            _clear_stale(failed_copy, unrepaired_copy)
            L(f"result: open (nm=0 open={_pmf_open})")
            return _result(status='open', size=size, dst=os.path.basename(open_copy),
                           stdout=stdout, stderr=stderr)
        else:
            if os.path.exists(_pmf_tmp):
                os.unlink(_pmf_tmp)
    except Exception as _pmf_err:
        if os.path.exists(_pmf_tmp):
            os.unlink(_pmf_tmp)
        L(f"pymeshfix {label}: FAILED — {_pmf_err}")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Worker function defined at module level so ProcessPoolExecutor can pickle it.
# On Linux (fork) workers inherit parent state; on Windows/macOS (spawn) they
# re-import this module — the __main__ guard below prevents the pool from
# being recreated in each worker process.

def process_file(src, is_part=False):
    """Run in a worker process. Returns a dict describing the outcome.
    is_part=True: src is already in the output folder (written by split_shells),
    so dst==src and no output_path remapping is needed. Splitting is suppressed.

    Thin wrapper around _process_file_impl that guarantees every pipeline
    intermediate is removed, however the pipeline exits (todo M3).  The impl
    appends each temp it creates to `temps`; anything still on disk afterwards
    that isn't the final output gets unlinked."""
    import time as _t0mod
    temps = []
    # Facts the impl records as it goes, so one summary row can be written on
    # every exit path — including exceptions — without threading return values
    # through a dozen `return` statements.
    stats = {'path': []}
    _started = _t0mod.monotonic()
    result = None
    _rel_for_log = (os.path.basename(src) if is_part
                    else os.path.relpath(src, INPUT_FOLDER))
    if not is_part:
        log_summary_start(_rel_for_log, os.getpid())
    try:
        result = _process_file_impl(src, is_part=is_part, temps=temps, stats=stats)
        return result
    finally:
        for _t in temps:
            try:
                if _t and os.path.exists(_t):
                    os.unlink(_t)
            except OSError:
                pass
        # Parts are an internal detail of splitting a parent mesh; summarising
        # them would double-count the parent's triangles.
        # Carry the bbox finding on the result too, not just into the summary
        # file — the TUI counts it live, and re-reading the summary every 0.25s
        # to find out would be absurd.
        if result is not None and stats.get('bbox_drift'):
            result['bbox_drift'] = stats['bbox_drift']
        if not is_part:
            # Read the delivered triangle count from the output itself rather
            # than tracking it through the pipeline — whatever path ran, this is
            # what actually landed on disk.
            if 'tris_out' not in stats and result and result.get('dst'):
                _out = os.path.join(
                    os.path.dirname(output_path(src, OUTPUT_SUFFIX, INPUT_FOLDER)),
                    result['dst'])
                _n, _e = _read_stl_header(_out)
                if not _e:
                    stats['tris_out'] = _n
            log_summary({
                'file':         os.path.relpath(src, INPUT_FOLDER)
                                if not is_part else os.path.basename(src),
                'status':       (result or {}).get('status', 'exception'),
                'secs':         f"{_t0mod.monotonic() - _started:.1f}",
                'tris_in':      stats.get('tris_in', ''),
                'tris_out':     stats.get('tris_out', ''),
                'nm_in':        stats.get('nm_in', ''),
                'open_in':      stats.get('open_in', ''),
                'blender_secs': stats.get('blender_secs', ''),
                'path':         '+'.join(stats['path']) or 'none',
                'bbox_drift':   stats.get('bbox_drift', ''),
            })


def _process_file_impl(src, is_part=False, temps=None, stats=None):
    if temps is None:
        temps = []
    if stats is None:
        stats = {'path': []}
    fixed_root  = os.path.dirname(os.path.abspath(INPUT_FOLDER))
    if is_part:
        # Part files are already in the output folder — treat them as their own dst.
        dst      = src
        rel      = os.path.basename(src)  # display name only
    else:
        dst      = output_path(src, OUTPUT_SUFFIX, INPUT_FOLDER)
        rel      = os.path.relpath(src, INPUT_FOLDER)
    dst_base    = os.path.splitext(dst)[0]

    # State files use <name>.<signal>.stl naming so they open in any STL viewer.
    # All live next to the output (dst_base), never next to the source.
    # Contents:
    #   <dst_base>.broken.stl     — copy of source; permanently bad mesh; never retried
    #   <dst_base>.failed.stl     — copy of source; transient error (crash/timeout); delete to retry
    #   <dst_base>.unrepaired.stl — copy of source; nm>0 remains; delete to retry
    #   <dst_base>.open.stl       — repaired output; nm=0 but open edges remain (slicer handles)
    #   <dst_base>.timeout.stl    — copy of source; exceeded TIMEOUT, killed by the
    #                               parent watchdog (written there, not here — a
    #                               SIGKILLed worker never runs this code)

    broken_copy     = dst_base + '.broken.stl'
    failed_copy     = dst_base + '.failed.stl'
    unrepaired_copy = dst_base + '.unrepaired.stl'
    open_copy       = dst_base + '.open.stl'

    import time as _time
    _t0 = _time.monotonic()
    _tp = [_t0]  # mutable so the closure can update it

    def L(msg, step=None):
        now = _time.monotonic()
        elapsed = now - _t0
        delta   = now - _tp[0]
        _tp[0]  = now
        prefix = f"[{step}] " if step else ""
        log_step(rel, f"+{elapsed:5.1f}s  dt={delta:5.1f}s  {prefix}{msg}")

    # Signal files mean "don't retry this input" -- but only for a real input.
    # A part lives in ~parts/, which is scratch rebuilt on every split, and its
    # state is carried by the ~ prefix instead.  Honouring them here made a part
    # with a stale .failed.stl report 'skip', which every merge site counts as
    # success: the merge then stitched in unrepaired geometry and called it done.
    if not is_part:
        if os.path.exists(broken_copy):
            return {'rel': rel, 'status': 'skip', 'reason': 'previously broken — bad mesh data'}
        if os.path.exists(failed_copy):
            return {'rel': rel, 'status': 'skip',
                    'reason': f"previously failed — delete {os.path.basename(failed_copy)} from output folder to retry"}
        if os.path.exists(unrepaired_copy):
            return {'rel': rel, 'status': 'skip',
                    'reason': f"previously unrepaired — delete {os.path.basename(unrepaired_copy)} from output folder to retry"}
        if os.path.exists(open_copy):
            return {'rel': rel, 'status': 'skip',
                    'reason': f"previously open-edges — delete {os.path.basename(open_copy)} from output folder to retry"}
    if not is_part and os.path.exists(dst):
        return {'rel': rel, 'status': 'skip',
                'reason': f"already fixed: {os.path.relpath(dst, fixed_root)}"}
    is_obj = src.lower().endswith('.obj')

    n_tris, is_ascii, err = check_stl_integrity(src)
    if err:
        if src != broken_copy:  # avoid copying a part onto itself
            _ensure_parent(broken_copy)
            shutil.copy2(src, broken_copy)
        L(f"corrupt: {err}")
        return {'rel': rel, 'status': 'corrupt', 'reason': err}

    # Admission control, before anything touches the mesh.  A file whose
    # projected peak exceeds most of the run's entire memory budget cannot be
    # made to fit by using fewer workers — it would be OOM-killed running alone,
    # taking the pool down with it.  Refusing it up front costs one file and
    # names the reason, instead of losing a worker and every file queued behind
    # it.  AI-generated meshes reach this size routinely.
    if n_tris > 0 and mesh_is_too_large(n_tris):
        _budget = _run_memory_budget()
        _need   = estimate_peak_bytes(n_tris)
        _msg = (f"needs ~{_need/1024**3:.1f} GB but the run's budget is "
                f"{_budget/1024**3:.1f} GB — raise MEM_MAX or pre-decimate this file")
        L(f"too large: {n_tris:,} tris — {_msg}")
        size = _save_indicator(failed_copy, src, [])
        return {'rel': rel, 'status': 'failed', 'is_mesh_bad': False,
                'reason': f"too large: {_msg}",
                'stdout': f"{n_tris:,} triangles; {_msg}", 'stderr': ''}

    # -----------------------------------------------------------------------
    # Pipeline (binary STL only; OBJ/ASCII always go to Blender for conversion)
    #
    # Step A — scan source
    # Step B — decimate with PyMeshLab if over face limit (topology-safe QEC)
    # Step C — repair open edges with PyMeshFix
    # Step D — split multi-shell STLs (only after PyMeshFix, only if not a part file)
    # Step E — if still NM edges, fall back to Blender repair loop
    # -----------------------------------------------------------------------

    if not is_obj and not is_ascii and n_tris > 0:
        nm_src, open_src, _scan_err = scan_mesh_errors(src, n_tris)
        scan_note = "  (too large to scan — will decimate+repair)" if nm_src == -1 and not _scan_err else ""
        if _scan_err:
            scan_note = f"  (scan error: {_scan_err})"
        L(f"src: tris={n_tris:,}  nm={nm_src}  open={open_src}{scan_note}")
        stats['tris_in'] = n_tris
        stats['nm_in']   = nm_src
        stats['open_in'] = open_src

        # Perfect mesh within limit — just copy, nothing to do.
        # Winding seams are a defect scan_mesh_errors cannot see — it counts
        # non-manifold and open edges only.  A mesh can be nm=0 open=0 and
        # still contain two regions that disagree about which way is out; the
        # model that prompted all this was exactly that, and took the clean
        # copy path straight past every repair stage.  Checked here, before
        # that shortcut, and reused by step E0 below.
        # Runs for shell parts too, not just whole files.  The two splits cut on
        # different things and nest rather than compete: the shell split
        # separates components that do not touch, this one separates connected
        # regions that disagree about which way is out.  A shell part is exactly
        # where the second kind lives — a head shell with hair sculpted onto it
        # is one component containing two surfaces, and without this it reached
        # PyMeshFix unprotected and came back with the hair deleted.
        _seam_edges, _seam_loops = [], 0
        if nm_src != -1 and open_src != -1:
            try:
                # `working` is not assigned until below; at this point the
                # source file is what would be copied or repaired.
                _sv, _sf = _weld_binary_stl(src)
                _seam_edges, _seam_loops = find_winding_seams(_sv, _sf)
                del _sv, _sf
                if _seam_loops:
                    L(f"seam check: {len(_seam_edges)} winding-seam edges in "
                      f"{_seam_loops} closed loop(s)")
                    stats['seam_loops'] = _seam_loops
            except Exception as _sc_err:
                L(f"seam check failed — {_sc_err}")

        if (nm_src == 0 and open_src == 0 and not _seam_loops
                and (MAX_FACES == 0 or n_tris <= MAX_FACES)):
            if src != dst:
                # Create dst's own parent directly — never recompute it via
                # output_path(), which for a part file resolves somewhere else
                # entirely (todo H1).
                _ensure_parent(dst)
                shutil.copy2(src, dst)
            size = os.path.getsize(dst)
            _clear_stale(failed_copy, unrepaired_copy, open_copy)
            L(f"result: ok (clean copy — no repair needed)")
            return {'rel': rel, 'status': 'ok', 'dst': os.path.basename(dst),
                    'size': size, 'is_ascii': False, 'bypassed': True}

        working = src
        working_tris = n_tris
        _dec_tmp = None
        _pmf_tmp = None

        # Step B — split multi-shell (only on original files, not parts).
        # Parts are written to the output folder; source is never modified.
        # Skip entirely if dst or dst.failed already exists, or mesh is too large for PyMeshLab.
        #
        # The split is deferred to step B2 whenever decimation is going to run,
        # for two separate reasons.
        #
        # Over the size limit, it has to be: the mesh cannot be scanned or split
        # at full resolution.  Skipping the split outright is how a 39-shell
        # model 3% over the threshold reached PyMeshFix intact and came back as
        # a single shell with its head deleted.
        #
        # Under the limit it is a choice, and the right one, because MAX_FACES
        # is a per-file budget.  Split first and every part gets the full
        # budget: a 240-face speck is left untouched while a 1.3M-face body
        # absorbs the entire reduction alone.  Decimate first and one budget is
        # spread across the whole model, so every shell is reduced by the same
        # proportion — measured on Mandy_Body_Dinamuuu3D, all 39 shells kept
        # 43-50% of their faces.
        #
        # A file under MAX_FACES is never decimated, so there is no "after
        # decimation" for it: those still split here.
        _will_decimate = MAX_FACES > 0 and n_tris > MAX_FACES
        _split_deferred = (not is_part and _PYMESHLAB_AVAILABLE
                           and (n_tris > _LARGE_MESH_TRI_LIMIT or _will_decimate))
        if _split_deferred:
            _why = ('over the '
                    f'{_LARGE_MESH_TRI_LIMIT:,} scan limit'
                    if n_tris > _LARGE_MESH_TRI_LIMIT
                    else 'so one face budget is shared across every shell')
            L(f"step B: deferred — {n_tris:,} tris, {_why}; "
              f"will split after decimation")
        if (not is_part and _PYMESHLAB_AVAILABLE and not _split_deferred
                and n_tris <= _LARGE_MESH_TRI_LIMIT):
            if os.path.exists(dst) or os.path.exists(dst_base + '.failed.stl'):
                pass  # already handled — fall through to normal repair
            else:
                dst_dir = _parts_dir_for(dst)
                _stale = _clear_stale_parts_dirs(dst, dst_dir)
                if _stale:
                    L(f"parts: removed {_stale} stale dir(s) from a different "
                      f"MAX_FACES")
                L("step B: split multi-shell")
                parts = split_shells(src, dst_dir, L=L)
                if parts:
                    L(f"split: {len(parts)} shells → {', '.join(os.path.basename(p) for p in parts)}")
                    # Repair each part inline (same worker process).
                    # Recursion is depth-1 by construction: this whole block is
                    # guarded by `not is_part`, and every recursive call passes
                    # is_part=True, so a part can never split again.  Removing
                    # that guard would recurse without bound (todo L4).
                    # Each part gets TIMEOUT_PART; the file as a whole gets
                    # min(TIMEOUT, TIMEOUT_PART * n_parts).  Two independent
                    # caps, not a shared pool — a part that finishes in 1s
                    # donates nothing to the next one.  The clock is checked
                    # between parts only: a part already blocked inside a
                    # library call cannot be interrupted from here, and does
                    # not need to be, because a part that overruns dooms the
                    # file anyway and the worker's own kill is the backstop.
                    _cap = _part_cap(len(parts))
                    _deadline = _time.monotonic() + _cap
                    # n is known now, so lift this process's own cap from the
                    # whole-mesh TIMEOUT_PART it was armed with to what a split
                    # of this size is actually allowed.
                    _arm_mesh_alarm(_cap, 'split')
                    L(f"budget: {_cap:.0f}s for {len(parts)} part(s) "
                      f"({TIMEOUT_PART}s each, ceiling {TIMEOUT}s)")
                    part_results = []
                    for part_path in parts:
                        if _time.monotonic() >= _deadline:
                            L(f"  budget exhausted after {len(part_results)}"
                              f"/{len(parts)} part(s) — stopping")
                            break
                        L(f"  part: {os.path.basename(part_path)}")
                        part_result = _repair_part(part_path, L=L)
                        part_results.append(part_result)
                        L(f"  part {os.path.basename(part_path)}: {part_result['status']}")
                    if len(part_results) < len(parts):
                        part_results.append({'status': 'interrupted'})
                    # 'skip' means the part was already repaired in a prior run — treat as ok.
                    all_ok = all(r['status'] in ('ok', 'skip') for r in part_results)
                    if all_ok:
                        try:
                            ms_merge = _pymeshlab.MeshSet()
                            for part_path in parts:
                                if os.path.exists(part_path):
                                    ms_merge.load_new_mesh(part_path)
                            # generate_by_merging_visible_meshes was added in PyMeshLab 2022.2;
                            # fall back to flatten_visible_layers on older builds.
                            if hasattr(ms_merge, 'generate_by_merging_visible_meshes'):
                                ms_merge.generate_by_merging_visible_meshes()
                            else:
                                ms_merge.flatten_visible_layers(mergevisible=True)
                            _ensure_parent(dst)
                            ms_merge.save_current_mesh(dst, binary=True)
                            size = os.path.getsize(dst)
                            _clear_stale(failed_copy, unrepaired_copy, open_copy)
                            # Merge succeeded — the parts have served their purpose.
                            # Drop them so the output tree doesn't grow a duplicate
                            # copy of every split mesh (todo H2).
                            _n_removed = _cleanup_parts(dst_dir, parts)
                            L(f"merged {len(parts)} parts → {os.path.basename(dst)}  ({size:,} bytes)"
                              f"; removed {_n_removed} part file(s)")
                            return {'rel': rel, 'status': 'ok', 'dst': os.path.basename(dst),
                                    'size': size, 'is_ascii': False, 'split': len(parts)}
                        except Exception as _merge_err:
                            L(f"merge failed: {_merge_err}")
                            all_ok = False
                    if not all_ok:
                        n_ok = sum(1 for r in part_results if r['status'] in ('ok', 'skip'))
                        _ensure_parent(failed_copy)
                        shutil.copy2(src, failed_copy)
                        L(f"split partial: {n_ok}/{len(parts)} parts ok — saved as {os.path.basename(failed_copy)}")
                        return {'rel': rel, 'status': 'failed', 'is_mesh_bad': False,
                                'stdout': f"{n_ok}/{len(parts)} parts succeeded", 'stderr': ''}

        # Step C — decimate if over face limit.
        #
        # Three decimators, tried in order, all running the same Garland-Heckbert
        # quadric edge collapse.  They differ only in how much machinery sits
        # around it, which is where the cost is.  Measured, 2.55M tris -> 900k:
        #     fast_simplification  ~5.6s   ~915 MB   (numpy arrays)
        #     pymeshlab            42.0s   1557 MB   (full mesh database)
        #     blender              43s     OOM'd under a worker RLIMIT_AS
        # Size no longer selects the decimator: fast_simplification handles a
        # 2.55M-triangle mesh in less memory than PyMeshLab needs for 1.2M, so
        # there is no longer a band where the mesh is too big for Python and has
        # to go out to Blender.  Blender remains only as a last-resort fallback.
        if MAX_FACES > 0 and working_tris > MAX_FACES:
            _dec_tmp = dst + '.decimate.stl'
            temps.append(_dec_tmp)
            _ensure_parent(_dec_tmp)
            _dec_done = False

            if _FASTSIMP_AVAILABLE:
                L(f"step C: decimate {working_tris:,} tris → target {MAX_FACES:,} (fast_simplification)")
                try:
                    _r = run_fast_decimate(working, _dec_tmp, MAX_FACES)
                    if _r is not None:
                        _dec_nm, _dec_open, _dec_faces = _r
                        L(f"decimate: {working_tris:,} → {_dec_faces:,} faces  "
                          f"nm={_dec_nm}  open={_dec_open}")
                        working, working_tris = _dec_tmp, _dec_faces
                        nm_src, open_src = _dec_nm, _dec_open
                        _dec_done = True
                        stats['path'].append('fastsimp')
                except Exception as _dec_err:
                    L(f"decimate (fast_simplification): FAILED — {_dec_err}")

            if not _dec_done and _PYMESHLAB_AVAILABLE and working_tris <= _LARGE_MESH_TRI_LIMIT:
                L(f"step C: decimate {working_tris:,} tris → target {MAX_FACES:,} (pymeshlab)")
                try:
                    _dec_nm, _dec_open, _dec_faces = run_pymeshlab_decimate(
                        working, _dec_tmp, MAX_FACES)
                    L(f"decimate: {working_tris:,} → {_dec_faces:,} faces  "
                      f"nm={_dec_nm}  open={_dec_open}")
                    working, working_tris = _dec_tmp, _dec_faces
                    nm_src, open_src = _dec_nm, _dec_open
                    _dec_done = True
                    stats['path'].append('pymeshlab-dec')
                except Exception as _dec_err:
                    L(f"decimate (pymeshlab): FAILED — {_dec_err}")

            if not _dec_done:
                L(f"step C: decimate {working_tris:,} tris → target {MAX_FACES:,} (blender fallback)")
                _bd_ok, _bd_faces, _bd_stdout, _bd_stderr = blender_decimate(
                    working, _dec_tmp, MAX_FACES,
                    elapsed=_time.monotonic() - _t0,
                    L=L, route='decimate')
                if _bd_ok and os.path.exists(_dec_tmp):
                    working = _dec_tmp
                    # Blender's reported face count is advisory only — the scan
                    # reads the authoritative triangle count from the STL header
                    # itself, so a disagreement can't corrupt the edge counts.
                    working_tris = _bd_faces if _bd_faces > 0 else MAX_FACES
                    nm_src, open_src, _scan_err2 = scan_mesh_errors(working)
                    _scan_note2 = f"  scan error: {_scan_err2}" if _scan_err2 else ""
                    L(f"decimate (blender): → {working_tris:,} faces  "
                      f"nm={nm_src}  open={open_src}{_scan_note2}")
                    _dec_done = True
                    stats['path'].append('blender-dec')
                else:
                    if os.path.exists(_dec_tmp):
                        os.unlink(_dec_tmp)
                    _dec_tmp = None
                    for _bl in (_bd_stdout or '').splitlines():
                        if _bl.strip():
                            L(f"  stdout: {_bl.strip()}")
                    for _bl in (_bd_stderr or '').splitlines():
                        if _bl.strip():
                            L(f"  stderr: {_bl.strip()}")
                    # Every decimator failed.  Sending the full-resolution mesh
                    # to Blender repair would fail the same way, so stop here.
                    L("decimate: FAILED — all decimators exhausted, skipping Blender repair")
                    size = _save_indicator(failed_copy, src, [])
                    return {'rel': rel, 'status': 'failed', 'is_mesh_bad': False,
                            'stdout': 'all decimators failed on oversized mesh', 'stderr': ''}

            # Flag heavy reductions for review, whichever decimator ran.
            if _dec_done and working_tris > 0:
                _ratio = float(n_tris) / float(working_tris)
                if _ratio >= REVIEW_RATIO:
                    L(f"review: decimated {_ratio:.1f}x — listed in "
                      f"{os.path.basename(REVIEW_FILE)}")
                    log_review(rel, n_tris, working_tris)

            # Step B2 — the split step B was too large to run, retried now that
            # decimation has brought the mesh under the limit.
            #
            # PyMeshFix rebuilds a single manifold surface and discards every
            # other component, so a multi-shell mesh that reaches it unsplit
            # loses all but its largest shell.  That is how a 39-shell model
            # 3% over the threshold came back as one shell with its head
            # deleted: 2,061,994 -> 394,432 faces, reported ok.
            #
            # Decimation preserves components (measured: 444 shells in, 445
            # out, smallest still 3 verts), so the deferred split sees the same
            # structure the source had — and splitting a 900k mesh costs less
            # than splitting the 2M original would have.  split_shells() drops
            # fragments under max(100, largest/1000) faces, so debris-only
            # meshes still return no parts and take the normal path.
            # Runs for every deferred split, whether it was deferred because the
            # mesh was too large to scan or so that one face budget could be
            # shared across all its shells.
            if _split_deferred and _dec_done and working_tris <= _LARGE_MESH_TRI_LIMIT:
                if os.path.exists(dst) or os.path.exists(dst_base + '.failed.stl'):
                    pass
                else:
                    dst_dir = _parts_dir_for(dst)
                    _stale = _clear_stale_parts_dirs(dst, dst_dir)
                    if _stale:
                        L(f"parts: removed {_stale} stale dir(s) from a "
                          f"different MAX_FACES")
                    L(f"step B2: split multi-shell (post-decimation, "
                      f"{working_tris:,} tris)")
                    parts = split_shells(working, dst_dir, L=L)
                    if parts:
                        L(f"split: {len(parts)} shells → "
                          f"{', '.join(os.path.basename(p) for p in parts)}")
                        _cap = _part_cap(len(parts))
                        _deadline = _time.monotonic() + _cap
                        _arm_mesh_alarm(_cap, 'split')
                        L(f"budget: {_cap:.0f}s for {len(parts)} part(s) "
                          f"({TIMEOUT_PART}s each, ceiling {TIMEOUT}s)")
                        part_results = []
                        for part_path in parts:
                            if _time.monotonic() >= _deadline:
                                L(f"  budget exhausted after "
                                  f"{len(part_results)}/{len(parts)} part(s) "
                                  f"— stopping")
                                break
                            L(f"  part: {os.path.basename(part_path)}")
                            part_result = _repair_part(part_path, L=L)
                            part_results.append(part_result)
                            L(f"  part {os.path.basename(part_path)}: "
                              f"{part_result['status']}")
                        if len(part_results) < len(parts):
                            part_results.append({'status': 'interrupted'})
                        if all(r['status'] in ('ok', 'skip') for r in part_results):
                            _merged = _merge_parts(parts, dst, dst_dir, L, stats,
                                                   failed_copy, unrepaired_copy,
                                                   open_copy, rel)
                            if _merged is not None:
                                return _merged
                        else:
                            _n_ok = sum(1 for r in part_results
                                        if r['status'] in ('ok', 'skip'))
                            L(f"split: {_n_ok}/{len(parts)} parts repaired — "
                              f"falling through to whole-mesh repair")
                    else:
                        L("split: single shell (or only fragments) — "
                          "continuing with the whole mesh")

        # Step D (PyMeshLab NM repair) was removed here.
        #
        # It resolved non-manifold edges by deleting the offending faces, which
        # turns a topology defect into boundary loops: on a 900k-face mesh with
        # nm=3 it deleted 6 faces and produced 12 open edges.  PyMeshFix then
        # reconstructed around those holes and deleted 151,144 faces — 17% of
        # the mesh — cutting 4.9mm off the bottom of the model.  Measured:
        #
        #   decimated             900,000 tris  nm=3  open=0   z=[0.00,108.23]
        #   + step D + pymeshfix  748,856 tris  nm=0  open=0   z=[4.90,108.23]
        #   pymeshfix alone       899,976 tris  nm=0  open=0   z=[0.00,108.23]
        #
        # PyMeshFix repairs non-manifold edges directly, so step D was creating
        # the damage it then had to repair.  Its output is now handled by step E
        # alone, which is both cleaner and faster (29s vs 62s on that file).
        # See tag v1.0-pre-stepD-removal for the previous behaviour.

        # Step E — PyMeshFix.  Repairs non-manifold edges and open edges alike,
        # so it runs whenever either is present.
        # Winding seams count as needing repair.  A mesh can be nm=0 open=0
        # and still have a reversed region — that is exactly the case PyMeshFix
        # re-winds correctly, and without this it skipped step E entirely and
        # the seam survived into the output.
        _needs_pmf = nm_src > 0 or open_src > 0 or _seam_loops > 0
        if not _PYMESHFIX_AVAILABLE:
            L("skip E: pymeshfix unavailable")
        elif nm_src == -1 or open_src == -1:
            L("skip E: mesh too large to scan")
        elif not _needs_pmf:
            L("skip E: nm=0 open=0 (nothing to repair)")
        # Step E0 — separate regions whose winding cannot be reconciled.
        #
        # PyMeshFix rebuilds one coherent surface.  Handed a mesh containing two
        # regions that disagree about which way is out — hair over a scalp,
        # cloth over a body — it keeps one and deletes the other, reporting
        # success.  On one model that cost 562,288 faces -> 394,432 and the
        # model's head, with the bounding box unchanged so nothing flagged it.
        #
        # The two regions meet along closed loops of "seam" edges, where both
        # faces traverse the shared edge the same way.  Cutting there gives
        # pieces that are each internally consistent, and PyMeshFix then
        # preserves them: 100.0% and 100.2% of their volume, where the joined
        # mesh lost 15%.  The pieces are repaired separately and written back
        # as separate shells of one file — measured volume afterwards was
        # identical to the source, to the digit.
        #
        # Only CLOSED loops trigger this.  A few seam edges with loose ends are
        # local noise: the same model before repair had 5 such edges in 0 loops
        # and needed no split.
        # A seam piece is seam-free by construction — split_at_seams() cuts
        # exactly those edges — so it can never re-enter this branch.  Guarding
        # on the name as well makes that explicit and bounds the recursion at
        # one level even if a piece somehow came back with a loop of its own.
        _is_seam_piece = _SEAM_MARKER in os.path.basename(src).lower()
        # Seam loops alone do NOT justify splitting.  PyMeshFix re-winds a
        # reversed region correctly when it can — on a sphere with its cap
        # reversed (40 seam edges, 1 loop) it returns the same 760 faces with
        # the winding corrected, where splitting first gives 880 faces and
        # introduces 2 non-manifold edges.  The split is only worth it when
        # PyMeshFix would instead DELETE the region, and nothing measurable
        # here distinguishes those two cases in advance: both meshes had
        # exactly 40 seam edges.  So the decision is deferred until after
        # step E, where the damage is a measured fact rather than a guess —
        # see the volume check below.
        if False and _seam_loops > 0 and not _is_seam_piece:
            try:
                _sv, _sf = _weld_binary_stl(working)
                _seam = _seam_edges
                if _seam:
                    L(f"step E0: splitting at {len(_seam)} seam edges "
                      f"({_seam_loops} closed loop(s))")
                    _pieces = split_at_seams(_sv, _sf, _seam)
                    del _sv, _sf
                    if len(_pieces) > 1:
                        _seam_dir = _parts_dir_for(dst)
                        _ensure_parent(os.path.join(_seam_dir, 'x'))
                        _base = os.path.splitext(os.path.basename(dst))[0]
                        _seam_parts = []
                        for _i, (_pv, _pf) in enumerate(_pieces):
                            _p = os.path.join(_seam_dir,
                                              f"{_base}{_SEAM_MARKER}{_i}.stl")
                            # Same pending/commit protocol as shell parts.
                            _write_binary_stl(_pending_part_path(_p), _pv, _pf)
                            temps.append(_pending_part_path(_p))
                            temps.append(_p)
                            _seam_parts.append(_p)
                        L(f"split: {len(_seam_parts)} region(s) — "
                          + ", ".join(f"{len(pf):,} faces" for _, pf in _pieces))
                        _seam_cap = _part_cap(len(_seam_parts))
                        _seam_deadline = _time.monotonic() + _seam_cap
                        _arm_mesh_alarm(_seam_cap, 'seam-split')
                        L(f"budget: {_seam_cap:.0f}s for "
                          f"{len(_seam_parts)} region(s)")
                        _n_ok = 0
                        _n_run = 0
                        for _p in _seam_parts:
                            if _time.monotonic() >= _seam_deadline:
                                L(f"  budget exhausted after {_n_run}"
                                  f"/{len(_seam_parts)} region(s) — stopping")
                                break
                            _n_run += 1
                            _r = _repair_part(_p, L=L, label='region')
                            L(f"  region {os.path.basename(_p)}: {_r['status']}")
                            if _r['status'] in ('ok', 'skip'):
                                _n_ok += 1
                        if _n_ok == len(_seam_parts):
                            _merged = _merge_parts(_seam_parts, dst, _seam_dir, L,
                                                   stats, failed_copy,
                                                   unrepaired_copy, open_copy, rel)
                            if _merged is not None:
                                stats['path'].append(f'seamsplit{len(_seam_parts)}')
                                return _merged
                        L("seam split: not all regions repaired — "
                          "falling through to whole-mesh repair")
            except Exception as _seam_err:
                L(f"step E0: seam check failed — {_seam_err}")

        if _PYMESHFIX_AVAILABLE and _needs_pmf and nm_src != -1 and open_src != -1:
            _pmf_tmp = dst + '.pymeshfix.stl'
            temps.append(_pmf_tmp)
            _ensure_parent(_pmf_tmp)
            L(f"step E: pymeshfix repair (nm={nm_src} open={open_src})")
            _bounds_before = stl_bounds(working)
            try:
                _pmf_nm, _pmf_open = run_pymeshfix(working, _pmf_tmp)
                L(f"pymeshfix: nm={_pmf_nm}  open={_pmf_open}")

                # Did it repair the mesh, or delete part of it?
                #
                # PyMeshFix rebuilds one coherent surface.  Given a mesh whose
                # regions disagree about which way is out it usually re-winds
                # them, which is correct and cheap — but sometimes it discards
                # one instead, and nothing measurable beforehand says which:
                # the model that lost its head and a sphere with its cap
                # reversed both had exactly 40 seam edges, and PyMeshFix
                # re-wound the sphere while deleting 30% of the model.
                #
                # So the question is asked afterwards, when the answer is a
                # measured fact.  Enclosed volume is the signal: it caught
                # every known case, including two the bounding box could not
                # because the lost geometry was inside the silhouette.
                _vol_before = _mesh_volume(working)
                _vol_after = _mesh_volume(_pmf_tmp)
                if (_vol_before and _vol_after
                        and _vol_before >= _VOLUME_MIN_MEANINGFUL
                        and _vol_after < _vol_before * _VOLUME_LOSS_LIMIT
                        and not _is_seam_piece):
                    L(f"pymeshfix: volume {_vol_before:,.0f} -> {_vol_after:,.0f} "
                      f"({100 * _vol_after / _vol_before:.0f}%) — geometry was "
                      f"deleted, retrying split at the winding seams")
                    _recovered = _repair_by_seam_split(
                        working, dst, dst_base, temps, stats, L, rel,
                        failed_copy, unrepaired_copy, open_copy,
                        elapsed=_time.monotonic() - _t0)
                    if _recovered is not None:
                        return _recovered
                    L("seam split did not recover it — keeping the "
                      "pymeshfix result")

                # Observe only — the extents are recorded, never acted on.  A
                # repair should not move a model's bounding box: a shrink means
                # geometry was deleted, growth means it was invented.  Logged so
                # a full-collection run can show how often this happens and to
                # what, before any policy is decided.
                _drift = compare_bounds(_bounds_before, stl_bounds(_pmf_tmp))
                if _drift:
                    L(f"pymeshfix: bbox changed (inspect) — {_drift}")
                    stats['bbox_drift'] = _drift
                    # Keep the source next to the output.  The repair may be
                    # correct (a stray artifact removed) or destructive (end
                    # caps pulled shut) — the numbers cannot tell those apart,
                    # so both files are kept and the choice is made by eye.
                    _orig = save_original_copy(src, dst_base)
                    if _orig:
                        L(f"saved {os.path.basename(_orig)} — original geometry, "
                          f"in case the repair is wrong")
                # open_in -> open_out here is the boundary-loop fill that could
                # have sealed an intentional connector hole.
                stats['path'].append(f'pymeshfix(nm{nm_src}->{_pmf_nm},'
                                     f'open{open_src}->{_pmf_open})')
                if working != src and os.path.exists(working):
                    os.unlink(working)
                working = _pmf_tmp
                nm_src = _pmf_nm
                open_src = _pmf_open
            except Exception as _pmf_err:
                if os.path.exists(_pmf_tmp):
                    os.unlink(_pmf_tmp)
                _pmf_tmp = None
                L(f"pymeshfix: FAILED — {_pmf_err}")

        # If clean after all python passes, post-verify with an independent edge scan
        # before writing the final output — pymeshfix self-report is not always reliable.
        _pv_ok = True
        if nm_src == 0 and open_src == 0:
            nm_src, open_src, _pv_ok = _post_verify(working, L)
        # Open edges that are too small to print do not justify another repair
        # pass.  nm edges still do — those are topology, not a hole, and a
        # slicer can genuinely mis-fill them.  See _open_loops_are_printable().
        if nm_src == 0 and open_src > 0 and MIN_LAYER > 0:
            try:
                _n, _o, _e, _ec = scan_mesh_errors(working, return_edges=True)
                if _ec is not None:
                    _tiny, _nloops, _big = _open_loops_are_printable(_ec)
                    if _tiny:
                        L(f"open edges below print scale — {open_src} edge(s) in "
                          f"{_nloops} loop(s), largest {_big:.4f}mm < "
                          f"{MIN_LAYER}mm layer; accepting without blender")
                        stats['path'].append(f'subprint-open{open_src}')
                        open_src = 0
            except Exception as _tiny_err:
                L(f"print-scale check failed — {_tiny_err}")
        if nm_src == 0 and open_src == 0:
            _ensure_parent(dst)
            os.replace(working, dst)
            size = os.path.getsize(dst)
            _clear_stale(failed_copy, unrepaired_copy, open_copy)
            L(f"result: ok (no blender needed)"
              + ("" if _pv_ok else " — UNVERIFIED, scan could not run"))
            return {'rel': rel, 'status': 'ok', 'dst': os.path.basename(dst),
                    'size': size, 'is_ascii': False, 'bypassed': True,
                    'verified': _pv_ok}

        # Step F — Blender fallback: NM edges remain that PyMeshFix couldn't clear.
        if nm_src == -1:
            L("blender: fallback — mesh too large for Python passes, sending directly")
        else:
            L(f"blender: fallback — nm={nm_src}  open={open_src} remain after python passes")
        blender_src = working

    else:
        # OBJ or ASCII — Blender handles import/conversion, no pre-scan possible.
        blender_src = src
        _dec_tmp = None

    _ensure_parent(dst)

    # Mark the Blender call on both entry paths — the OBJ/ASCII branch above has
    # no "fallback" line, so without this those files reach Blender unlogged and
    # cannot even be counted afterwards.  The dt= on the following line is then
    # Blender's own wall time, isolated from the post-verify and file writes.
    _bl_t0 = _time.monotonic()
    # start/done/skipped/KILLED are logged inside _run_blender_script, which
    # every route funnels through, so they are not repeated here -- doing both
    # double-counted each invocation in the log.
    # Captured before the call: blender_src is unlinked a few lines below.
    _bl_bounds_before = stl_bounds(blender_src)
    success, open_only, unrepaired, stdout, stderr = fix_stl(
        blender_src, dst, MERGE_DIST,
        is_ascii=is_ascii, is_obj=is_obj,
        elapsed=_time.monotonic() - _t0,
        L=L, route=('obj' if is_obj else 'ascii' if is_ascii else 'fallback'),
    )
    _bl_secs = _time.monotonic() - _bl_t0
    stats['blender_secs'] = f"{_bl_secs:.1f}"
    stats['path'].append('blender')
    # Observe only, as in step E.  Blender merges doubles and can decimate, so
    # small movement here is expected — the number is what makes it judgeable.
    if os.path.exists(dst):
        _bl_drift = compare_bounds(_bl_bounds_before, stl_bounds(dst))
        if _bl_drift:
            L(f"blender: bbox changed (inspect) — {_bl_drift}")
            stats['bbox_drift'] = _bl_drift
            _orig = save_original_copy(src, dst_base)
            if _orig:
                L(f"saved {os.path.basename(_orig)} — original geometry, "
                  f"in case the repair is wrong")

    # Clean up pre-pass temps now that Blender is done.
    if blender_src != src and os.path.exists(blender_src):
        os.unlink(blender_src)

    # Log meaningful lines from Blender stdout.
    for line in stdout.splitlines():
        if any(kw in line.lower() for kw in (
                'repair pass', 'merge doubles', 'decimate', 'final nm',
                'already clean', 'blender_ok', 'blender_open', 'blender_unrepaired',
                'blender_empty', 't-junction', 'post-verify')):
            L(f"blender: {line.strip()}")

    def _result(**kwargs):
        return {'rel': rel, **kwargs}

    _pv_ok = True
    if success and os.path.exists(dst):
        _pv_nm, _pv_open, _pv_ok = _post_verify(dst, L, label="blender post-verify")
        if _pv_nm > 0 or _pv_open > 0:
            success = False
            open_only = True

    if success and os.path.exists(dst):
        size = os.path.getsize(dst)
        _clear_stale(failed_copy, unrepaired_copy, open_copy)
        L(f"result: ok (blender)"
          + ("" if _pv_ok else " — UNVERIFIED, scan could not run"))
        return _result(status='ok', dst=os.path.basename(dst), size=size,
                       is_ascii=is_ascii, is_obj=is_obj, verified=_pv_ok)
    elif open_only and os.path.exists(dst):
        if _PYMESHFIX_AVAILABLE:
            r = _try_pymeshfix_after_blender(
                dst, dst, open_copy, failed_copy, unrepaired_copy,
                is_ascii, is_obj, stdout, stderr, "post-blender", L, _result,
                temps=temps)
            if r is not None:
                return r
        _ensure_parent(open_copy)
        os.replace(dst, open_copy)
        size = os.path.getsize(open_copy)
        _clear_stale(failed_copy, unrepaired_copy)
        _open_reason = "open edges remain after pymeshfix" if _PYMESHFIX_AVAILABLE else "pymeshfix unavailable"
        L(f"result: open ({_open_reason})")
        return _result(status='open', size=size, dst=os.path.basename(open_copy),
                       stdout=stdout, stderr=stderr)
    elif unrepaired:
        if _PYMESHFIX_AVAILABLE:
            r = _try_pymeshfix_after_blender(
                src, dst, open_copy, failed_copy, unrepaired_copy,
                is_ascii, is_obj, stdout, stderr, "post-blender (unrepaired)", L, _result,
                temps=temps)
            if r is not None:
                return r
        size = _save_indicator(unrepaired_copy, src, [failed_copy])
        L(f"result: unrepaired (nm remains)")
        for _bl in stdout.splitlines():
            if _bl.strip():
                L(f"  stdout: {_bl.strip()}")
        return _result(status='unrepaired', size=size, stdout=stdout, stderr=stderr)
    else:
        is_mesh_bad = 'BLENDER_EMPTY' in stdout
        dest_copy = broken_copy if is_mesh_bad else failed_copy
        size = _save_indicator(dest_copy, src, [])
        L(f"result: {'broken' if is_mesh_bad else 'failed'}")
        for _bl in stdout.splitlines():
            if _bl.strip():
                L(f"  stdout: {_bl.strip()}")
        for _bl in stderr.splitlines():
            if _bl.strip():
                L(f"  stderr: {_bl.strip()}")
        return _result(status='failed', is_mesh_bad=is_mesh_bad, stdout=stdout, stderr=stderr)


_worker_status = None   # set by _worker_init to the shared Manager dict
_blender_proc  = None   # current Blender subprocess in this worker (or None)


def _unlimit_child_address_space():
    """preexec_fn for Blender: undo the worker's inherited RLIMIT_AS.

    RLIMIT_AS is inherited across fork/exec and caps *virtual* address space,
    not resident memory.  Blender reserves far more VA than it ever resides
    (thread stacks, mmap'd arenas, driver mappings), so a worker cap sized for
    Python's own allocations aborts Blender at a fraction of that figure —
    observed as `Malloc returns null: ... total 1.2 GB` under a 3 GiB cap while
    14 GB of real memory was free.  The cgroup MemoryMax from run.sh is what
    bounds Blender; this limit is only meant to bound the worker itself.

    Runs in the forked child between fork and exec, so it must stay async-signal
    safe: no logging, no allocation beyond the resource call itself."""
    try:
        import resource as _resource
        _soft, _hard = _resource.getrlimit(_resource.RLIMIT_AS)
        if _soft != _resource.RLIM_INFINITY:
            _resource.setrlimit(_resource.RLIMIT_AS,
                                (_resource.RLIM_INFINITY, _hard))
    except Exception:
        pass  # a child that keeps the cap is still better than no child


def _kill_own_children(sig):
    """Signal every direct child of this process, whatever it is.

    The tracked _blender_proc global has an unavoidable race: SIGTERM can land
    between Popen() returning and the assignment completing, leaving a Blender
    child the handler cannot see (todo M1).  Reading the kernel's own child list
    from /proc closes that hole — it is authoritative regardless of how far the
    Python-side bookkeeping got.  Returns the number of processes signalled."""
    n = 0
    try:
        pid = os.getpid()
        # children_<pid> lists the direct children of each thread of this process.
        task_dir = f'/proc/{pid}/task'
        seen = set()
        for tid in os.listdir(task_dir):
            try:
                with open(f'{task_dir}/{tid}/children') as f:
                    kids = f.read().split()
            except OSError:
                continue
            for kid in kids:
                if kid in seen:
                    continue
                seen.add(kid)
                try:
                    os.kill(int(kid), sig)
                    n += 1
                except (OSError, ValueError):
                    pass
    except OSError:
        pass
    return n


def _worker_init(shared_status, nice_level, mem_limit_bytes=0):
    """Called once in each worker process at pool startup.
    Limits OpenBLAS/MKL thread counts to 1 (pymeshlab/numpy otherwise spawn
    N-core thread pools per worker, saturating the CPU and freezing the desktop).
    Also raises the process nice level so the UI stays responsive.
    Installs a SIGTERM handler that kills any in-flight Blender subprocess so
    workers don't leave orphaned Blender processes when the pool is torn down.
    mem_limit_bytes, when non-zero, caps this worker's address space so one
    oversized mesh fails that single file instead of pushing the whole cgroup
    into an OOM kill that takes down the run (todo L3)."""
    import signal as _signal
    global _worker_status
    _worker_status = shared_status
    import os as _os
    # Limit BLAS/MKL threads — must be set before numpy/pymeshlab do any work.
    for _var in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                 'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        _os.environ[_var] = '1'
    # Lower CPU priority so the desktop stays responsive.
    try:
        _os.nice(nice_level)
    except OSError:
        pass
    # Per-worker address-space cap: an over-large allocation raises MemoryError
    # in this worker only, which process_file_safe turns into a .failed.stl.
    if mem_limit_bytes > 0:
        try:
            import resource as _resource
            _soft, _hard = _resource.getrlimit(_resource.RLIMIT_AS)
            if _hard == _resource.RLIM_INFINITY or _hard > mem_limit_bytes:
                _resource.setrlimit(_resource.RLIMIT_AS, (mem_limit_bytes, _hard))
        except (ImportError, ValueError, OSError):
            pass
    # On SIGTERM, kill the in-flight Blender subprocess then exit cleanly.
    def _sigterm(_sig, _frame):
        global _blender_proc
        if _blender_proc is not None:
            try:
                _blender_proc.kill()
            except Exception:
                pass
        # Sweep for any child the global missed (see _kill_own_children).
        _kill_own_children(_signal.SIGKILL)
        raise SystemExit(0)
    _signal.signal(_signal.SIGTERM, _sigterm)


_libc = None

def release_worker_memory():
    """Return freed heap back to the OS after finishing a file.

    Python's garbage collector reclaims objects, but glibc keeps the underlying
    arenas mapped for reuse, so an idle worker goes on counting against the
    cgroup's memory limit.  Measured after a real 2.5M-triangle decimation: RSS
    stayed at 902 MB through gc.collect() and only dropped to 787 MB once
    malloc_trim() ran.  With several workers idling between files that is
    gigabytes of dead weight competing with the workers still doing something.

    malloc_trim() is glibc-specific; on any other libc this is a no-op."""
    import gc as _gc
    _gc.collect()
    global _libc
    if _libc is False:      # probed once, not available here
        return
    try:
        if _libc is None:
            import ctypes as _ctypes
            _libc = _ctypes.CDLL("libc.so.6")
        _libc.malloc_trim(0)
    except Exception:
        _libc = False       # unavailable — stop retrying


def process_file_subprocess(src, is_part=False, budget=None):
    """Run one file in its own process and report what happened to it.

    This is what the pool calls.  The mesh work happens in a child, so a
    timeout or the OOM killer takes the child and leaves this worker alive:
    ProcessPoolExecutor fails EVERY pending future when one of its own workers
    dies, so killing a worker previously destroyed every file running beside
    it — one run lost 635s of work on an innocent 7M-triangle mesh that way.

    Because this process spawned the child, it also knows exactly what killed
    it.  A negative returncode is the signal number: -9 is SIGKILL, and this
    worker knows whether it did the killing (its own timeout) or something
    else did (the OOM killer).  That is the attribution the parent could never
    make from a summary row stuck at 'started' — which is how a set of
    timeouts once got reported as 'likely out of memory'.

    The timeout is enforced here rather than by the parent's watchdog: this
    process is already doing nothing but waiting, whereas the parent has to be
    scheduled to notice, and under memory pressure that ran ~500s late.
    """
    import json as _json
    import subprocess as _sp
    import time as _t

    # How long this one mesh may take.  A caller repairing a shell part passes
    # that part's remaining budget; an unsplit model passes nothing and gets
    # TIMEOUT_PART — the same limit a single part gets, because an unsplit model
    # IS a single mesh.  TIMEOUT is a ceiling for split files only and is
    # applied by the split loop, not here.  Floored, because a nearly exhausted
    # file cap would otherwise hand the child a budget it cannot meet and then
    # report it as a hang.
    # A whole file is spawned with TIMEOUT, the ceiling, because at spawn time
    # nobody knows yet whether it will split or into how many parts.  The child
    # applies the tighter limit itself once it knows: _part_cap(n) for a split,
    # TIMEOUT_PART for a mesh that stays whole.  Spawning with TIMEOUT_PART
    # instead would kill a five-part file before its second part started.
    _budget = int(budget) if budget else TIMEOUT
    if _budget < 30:
        _budget = 30

    rel = os.path.relpath(src, INPUT_FOLDER) if not is_part else os.path.basename(src)
    _pid = os.getpid()
    if _worker_status is not None:
        try:
            try:
                _bytes = os.path.getsize(src)
            except OSError:
                _bytes = 0
            _tris, _hdr_err = _read_stl_header(src)
            _worker_status[_pid] = {'rel': rel, 'started': _t.monotonic(),
                                    'bytes': _bytes,
                                    'tris': 0 if _hdr_err else _tris}
        except Exception:
            pass

    _rd, _wr = os.pipe()
    cmd = [sys.executable, os.path.abspath(__file__),
           '--one-file', src,
           '--input', INPUT_FOLDER,
           '--suffix', OUTPUT_SUFFIX,
           '--merge-dist', str(MERGE_DIST),
           '--min-layer', str(MIN_LAYER),
           '--blender-reserve-pct', str(BLENDER_RESERVE_PCT),
           '--max-faces', str(MAX_FACES),
           '--timeout', str(_budget),
           '--timeout-part', str(TIMEOUT_PART),
           '--result-fd', str(_wr)]
    if is_part:
        cmd.append('--is-part')

    result = None
    started = _t.monotonic()
    try:
        proc = _sp.Popen(cmd, pass_fds=(_wr,),
                         stdout=_sp.DEVNULL, stderr=_sp.PIPE, text=True,
                         # The child must not inherit a cap that would kill it
                         # for the parent's reasons (see the RLIMIT_AS history).
                         preexec_fn=_unlimit_child_address_space)
        os.close(_wr)
        _wr = None
        _payload = b''
        _stderr = ''
        try:
            # Wait FIRST, then read.  Reading the pipe up front blocks until the
            # child closes it, which a hung child never does — the timeout then
            # could not fire, and the kill arrived down the wrong path with the
            # wrong status.  Once the process has exited the pipe is closed, so
            # the read cannot block.
            _, _stderr = proc.communicate(timeout=_budget)
            _rc = proc.returncode
            with os.fdopen(_rd, 'rb') as _f:
                _rd = None
                _payload = _f.read()
        except _sp.TimeoutExpired:
            # Kill the whole tree: the child may itself have a Blender running.
            for _k in _child_pids_of(proc.pid):
                try:
                    os.kill(_k, signal.SIGKILL)
                except OSError:
                    pass
            proc.kill()
            try:
                _, _stderr = proc.communicate(timeout=30)
            except Exception:
                _stderr = ''
            _held = _t.monotonic() - started
            log_step(rel, f"TIMEOUT after {_held:.0f}s (limit {_budget}s) — "
                          f"killed the repair process")
            # Only whole files get a marker.  A part lives in ~parts/ and is
            # deleted on merge, so a .timeout.stl beside it would be a copy of
            # a transient fragment — not the printable fallback of the source
            # that every other indicator is.  The part reports 'interrupted'
            # upward instead and the file-level handler marks the real source.
            if not is_part:
                _marker = mark_timeout(src, INPUT_FOLDER, OUTPUT_SUFFIX)
                if _marker:
                    log_step(rel, f"wrote {os.path.basename(_marker)} — "
                                  f"delete it to retry this file")
            result = {'rel': rel, 'status': 'interrupted',
                      'reason': (f'exceeded the {_budget}s '
                                 + ('part' if is_part else 'per-file')
                                 + ' timeout'),
                      'timed_out': True, 'stdout': '', 'stderr': ''}
            _rc = None
        if result is None:
            if _payload:
                try:
                    result = _json.loads(_payload.decode('utf-8', 'replace'))
                except ValueError:
                    result = None
            if result is None:
                # No result came back: the child died before it could write one.
                # The signal says what happened, which is the whole point of
                # running it out here.
                if _rc is not None and _rc < 0:
                    _sig = -_rc
                    _why = {9: 'SIGKILL — almost certainly the OOM killer',
                            11: 'SIGSEGV — crash inside a mesh library',
                            6: 'SIGABRT — library aborted'}.get(
                                _sig, f'signal {_sig}')
                    _msg = f'repair process died: {_why}'
                else:
                    _msg = (f'repair process exited {_rc} without a result'
                            + (f': {(_stderr or "").strip()[:300]}' if _stderr else ''))
                log_step(rel, _msg)
                try:
                    _dst_base = os.path.splitext(
                        output_path(src, OUTPUT_SUFFIX, INPUT_FOLDER))[0]
                    _failed = _dst_base + '.failed.stl'
                    _ensure_parent(_failed)
                    if not os.path.exists(_failed):
                        shutil.copy2(src, _failed)
                except Exception:
                    pass
                result = {'rel': rel, 'status': 'failed', 'is_mesh_bad': False,
                          'stdout': _msg, 'stderr': (_stderr or '')[:2000]}
    except Exception as _exc:
        log_step(rel, f"could not run repair process: {type(_exc).__name__}: {_exc}")
        result = {'rel': rel, 'status': 'failed', 'is_mesh_bad': False,
                  'stdout': str(_exc), 'stderr': ''}
    finally:
        for _fd in (_rd, _wr):
            if _fd is not None:
                try:
                    os.close(_fd)
                except OSError:
                    pass

    if _worker_status is not None:
        try:
            _worker_status.pop(_pid, None)
        except Exception:
            pass
    release_worker_memory()
    return result


def _child_pids_of(pid):
    """Direct children of `pid`, from /proc.  Used to reach a Blender that the
    repair process launched, which would otherwise be reparented to init and
    keep its memory for as long as it runs."""
    out = []
    try:
        for tid in os.listdir(f'/proc/{pid}/task'):
            try:
                with open(f'/proc/{pid}/task/{tid}/children') as f:
                    out.extend(int(k) for k in f.read().split())
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def process_file_safe(src, is_part=False):
    """Wrapper around process_file that catches any unhandled exception, writes it
    to the log, saves a .failed.stl indicator, and returns a failed result dict
    instead of propagating — so the pool never loses a worker to an unexpected crash."""
    import traceback as _tb
    import time as _t
    rel = os.path.relpath(src, INPUT_FOLDER) if not is_part else os.path.basename(src)
    _pid = os.getpid()
    if _worker_status is not None:
        try:
            # Size and triangle count come from a stat plus an 84-byte header
            # read — negligible next to the work about to be done on this file,
            # and it lets the panel show what each worker is actually chewing on.
            try:
                _bytes = os.path.getsize(src)
            except OSError:
                _bytes = 0
            _tris, _hdr_err = _read_stl_header(src)
            _worker_status[_pid] = {'rel': rel, 'started': _t.monotonic(),
                                    'bytes': _bytes,
                                    'tris': 0 if _hdr_err else _tris}
        except Exception:
            pass
    try:
        result = process_file(src, is_part=is_part)
    except subprocess.TimeoutExpired as _texc:
        # The mesh's own SIGALRM cap fired (see _arm_mesh_alarm).  This is a
        # timeout, not a crash, and must be marked as one: .failed.stl and
        # .timeout.stl mean different things to the next run, and a timeout
        # recorded as a failure would be retried from scratch every time.
        # Caught ahead of the generic handler below, which would otherwise
        # swallow it — TimeoutExpired is an Exception subclass.
        _secs = getattr(_texc, 'timeout', 0) or 0
        log_step(rel, f"TIMEOUT after {_secs:.0f}s (own cap) — "
                      f"stopped this mesh")
        if not is_part:
            _marker = mark_timeout(src, INPUT_FOLDER, OUTPUT_SUFFIX)
            if _marker:
                log_step(rel, f"wrote {os.path.basename(_marker)} — "
                              f"delete it to retry this file")
        result = {'rel': rel, 'status': 'interrupted', 'is_mesh_bad': False,
                  'stdout': f'TIMEOUT after {_secs:.0f}s', 'stderr': ''}
    except Exception as _exc:
        msg = f"UNHANDLED EXCEPTION: {type(_exc).__name__}: {_exc}"
        log_step(rel, msg)
        for _line in _tb.format_exc().splitlines():
            log_step(rel, f"  {_line}")
        # Best-effort indicator file so the run is retryable.
        try:
            _dst      = output_path(src, OUTPUT_SUFFIX, INPUT_FOLDER)
            _dst_base = os.path.splitext(_dst)[0]
            _failed   = _dst_base + '.failed.stl'
            _ensure_parent(_failed)
            if not os.path.exists(_failed):
                shutil.copy2(src, _failed)
        except Exception:
            pass
        result = {'rel': rel, 'status': 'failed', 'is_mesh_bad': False,
                  'stdout': msg, 'stderr': ''}
    if _worker_status is not None:
        try:
            _worker_status.pop(_pid, None)
        except Exception:
            pass
    release_worker_memory()
    return result


if __name__ == '__main__' and _ONE_FILE:
    # Single-file mode.  The pool worker runs each file this way so that a
    # timeout or an OOM kill lands on this process rather than on the worker:
    # ProcessPoolExecutor fails EVERY pending future when one of its own
    # workers dies, so killing a worker used to destroy every file running
    # beside it (one real run lost 635s of work on an innocent 7M-triangle
    # mesh that way).  Here the worker stays alive, sees the exit code, and
    # reports an ordinary failed result.
    #
    # Also usable by hand to debug one mesh:
    #   python stl_batch_fix.py --one-file "Zelda NSFW/Chair_foot1.stl"
    import json as _json

    # Kill Blender before dying, on either signal.
    #
    # This process is the one that actually spawns Blender, and until this
    # handler existed nothing killed it on an interrupt.  The sweep a pool
    # worker runs on SIGTERM (_kill_own_children) reads /proc for its OWN
    # direct children, which is this process — Blender is one level deeper and
    # was never reached.  Both signals arrive here by default: SIGINT because
    # this child stays in the caller's process group (no start_new_session), so
    # Ctrl+C hits it directly, and SIGTERM from the worker's own shutdown.
    #
    # SIGTERM was the worse of the two: its default disposition is SIG_DFL, so
    # the process died instantly without running even a finally block, and
    # Blender reparented to init and kept its memory until it finished on its
    # own.  Only the timeout path was safe, because process_file_subprocess
    # explicitly walks _child_pids_of() before killing this child.
    def _one_file_signal(_sig, _frame):
        global _blender_proc
        if _blender_proc is not None:
            try:
                _blender_proc.kill()
            except Exception:
                pass
        _kill_own_children(signal.SIGKILL)
        os._exit(130 if _sig == signal.SIGINT else 143)

    signal.signal(signal.SIGINT, _one_file_signal)
    signal.signal(signal.SIGTERM, _one_file_signal)

    # A mesh that stays whole is one mesh and gets TIMEOUT_PART, the same limit
    # a single part gets.  The parent had to spawn us with the TIMEOUT ceiling
    # because it could not know yet whether this file splits; this is where that
    # is narrowed back down.  A file that does split raises the cap to
    # _part_cap(n) once n is known, so only genuinely split files reach beyond
    # TIMEOUT_PART.
    _arm_mesh_alarm(TIMEOUT_PART or TIMEOUT, 'whole mesh')

    _r = process_file_safe(_ONE_FILE, is_part=_ONE_IS_PART)
    if _RESULT_FD is not None:
        # The result travels over an inherited pipe rather than stdout, which
        # carries Blender's chatter and anything a C library decides to print.
        try:
            with os.fdopen(_RESULT_FD, 'w') as _f:
                _json.dump(_r, _f)
        except Exception:
            pass
    else:
        print(_json.dumps(_r, indent=2))
    # 0 = handled (whatever the outcome), 1 = no result at all.
    sys.exit(0 if _r else 1)


if __name__ == '__main__':
    if not shutil.which(BLENDER):
        print(f"Error: '{BLENDER}' not found in PATH.")
        print("Install it with:  sudo apt install blender")
        sys.exit(1)

    if not os.path.isdir(INPUT_FOLDER):
        print(f"Error: input folder not found: {INPUT_FOLDER}")
        sys.exit(1)

    files = collect_stl_files(INPUT_FOLDER, RECURSIVE)
    if not files:
        print(f"No STL files found in: {INPUT_FOLDER}")
        sys.exit(0)

    companions = collect_companion_files(INPUT_FOLDER, RECURSIVE)
    if companions:
        copied = skipped_copy = 0
        for src, dst in companions:
            if os.path.exists(dst):
                skipped_copy += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        if copied:
            print(f"Copied {copied} companion file(s) to output folder"
                  + (f" ({skipped_copy} already present)" if skipped_copy else ""))

    # Truncate log files at the start of each run, keeping LOG_KEEP previous
    # runs as .1 … .N so a run that has to be restarted does not take its own
    # evidence with it.
    retarget_logs()
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    rotate_all_logs()
    open(LOG_FILE, 'w').close()
    _reset_review_file()
    _reset_summary_file()

    # WORKERS = 0 means "decide from RAM and cores", the same contract the TUI
    # honours.  Without this the standalone CLI passed max_workers=0 straight to
    # ProcessPoolExecutor, which raises ValueError — the default config made the
    # documented `python stl_batch_fix.py --input ...` invocation unusable.
    if WORKERS <= 0:
        workers = auto_worker_count(measure_files(files))
        print(f"Workers       : auto → {min(workers, len(files))}")
    else:
        workers = WORKERS
    workers = max(1, min(workers, len(files)))
    print(f"Found {len(files)} STL file(s) in '{INPUT_FOLDER}'")
    print(f"Output suffix : {OUTPUT_SUFFIX!r}")
    print(f"Merge dist    : {MERGE_DIST} mm")
    print(f"Max faces     : {MAX_FACES:,}" if MAX_FACES > 0 else "Max faces     : disabled")
    print(f"Workers       : {workers} (configured: {WORKERS})")
    print(f"Timeout       : {TIMEOUT} s")
    print(f"Recursive     : {RECURSIVE}")
    print(f"Blender       : {BLENDER}")
    print(f"PyMeshFix     : {'available' if _PYMESHFIX_AVAILABLE else 'not available'}")
    print(f"PyMeshLab     : {'available' if _PYMESHLAB_AVAILABLE else 'not available'}")
    print(f"FastSimplify  : {'available' if _FASTSIMP_AVAILABLE else 'not available (decimation falls back to PyMeshLab/Blender)'}")
    print()

    ok = 0
    skipped = 0
    failed = []
    unrepaired_list = []
    open_list = []
    corrupt = []

    # Submit all work to the pool. Each file — including multi-shell originals —
    # is processed entirely within a single worker (split + repair each part +
    # merge all happen in process_file). No dynamic future submission needed.
    total = len(files)
    import multiprocessing as _mp
    _mgr = _mp.Manager()
    _shared = _mgr.dict()
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers,
                                                initializer=_worker_init,
                                                initargs=(_shared, 10)) as pool:
        future_to_src = {pool.submit(process_file_safe, src): (i, src)
                         for i, src in enumerate(files, 1)}
        pending = set(future_to_src)

        while pending:
            done, pending = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                i, src = future_to_src[future]
                rel = os.path.relpath(src, INPUT_FOLDER)
                try:
                    result = future.result()
                except Exception as exc:
                    print(f"[{i}/{total}] {rel} ... ERROR  (worker exception: {exc})")
                    failed.append(rel)
                    continue

                if result['status'] == 'skip':
                    if result['reason'].startswith('already fixed'):
                        pass  # already in output folder — no log
                    else:
                        print(f"[{i}/{total}] {rel} ... SKIP  ({result['reason']})")
                    skipped += 1
                elif result['status'] == 'corrupt':
                    print(f"[{i}/{total}] {rel} ... CORRUPT — {result['reason']}")
                    corrupt.append(rel)
                elif result['status'] == 'ok':
                    if result.get('split'):
                        prefix = f"SPLIT({result['split']}) + MERGED  →  "
                    elif result.get('bypassed'):
                        prefix = "CLEAN COPY  →  "
                    elif result.get('pymeshfix') and result.get('is_obj'):
                        prefix = "OBJ→STL (pymeshfix)  →  "
                    elif result.get('pymeshfix'):
                        prefix = "OK (pymeshfix)  →  "
                    elif result.get('is_obj'):
                        prefix = "OBJ→STL  →  "
                    elif result.get('is_ascii'):
                        prefix = "(ASCII STL) OK  →  "
                    else:
                        prefix = "OK  →  "
                    print(f"[{i}/{total}] {rel} ... {prefix}{result['dst']}  ({result['size']:,} bytes)")
                    ok += 1
                elif result['status'] == 'open':
                    size_str = f"  ({result['size']:,} bytes)" if result.get('size') else ""
                    print(f"[{i}/{total}] {rel} ... OPEN EDGES  →  {result['dst']}{size_str}")
                    in_tb = False
                    for line in (result['stdout'] + result['stderr']).splitlines():
                        if 'Traceback' in line:
                            in_tb = True
                        if in_tb or any(w in line.lower() for w in (
                                'blender_open', 'final nm', 'repair pass',
                                'merge doubles', 'decimate', 'normal vote')):
                            print(f"    {line}")
                    open_list.append(rel)
                elif result['status'] == 'unrepaired':
                    size_str = f"  ({result['size']:,} bytes)" if result.get('size') else ""
                    print(f"[{i}/{total}] {rel} ... UNREPAIRED  [original copied as .unrepaired.stl{size_str}]")
                    in_tb = False
                    for line in (result['stdout'] + result['stderr']).splitlines():
                        if 'Traceback' in line:
                            in_tb = True
                        if in_tb or any(w in line.lower() for w in (
                                'blender_unrepaired', 'final nm', 'repair pass',
                                'merge doubles', 'decimate', 'normal vote')):
                            print(f"    {line}")
                    unrepaired_list.append(rel)
                else:
                    if result.get('stdout', '').endswith('succeeded'):
                        label = result['stdout']  # "N/M parts succeeded"
                    elif result.get('is_mesh_bad'):
                        label = "broken"
                    else:
                        label = "transient — will retry"
                    print(f"[{i}/{total}] {rel} ... FAILED  [{label}]")
                    in_tb = False
                    for line in (result['stdout'] + result['stderr']).splitlines():
                        if 'Traceback' in line:
                            in_tb = True
                        if in_tb or any(w in line.lower() for w in (
                                'error', 'exception', 'blender_empty',
                                'merge doubles', 'decimate', 'repair pass',
                                'final nm', 'faces after', 'normal vote',
                                'timeout', 'non-manifold', 'boundary')):
                            print(f"    {line}")
                    failed.append(rel)
    _mgr.shutdown()

    print()
    print(f"Done: {ok} succeeded, {len(open_list)} open-edges, {skipped} skipped, "
          f"{len(unrepaired_list)} unrepaired, {len(failed)} failed, {len(corrupt)} corrupt.")
    if corrupt:
        print("Corrupt files (size mismatch — likely truncated download):")
        for f in corrupt:
            print(f"  {f}")
    if open_list:
        print("Open-edges files (nm=0, best repair written as .open.stl):")
        for f in open_list:
            print(f"  {f}")
    if unrepaired_list:
        print("Unrepaired files (nm>0 remains; original saved as .unrepaired.stl; delete to retry):")
        for f in unrepaired_list:
            print(f"  {f}")
    if failed:
        print("Failed files:")
        for f in failed:
            print(f"  {f}")
    if failed or corrupt or unrepaired_list:
        sys.exit(1)
