# Tests and fixtures

Run project Python commands through `tools/project_python.sh`; it selects the project environment and repository root regardless of the caller's current directory.

```bash
tools/project_python.sh -m unittest discover -s tests/tests -t . -q -p 'test_*.py'
tools/project_python.sh -m unittest tests.tests.test_welder
tools/project_python.sh tests/tests/make_fixtures.py --check
```

## Layout

- `tests/tests/test_<module>.py`: focused `libs` contracts and algorithms.
- `test_repair_pipeline.py`: refactor geometry sequence against known controls.
- `test_regression_fixtures.py`: guards source geometry of review probes.
- `make_fixtures.py`: generates/checks the model-loss probes in `tests/probes/`.

## What tests must prove

- Assert intended geometry survives, not merely `open=0` and `nm=0`.
- Verify step order and that every split part reaches repair independently.
- Compare controlled defects with a known-correct surface.
- Separate wrapper tests from policy tests; native tool success is not pipeline success.
- Use real Blender/PyMeshFix only where a stub cannot prove the boundary.
- Cover scale, opposite winding, component retention, invalid input, timeout/crash, atomic writes, and exactly-once reporting.

High-priority probes are `small_valid_shell.stl`, `opposite_volume_shells.stl`, `decimation_lost_appendage.stl`, and `reversed_tjunction_chain.stl`. Their construction is tested; pipeline verdict tests remain missing.

## Defect-sphere fixtures

`tests/tests/defect_spheres.py` is the single source of defect meshes with a
known true shape (`fixtures()`, 22 fixtures). The single-defect probe
builders moved there from `tools/make_probe_meshes.py`, which re-exports
them; committed `tests/probes/sphere_*.stl` stay byte-reproducible (tested).

Each `Fixture` carries `truth`: the clean, closed, outward shells whose
UNION is the intended printable shape. Shape tests compare against the
union's BOUNDARY — buried faces of overlapping or touching shells excluded
— via `union_boundary_samples`, against the clean polyhedron (the UV sphere
sags ~0.12 mm), with `truth_volume` (union volume, computed numerically) and
`vanish_boxes` that must end up empty (debris).

Fixtures: control `correct`; winding `inverted`, `inverted_third`, `seam`;
open edges `tjunction`, `tjunction_many`, `hole`; non-manifold `fin`
(also open), and NM as the only scanned defect `nm_only` (20 closed fins:
two back-to-back triangles per sphere edge, exactly 20 NM edges, 0 open,
0 seams), `nm_seam` (+ reversed cap), `nm_hole` (+ 12 faces removed);
`degenerate`; duplicated `doubles`; multi-shell `two_shells`,
`shell_inverted`, `overlapping_shells` (union), `touching_shells` (boxes
sharing a plane); debris `debris_sheet` (open), `debris_speck` (closed,
below the splitter floor — intent, not a splitter rule); `thin_rod` (ONE
shell: sphere + attached rod r 0.75); `inch_scale`; and `allbad`.
`test_defect_spheres.py` checks each declares-and-has its defects, truths
are clean, volumes and the union boundary are right (analytic boxes), and
geometric facts hold. A decimation-plateau fixture is deferred to the
per-step tests (item 9).

## Inventory for the outcome-test rework (2026-10-04)

Goal (TODO "Tests"): end-to-end runs of `batch_repair.py` on defect-sphere
fixtures, plus per-step tests with the fixtures each step fixes, asserting
outcomes (published, closed, radius/volume tolerance, debris gone,
indicator) — not internals. A scripted pass over 791 tests flagged 68 that
assert only internals (mocks, call counts, step names, fakes); by hand they
fall into three groups. Removal waits until the replacement exists.

**1. Forced failure paths — keep.** Mocks force conditions no fixture can
produce; they protect behaviour, not composition.

- Tool failure → result/step failure, not a crash: `test_alphawrap`
  (empty result), `test_decimator` (failure recorded), `test_meshfix`
  (returned failure), `test_meshlab` (filter exception), `test_blender`
  (returned failure / raised exception reported), `test_winding` (open
  result rejected, invalid input rejected before native calls, failures are
  step results).
- Libraries are not tested (owner, 2026-10-05): they are checked once when a
  run starts (`libs.dependencies`) and never break during development. No
  test checks availability or a missing library; required libraries are not
  skip-guarded. CGAL is optional, so `test_alphawrap` and the alpha-wrap
  tests in `test_repairer` skip without it.
- I/O and runner failures: `test_converter` (companion copy fails, is
  counted once, interrupted copy leaves nothing, each mesh emitted once),
  `test_batch_repair_cli` (config rejected before intake, malformed TOML,
  intake exception incomplete, companion copy failure, model log
  unwritable / unopenable), `test_batch_repair_progress` (launch failure
  elapsed), `test_batch_repair_run` (SIGINT during pool start),
  `test_runstate` (spawn/cancel race), `test_runconfig` (`resolve`: autos,
  fraction, zero budget, sysconf unavailable; example lists every field).

**2. Configuration plumbing — keep until end-to-end covers it.** A value
reaching the place it is used: `test_batch_repair_run` gate flag to
`_spawn_child`; `test_batch_repair_cli` GB → bytes per child;
`test_winding` configured budget used. Replace with end-to-end runs whose
config changes the outcome (e.g. `skip_clean`, a tiny budget →
`BudgetError` in the log), then delete.

**3. Pipeline composition pins — replace, then delete.** These encode HOW
the pipeline is assembled, so every refactor rewrites them:

| Test (test_repairer.py) | Protects | Outcome replacement |
|---|---|---|
| `TestSequence` (order, one call per part, part whole, destination kept, single shell not split) | each part reaches repair intact; merge keeps destination | end-to-end: multi-shell sphere fixture → every shell present, published at the right path |
| `TestPartIdentity` (part ids, `-` for split/merge) | log attribution | end-to-end: `batch.log` lines carry `1/2`, `2/2` |
| `TestBlenderBeforePymeshfix.test_production_sequence_is_winding_decimate_meshfix` | default step list | end-to-end outcome on defect fixtures (sequence names unnecessary) |
| `TestAlphaWrapBinding` (whole-mesh spacing, caps, custom tool bypasses CGAL) | spacing from WHOLE mesh, not per part | per-step: two shells of different size rebuilt with the same `h` (equal bevel / face density) |
| `TestPartDecimation` (7) | one pass per part with its own target, skipped within target, never fails on a miss, a decimator error fails before MeshFix | per-step: covered for shape by `test_decimator.TestShapeIsKept` (rebuilt rod tip, round sphere); end-to-end `c_multishell_rod` |
| `TestContract` (no remove-T-vertices, no re-orient filter) | removed MeshLab filters stay out | none needed — those tools are out of the default pipeline; delete with the unwired-tool suites |
| `TestFailure` (default tool / explicit alpha-wrap propagate failure; closing measurement failure) | a failed part fails the repair | keep the closing-measurement one (forced failure); the other two are covered by `test_winding`/`test_alphawrap` step-failure tests |
| `TestCleanGates.test_model_skip_keeps_the_non_finite_guard` | NaN guard on the skip path | keep (forced failure) |

**Suites outside the default pipeline** (decide with item "remove tests of
unwired tools"): `test_welder` (39), `test_meshlab` (5), alpha-wrap now explicit-use
(`test_alphawrap`, 20), Blender repair parts of `test_blender`.

Not a target: frozen-dataclass checks (10) — they protect immutability,
which nothing else checks, and never churn. Exact duplicates were removed
2026-10-04 (`test_welder` endpoint/clean-mesh, `test_decimator`
fastsimp-failure).
