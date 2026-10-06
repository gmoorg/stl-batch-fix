"""Probe, load, and write meshes."""

from __future__ import annotations

import contextlib
import math
import os
import struct
import tempfile
from dataclasses import dataclass, replace
from enum import Enum

import numpy as np

#: Bytes per triangle in a binary STL: 12 normal + 36 vertices + 2 attribute.
BYTES_PER_TRIANGLE = 50

#: 80-byte comment + 4-byte triangle count.
HEADER_BYTES = 84

#: How much of the file `kind()` reads to tell ASCII from binary.
_SNIFF_BYTES = 256

#: Triangles `load` reads per chunk: a 51 KB buffer (owner, 2026-10-05).
#: The chunks replace one whole-file read, an allocation of 50 B/triangle
#: that only held bytes on their way to the coordinate array.
CHUNK_TRIANGLES = 1024

#: One binary STL triangle record, little-endian and unaligned (50 bytes).
_RECORD = np.dtype([('normal', '<f4', 3), ('corners', '<f4', 9), ('attribute', '<u2')])
assert _RECORD.itemsize == BYTES_PER_TRIANGLE


class Kind(Enum):
    """What sort of file this is, by inspection rather than by extension."""

    BINARY_STL = 'binary_stl'
    ASCII_STL = 'ascii_stl'
    OBJ = 'obj'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class Geometry:
    """A welded mesh: shared vertices, and faces indexing into them.

    This is the expensive thing.  A binary STL on disk stores no vertex
    sharing — each triangle carries its own three coordinate triples, so a
    vertex touched by six faces appears six times (measured: exactly 6.0x on
    real models).  Recovering that sharing is what `load` does, and it is a
    precondition of edge-based algorithms rather than an optimisation: quadric
    edge collapse works on edges, so it must know which faces meet where.

    Vertices are float64 and faces int64, checked on construction (owner,
    2026-10-04): every library we call takes float64 vertices (PyMeshLab,
    libigl, pymeshfix) and libigl takes int64 faces, so a mesh is handed to
    each without a converted copy and its results come back unrounded. STL is
    float32 only at the edges: `load` welds on the float32 bits and converts
    the welded table once; `write` rounds once.
    """

    verts: np.ndarray                 # (n_verts, 3) float64
    faces: np.ndarray                 # (n_faces, 3) int64, indices into verts

    def __post_init__(self):
        # A dtype check only, no copy: a producer that forgets the dtype fails
        # here instead of putting the per-library casts back.
        if self.verts.dtype != np.float64:
            raise TypeError(f"Geometry.verts must be float64, not {self.verts.dtype}")
        if self.faces.dtype != np.int64:
            raise TypeError(f"Geometry.faces must be int64, not {self.faces.dtype}")


@dataclass(frozen=True)
class LoadDrops:
    """Triangles `load` removed from a binary STL, by reason. Informational:
    the holes they leave are found by the scanner like any other."""

    nonfinite: int                    # a NaN or infinite coordinate
    degenerate: int                   # finite, two corners equal (after -0.0 -> 0.0)


@dataclass(frozen=True)
class Mesh:
    """What a file says about itself, and optionally the mesh itself.

    path        the file this mesh came from
    destination where its repaired result belongs.  **Required**, because the
                marker names the indicator scan looks for are derived from it
                (`<base>.failed.stl` and friends) — two derivations would mean
                two spellings, and a rerun would silently reprocess everything.
                One field, one source of truth.
    kind        binary STL, ASCII STL, OBJ, or unknown
    triangles   count from the header, or **None when it cannot be known
                without parsing** — ASCII STL and OBJ always report None
    is_valid    False when the file cannot be read as what it claims to be
    problem     why, when is_valid is False; None otherwise
    geometry    the welded mesh, once `load` has been called; None before that
    load_drops  what `load` removed reading this mesh's STL (`LoadDrops`);
                None when no STL load produced it (a probe, a PLY read).
                Kept by `with_geometry`/`with_destination`; read once, for
                the job's step note

    Frozen, and every operation returns a new one.  `load` does not fill in
    geometry on the mesh you hand it — it gives you back a second mesh that has
    it.  That is what keeps the cheap object cheap: a `Mesh` sitting in a queue
    stays a couple of hundred bytes no matter what a worker does with its own
    copy elsewhere.
    """

    path: str
    destination: str
    kind: Kind
    triangles: int | None
    is_valid: bool
    problem: str | None = None
    geometry: Geometry | None = None
    load_drops: LoadDrops | None = None

    @property
    def needs_conversion(self) -> bool:
        """True when this must become a binary STL before it can be measured."""
        return self.kind in (Kind.ASCII_STL, Kind.OBJ)

    @property
    def is_loaded(self) -> bool:
        """True when the geometry is in memory and this object is large."""
        return self.geometry is not None

    def with_geometry(self, geometry: Geometry) -> Mesh:
        """A copy carrying `geometry`, with `triangles` re-derived from it.

        This is how a step that changes the mesh — decimation, repair — reports
        its result: it returns a new `Mesh` rather than writing a file, so the
        caller decides whether the result is worth keeping.  The triangle count
        is taken from the faces rather than carried over, because the whole
        point of those steps is that it changed.
        """
        return Mesh(self.path, self.destination, self.kind,
                    len(geometry.faces), self.is_valid, self.problem, geometry,
                    self.load_drops)


    def with_destination(self, destination: str) -> Mesh:
        """A copy bound for a different output.

        `with_geometry` is for a step that *transforms* a mesh — same file,
        changed geometry — so it carries the destination across unchanged.
        A split is the other shape: one input becomes several outputs, and each
        needs its own destination or their markers collide.  This is how a
        part gets one.
        """
        return Mesh(self.path, destination, self.kind, self.triangles,
                    self.is_valid, self.problem, self.geometry, self.load_drops)


def require_geometry(mesh: Mesh) -> None:
    """Raise if `mesh` has no in-memory geometry to inspect or rewrite."""
    if mesh.geometry is None:
        raise ValueError(
            f"{mesh.path} has no geometry to operate on — load it first")


def ensure_parent_dir(path: str) -> None:
    """Create the parent directory for a file path when it is not empty."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def kind(path: str) -> Kind:
    """Classify by content, using the extension only for OBJ.

    A binary STL can start with `solid`, so its header count and file size break
    that tie. Only 256 bytes are sniffed: an unusually long ASCII solid name may
    be rejected as invalid, rather than silently parsed as binary.
    """
    if path.lower().endswith('.obj'):
        return Kind.OBJ
    try:
        with open(path, 'rb') as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return Kind.UNKNOWN
    if not head:
        return Kind.UNKNOWN

    looks_ascii = (head.lstrip()[:5].lower() == b'solid'
                   and b'facet normal' in head.lower())
    if not looks_ascii:
        return Kind.BINARY_STL
    if len(head) >= HEADER_BYTES:
        declared = struct.unpack_from('<I', head, 80)[0]
        try:
            size = os.path.getsize(path)
        except OSError:
            return Kind.ASCII_STL
        if size >= HEADER_BYTES + declared * BYTES_PER_TRIANGLE:
            return Kind.BINARY_STL     # text header on a binary file
    return Kind.ASCII_STL


def triangle_count(path: str) -> tuple[int, str | None]:
    """Triangles in a binary STL, cross-checked against the file size.

    Returns `(count, problem)`.  The header's count is authoritative — never
    trust one supplied by a caller, which may come from a different tool's face
    counter and disagree with the bytes on disk.

    A file *larger* than the count implies is accepted: some exporters append
    colour data after the triangles, which is non-standard but read fine by
    every slicer.  Only a file too short to hold what it claims is a problem.
    """
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(HEADER_BYTES)
    except OSError as exc:
        return -1, f"{type(exc).__name__}: {exc}"
    if len(head) < HEADER_BYTES:
        return -1, "file shorter than an STL header"
    count = struct.unpack_from('<I', head, 80)[0]
    needed = HEADER_BYTES + count * BYTES_PER_TRIANGLE
    if size < needed:
        return -1, (f"header claims {count:,} triangles ({needed:,} bytes) "
                    f"but the file is {size:,} bytes")
    if count == 0:
        # An 84-byte file is structurally valid and holds no model.  Rejected
        # here rather than in `load` because this is the one place both `probe`
        # and `load` derive the count, so one check covers the cheap metadata
        # path and the loading path alike — and a caller that trusted `probe`
        # can no longer hand `load` an empty array to index.
        return -1, "no triangles: the file declares an empty mesh"
    return count, None


def probe(path: str, destination: str) -> Mesh:
    """Everything a queue needs to know about a file, cheaply.

    `destination` is where the repaired result belongs.  It is required rather
    than derived here because deriving it is the caller's policy — the output
    tree layout, the OBJ-becomes-STL rule — and `indicators` must look for
    markers at exactly the same spelling this mesh will be written to.

    Reads at most a few hundred bytes.  Never parses geometry, so the cost is
    the same for a 5 MB file and a 700 MB one — which is what makes it usable
    on a whole collection before any work begins.
    """
    file_kind = kind(path)

    if file_kind is Kind.UNKNOWN:
        return Mesh(path, destination, file_kind, None, False,
                    "unreadable or empty")

    if file_kind is Kind.OBJ:
        try:
            empty = os.path.getsize(path) == 0
        except OSError as exc:
            return Mesh(path, destination, file_kind, None, False, str(exc))
        if empty:
            return Mesh(path, destination, file_kind, None, False,
                        "OBJ file is empty")
        # No count without parsing; Blender validates at import.
        return Mesh(path, destination, file_kind, None, True)

    if file_kind is Kind.ASCII_STL:
        # Counting would mean scanning the whole file for `facet` lines, which
        # is the expense this module exists to avoid.  It is converted before
        # it is queued, and the conversion's output is what gets measured.
        return Mesh(path, destination, file_kind, None, True)

    count, problem = triangle_count(path)
    if problem is not None:
        return Mesh(path, destination, file_kind, None, False, problem)
    return Mesh(path, destination, file_kind, count, True)


def _read_fully(f, view: memoryview) -> int:
    """Fill `view` from `f`; the bytes read, fewer only at end of file.
    `readinto` may return less than asked before the end, so loop."""
    got = 0
    while got < len(view):
        n = f.readinto(view[got:])
        if not n:
            break
        got += n
    return got


def _filter_chunk(corners: np.ndarray) -> tuple[np.ndarray, int, int]:
    """One chunk's kept triangles from its (n, 9) corner view: contiguous,
    -0.0 folded to 0.0. Also returns how many were dropped for a NaN/inf
    coordinate and how many (finite) for coincident corners."""
    # One contiguous copy that also folds -0.0 to 0.0: adding 0.0 leaves
    # every other value, NaN and infinity included, untouched. The fold comes
    # before the coincidence test because -0.0 and 0.0 differ in bits.
    tri = corners + np.float32(0.0)
    finite = np.isfinite(tri).all(axis=1)
    # Equal bits == equal values here (no NaN survives `finite`), and equal
    # corners are exactly what the weld would turn into a repeated index.
    c = tri.view(np.uint32).reshape(-1, 3, 3)
    coincident = ((c[:, 0] == c[:, 1]).all(axis=1) | (c[:, 1] == c[:, 2]).all(axis=1)
                  | (c[:, 0] == c[:, 2]).all(axis=1))
    nonfinite = len(tri) - int(finite.sum())
    degenerate = int((finite & coincident).sum())
    if nonfinite + degenerate == 0:
        return tri, 0, 0
    return tri[finite & ~coincident], nonfinite, degenerate


def load(mesh: Mesh, *, chunk_triangles: int = CHUNK_TRIANGLES) -> Mesh:
    """Weld `mesh` into memory and return a **new** Mesh carrying it.

    Only binary STL can be loaded; anything else must be converted first, and
    asking is a programming error rather than a data problem, so it raises.

    The file is read `chunk_triangles` records at a time into one reused
    buffer and filtered as it arrives, so only the kept coordinates
    (36 B/triangle) accumulate; the file's bytes are never held whole. That
    whole-file read was an allocation with no purpose (owner, 2026-10-05),
    though not the load's peak: the weld's sort over all corners is, and
    chunking leaves it as it was. `chunk_triangles` is a parameter so tests
    can force chunk boundaries on small fixtures.

    The sort is done on the raw coordinate *bits* viewed as three uint32
    columns rather than on float rows.  Identical float32 values have identical
    bit patterns, so `np.lexsort` over the integer columns is exact and about
    4x faster than `np.unique(axis=0)` while allocating less (measured
    6.9s/461 MB -> 1.75s/383 MB on a 2.55M-triangle mesh).

    Negative zero is normalised first: -0.0 and 0.0 compare equal as floats but
    have different bits, so without this a shared vertex would split in two and
    leave a crack that quadric edge collapse cannot close.

    Two kinds of triangle are dropped, not the file (owner, 2026-10-05:
    garbage in, garbage out), and counted in the result's `load_drops`:
    - a NaN or infinite coordinate: such a vertex has no position to
      recover; dropping its triangles leaves a hole the repair rebuilds;
    - coincident corners (two corners with equal bits after the -0.0 fold):
      exactly the triangles the weld would turn into a repeated index
      (`[a, a, b]`), which segfaults PyMeshLab's array path. Zero area, so
      no hole. `meshlab.to_mesh` still drops those faces for meshes that
      arrive other ways. Zero-area triangles with three distinct corners
      are kept.
    A triangle with both counts as non-finite. The returned `triangles` is
    the kept count; kept triangles keep their order and winding. A file with
    no kept triangle is invalid, with `load_drops` saying why. Stored normals
    are not checked.
    """
    if mesh.kind is not Kind.BINARY_STL:
        raise ValueError(
            f"only a binary STL can be loaded, not {mesh.kind.value} "
            f"({mesh.path}) — convert it first")

    count, problem = triangle_count(mesh.path)
    if problem is not None:
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    problem)

    def invalid(reason: str, drops: LoadDrops | None = None) -> Mesh:
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    reason, None, drops)

    # Drop bad triangles here, where the coordinates first become numbers.
    # Nothing downstream copes with NaN: a NaN mesh scans as open=0, nm=0
    # and produces a NaN volume, every comparison that guards the pipeline
    # is `<` (False against NaN), and `cKDTree` raises from outside the
    # repair sequence's own error handling.
    coords = np.empty((count, 9), dtype=np.float32)
    kept = nonfinite = degenerate = 0
    try:
        with open(mesh.path, 'rb') as f:
            f.seek(HEADER_BYTES)
            buffer = bytearray(min(count, chunk_triangles) * BYTES_PER_TRIANGLE)
            done = 0
            while done < count:
                n = min(chunk_triangles, count - done)
                got = _read_fully(f, memoryview(buffer)[:n * BYTES_PER_TRIANGLE])
                if got < n * BYTES_PER_TRIANGLE:
                    return invalid(
                        f"short read: expected {count * BYTES_PER_TRIANGLE:,} "
                        f"bytes, got {done * BYTES_PER_TRIANGLE + got:,}")
                good, bad_nonfinite, bad_degenerate = _filter_chunk(
                    np.frombuffer(buffer, dtype=_RECORD, count=n)['corners'])
                coords[kept:kept + len(good)] = good
                kept += len(good)
                nonfinite += bad_nonfinite
                degenerate += bad_degenerate
                done += n
            del buffer, good
    except OSError as exc:
        return invalid(f"{type(exc).__name__}: {exc}")
    drops = LoadDrops(nonfinite, degenerate)
    if kept == 0:
        return invalid("no finite triangles: every triangle has a NaN or "
                       "infinite coordinate" if nonfinite == count else
                       "no non-degenerate triangles: every finite triangle "
                       "has two coincident corners", drops)
    if kept < count:
        # Release the unused tail in place (realloc), not by copying the kept
        # rows: one dropped triangle would otherwise copy the whole array.
        # No view of `coords` exists here (the chunk arrays are gone), which
        # is what `refcheck` would verify.
        coords.resize((kept, 9), refcheck=False)

    # Each intermediate is released the moment it is no longer needed.  On a
    # 7M-triangle mesh holding them all to the end peaks at 1000 MB against
    # 667 MB when freed eagerly — and that mesh was OOM-killed at 2.5 GB once
    # the decimator's own structures were added on top.
    bits = coords.view(np.uint32).reshape(-1, 3)
    order = np.lexsort((bits[:, 2], bits[:, 1], bits[:, 0]))
    srt = bits[order]
    new = np.empty(len(srt), dtype=bool)
    new[0] = True
    np.any(srt[1:] != srt[:-1], axis=1, out=new[1:])
    ids = np.cumsum(new) - 1
    inv = np.empty(len(srt), dtype=np.int64)
    inv[order] = ids
    del order, ids
    # Welded on the float32 bits, then converted once: float64 holds every
    # float32 exactly, so `write` gives back the file's coordinates bit for bit.
    verts = srt[new].view(np.float32).reshape(-1, 3).astype(np.float64)
    del srt, new, bits, coords

    return replace(mesh.with_geometry(Geometry(verts, inv.reshape(kept, 3))),
                   load_drops=drops)


@contextlib.contextmanager
def staged_write(path: str):
    """Yield a temporary sibling path that becomes `path` only on success.

    Every file this project publishes under a name a rerun trusts goes through
    here.  `indicators.check` answers "was this already done?" by asking
    whether the path exists, so a write interrupted partway used to leave a
    truncated file that the next run skipped as finished work.

    The staging file is a sibling so the rename stays within one filesystem,
    where `os.replace` is atomic.  Cleanup catches `BaseException` because
    `KeyboardInterrupt` is precisely the interruption this guards against.

    The staging name is short and fixed rather than derived from the
    destination: a model's own name can sit near the filesystem's 255-byte
    limit, and prefixing it would fail with `ENAMETOOLONG` on a path that
    writes fine today.  Nothing reads these names, so they carry no meaning.
    """
    ensure_parent_dir(path)
    directory = os.path.dirname(path) or '.'
    fd, staged = tempfile.mkstemp(dir=directory, prefix='.stlfix-',
                                  suffix='.part')
    try:
        os.close(fd)
        # `mkstemp` makes the file 0600, and `os.replace` carries that onto the
        # destination — so staging would quietly make every output private
        # where it used to follow the umask.  Deliverables are meant to be
        # readable by whoever collects them, so restore the mode the ordinary
        # `open` would have produced.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(staged, 0o666 & ~umask)
        yield staged
        os.replace(staged, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        raise


def write(mesh: Mesh) -> None:
    """Write loaded geometry as binary STL to `mesh.destination`.

    There is no path override: output and marker names must agree. Face normals
    are computed from winding; degenerate faces receive zero normals.

    A coordinate finite in float64 but beyond float32's range would become
    infinite here, so it raises ValueError before anything is written.
    """
    require_geometry(mesh)
    path = mesh.destination

    verts, faces = mesh.geometry.verts, mesh.geometry.faces
    n = len(faces)
    with np.errstate(over='ignore'):
        tv = verts[faces].astype(np.float32)           # (n, 3, 3); the one rounding
    if not np.isfinite(tv).all():
        raise ValueError(f"{path}: vertex coordinates are not finite in float32")
    nrm = np.cross(tv[:, 1] - tv[:, 0], tv[:, 2] - tv[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    # Degenerate faces have no normal to speak of; leave those zeroed rather
    # than dividing by zero and writing NaNs into the file.
    ok = ln > 1e-20
    nrm[ok] /= ln[ok][:, None]
    nrm[~ok] = 0.0

    buf = np.zeros((n, BYTES_PER_TRIANGLE), dtype=np.uint8)
    buf[:, 0:12] = nrm.astype(np.float32).view(np.uint8)
    buf[:, 12:48] = tv.reshape(n, 9).view(np.uint8)

    with staged_write(path) as staged:
        with open(staged, 'wb') as f:
            f.write(b'\0' * 80)
            f.write(struct.pack('<I', n))
            f.write(buf.tobytes())
            f.flush()
            os.fsync(f.fileno())


#: The PLY layouts this module writes and the only two `read_ply` accepts.
#:
#: Deliberately bare: `x, y, z` and a triangle list, nothing else. No normals —
#: `write` derives STL's from the winding, so they carry no information
#: (measured: Blender's old normal vote reported "agree: 801, disagree: 0") —
#: and no colour, UVs or custom properties.
#:
#:   float32  `property float` x, y, z (extra float properties tolerated:
#:            Blender may append them) and `list uchar uint` faces — what
#:            Blender's `wm.ply_export` writes back.
#:   float64  exactly `property double` x, y, z, faces `list uchar int` or
#:            `list uchar uint` — what PyMeshLab's `save_current_mesh` writes
#:            with every extra turned off (VCG holds coordinates as float64),
#:            and `write_ply` writes.
#:
#: **This is not a general PLY reader and must not become one** (owner,
#: 2026-10-05: narrow is enough). Two fixed agreements, not a format: ASCII,
#: big-endian, other scalar types, colour or variable property order raise.
_PLY_MAGIC = b'ply\n'
_PLY_HEADER_END = b'\nend_header\n'
_PLY_FACE_RECORD = 13                       # uchar count + three 4-byte indices


def write_ply(mesh: Mesh, path: str) -> None:
    """Write a narrow binary little-endian PLY.

    Unlike deliverable STL, PLY preserves the welded vertex table across a
    boundary (the Blender subprocess, the decimation cache). Coordinates are
    written as `double`, `Geometry`'s float64, never cast down. `path` is
    explicit because this is a temporary file, not `mesh.destination`.
    """
    require_geometry(mesh)

    verts = np.ascontiguousarray(mesh.geometry.verts, dtype='<f8')
    faces = np.ascontiguousarray(mesh.geometry.faces, dtype=np.uint32)

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {len(verts)}\n"
        "property double x\n"
        "property double y\n"
        "property double z\n"
        f"element face {len(faces)}\n"
        "property list uchar uint vertex_indices\n"
        "end_header\n"
    ).encode('ascii')

    # One record per face: a uchar count of 3, then three uint32 indices.
    # Built as a structured array rather than a Python loop, which on a
    # 900k-face mesh is the difference between milliseconds and seconds.
    records = np.zeros(len(faces), dtype=[('n', 'u1'), ('v', '<u4', 3)])
    records['n'] = 3
    records['v'] = faces

    ensure_parent_dir(path)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(verts.tobytes())
        f.write(records.tobytes())


@dataclass(frozen=True)
class _PlyLayout:
    """What `_ply_layout` read from a header: where the body starts and how
    to read it."""
    body: int                # offset of the first body byte
    n_verts: int
    n_faces: int
    coord: str               # '<f4' or '<f8'
    width: int               # vertex properties per vertex
    index: str               # '<u4' or '<i4'


def _ply_layout(data: bytes, path: str) -> _PlyLayout:
    """Parse and check a PLY header against the two accepted layouts (see
    the module note above `write_ply`); raise ValueError on anything else."""
    if not data.startswith(_PLY_MAGIC):
        raise ValueError(f"{path} is not a PLY file")
    # `end_header` must be a whole line: a comment may contain the words.
    stop = data.find(_PLY_HEADER_END)
    if stop < 0:
        raise ValueError(f"{path} has no PLY header terminator")
    body = stop + len(_PLY_HEADER_END)
    lines = data[len(_PLY_MAGIC):stop].decode('ascii', 'replace').split('\n')

    seen_format = False
    elements: list[tuple[str, int, list[list[str]]]] = []
    for line in lines:
        words = line.split()
        if not words or words[0] in ('comment', 'obj_info'):
            continue
        if words[0] == 'format':
            if seen_format or elements:
                raise ValueError(f"{path}: misplaced or repeated PLY format line")
            if words[1:] != ['binary_little_endian', '1.0']:
                raise ValueError(
                    f"{path} is not binary little-endian PLY — this reader "
                    f"handles only the two layouts write_ply documents")
            seen_format = True
        elif words[0] == 'element':
            if not seen_format:
                raise ValueError(f"{path}: PLY element before the format line")
            if len(words) != 3 or not words[2].isdigit():
                raise ValueError(f"{path}: bad PLY element line {line!r}")
            elements.append((words[1], int(words[2]), []))
        elif words[0] == 'property':
            if not elements:
                raise ValueError(f"{path}: PLY property before any element")
            elements[-1][2].append(words[1:])
        else:
            raise ValueError(f"{path}: unexpected PLY header line {line!r}")
    if [name for name, _, _ in elements] != ['vertex', 'face']:
        raise ValueError(
            f"{path} must declare exactly a vertex then a face element, "
            f"not {[name for name, _, _ in elements]}")
    (_, n_verts, vprops), (_, n_faces, fprops) = elements

    if vprops and all(len(p) == 2 and p[0] == 'float' for p in vprops) \
            and [p[1] for p in vprops[:3]] == ['x', 'y', 'z']:
        coord, index_types = '<f4', ('uint',)
    elif vprops == [['double', 'x'], ['double', 'y'], ['double', 'z']]:
        coord, index_types = '<f8', ('uint', 'int')
    else:
        raise ValueError(
            f"{path} vertex properties {vprops} match neither accepted "
            f"layout (float x, y, z [+ float extras], or double x, y, z)")
    face = fprops[0] if len(fprops) == 1 else []
    if len(face) != 4 or face[:2] != ['list', 'uchar'] or face[2] not in index_types \
            or face[3] != 'vertex_indices':
        raise ValueError(f"{path} face properties {fprops} are not accepted "
                         f"with {'double' if coord == '<f8' else 'float'} vertices")
    width = len(vprops)
    expected = body + n_verts * width * int(coord[-1]) + n_faces * _PLY_FACE_RECORD
    if len(data) != expected:
        raise ValueError(f"{path} is {len(data):,} bytes; its header declares "
                         f"{expected:,} (truncated or trailing data)")
    return _PlyLayout(body, n_verts, n_faces, coord, width,
                      '<u4' if face[2] == 'uint' else '<i4')


def read_ply(path: str, mesh: Mesh) -> Mesh:
    """Read a PLY in one of the two accepted layouts and attach its geometry
    to `mesh`.

    The vertex table is preserved, so no welding is needed. Anything outside
    the two layouts raises ValueError instead of being guessed at. Double
    coordinates are kept exactly; float coordinates are widened to float64,
    `Geometry`'s type.
    """
    with open(path, 'rb') as f:
        data = f.read()
    layout = _ply_layout(data, path)

    block = np.frombuffer(data, dtype=layout.coord, count=layout.n_verts * layout.width,
                          offset=layout.body)
    coords = block.reshape(layout.n_verts, layout.width)[:, :3]
    # `load` guards the file entrance; this is the other one. A NaN arriving
    # from Blender reaches `repairer._count_lost`, whose cKDTree raises from
    # outside the repair sequence's own error handling, so the failure escapes
    # as a crash instead of a failed result.
    if not np.isfinite(coords).all():
        raise ValueError(f"{path} contains NaN or infinite vertex coordinates")
    verts = np.ascontiguousarray(coords, dtype=np.float64)

    offset = layout.body + layout.n_verts * layout.width * int(layout.coord[-1])
    records = np.frombuffer(data, dtype=[('n', 'u1'), ('v', layout.index, 3)],
                            count=layout.n_faces, offset=offset)
    if layout.n_faces and not np.all(records['n'] == 3):
        raise ValueError(
            f"{path} contains a non-triangular face — this boundary carries "
            f"triangles only, and Blender triangulates before export")
    faces = records['v'].astype(np.int64)
    if layout.n_faces and (faces.min() < 0 or faces.max() >= layout.n_verts):
        raise ValueError(f"{path} has face indices outside its {layout.n_verts} vertices")

    # A PLY is a new input: whatever an STL load once dropped is not its history.
    return replace(mesh.with_geometry(Geometry(verts, np.ascontiguousarray(faces))),
                   load_drops=None)


def bounds(path: str) -> tuple[tuple[float, float, float],
                               tuple[float, float, float]] | None:
    """Extents of a binary STL as `((minx, miny, minz), (maxx, maxy, maxz))`.

    Streams the vertex block in chunks rather than welding the mesh: extents
    need no connectivity, and welding a 900k-face mesh to answer this would
    cost hundreds of MB inside a worker already near its budget.

    Returns None when the file cannot be read as a binary STL.
    """
    count, problem = triangle_count(path)
    if problem is not None or count <= 0:
        return None
    try:
        lo = np.full(3, np.inf, dtype=np.float64)
        hi = np.full(3, -np.inf, dtype=np.float64)
        chunk = 200_000                       # triangles per pass, ~10 MB
        with open(path, 'rb') as f:
            f.seek(HEADER_BYTES)
            remaining = count
            while remaining > 0:
                take = min(chunk, remaining)
                buf = f.read(take * BYTES_PER_TRIANGLE)
                if len(buf) < take * BYTES_PER_TRIANGLE:
                    return None
                block = np.frombuffer(buf, dtype=np.uint8).reshape(
                    take, BYTES_PER_TRIANGLE)
                # Bytes 12:48 are the three vertices; 0:12 is the face normal,
                # which must not be included in the extents.
                verts = block[:, 12:48].copy().view(np.float32).reshape(-1, 3)
                np.minimum(lo, verts.min(axis=0), out=lo)
                np.maximum(hi, verts.max(axis=0), out=hi)
                remaining -= take
        if not np.all(np.isfinite(lo)) or not np.all(np.isfinite(hi)):
            return None
        return tuple(lo), tuple(hi)
    except (OSError, ValueError):
        return None


def dimensions(path: str) -> tuple[float, float, float] | None:
    """Size of the model along each axis, or None if it cannot be read."""
    extents = bounds(path)
    if extents is None:
        return None
    lo, hi = extents
    return tuple(hi[i] - lo[i] for i in range(3))


def diagonal(path: str) -> float | None:
    """Length of the bounding box's diagonal — one number for "how big".

    Useful for expressing an absolute tolerance as a fraction of the model,
    which is how a fixed millimetre constant stops meaning two different things
    on a 4 mm part and a 200 mm one.
    """
    dims = dimensions(path)
    if dims is None:
        return None
    return math.sqrt(sum(d * d for d in dims))
