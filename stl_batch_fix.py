#!/mnt/sda2/python/.venv/bin/python
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INPUT_FOLDER   = os.environ.get('INPUT_FOLDER', "/mnt/sda2/STL/Fixing/")
OUTPUT_SUFFIX  = ""
MERGE_DIST     = 0.01 # mm — merge vertices closer than this (T-junction fix)
BLENDER        = "blender"
RECURSIVE      = True
WORKERS        = 3      # parallel Blender processes (default = physical core count)
TIMEOUT        = 1_200   # seconds — kill Blender if it runs longer than this
MAX_FACES      = 900_000    # decimate if face count exceeds this (0 = disabled)
LOG_FILE       = "/mnt/sda2/STL/Fixed/repair_log.tsv"

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
    parser.add_argument('--recursive',    dest='recursive', action='store_true',  default=None,
                        help=f"Search input folder recursively (default: {RECURSIVE})")
    parser.add_argument('--no-recursive', dest='recursive', action='store_false',
                        help="Search input folder non-recursively")
    parser.add_argument('--workers',      type=int, default=None,
                        help=f"Parallel Blender processes (default: {WORKERS})")
    parser.add_argument('--timeout',      type=int, default=None,
                        help=f"Seconds before killing a hung Blender process (default: {TIMEOUT})")
    parser.add_argument('--max-faces',    type=int, default=None,
                        help=f"Decimate mesh if face count exceeds this (default: {MAX_FACES}, 0=disabled)")
    args = parser.parse_args()
    if args.input      is not None: INPUT_FOLDER  = args.input
    if args.suffix     is not None: OUTPUT_SUFFIX = args.suffix
    if args.merge_dist is not None: MERGE_DIST    = args.merge_dist
    if args.recursive  is not None: RECURSIVE     = args.recursive
    if args.workers    is not None: WORKERS       = args.workers
    if args.timeout    is not None: TIMEOUT       = args.timeout
    if args.max_faces  is not None: MAX_FACES     = args.max_faces

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

# Above this limit all in-process Python work (edge scan, PyMeshLab split/decimate/NM repair,
# PyMeshFix pre-scan) is skipped — the file goes straight to Blender, which streams from disk.
# Keeps per-worker RAM within bounds when running multiple workers in parallel.
_LARGE_MESH_TRI_LIMIT = 2_000_000


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
        # Minimum shell size: at least 0.1% of the largest shell, or 100 faces.
        min_faces = max(100, meshes[0][0] // 1000) if meshes else 100
        meshes = [(n, i) for n, i in meshes if n >= min_faces]
        if len(meshes) <= 1:
            return []
        os.makedirs(dst_dir, exist_ok=True)
        base_no_ext = os.path.splitext(os.path.basename(src))[0]
        results = []
        for idx, (_, mesh_idx) in enumerate(meshes):
            ms.set_current_mesh(mesh_idx)
            out_path = os.path.join(dst_dir, f"{base_no_ext}.part.{idx}.stl")
            ms.save_current_mesh(out_path, binary=True)
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


def log_step(rel, msg):
    """Write one log line immediately. Safe for concurrent workers via file locking."""
    import fcntl, time
    line = f"{rel}\t{msg}\n"
    last_err = None
    for _ in range(10):
        try:
            with open(LOG_FILE, 'a') as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(line)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return
        except OSError as _e:
            last_err = _e
            time.sleep(0.05)
    # A log line vanishing without a trace is exactly what makes a crash
    # impossible to attribute — surface it on stderr instead (todo L7).
    try:
        sys.stderr.write(f"log_step: giving up after 10 attempts ({last_err}): {line}")
        sys.stderr.flush()
    except Exception:
        pass


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
    '.unrepaired.stl', '.failed.stl', '.broken.stl', '.open.stl',
    # Pipeline intermediates — left behind if a run is killed mid-file (todo M4).
    '.decimate.stl', '.repairnm.stl', '.pymeshfix.stl', '.merge.stl', '.partial',
)

# Per-shell files written by split_shells() into the ~parts subfolder (todo H2).
_PART_MARKER = '.part.'

def collect_stl_files(folder, recursive):
    files = []
    suffix_lower = OUTPUT_SUFFIX.lower()
    def _keep(name):
        name_lower = name.lower()
        ext = os.path.splitext(name_lower)[1]
        if ext not in ('.stl', '.obj'):
            return False
        for sig in _SIGNAL_SUFFIXES:
            if name_lower.endswith(sig):
                return False
        # <base>.part.<N>.stl — a split shell, not an input mesh.
        if _PART_MARKER in name_lower:
            return False
        if suffix_lower and ext == '.stl':
            base = os.path.splitext(name_lower)[0]
            if base.endswith(suffix_lower):
                return False
        return True
    if recursive:
        for root, dirs, names in os.walk(folder):
            # Never descend into split-part scratch folders.
            dirs[:] = [d for d in dirs if d != PARTS_DIRNAME]
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
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
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


def run_pymeshlab_repair_nm(src, dst):
    """Remove NM edges and vertices using PyMeshLab. Returns (nm, open_edges) after repair."""
    ms = _pymeshlab.MeshSet()
    ms.load_new_mesh(src)
    ms.meshing_repair_non_manifold_edges()
    ms.meshing_repair_non_manifold_vertices()
    ms.save_current_mesh(dst, binary=True)
    n = ms.current_mesh().face_number()
    nm, open_e, _ = scan_mesh_errors(dst, n)
    return nm, open_e


def fix_stl(src, dst, merge_dist, is_ascii=False, is_obj=False):
    global _blender_proc
    script = BLENDER_SCRIPT.format(src=src, dst=dst, merge_dist=merge_dist,
                                   is_ascii=repr(bool(is_ascii)),
                                   is_obj=repr(bool(is_obj)))
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
        tmp.write(script)
        script_path = tmp.name
    try:
        proc = subprocess.Popen(
            [BLENDER, '--background', '--python', script_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _blender_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False, False, False, f'TIMEOUT after {TIMEOUT}s', ''
        finally:
            _blender_proc = None
        success    = proc.returncode == 0 and 'BLENDER_OK' in stdout
        open_only  = proc.returncode == 0 and 'BLENDER_OPEN' in stdout
        unrepaired = 'BLENDER_UNREPAIRED' in stdout
        return success, open_only, unrepaired, stdout, stderr
    finally:
        os.unlink(script_path)

def blender_decimate(src, dst, max_faces):
    """Run stl_batch_fix.decimate.blender on src, writing a decimated binary STL to dst.
    Returns (ok, n_faces_out, stdout, stderr).  n_faces_out is -1 on failure."""
    global _blender_proc
    script = BLENDER_DECIMATE_SCRIPT.format(src=src, dst=dst, max_faces=max_faces)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp:
        tmp.write(script)
        script_path = tmp.name
    try:
        proc = subprocess.Popen(
            [BLENDER, '--background', '--python', script_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _blender_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False, -1, f'TIMEOUT after {TIMEOUT}s', ''
        finally:
            _blender_proc = None
        ok = proc.returncode == 0 and 'BLENDER_DECIMATE_OK' in stdout
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
    finally:
        os.unlink(script_path)


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
    'ok' on a library's word alone.  Returns (nm, open_e): (0, 0) when the file
    genuinely verifies clean, the scanned counts when it does not.

    An ASCII STL cannot be edge-scanned by this code path.  Rather than silently
    treating unverifiable as clean (todo L8), that is logged and reported as
    clean only because the caller has no better signal — Blender always writes
    binary here, so in practice this branch is unreachable."""
    if is_ascii_stl(path):
        L(f"{label}: ASCII STL — cannot edge-scan, accepting repair unverified")
        return 0, 0
    nm, open_e, err = scan_mesh_errors(path)
    if err:
        L(f"{label} scan error — {err}")
        return 0, 0
    if nm == -1:
        L(f"{label}: mesh too large to scan, accepting repair unverified")
        return 0, 0
    if nm > 0 or open_e > 0:
        L(f"{label} — reported ok but scan found nm={nm} open={open_e}")
    return nm, open_e


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
        for candidate in (part_path,
                          *(stem + sfx for sfx in _SIGNAL_SUFFIXES)):
            try:
                if os.path.exists(candidate):
                    os.unlink(candidate)
                    removed += 1
            except OSError:
                pass
    try:
        os.rmdir(parts_dir)   # only succeeds when nothing else is left in it
    except OSError:
        pass
    return removed


def _save_indicator(indicator_path, src_path, stales):
    """Copy src_path to indicator_path, delete stale indicators, return file size."""
    os.makedirs(os.path.dirname(os.path.abspath(indicator_path)), exist_ok=True)
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
            _pv_nm, _pv_open = _post_verify(_pmf_tmp, L, label=f"pymeshfix {label} post-verify")
            if _pv_nm > 0 or _pv_open > 0:
                _pmf_open = _pv_open  # fall through to open-edges path below
            if _pmf_open == 0:
                os.replace(_pmf_tmp, dst)
                size = os.path.getsize(dst)
                _clear_stale(failed_copy, unrepaired_copy, open_copy)
                L(f"result: ok ({label})")
                return _result(status='ok', dst=os.path.basename(dst),
                               size=size, is_ascii=is_ascii, is_obj=is_obj, pymeshfix=True)
        if _pmf_nm == 0 and _pmf_open > 0:
            os.makedirs(os.path.dirname(os.path.abspath(open_copy)), exist_ok=True)
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
    temps = []
    try:
        return _process_file_impl(src, is_part=is_part, temps=temps)
    finally:
        for _t in temps:
            try:
                if _t and os.path.exists(_t):
                    os.unlink(_t)
            except OSError:
                pass


def _process_file_impl(src, is_part=False, temps=None):
    if temps is None:
        temps = []
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
            os.makedirs(os.path.dirname(os.path.abspath(broken_copy)), exist_ok=True)
            shutil.copy2(src, broken_copy)
        L(f"corrupt: {err}")
        return {'rel': rel, 'status': 'corrupt', 'reason': err}

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

        # Perfect mesh within limit — just copy, nothing to do.
        if nm_src == 0 and open_src == 0 and (MAX_FACES == 0 or n_tris <= MAX_FACES):
            if src != dst:
                # Create dst's own parent directly — never recompute it via
                # output_path(), which for a part file resolves somewhere else
                # entirely (todo H1).
                os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
                shutil.copy2(src, dst)
            size = os.path.getsize(dst)
            _clear_stale(failed_copy, unrepaired_copy, open_copy)
            L(f"result: ok (clean copy — no repair needed)")
            return {'rel': rel, 'status': 'ok', 'dst': os.path.basename(dst),
                    'size': size, 'is_ascii': False, 'bypassed': True}

        working = src
        working_tris = n_tris
        _dec_tmp = None
        _nm_tmp = None
        _pmf_tmp = None

        # Step B — split multi-shell (only on original files, not parts).
        # Parts are written to the output folder; source is never modified.
        # Skip entirely if dst or dst.failed already exists, or mesh is too large for PyMeshLab.
        if not is_part and _PYMESHLAB_AVAILABLE and n_tris <= _LARGE_MESH_TRI_LIMIT:
            if os.path.exists(dst) or os.path.exists(dst_base + '.failed.stl'):
                pass  # already handled — fall through to normal repair
            else:
                dst_dir = os.path.join(
                    os.path.dirname(os.path.abspath(dst)),
                    PARTS_DIRNAME
                )
                L("step B: split multi-shell")
                parts = split_shells(src, dst_dir, L=L)
                if parts:
                    L(f"split: {len(parts)} shells → {', '.join(os.path.basename(p) for p in parts)}")
                    # Repair each part inline (same worker process).
                    # Recursion is depth-1 by construction: this whole block is
                    # guarded by `not is_part`, and every recursive call passes
                    # is_part=True, so a part can never split again.  Removing
                    # that guard would recurse without bound (todo L4).
                    part_results = []
                    for part_path in parts:
                        L(f"  part: {os.path.basename(part_path)}")
                        part_result = process_file(part_path, is_part=True)
                        part_results.append(part_result)
                        L(f"  part {os.path.basename(part_path)}: {part_result['status']}")
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
                            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
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
                        os.makedirs(os.path.dirname(os.path.abspath(failed_copy)), exist_ok=True)
                        shutil.copy2(src, failed_copy)
                        L(f"split partial: {n_ok}/{len(parts)} parts ok — saved as {os.path.basename(failed_copy)}")
                        return {'rel': rel, 'status': 'failed', 'is_mesh_bad': False,
                                'stdout': f"{n_ok}/{len(parts)} parts succeeded", 'stderr': ''}

        # Step C — decimate if over face limit.
        # Large meshes (> _LARGE_MESH_TRI_LIMIT) use Blender for decimation to avoid
        # loading the whole mesh into Python RAM; result feeds back into the normal pipeline.
        if MAX_FACES > 0 and working_tris > MAX_FACES:
            if working_tris > _LARGE_MESH_TRI_LIMIT:
                _dec_tmp = dst + '.decimate.stl'
                temps.append(_dec_tmp)
                os.makedirs(os.path.dirname(os.path.abspath(_dec_tmp)), exist_ok=True)
                L(f"decimate: {working_tris:,} tris — too large for PyMeshLab, using Blender")
                _bd_ok, _bd_faces, _bd_stdout, _bd_stderr = blender_decimate(working, _dec_tmp, MAX_FACES)
                if _bd_ok and os.path.exists(_dec_tmp):
                    working = _dec_tmp
                    # Blender's reported face count is advisory only — the scan
                    # reads the authoritative triangle count from the STL header
                    # itself, so a disagreement can't corrupt the edge counts.
                    working_tris = _bd_faces if _bd_faces > 0 else MAX_FACES
                    nm_src, open_src, _scan_err2 = scan_mesh_errors(working)
                    _scan_note2 = f"  scan error: {_scan_err2}" if _scan_err2 else ""
                    L(f"decimate (blender): → {working_tris:,} faces  nm={nm_src}  open={open_src}{_scan_note2}")
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
                    # File is too large even for Blender to decimate — sending the
                    # original to Blender repair would also OOM. Fail immediately.
                    L(f"decimate (blender): FAILED — file too large, skipping Blender repair")
                    size = _save_indicator(failed_copy, src, [])
                    return {'rel': rel, 'status': 'failed', 'is_mesh_bad': False,
                            'stdout': 'blender decimate OOM/timeout on oversized mesh', 'stderr': ''}
            elif _PYMESHLAB_AVAILABLE:
                _dec_tmp = dst + '.decimate.stl'
                temps.append(_dec_tmp)
                L(f"decimate: {working_tris:,} tris → target {MAX_FACES:,} (pymeshlab)")
                try:
                    _dec_nm, _dec_open, _dec_faces = run_pymeshlab_decimate(working, _dec_tmp, MAX_FACES)
                    L(f"decimate: {working_tris:,} → {_dec_faces:,} faces  nm={_dec_nm}  open={_dec_open}")
                    working = _dec_tmp
                    working_tris = _dec_faces
                    nm_src = _dec_nm
                    open_src = _dec_open
                except Exception as _dec_err:
                    if os.path.exists(_dec_tmp):
                        os.unlink(_dec_tmp)
                    _dec_tmp = None
                    L(f"decimate: FAILED — {_dec_err}")

        # Step D — PyMeshLab NM repair.
        if not _PYMESHLAB_AVAILABLE:
            L("skip D: pymeshlab unavailable")
        elif nm_src == -1:
            L("skip D: mesh too large to scan")
        elif nm_src == 0:
            L("skip D: nm=0 (no NM edges)")
        if _PYMESHLAB_AVAILABLE and nm_src > 0:
            _nm_tmp = dst + '.repairnm.stl'
            temps.append(_nm_tmp)
            os.makedirs(os.path.dirname(os.path.abspath(_nm_tmp)), exist_ok=True)
            L(f"step D: pymeshlab NM repair (nm={nm_src})")
            try:
                _nm_nm, _nm_open = run_pymeshlab_repair_nm(working, _nm_tmp)
                L(f"pymeshlab NM repair: nm={_nm_nm}  open={_nm_open}")
                if working != src and os.path.exists(working):
                    os.unlink(working)
                working = _nm_tmp
                nm_src = _nm_nm
                open_src = _nm_open
            except Exception as _nm_err:
                if os.path.exists(_nm_tmp):
                    os.unlink(_nm_tmp)
                _nm_tmp = None
                L(f"pymeshlab NM repair: FAILED — {_nm_err}")

        # Step E — PyMeshFix for open edges.
        if not _PYMESHFIX_AVAILABLE:
            L("skip E: pymeshfix unavailable")
        elif open_src == -1:
            L("skip E: mesh too large to scan")
        elif open_src == 0:
            L("skip E: open=0 (no open edges)")
        if _PYMESHFIX_AVAILABLE and open_src > 0:
            _pmf_tmp = dst + '.pymeshfix.stl'
            temps.append(_pmf_tmp)
            L(f"step E: pymeshfix open-edge fill (open={open_src})")
            try:
                _pmf_nm, _pmf_open = run_pymeshfix(working, _pmf_tmp)
                L(f"pymeshfix: nm={_pmf_nm}  open={_pmf_open}")
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
        if nm_src == 0 and open_src == 0:
            nm_src, open_src = _post_verify(working, L)
        if nm_src == 0 and open_src == 0:
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            os.replace(working, dst)
            size = os.path.getsize(dst)
            _clear_stale(failed_copy, unrepaired_copy, open_copy)
            L(f"result: ok (no blender needed)")
            return {'rel': rel, 'status': 'ok', 'dst': os.path.basename(dst),
                    'size': size, 'is_ascii': False, 'bypassed': True}

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

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

    success, open_only, unrepaired, stdout, stderr = fix_stl(
        blender_src, dst, MERGE_DIST,
        is_ascii=is_ascii, is_obj=is_obj,
    )

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

    if success and os.path.exists(dst):
        _pv_nm, _pv_open = _post_verify(dst, L, label="blender post-verify")
        if _pv_nm > 0 or _pv_open > 0:
            success = False
            open_only = True

    if success and os.path.exists(dst):
        size = os.path.getsize(dst)
        _clear_stale(failed_copy, unrepaired_copy, open_copy)
        L(f"result: ok (blender)")
        return _result(status='ok', dst=os.path.basename(dst), size=size, is_ascii=is_ascii, is_obj=is_obj)
    elif open_only and os.path.exists(dst):
        if _PYMESHFIX_AVAILABLE:
            r = _try_pymeshfix_after_blender(
                dst, dst, open_copy, failed_copy, unrepaired_copy,
                is_ascii, is_obj, stdout, stderr, "post-blender", L, _result,
                temps=temps)
            if r is not None:
                return r
        os.makedirs(os.path.dirname(os.path.abspath(open_copy)), exist_ok=True)
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
            _worker_status[_pid] = {'rel': rel, 'started': _t.monotonic()}
        except Exception:
            pass
    try:
        result = process_file(src, is_part=is_part)
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
            os.makedirs(os.path.dirname(os.path.abspath(_failed)), exist_ok=True)
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
    return result


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

    # Truncate log file at the start of each run.
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    open(LOG_FILE, 'w').close()

    workers = min(WORKERS, len(files))
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
