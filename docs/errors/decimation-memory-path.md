# Memory path of the initial decimation

Reference for [oom-kill-silent.md](oom-kill-silent.md) and
[decimation-segfault.md](decimation-segfault.md). It covers what is allocated
between reading an STL and PyMeshLab's quadric decimation, and what loading
the file directly into PyMeshLab costs instead. Measured 2026-10-04 with
PyMeshLab and pymeshfix 0.18.1 from the project venv, on a 31 GiB machine
with nothing else running.

`F` = face count, `V` ≈ `F/2` = vertex count. Sizes are in bytes. Measured
RSS columns (`G`) are GiB, as `meshlab_file_decimate.py` prints them; per-face
figures divide those by face count.

## Current path (code reading)

`batch_repair._prepare_one_file` → `mesh_io.load` → `processor.decimate_initial`
→ `decimator.decimate` → `meshlab.apply_filters`.

### 1. `mesh_io.load` (welds the binary STL)

| Array | Size | Lifetime |
|---|---|---|
| `f.read()` bytes (`raw`) | 50·F | freed after `coords` is copied |
| `coords` (9 float32 per face) | 36·F | freed at end |
| `order` from `np.lexsort` (int64) | 24·F | freed before `verts` copy |
| `srt = bits[order]` | 36·F | freed at end |
| `new` (bool) | 3·F | freed at end |
| `ids = cumsum(new)` (int64) | 24·F | freed with `order` |
| `inv` (int64), which becomes `faces` | 24·F | **kept** |
| `verts` (float32) | 12·V ≈ 6·F | **kept** |

Transient peak is about 147·F (≈ 5.8 GB at 39.6M faces). Steady state is
**30·F**, of which 24·F is the int64 face array.

### 2. `meshlab.to_mesh`

| Copy | Size | Why |
|---|---|---|
| verts float32 → float64 | 12·F | constructor signature is `numpy.float64[m, 3]` |
| faces int64 → int32 | 12·F | constructor signature is `numpy.int32[m, 3]` |

These are temporaries, freed once `pymeshlab.Mesh(...)` returns.

### 3. `pymeshlab.Mesh(vertex_matrix, face_matrix)`

This builds a full VCG mesh (CMeshO). Each VCG vertex and face carries more than
coordinates: normals, flags, marks, adjacency pointers, and colour/quality on
vertices. See the table under [Measured](#measured-loading-straight-from-file)
for its real size.

### 4. `ms.add_mesh(mesh)`: second VCG mesh (confirmed by the docs)

PyMeshLab's docstring says `add_mesh` "Adds a **copy** of the given mesh". The
temporary `pymeshlab.Mesh` from step 3 is still alive during the call, so two
full VCG meshes exist at once. That's 2 × ~430·F, the largest avoidable cost
on this path. The live RSS of this step has **not been measured** yet.

### 5. Held by the caller during decimation

The loaded `Mesh` (30·F) stays referenced through decimation. It's needed
afterwards: `decimate` returns the source mesh on failure, for the
`UNDECIMATED` fallback.

### 6. `meshlab.from_mesh`

Copies the 900k-face result back as float32/int64. Negligible.

## Measured: loading straight from file

`MeshSet.load_new_mesh(path)` followed by
`meshing_decimation_quadric_edge_collapse(targetfacenum=900000)`, one file
per process, with RSS and high-water mark read from `/proc/self/status`.
Script: [tools/experiments/meshlab_file_decimate.py](../../tools/experiments/meshlab_file_decimate.py)
(usage, including the memory-capped run, in its docstring).

| File | Faces | RSS loaded | Peak after load | Peak overall | Decimate time | Result |
|---|---|---|---|---|---|---|
| Base_Pillar_R | 1.00M | 0.51 G | 0.53 G | 0.62 G | 4 s | OK |
| bodynsfw | 1.18M | 0.42 G | 0.61 G | 0.72 G | 5 s | OK |
| face | 1.22M | 0.51 G | 0.62 G | 0.75 G | 5 s | OK |
| legs | 1.63M | 0.69 G | 0.81 G | 1.10 G | 9 s | OK |
| whole-costume02 | 2.00M | 0.78 G | 0.99 G | 1.28 G | 19 s | OK |
| Mandy_Body | 2.06M | 0.83 G | 1.02 G | 1.26 G | 20 s | OK |
| Bat Girl Merge | 4.83M | 2.16 G | 2.27 G | 2.92 G | 63 s | OK |
| Azula | 39.6M | 12.67 G | 18.08 G | > 23.3 G | killed after ~100 s | **killed** (cgroup cap 22.8 G) |

Azula run in detail (second attempt, RSS sampled every 15 s, cap 22.8 GB,
no swap; the first attempt at a 21 GB cap left no output):

```text
start   rss=0.07G
  [ 15s] 12.18G   [ 45s] 14.24G   [ 60s] 16.92G   [ 75s] 18.08G   [ 90s] 17.19G
loaded  rss=12.67G peak=18.08G  v=19,810,000 f=39,620,499  96.6 s
  [105s] 15.33G   [120s] 17.50G   [135s] 19.99G   [150s] 20.85G
  [165s] 21.16G   [180s] 21.43G   [195s] 22.23G
killed  SIGKILL (memcg OOM), anon-rss 23.3 GB, wall 3:17.6, CPU 168 s user + 30 s sys
```

Findings:

- A VCG mesh costs about **430 B/face** including its vertices
  (Bat Girl: 2.09 GB over the 0.07 GB baseline for 4.83M faces). The peak
  during decimation is about 35 % above the loaded size (2.16 → 2.92 G).
  Peaks are high-water marks, which include import transients, so this is
  not decimation's own allocation in isolation. The script's raw-STL NaN
  scan, now chunked, was smaller than the import peak on every file here;
  Base_Pillar_R reran with identical peaks (2026-10-05).
- **None of the seven segfault files crashed** when loaded from file. The
  cause was later found on the array path: degenerate faces, which the
  file importer drops (see [decimation-segfault.md](decimation-segfault.md)).
- **Azula can't fit even this way on this machine.**
  - **Load** takes 96.6 s. It peaks at 18.1 GB (the STL importer's
    temporary buffers) and settles at 12.67 GB, about **320 B/face**. Per
    face, that's less than Bat Girl's 430 B/face figure, so the small-file
    numbers include fixed overhead and overstate large files.
  - **Decimation** then grew RSS steadily from 12.7 GB to past 23.3 GB in
    about 100 s, still climbing when the cap killed it. That's at least
    +10.6 GB (≥ 0.84 × the loaded mesh) against +35 % on Bat Girl. Either the
    decimation overhead doesn't scale linearly, or most of it comes after
    the first pass. The final peak is unknown.
  - The machine had 23.4 GB available with no swap, so the peak is out of
    reach even with nothing else running. Saving our own copies can't fix
    this. A 39.6M-face input has to be reduced before it becomes one VCG
    mesh.

## Assessment: store vertices as float64 to avoid the conversion copy

> **Superseded** by [Decision: float64 internal vertex buffer](#decision-float64-internal-vertex-buffer-owner-2026-10-04).
> This assessment only looked at the PyMeshLab hand-off, and it assumed
> full-size sources pass through our process. Decimate-from-file removes
> that. The pybind11 point still holds: float64 storage doesn't avoid
> PyMeshLab's own copy.

Proposed in discussion. **It would not remove a copy, and it costs memory
everywhere else:**

- The constructor takes `numpy.float64[m, 3]`, which is pybind11's Eigen
  type caster. Pybind11 copies a numpy array into an Eigen matrix even when
  the dtype already matches (row-major numpy vs column-major Eigen). I haven't
  measured this, but that is the caster's documented behaviour for by-value
  Eigen parameters. So float64 storage would skip our `astype` copy and
  then pay the same copy inside pybind11.
- It would double resident vertex storage (6·F → 12·F) for the whole run.
  Native working memory changes less: winding already converts to float64
  (`_validated`) and marching cubes returns float64.
- Binary STL is float32, so float64 adds no precision. The weld in
  `mesh_io.load` sorts on float32 bit patterns, which is exact precisely
  because the data is float32.
- Even if it worked, it saves 12·F against a VCG mesh of ~430·F (under 3 %).

**Faces as int32** ~~is the better of the two dtype changes~~. *Withdrawn:*
libigl (winding, the heaviest step) takes `int64` faces, so int32 storage
would add a copy there. PyMeshLab and pymeshfix take `int32`. Some
conversion happens either way, and int64 avoids it where it costs most.

## Ranked options (none implemented or tested)

1. **Don't build the VCG mesh twice** (chosen; see
   [Decision](#decision-decimate-from-file-cache-as-ply-owner-2026-10-04)).
   Feed PyMeshLab the source file with
   `load_new_mesh(source_path)`. This saves one full VCG mesh (~430·F) plus
   all of our 30·F + 24·F. It needs checking first:
   - NaN/inf validation currently lives in `mesh_io.load`.
   - PyMeshLab's STL import welds vertices its own way. Whether it folds
     −0.0 like `mesh_io.load` does is unknown, and the `load` docstring says
     an unfolded −0.0 leaves cracks decimation can't close.
   - Compare the decimated output of both paths (faces, NM edges, volume).
2. **Pre-reduce huge inputs** before quadric decimation. Options include vertex
   clustering, or splitting and decimating shells separately. This is the
   only option that helps Azula-class files, because memory is ~430·F of the
   *input*, not the 900k target.
3. **Faces as int32** in `Geometry`: small, permanent saving.
4. **Admission already exists.** The prepare pass reserves
   `jobmemory.prepare_bytes` = 0.4 GB + 700 B/face. A job whose estimate
   exceeds the budget (`memory_budget_fraction` × RAM) is admitted *alone*
   (`batch_repair.py` selector, "admitting … alone"). Azula's estimate is
   ~28 GB against a ~21.7 GB budget, so it ran alone by design. Running alone
   can't save a job larger than free RAM: it needs refusing or pre-reducing,
   not admitting. The measured file-path peak (~0.6 KB/face) is close to the
   700 B/face constant. The current array path's real peak is not measured.

## Decision: decimate from file, cache as PLY (owner, 2026-10-04)

Not implemented. Planned for the initial decimation in the prepare pass, for
sources over `max_faces`:

```text
MeshSet.load_new_mesh(source) → quadric decimation → save_current_mesh(cache.ply) → mesh_io.read_ply
```

**Why:**

- **Memory.** Our process never holds the full-size source. The load peak
  (~147·F), resident arrays (30·F), conversion copies (24·F) and the second
  VCG mesh from `add_mesh` all go away. Measured file-path peaks: 0.6–2.9 GB
  for 1–4.8M faces.
- **Exact hand-off.** The cache keeps the decimator's vertex table instead of
  being re-welded from STL triangles. This follows the existing rule in
  [modules.md](../refactor/modules.md) ("Do not replace indexed PLY exchange
  with lossy STL round trips"). It's also smaller (~17 MB vs ~45 MB at 900k)
  and reloads without the `lexsort` weld.

**Does not solve:** Azula-class inputs. A 39.6M-face file needs > 23 GB even
on the file path (option 2 above).

**To settle while planning:**

- **Non-finite input.** Today `mesh_io.load` rejects any NaN/inf file.
  PyMeshLab's importer silently drops the NaN faces instead (measured on
  `torso_girl_SUP.stl`: 224 of 889,454 dropped; see
  [non-finite-coordinates.md](non-finite-coordinates.md)). Choose
  deliberately: keep rejecting (cheap raw pre-scan) or accept PyMeshLab's
  drop.
- **Weld equivalence.** VCG's STL import welds by float comparison
  (−0.0 == 0.0, probably), and ours welds by bit pattern after folding −0.0.
  Compare the decimated output of both paths (faces, vertices, NM/open
  edges, volume) on a few models.
- **PLY dialect: `read_ply` can't read PyMeshLab's PLY** (measured
  2026-10-04 on a 4-face mesh):

  | Writer / reader | Vertex x, y, z | Face indices |
  |---|---|---|
  | `mesh_io.write_ply` | `float` (float32) | `list uchar uint` |
  | `mesh_io.read_ply` accepts | `float` only; any other vertex property type is rejected | `uchar uint` |
  | PyMeshLab `save_current_mesh`, default | `double` + `uchar` RGBA + `double` quality | `list uchar int` |
  | PyMeshLab, every `save_*` flag off | **`double`** | `list uchar int` |

  PyMeshLab writes `double` even with all extras off, because VCG holds
  coordinates as float64 in this build. So decimation output is float64
  natively, and our float32 storage rounds it (today, at the STL cache).
  `read_ply` rejects both the `double` vertex properties and the `int` index
  type. By design it is a two-party agreement with Blender's exporter, "not a
  general PLY reader".

  **Decision (owner, 2026-10-04): broaden our PLY support to 64-bit.**
  `read_ply` gains PyMeshLab's layout (`double` x/y/z, `list uchar int`
  indices) as a second narrow dialect, so the cache keeps the decimator's
  float64 coordinates. `write_ply` also gains float64 output, so parts are
  written without casting down. Caveat: the only reader of part PLYs today
  is Blender (explicit-use repair), which stores coordinates as float32
  internally, so precision through Blender is lost whatever we write.
  Rejected alternative: writing the cache ourselves through float32
  `write_ply`.

  Reproduce with
  [tools/experiments/meshlab_import_probe.py](../../tools/experiments/meshlab_import_probe.py)
  (synthetic defect table, or a real FILE for the save/re-read check).

  Measured (2026-10-04): PyMeshLab saves the PLY whether or not decimation
  changed anything (`Sword_and_head1.stl`, 399,759 faces under the 900k
  target: 10.0 MB in 0.03 s). Re-reading it gives bit-identical vertices
  and faces (byte comparison, rechecked 2026-10-05). The PLY round trip adds
  and merges nothing. Defect probes (synthetic binary STLs) show what
  PyMeshLab's **import** does. Export changes no vertex or face counts
  (coordinates compared on the real file only):

  | Case | Faces in file | Import v / f | PLY round trip v / f |
  |---|---|---|---|
  | clean tetrahedron | 4 | 4 / 4 | 4 / 4 |
  | duplicate face | 5 | 4 / 5 | 4 / 5 |
  | duplicate face, reversed | 5 | 4 / 5 | 4 / 5 |
  | coincident duplicate shell | 8 | 4 / 8 | 4 / 8 |
  | degenerate face | 5 | 4 / **4** | 4 / 4 |
  | vertex 1e-6 from another | 5 | 6 / 5 | 6 / 5 |

  The importer welds exactly equal vertices only (as `mesh_io.load` does),
  keeps duplicate faces, and **drops degenerate faces**, which
  `mesh_io.load` keeps. That probably explains `Sword_and_head1.stl`
  importing 399,759 of its 399,762 faces (not confirmed face by face).

  Why the PLY cache matters beyond memory: decimation places merged
  vertices at newly computed float64 positions. The STL cache rounds them
  to float32 on write, and the reload weld then merges any that rounded to
  the same value, which changes the decimator's topology. A float64 PLY
  cache doesn't round and doesn't re-weld. How often this happens with the
  STL cache is not measured.
- **UNDECIMATED fallback.** Today it returns the loaded source mesh. Without
  our load, it would copy the source file instead (consistent with
  fallback markers being full source copies). That copy is a marker, not
  repair input or a PLY cache; keep the fallback's kind and diagnostics
  distinct. Decimation is best effort and can stay above `max_faces`, and
  `max_faces = 0` disables it, so "every repair input is ≤ `max_faces`"
  below is not guaranteed.
- **Cache location (owner, 2026-10-05): out of the source folder.** Today
  the cache is `<input>/stl-decimated/` (`indicators.DECIMATED_DIRNAME`),
  inside the input tree the batch scans. The PLY cache should go to an
  intermediate folder beside it instead, e.g. `<input>.decimated/`; the
  exact name is to settle while planning.
- **Existing cache.** The 15 GB of `stl-decimated/*.900000.stl` files become
  unused. Rebuild, or read both formats during a transition.
- **Sources at or under `max_faces`** still load through `mesh_io.load`
  (no decimation). Unchanged.

## Decision: float64 internal vertex buffer (owner, 2026-10-04)

Not implemented. `Geometry.verts` moves from float32 to **float64**. Do it
**after** decimate-from-file lands, as its own plan.

**Why.** float32 saves nothing when every consumer casts to float64. While
a library runs, we hold our float32 array *and* its float64 copy, so the peak is
1.5× plain float64 storage, plus the CPU to convert, several times per
mesh. Every library we use takes float64:

| Consumer | Takes vertices as | Today |
|---|---|---|
| PyMeshLab (`pymeshlab.Mesh`) | float64 | `meshlab.to_mesh` converts |
| libigl (winding: `fast_winding_number`, `AABB`, `bfs_orient`, `marching_cubes`) | float64 | `winding._validated` converts, in both `plan` and `reconstruct` |
| pymeshfix (`PyTMesh.load_array`) | float64 | `meshfix.py` converts |
| `scanner.volume` / `component_volume` | float64 math | `astype(np.float64)` |
| `jobmemory._area` | float64 math | `astype(np.float64)` |
| final STL write | **float32** | the only real float32 requirement |

Results also come back from each library as float64 and are rounded to
float32 by us (`winding._weld`, `meshlab.from_mesh`, MeshFix
`return_arrays`). So a mesh is rounded after every step instead of once at
the end.

**Why float32 was there, and why that no longer holds:**

- *File format*: binary STL is float32. That stays true at the edges only
  (load, final write).
- *Memory*: the real reason while full-size sources (up to 39.6M faces)
  passed through our process. After decimate-from-file, a mesh is
  ≤ `max_faces` before repair. The largest observed winding output
  (5.17M faces, Laura base) costs ~31 MB more in float64, against winding
  grids of several GB. The only float32 saving left is idle arrays (parts
  waiting or finished in the repairer): tens of MB.

**What stays float32:**

- **STL source weld** (`mesh_io.load`, sources ≤ `max_faces`): weld on
  the float32 bit patterns as today, *then* convert the welded vertex table
  to float64. Exact: float64 represents every float32.
- **Final STL write**: rounds to float32 once. Untouched source vertices come
  out bit-identical, because float64 → float32 returns the original value.
- **Blender** (explicit-use repair) stores float32 internally. Precision
  through it is lost whatever we write.

**What stays as it is:** face indices remain **int64** (libigl's type; see
the withdrawn int32 note above).

**Scope to plan** (from code reading, not exhaustive): `mesh_io.Geometry`
docstring/dtype, `mesh_io.load` (convert after weld), `mesh_io.write`
(cast at write), `read_ply`/`write_ply` (float64 dialect, already decided),
`meshlab.from_mesh`, `winding._weld` and `_validated`, `meshfix`,
`scanner`, `jobmemory`, anything else that assumes float32, and the tests that
pin dtypes. Re-fit the `jobmemory` per-face constants afterwards. They were
measured with float32 storage.

## Still to measure

- Stage-by-stage RSS of the current array path on Bat Girl (after load,
  `to_mesh`, `add_mesh`, decimation), to confirm the double VCG mesh in
  practice.
