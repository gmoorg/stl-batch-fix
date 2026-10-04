# TODO

Open tasks only. Implemented behavior: [modules](modules.md) and
[pipeline](orchestration.md). Historical review evidence and test results are
[archived](../../../stl-batch-fix.old/archive/README.md). Remove completed tasks; update the owning reference.

## Reconstruction

- [ ] Winding-number reconstruction (`libs/winding.py`, default part step
  since 2026-10-03) follow-ups, details in [reconstruction](reconstruction.md):
  more broken models (large holes); stream each block's output to lower the
  memory floor; avoid the per-block winding-number octree rebuild on large
  inputs. Post-reconstruction decimation is bounded by no budget (memory and
  time follow ~3·A/h² rebuilt faces; sphere r 132: 29 M faces, 353 s,
  13.9 GB) — admission reserves for it, reducing it needs an owner decision
  (e.g. per-part spacing from area, or decimating blocks before the weld).

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
