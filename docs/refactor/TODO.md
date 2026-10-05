# TODO

Open tasks only. Implemented behavior: [modules](modules.md) and
[pipeline](orchestration.md). Historical review evidence and test results are
[archived](../../../stl-batch-fix.old/archive/README.md). Remove completed tasks; update the owning reference.

## Priority (owner, 2026-10-05)

Work in this order; details in
[decimation-memory-path.md](../errors/decimation-memory-path.md). Done (2026-10-05): the
narrow PLY dialects; the PLY decimation cache beside the input folder
(decimate-from-file was measured and not adopted: PyMeshLab's STL reader
needs more memory than our loader).

1. [ ] **Chunked STL loader** (owner idea, 2026-10-05; order relative to
   float64 to confirm). `mesh_io.load` today reads the whole file in one
   `f.read()` (50 B/triangle), copies the corner coordinates (36 B/tri),
   then welds by sorting ALL corners (`lexsort` over a `(3F, 3)` uint32 view,
   `order`/`inv` int64): load peak ~147 B/triangle (Bat Girl 4.83M: 0.77 GiB).
   Proposed, all NumPy:
   - read fixed-size chunks (e.g. 1M triangles) straight into float32 —
     no raw-file buffer;
   - per chunk: drop triangles with a NaN/inf coordinate and triangles whose
     corners coincide (equal float32 bits after the -0.0 fold == index-
     degenerate after an exact weld); COUNT both and carry the counts so
     `scanner.scan`'s result reports them (owner: "count them, drop them,
     add to scanner result");
   - weld incrementally: sort + dedupe the chunk, `np.searchsorted` it
     against a sorted table of unique vertices (each with its fixed index
     from first sight), reuse matches, merge new vertices in with one
     `np.insert` block copy per chunk; write the chunk's face indices.
   Memory then follows the unique vertices (~F/2) plus one chunk, not all
   corners; float64 would only widen the vertex table (~12 B/triangle).
   Must give a bit-identical result to today's loader (measure on Bat Girl:
   memory, time, identical vertices/faces). Does not lower the job peak
   (PyMeshLab mesh build 1.55 GiB, decimation 2.05 GiB on Bat Girl), only
   the load stage. If degenerates are dropped here, `meshlab.to_mesh`'s drop
   stays as the guard for other array paths.
   A compiled hash-table weld (C via `cffi` + gcc, both present; or Numba,
   not installed) is the fallback if the NumPy version is not enough.
2. [ ] **float64 `Geometry.verts`**, as its own plan. Then re-fit
   `jobmemory.prepare_bytes` (calibrated on the old array path). Weld stays
   on float32 bits; convert the welded table (~12 B/triangle).
3. Everything else: volume guard on open shells
   ([volume-loss-rejected.md](../errors/volume-loss-rejected.md)), recording
   the crash signal, MeshFix time/NM guard, the NM-only fast path below.

## Reconstruction

- [ ] Winding-number reconstruction (`libs/winding.py`, default part step
  since 2026-10-03) follow-ups, details in [reconstruction](reconstruction.md):
  more broken models (large holes); stream each block's output to lower the
  memory floor; avoid the per-block winding-number octree rebuild on large
  inputs. Post-reconstruction decimation is bounded by no budget (memory and
  time follow ~3·A/h² rebuilt faces; sphere r 132: 29 M faces, 353 s,
  13.9 GB) — admission reserves for it, reducing it needs an owner decision
  (e.g. per-part spacing from area, or decimating blocks before the weld).
- [ ] NM-only fast path in the clean gate (owner idea, 2026-10-05). Today
  `repairer.is_already_clean` (with `skip_clean`) skips repair only when a
  part has no NM edges, no open edges and no winding seams; anything else
  goes through winding → decimate → conditional MeshFix. Proposal: when NM
  edges are the ONLY defect found, skip winding and the post decimation and
  run MeshFix directly; any other defect still goes to winding. Cheaper and
  keeps the original surface instead of a rebuilt one. To settle first:
  - "Only NM" is limited by what the scan sees: self-intersections,
    overlapping shells and a whole inverted shell are invisible to it, and
    `winding_seams` cannot check winding across the NM edges themselves.
  - MeshFix fixes NM by deleting faces around them and refilling. With many
    NM edges it can run past the per-file timeout (the Aloy/Laura cases had
    thousands), and on irreconcilable winding it can delete whole surfaces.
    So: a shape check on its result (retained volume, open/NM left), and on
    failure fall back to winding rather than failing the part.
  - Possibly an NM-count limit for the fast path; measure MeshFix time
    against NM count first.

## Print-risk check and rib supports

`check_3mf.py` / `support_3mf.py`, see [modules](modules.md#print-risk-check-separate-from-repair).
The first rib output (`/mnt/sda2/ank.supported.3mf`, 2026-10-04) is not yet
confirmed by a test print: Bambu loading, ribs in the sliced preview and
removal are all unverified.

- [ ] Triangular ribs for easier removal: taper each rib's cross-section to
  a narrow edge at the model instead of a full-width rectangular wall, so it
  touches along a thinner line. In the 0.1–0.44 mm band a rib is only 1–2
  layers tall, so a taper there may not survive slicing — decide together
  with the lift idea below, which makes ribs tall enough to taper.
- [ ] Tilt search: try orientations within ±5° and pick the one with the
  most plate contact / least unsupportable area. A first measurement on the
  ank body (2026-10-04) gave 0.7 → 14.4 mm² of contact at a 3.3° tilt, at
  the cost of more unsupportable area (6.5 → 12.5 mm²); the trade-off rule is
  open.
- [ ] Lift the whole model so the ribs can be joined: with the model raised,
  ribs become tall enough to stand on a shared connecting base, so they come
  off as one piece instead of single-line ribs each fused to the plate where
  they may stay stuck. Needs a lift height (and how it interacts with Bambu
  dropping objects to the bed) and the base's shape.
- [ ] Look online for existing tools that already do this (generated or
  custom breakaway supports for FDM, near-plate undersides, 3MF
  post-processing) before extending ours.

## Tests

Target: end-to-end coverage — real `batch_repair.py` runs over a few fixtures
that each combine many defects — replacing per-tool unit tests of geometry.

- [ ] Extend the end-to-end set. `test_end_to_end` runs four defect
  composites (`defect_spheres.composites()`) through the real two-pass batch
  and checks shape, volume, debris, the rod tip and log attribution. Still
  missing: a meaningful small part that must survive, self-intersection, and
  real broken models (large holes).
- [ ] Per-step outcome tests (shape kept by each step on defect spheres):
  done for the decimator (`test_decimator.TestShapeIsKept`), not yet for
  winding, MeshFix or the split.
- Keep (not a task): the runner robustness tests with fake child scripts
  (crash, timeout, Ctrl+C, half-written output, recovery, exactly-once
  reporting, both passes) and the config validation tests — real models
  cannot fail on demand, and config mistakes must stop a run before any write.
- [ ] Then remove unit tests the end-to-end set covers, and all tests of tools
  absent from the default pipeline (welder, seam split, MeshLab filters,
  Blender repair, `open_loops_are_printable`).
  Justify each removal by the remaining coverage; never bless known geometry
  loss to make a test pass.
- [ ] Split the suite into fast tests (robustness, config) and the slow
  end-to-end set so the fast part can run on every change (the full suite
  takes ~8 min); update the test guide.

## Deferred (not critical now)

- [ ] Low priority: check that initial decimation keeps meaningful detail.
  Detail too small to survive decimation is usually too small to print, so
  this matters only for thin but long features — antennae, sword blades,
  fingers, cables — which can be printable yet lose their tips or break into
  pieces when the face budget is tight. Total retained volume cannot show
  this: a lost antenna is a tiny share of the volume.
- [ ] Evaluate the `is_already_clean` gate on real models (`skip_clean = true`):
  confirm Amidara base fails it and that gated output slices and prints. It
  may not be used at all; keep it off by default.
- [ ] How often MeshFix is needed after decimation: read it from `batch.log`
  after a run over the full collection (the `meshfix` step records whether it
  ran).
- [ ] Move step tuning values into `pipeconfig` (MeshFix clean/fill parameters,
  lost-vertex tolerance, retained-volume threshold, Blender timeouts); decide
  then whether any belong in `batch_repair.toml` (the shell floor already is:
  `min_shell_faces`).
- [ ] Job memory calibration has no MeshFix-heavy multi-part model yet
  (orchestration.md "Job memory"); add one when such a model turns up.
- [ ] Consider moving step ordering into `pipeconfig`, per-step IDs, and explicit
  split/merge entries. Do not restore removed ENABLE switches.
