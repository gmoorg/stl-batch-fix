# TODO

Open tasks only. Implemented behavior: [modules](modules.md) and
[pipeline](orchestration.md). Historical review evidence and test results are
[archived](../../archive/README.md). Remove completed tasks; update the owning reference.

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

- [ ] Build the end-to-end fixture set: a few models whose defects together
  cover open edges, non-manifold edges, inconsistent and globally inverted
  winding, oppositely wound shells, self-intersection, tiny debris shells, and
  a meaningful small part that must survive. Each test checks the published
  output and indicator, not only `open=0`/`nm=0`. Runtime: about 50 s per model
  through alpha-wrap regardless of shape — a 6,080-face sphere took 52 s, a
  1,232-face real foot 46 s (2026-10-02), because alpha scales with the model's
  own size and output is ~0.7–0.9M faces either way. Synthetic shapes are
  therefore not faster; keep the set small and run it with several workers.
- [ ] Keep the runner robustness tests, which use fake child scripts because
  real models cannot fail on demand: child crash, timeout, Ctrl+C (cleanup and
  second Ctrl+C), half-written output, recovery after a crash, and each file
  reported exactly once.
- [ ] Keep the config validation tests (`test_runconfig`, CLI config cases).
  They prove that a mistake in `batch_repair.toml` stops the run before any
  file is written, with a message naming the key: misspelled or unknown key,
  missing `input`/`output`/`max_faces`, wrong type (`workers = "4"`,
  `skip_clean = 1`), out-of-range value (negative, zero timeout, fraction
  above 1, `inf`), malformed TOML, unreadable file, NUL in a path, and that
  `batch_repair.example.toml` still lists every option with the code's
  default. They take milliseconds and need no fixtures.
- [ ] Then remove unit tests the end-to-end set covers, and all tests of tools
  absent from the default pipeline (welder, seam split, MeshLab filters,
  Blender repair, `open_loops_are_printable`, legacy `test_pipeline.py`).
  Justify each removal by the remaining coverage; never bless known geometry
  loss to make a test pass.
- [ ] Split the suite into fast tests (robustness, config) and the slow
  end-to-end set so the fast part can run on every change; update the test
  guide.

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
  lost-vertex tolerance, shell floor, retained-volume threshold, Blender
  timeouts); decide then whether any belong in `batch_repair.toml`.
- [ ] Consider moving step ordering into `pipeconfig`, per-step IDs, and explicit
  split/merge entries. Do not restore removed ENABLE switches.
