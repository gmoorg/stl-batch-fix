"""Probe, load, and write meshes.

`probe` returns cheap metadata; an unknown triangle count is None, never zero.
`load` returns a new frozen `Mesh` carrying welded geometry. `write` writes its
destination, and PLY helpers preserve the vertex table at the Blender boundary.
Loading is explicit because it materially changes memory use.
"""

from __future__ import annotations

import contextlib
import math
import os
import struct
import tempfile
from dataclasses import dataclass
from enum import Enum

import numpy as np

#: Bytes per triangle in a binary STL: 12 normal + 36 vertices + 2 attribute.
BYTES_PER_TRIANGLE = 50

#: 80-byte comment + 4-byte triangle count.
HEADER_BYTES = 84

#: How much of the file `kind()` reads to tell ASCII from binary.
_SNIFF_BYTES = 256


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
    """

    verts: np.ndarray                 # (n_verts, 3) float32
    faces: np.ndarray                 # (n_faces, 3) int64, indices into verts


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
                    len(geometry.faces), self.is_valid, self.problem, geometry)


    def with_destination(self, destination: str) -> Mesh:
        """A copy bound for a different output.

        `with_geometry` is for a step that *transforms* a mesh — same file,
        changed geometry — so it carries the destination across unchanged.
        A split is the other shape: one input becomes several outputs, and each
        needs its own destination or their markers collide.  This is how a
        part gets one.
        """
        return Mesh(self.path, destination, self.kind, self.triangles,
                    self.is_valid, self.problem, self.geometry)


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


def load(mesh: Mesh) -> Mesh:
    """Weld `mesh` into memory and return a **new** Mesh carrying it.

    Only binary STL can be loaded; anything else must be converted first, and
    asking is a programming error rather than a data problem, so it raises.

    The sort is done on the raw coordinate *bits* viewed as three uint32
    columns rather than on float rows.  Identical float32 values have identical
    bit patterns, so `np.lexsort` over the integer columns is exact and about
    4x faster than `np.unique(axis=0)` while allocating less (measured
    6.9s/461 MB -> 1.75s/383 MB on a 2.55M-triangle mesh).

    Negative zero is normalised first: -0.0 and 0.0 compare equal as floats but
    have different bits, so without this a shared vertex would split in two and
    leave a crack that quadric edge collapse cannot close.
    """
    if mesh.kind is not Kind.BINARY_STL:
        raise ValueError(
            f"only a binary STL can be loaded, not {mesh.kind.value} "
            f"({mesh.path}) — convert it first")

    count, problem = triangle_count(mesh.path)
    if problem is not None:
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    problem)

    # Each intermediate is released the moment it is no longer needed.  On a
    # 7M-triangle mesh holding them all to the end peaks at 1000 MB against
    # 667 MB when freed eagerly — and that mesh was OOM-killed at 2.5 GB once
    # the decimator's own structures were added on top.
    try:
        with open(mesh.path, 'rb') as f:
            f.seek(HEADER_BYTES)
            raw = np.frombuffer(f.read(count * BYTES_PER_TRIANGLE),
                                dtype=np.uint8)
    except OSError as exc:
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    f"{type(exc).__name__}: {exc}")
    if len(raw) < count * BYTES_PER_TRIANGLE:
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    f"short read: expected {count * BYTES_PER_TRIANGLE:,} "
                    f"bytes, got {len(raw):,}")
    raw = raw.reshape(count, BYTES_PER_TRIANGLE)

    # Bytes 12:48 of each record are the three vertices (9 float32).
    #
    # `.copy()`, not `ascontiguousarray`: the buffer from `frombuffer` is
    # read-only, and when the slice is already contiguous — which it is for a
    # single-triangle mesh — `ascontiguousarray` returns that read-only view
    # unchanged, and the -0.0 fold below then fails on it.  A mesh small enough
    # to hit that is a degenerate-face test case, not a model, so the bug would
    # have surfaced only on the strangest input.
    coords = raw[:, 12:48].copy().reshape(-1, 12)
    del raw
    fview = coords.view(np.float32).reshape(-1, 3)
    # Refuse NaN and infinity here, where the coordinates first become numbers.
    # Nothing downstream can catch them: a NaN mesh scans as open=0, nm=0 and
    # produces a NaN volume, and every comparison that guards the pipeline is
    # `<` — which is False against NaN, so each one reports that all is well.
    # The same input also reaches `cKDTree`, which raises from outside the
    # repair sequence's own error handling.  There is no repair for a vertex
    # that is not a position, so this is input validation, not a judgement.
    if not np.isfinite(fview).all():
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    "not finite: the file contains NaN or infinite coordinates")
    # Fold -0.0 to 0.0 in place; adding 0.0 leaves every other value untouched.
    np.add(fview, np.float32(0.0), out=fview)
    del fview

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
    verts = srt[new].view(np.float32).reshape(-1, 3).copy()
    del srt, new, bits, coords

    return mesh.with_geometry(Geometry(verts, inv.reshape(count, 3)))


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
    """
    require_geometry(mesh)
    path = mesh.destination

    verts, faces = mesh.geometry.verts, mesh.geometry.faces
    n = len(faces)
    tv = verts[faces].astype(np.float32)               # (n, 3, 3)
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


#: The PLY header this module writes, and the only dialect `read_ply` accepts.
#:
#: Deliberately bare: `x, y, z` as float32 and a face list of uint32, nothing
#: else.  No normals — `write` derives STL's from the winding, so they carry no
#: information (measured: Blender's old normal vote reported "agree: 801,
#: disagree: 0") — and no colour, UVs or custom properties.
#:
#: **This is not a general PLY reader and must not become one.**  It reads back
#: what Blender's `wm.ply_export` writes at this one boundary, and that is a
#: two-party agreement, not a format.  Supporting arbitrary dialects — ASCII,
#: big-endian, double precision, per-vertex colour, variable property order —
#: is the parsing burden the original PLY discussion explicitly ruled out of
#: scope.  A file that does not match raises.
_PLY_MAGIC = b'ply\n'
_PLY_HEADER_END = b'end_header\n'


def write_ply(mesh: Mesh, path: str) -> None:
    """Write a narrow binary little-endian PLY for Blender scratch work.

    Unlike deliverable STL, PLY preserves the welded vertex table across the
    subprocess boundary. `path` is explicit because this is a temporary file,
    not `mesh.destination`.
    """
    require_geometry(mesh)

    verts = np.ascontiguousarray(mesh.geometry.verts, dtype=np.float32)
    faces = np.ascontiguousarray(mesh.geometry.faces, dtype=np.uint32)

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {len(verts)}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
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


def read_ply(path: str, mesh: Mesh) -> Mesh:
    """Read Blender's binary PLY and attach its geometry to `mesh`.

    The vertex table is preserved, so no welding is needed. Reject other PLY
    dialects instead of guessing inside a repair round trip.
    """
    with open(path, 'rb') as f:
        data = f.read()

    if not data.startswith(_PLY_MAGIC):
        raise ValueError(f"{path} is not a PLY file")
    end = data.find(_PLY_HEADER_END)
    if end < 0:
        raise ValueError(f"{path} has no PLY header terminator")
    end += len(_PLY_HEADER_END)
    header = data[:end].decode('ascii', 'replace').splitlines()

    if 'format binary_little_endian 1.0' not in header:
        raise ValueError(
            f"{path} is not binary little-endian PLY — this reader handles "
            f"only what write_ply and Blender's exporter produce")

    n_verts = n_faces = None
    element = None
    vertex_properties = []
    for line in header:
        if line.startswith('element vertex '):
            element, n_verts = 'vertex', int(line.split()[-1])
        elif line.startswith('element face '):
            element, n_faces = 'face', int(line.split()[-1])
        elif line.startswith('property ') and element == 'vertex':
            vertex_properties.append(line.split()[-1])
    if n_verts is None or n_faces is None:
        raise ValueError(f"{path} declares no vertex or face element")

    # Blender writes x, y, z first and may append nx/ny/nz or colour depending
    # on export flags.  Extra float properties are tolerated and dropped; a
    # non-float property in the vertex block would change the stride, so it is
    # rejected rather than silently misread.
    if vertex_properties[:3] != ['x', 'y', 'z']:
        raise ValueError(
            f"{path} vertex properties start with {vertex_properties[:3]}, "
            f"expected ['x', 'y', 'z']")
    if any(line.startswith('property ') and not line.startswith('property float')
           for line in header
           if ' vertex_indices' not in line and line != 'property list uchar uint vertex_indices'):
        raise ValueError(f"{path} has a non-float vertex property")

    width = len(vertex_properties)
    block = np.frombuffer(data, dtype='<f4', count=n_verts * width, offset=end)
    verts = np.ascontiguousarray(block.reshape(n_verts, width)[:, :3])
    if not np.isfinite(verts).all():
        # `load` guards the file entrance; this is the other one.  A NaN
        # arriving from Blender reaches `repairer._count_lost`, whose cKDTree
        # raises from outside the repair sequence's own error handling, so the
        # failure escapes as a crash instead of a failed result.  Raising here
        # matches this reader's contract: malformed input is a ValueError.
        raise ValueError(
            f"{path} contains NaN or infinite vertex coordinates")

    offset = end + n_verts * width * 4
    records = np.frombuffer(data, dtype=[('n', 'u1'), ('v', '<u4', 3)],
                            count=n_faces, offset=offset)
    if n_faces and not np.all(records['n'] == 3):
        raise ValueError(
            f"{path} contains a non-triangular face — this boundary carries "
            f"triangles only, and Blender triangulates before export")

    return mesh.with_geometry(Geometry(
        verts.astype(np.float32),
        np.ascontiguousarray(records['v'], dtype=np.int64)))


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
