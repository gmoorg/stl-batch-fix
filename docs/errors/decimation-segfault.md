# Segfault in PyMeshLab initial decimation

## Symptom

The child process dies with `Fatal Python error: Segmentation fault` during the
first (whole-model) `decimate` step, before split or reconstruction runs. The
parent writes a `.failed.stl` fallback marker with the reason
`crashed: parent wrote fallback marker ...`.

`batch.log` has only the `start decimate` line and no reason. The faulthandler
traceback appears only in the per-model `.log`:

```text
Fatal Python error: Segmentation fault
  File ".../libs/meshlab.py", line 53 in apply_filters
  File ".../libs/decimator.py", line 69 in _decimate_meshlab
  File ".../libs/decimator.py", line 99 in decimate
  ...
  File ".../libs/processor.py", line 224 in decimate_initial
  File ".../batch_repair.py", line 436 in _prepare_one_file
```

The filter is `meshing_decimation_quadric_edge_collapse` with only
`targetfacenum = 900000`, everything else at its default.

## Cause (verified 2026-10-04): degenerate faces on the array path

A **degenerate face** has two corners on the same vertex index after welding,
for example `[a, b, a]`. `mesh_io.load` keeps such faces. `meshlab.to_mesh` then
builds a `pymeshlab.Mesh` from our arrays, which does no cleaning, so quadric
decimation receives them and segfaults. PyMeshLab's own file importer
(`load_new_mesh`) drops degenerate faces, so the same file decimates fine
when loaded that way.

Evidence, every run one process, nothing else running:

| File | Faces | Degenerate | Our array path | Same, degenerate faces removed | PyMeshLab import kept |
|---|---|---|---|---|---|
| Base_Pillar_R | 999,969 | 1 | **segfault** | OK → 900,000 (4.3 s) | 999,968 |
| bodynsfw | 1,179,616 | 6 | **segfault** | OK → 899,999 (6.1 s) | 1,179,610 |
| face | 1,216,534 | 41 | **segfault** | OK → 900,000 (6.8 s) | 1,216,493 |
| legs | 1,626,802 | 10 | **segfault** | OK → 900,000 (12.0 s) | 1,626,792 |
| whole-costume02 | 2,003,376 | 2 | **segfault** | OK → 900,000 (19.5 s) | 2,003,374 |
| Mandy_Body | 2,061,994 | 10 | **segfault** | OK → 899,999 (22.2 s) | 2,061,984 |
| Bat Girl Merge | 4,834,335 | 1 | **segfault** | OK → 899,999 (67.7 s) | 4,834,334 |

Control: four files that decimated fine in the batch run (torso_hood
3.65M, Bat Girl Head 3.84M, Blouse 3.97M, Merged_Sfw 3.89M) have **zero**
degenerate faces. On every crash file, the PyMeshLab importer keeps exactly
`faces − degenerate`, so it drops precisely the degenerate faces.

This rules out what was suspected earlier:

- *Running alongside other jobs / memory pressure*: the crash reproduces with
  the file alone, and the 16-face crop below still segfaults (rechecked
  2026-10-05: exit 139; without the degenerate face it decimates), so a
  failed allocation can't explain it. The kernel also logged no OOM during
  the run's crash window (15:15–15:40).
- *Input geometry as such*: the same files decimate fine without the
  degenerate faces.

## Minimal reproductions

- **Real, 16 faces:** `Base_Pillar_R` cropped to faces within 0.1 mm of its
  degenerate face still segfaults; the same crop without that face decimates.
  Crops of 16 → 999,969 faces all crash. In the 16-face crop, the degenerate
  face `[6, 9, 6]` uses two vertices no other face touches (an isolated
  zero-width segment, 0.048 mm long).
- **Synthetic, 5 faces:** a clean tetrahedron plus one isolated degenerate
  face `[P, Q, P]` on two new vertices segfaults. Synthetic results:

  | Case | Result |
  |---|---|
  | icosphere (81,920 faces) | OK |
  | + degenerate face on an existing edge `[V0, V0, V1]` | OK |
  | + "bridge" `[a, b, a]` between existing non-adjacent vertices (2 rings or antipodal) | OK |
  | + **isolated** `[P, Q, P]` on two new vertices | **segfault** |
  | + reversed duplicate face pair only | OK |
  | tetrahedron + isolated `[P, Q, P]` (5 faces) | **segfault** |

  So not every degenerate face crashes. An isolated one reliably does. In the
  full `Base_Pillar_R` mesh, the degenerate face's vertices are shared with
  other faces (its a–b edge has no other face). That is more like the bridge
  case, which didn't crash on the sphere. The exact condition in VCG isn't
  pinned down. Removing all degenerate faces fixed all seven real files.

The same minimal STLs through PyMeshLab's **file interface**
(`tools/experiments/meshlab_file_decimate.py`) don't crash. The importer drops
the degenerate face and keeps its now-unreferenced vertices, and decimation
runs normally. So stray vertices are harmless, and the face itself is what
crashes:

| File | Faces in file | Loaded v / f | Decimated v / f |
|---|---|---|---|
| 16-face real crop | 16 | 16 / **15** | 8 / 7 |
| tetrahedron + isolated `[P, Q, P]` | 5 | 6 / **4** | 3 / 2 |
| icosphere + isolated `[P, Q, P]` | 81,921 | 40,964 / **81,920** | 5,002 / 10,000 |
| icosphere + isolated + reversed duplicate pair | 81,923 | 40,964 / **81,922** | 5,002 / 10,000 |

Reproduce with
[tools/experiments/segfault_probe.py](../../tools/experiments/segfault_probe.py)
(modes `count | array | clean | synth CASE | crop`, one process per run) and
the 16-face real crop
[tools/experiments/data/segv_min16.stl](../../tools/experiments/data/segv_min16.stl)
(`array` exits 139, `clean` decimates).

## Can winding or alpha-wrap produce this defect?

Question from the owner (2026-10-04). It matters because PyMeshLab also
decimates reconstruction output, from arrays, after `winding` (default
`DEFAULT_PART_STEPS`: winding → decimate → meshfix).

**Index-degenerate faces (the verified crash): no.**

- **Winding: excluded by code.** `winding._weld` drops every face whose
  corners merged (`Fo[(Fo[:,0] != Fo[:,1]) & (Fo[:,1] != Fo[:,2]) & ...]`),
  and `winding._check` fails the reconstruction if `scanner.scan` counts any
  degenerate face. Post-reconstruction decimation never receives one.
- **Alpha-wrap (explicit-use): excluded by construction, not checked.**
  Output comes from CGAL's `Polyhedron_3`, which `alpha_wrap_3` guarantees
  is a watertight, orientable 2-manifold. A triangle facet there has three
  distinct vertices. `alphawrap.py` doesn't verify it, though; it checks only
  for empty output and non-triangular facets.

**Open: zero-area faces with three distinct indices.** Both modules compute
in float64 and cast vertices to float32 when building `Geometry` (`_weld`:
`Vo.astype(np.float32)`; `alphawrap._polyhedron_to_geometry`). Two distinct
vertices that round to the same float32 point give a face with distinct
indices but zero area. `scanner.scan` doesn't count that as degenerate, and
whether it crashes VCG's quadric decimation is **untested**. Only repeated
indices were tested. A synthetic probe would answer it: a face with two
distinct vertex indices at identical coordinates, passed as arrays. The
planned float64 `Geometry` removes this rounding.

**Other array inputs.** The explicit-use `meshlab.step_*` filters
(clean/orient) receive split parts of the *source*, which can carry the
source's degenerate faces. Not used by the default pipeline.

## Fix direction (not implemented)

Remove degenerate faces before the mesh reaches PyMeshLab's decimation. It's
lossless: a degenerate face has zero area and contributes nothing printable.
Candidates are `mesh_io.load`, which would then match PyMeshLab's importer,
or the decimator. The planned decimate-from-file path (see
[decimation-memory-path.md](decimation-memory-path.md)) gets this for free
from the importer. Arrays reaching PyMeshLab from other places
(post-reconstruction decimation, `meshlab.step_*`) would still need it.
Winding output is already checked for degenerate faces (`winding._check`).

Regression fixture candidates: the 5-face synthetic case, and the 16-face real
crop.
