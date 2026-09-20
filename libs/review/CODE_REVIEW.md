# `libs/` code review

Reviewed 2026-09-18. Scope: `libs/` only. Paths and line numbers refer to the reviewed working tree and can drift. Findings are open for discussion; add a note under **Your comment** for any ID. Severity reflects the evidence stated, not an agreed fix.

## FDM acceptance goal

The deliverable is an STL that a slicer can use to print the **intended model**. Edge counts, signed volume, and triangle count are evidence toward that goal, not definitions of it. A closed mesh missing a part can still be a failed print; a small separate shell may be a required feature. The [pipeline reference](../../docs/refactor/pipeline.md) records component preservation and the final face count as acceptance concerns. The user rescales models after repair, so a fixed authoring-unit threshold cannot establish final printable feature size. Where intent, placement, or print orientation is unknown, surface the uncertainty rather than silently deleting geometry.

## Why the current order exists

The [pipeline reference](../../docs/refactor/pipeline.md) records why orientation moved from a guarded whole-mesh pass to an unconditional **per-part** pass after splitting. The current code performs the following limited fixes in order:

| Stage | Specific failure it addresses |
|---|---|
| Decimate | Meet the slicer's face budget before expensive repair; it may introduce defects that later stages must repair. |
| Weld T-junctions | Split faces whose open edge skips vertices on the neighboring edge path, without deleting vertices. |
| CLEAN | Remove null and duplicate geometry and merge close vertices before the shell split, so coincident copies are visible together and hidden defects are exposed. |
| Split shells | Keep separate edge-connected parts out of one PyMeshFix call, which otherwise can discard all but one surface. |
| Orient each part, then PyMeshFix | Fix local inversions before hole repair; doing orientation before splitting erased seam evidence in measured fixtures. PyMeshFix then repairs each prepared part. |
| Merge and judge | Reassemble parts; test destructive loss before topology; write a clean result or a full-mesh fallback marker. |

The split produces independent `Mesh` values. Each retained part is oriented and passed to its repair tools separately, and the repaired parts are merged only after those calls finish. This separation prevents one PyMeshFix call from choosing one surface and discarding other shells.

The table describes the current default, not the complete target. `repairer.blender_part` already provides per-part Blender repair through PLY, but `repairer.repair` currently exposes it only as a wholesale replacement for PyMeshFix. Blender must appear in the final per-part route: measurements favor it for holes and fins, while PyMeshFix performs different work on non-manifold geometry and winding seams. The missing decision is the defect-based selector and the order for a part that needs both tools.

The CLEAN-before-split order was rechecked. Keep it: vertex merging exposes coincident duplicate faces, and splitting first can hide the copies in separate shells. The `doubles` regression demonstrated the consequence: split-first retained two shells at about 200% volume, while clean-first produced the intended control surface. The close-vertex merge could still join intended nearby shells; cover that risk with a focused fixture and preservation gate rather than moving the entire CLEAN stage after splitting.

`by_seams` and Blender routing are not in this default sequence. Findings below target failures that these stages do not catch; they are not a proposal to replace the ordered repair sequence.

## Findings in the current path

### R01 — Converter exceptions disappear

`libs/converter.py`:L141–165: 🔴 bug: `convert_one` exceptions are swallowed by `Pool` because `next_item` ignores `error`; `prepare` returns with no emitted mesh and no failure count. Convert exceptions into invalid `Mesh` results, or handle `error` in the selector and emit the failed source. Reproduced with a converter that raises: one scanned, zero emitted, zero failed.

**Your comment:** _Add your note here._

### R02 — Failed PyMeshFix call is reported as success

`libs/repairer.py`:L199–201,L315–330: 🔴 bug: `_repair_part` turns a failed PyMeshFix call into `(part, "pymeshfix failed")`, but `repair` treats every returned tuple as success and sets `Result.ok=True`. Carry an explicit success flag from the part tool and return a failed result or try the intended fallback. Reproduced by forcing `meshfix.repair` to fail on a clean tetrahedron: `Result.ok=True` and `problem=None`.

**Your comment:** _Add your note here._

### R03 — Zero-triangle STL crashes load

`libs/mesh_io.py`:L283–285: 🔴 bug: a valid binary STL header with zero triangles reaches `new[0]` on an empty array and raises `IndexError`; `probe` had marked it valid. Reject zero face input at probe/load or return an explicit invalid `Mesh`. Reproduced with an 84-byte STL.

**Your comment:** _Add your note here._

### R04 — Welder distance boundary is unresolved

`libs/welder.py`:L172–231: 🟡 unresolved boundary: the deliberate path-and-strip rule accepts any perpendicular distance from the spanning edge. Moving the midpoint in `test_welder.with_tjunction()` 10 units off a roughly 1.4-unit edge still gives one edge-connected shell, one hit, and a topologically clean result after repair. This proves the detector has no distance bound, but does not by itself prove the split is wrong: the [module reference](../../docs/refactor/modules.md#welder) records that real junctions can bend off the line. Determine what additional geometry, if any, distinguishes an intended bent edge from a hole before changing the rule.

**Your comment:** _Add your note here._

### R05 — Small shells can be discarded

`libs/splitter.py`:L78–88: 🟡 risk: every shell below 100 faces is discarded when a larger shell exists, without testing its size or meaning. This participates in the demonstrated model-loss case below. Retain discarded shells for review or make a dropped component prevent a clean result until a geometry-based debris rule is proven.

**Your comment:** _Add your note here._

### R06 — Decimation loss is not judged

`libs/processor.py`:L56–80,L116–129: 🟡 risk: the destruction check uses `repairer.Result.volume_in`, which is measured **after** decimation; the original `source` argument is unused, so decimation loss is never judged. Compare the original and decimated mesh before repair, with a separate outcome for destructive decimation.

**Your comment:** _Add your note here._

### R07 — PyMeshFix output capture can race

`libs/meshfix.py`:L65–78: 🟡 risk: `_Capture` replaces process-wide stdout/stderr file descriptors without a cross-thread lock; overlapping repairs can nest redirections and restore a descriptor another thread has already closed. Serialize the whole capture window or capture inside an isolated process.

**Your comment:** _Add your note here._

### R08 — Blender runner can lose a live handle

`libs/blender.py`:L136–149: 🟡 risk: concurrent calls on one `Runner` overwrite `_current`, and either call's `finally` can clear the other handle; `kill_current()` can miss a live Blender despite the class claiming multithread use. Track all active processes under the lock or enforce one run per instance.

**Your comment:** _Add your note here._

### R09 — Selector errors can disappear

`libs/pool.py`:L79–83: 🟡 risk: an exception from `item_selector` kills a worker thread, while `start()` joins and returns without raising it to the caller. Capture selector exceptions and surface them after join, or define an explicit failure callback.

**Your comment:** _Add your note here._

### R10 — Failed-result measurements can describe the wrong mesh

`libs/repairer.py`:L326–343: 🟡 risk: after a later step fails, `_failed(mesh)` may carry modified intermediate geometry but reports `faces_out=faces_in` and `volume_out=volume_in`. Measure the carried mesh or return the original input so the result fields describe the same mesh.

**Your comment:** _Add your note here._


## Algorithm and design review

This pass traced the current decisions through `mesh_io → decimator → welder → repairer → processor → indicators`, including the documented counterexamples. These are additional findings, with the exact extent of each reproduction stated.

### A01 — Reversed welder path leaves open edges

`libs/welder.py`:L202–232,L236–258: 🟡 repair limit: the open-edge walk does not require path vertices to progress along the spanning edge, but then sorts the chain by projected position before splitting. If the path runs `a → M1(t=2/3) → M2(t=1/3) → c`, the new face edges no longer match the path. Reproduced on the two-vertex sphere fixture with the inserted vertex positions exchanged: `find` reports one hit, `repair` adds two faces, and all four open edges remain. Reject or explicitly handle a non-monotone path; do not silently reorder it. The final `processor` scan should reject this as `OPEN_EDGES`; no false clean output was demonstrated. This is separate from the deliberate choice to permit a bent path off the edge line.

**Your comment:** _Add your note here._

### A02 — Signed-volume cancellation hides model loss

`libs/repairer.py`:L117–127, `libs/splitter.py`:L78–88, and `libs/processor.py`:L71–96: 🔴 demonstrated model loss: total **signed** input volume can nearly cancel across oppositely wound shells, while the splitter drops a small-face-count but large-volume shell; `volume_kept` then becomes huge and passes the `< 0.90` loss check. In a full `processor.process` run, a 760-face control sphere plus a four-face tetrahedron with equal opposite volume became the sphere alone (764 → 760 faces, two → one shell), yet returned `PROCESS` and reported 687,736,341% volume kept. A two-tetrahedron decision-only case with exactly zero input volume also returned `PROCESS` after one shell was removed because `volume_kept` returns 1.0 for a zero denominator. Compare component retention and use a volume measure that cannot cancel; treat zero or near-zero signed totals as indeterminate.

**Your comment:** _Add your note here._

### A03 — Non-finite geometry can pass the decision gate

`libs/mesh_io.py`:L274–294, `libs/scanner.py`:L106–122,L309–320, and `libs/processor.py`:L71–96: 🔴 validation gap: a binary STL with the same NaN coordinate at every use of one vertex loads as valid, scans as `open=0, nm=0`, and has NaN volume. A `repairer.Result` with NaN `volume_out` then bypasses the `< 0.90` check and `_decide` returns `PROCESS`. Reject non-finite coordinates and measurements before any success decision. The complete default repair path on that file was not exercised.

**Your comment:** _Add your note here._

### A04 — Interrupted writes can look complete

`libs/mesh_io.py`:L322–328, `libs/processor.py`:L145–166, and `libs/indicators.py`:L104–111: 🔴 retry bug: deliverable STLs and markers are written directly at their final names; an interruption after creation leaves a partial file that the next run treats as `ALREADY_FIXED` or a completed marker. A seven-byte fake output was classified `ALREADY_FIXED`. Write to a sibling temporary file, verify/flush it, then replace the final path; apply the same rule to copied markers.

**Your comment:** _Add your note here._

### A05 — OBJ and STL source identities collide

`libs/converter.py`:L50–56 and `libs/indicators.py`:L76–86: 🟡 identity collision: `model.obj` and `model.stl` in the same source directory map to the same output and export paths. Both equalities were reproduced. If both sources are admitted, one result can overwrite or be mistaken for the other. Detect collisions before emission or define an unambiguous naming policy.

**Your comment:** _Add your note here._

### A06 — Companion-copy error can abort preparation

`libs/converter.py`:L95–109: 🟡 design mismatch: the [orchestration contract](../../docs/refactor/orchestration.md) requires one copy error to affect only its item, but `prepare` calls `shutil.copy2` inline without handling failure. A simulated copy error raised out of `prepare`. Isolate the failure and emit one result for that source.

**Your comment:** _Add your note here._

### A07 — Edge topology alone can approve zero-area geometry

`libs/scanner.py`:L99–122 and `libs/processor.py`:L82–96: 🟡 acceptance limit: `Scan.is_clean` means only that indexed edges have two owners; it does not establish a geometric solid. Four faces with distinct indices but all vertices on one line give `open=0, nm=0, degenerate=0, volume=0`, and a synthetic repaired result is approved by `_decide`. Check zero-area geometry and a meaningful finite volume before using “clean” as a shipping verdict. Default tool behavior on this fixture was not tested.

**Your comment:** _Add your note here._

### A08 — Final face budget is unchecked

`libs/processor.py`:L116–129,L82–96: 🟡 final slicer-budget gap: decimation runs before repair, and the success decision never checks the **final** face count against `max_faces`. On the committed `sphere_tjunction.stl` probe, `process(max_faces=761)` accepted a 762-face result as `PROCESS`: welder repaired the junction and added one face after decimation was skipped. The documented Bambu limit is approximate, so one face over is not evidence of an unprintable model; the missing check matters if later repairs add enough faces to cross the practical limit. Measure final counts near that limit and reserve headroom or report an over-budget result; blindly decimating a repaired mesh again can recreate defects.

**Your comment:** _Add your note here._


## Verification limits

The zero triangle, converter exception, and PyMeshFix failure findings were reproduced with small inputs. The algorithm section states which additional findings were reproduced and which were tested at a decision boundary only. Using `../.venv/bin/python`, all **391 selected `libs/` unittest cases passed**, and full discovery passed **427 tests** during the review. After adding four source-fixture characterization tests, full discovery passed **431 tests**. No collection-wide run or proof of correctness for every mesh class was performed. An initial system-Python run gave missing-dependency errors, but that interpreter is not the project's environment.

## Proposed verification runs

Run these from the repository root with the project environment. The first command verifies fixture construction without asserting that today's pipeline behavior is correct. The focused commands should run after changes in the corresponding modules, followed by full discovery.

```bash
# Generate or validate committed STL fixtures
../.venv/bin/python tests/tests/make_fixtures.py --check
../.venv/bin/python -m unittest -v tests.tests.test_regression_fixtures

# Input, scheduling, and output-state findings
../.venv/bin/python -m unittest -v \
  tests.tests.test_converter tests.tests.test_mesh_io \
  tests.tests.test_pool tests.tests.test_indicators

# Geometry, repair, and decision findings
../.venv/bin/python -m unittest -v \
  tests.tests.test_decimator tests.tests.test_splitter \
  tests.tests.test_welder tests.tests.test_meshfix \
  tests.tests.test_repairer tests.tests.test_processor \
  tests.tests.test_repair_pipeline

# Blender concurrency findings
../.venv/bin/python -m unittest -v tests.tests.test_blender

# Required final gate
../.venv/bin/python -m unittest discover -s tests/tests -t . -q -p 'test_*.py'
```

Add these end-to-end assertions through `libs.processor` as the related behavior is settled:

| Fixture | Proposed pipeline assertion |
|---|---|
| `small_valid_shell.stl` | A clean result retains both shells; a result that drops the four-face shell must be non-success. |
| `opposite_volume_shells.stl` | Cancellation is treated as indeterminate; dropping either shell cannot return `PROCESS`. |
| `decimation_lost_appendage.stl` with `max_faces=20` | A clean result preserves the 0.25-unit protrusion within the agreed tolerance; otherwise report destructive decimation. |
| `reversed_tjunction_chain.stl` | The welder either rejects the backward path without changing geometry or repairs it without new unmatched edges; the final pipeline result must remain topologically clean. |

The remaining proposed regressions are specified individually in T01–T16 below. Do not turn an unresolved geometry judgment into a passing expectation merely to make the suite green.

**Your comment on this run plan:** _Add your note here._

## Test review

The following concerns are limited to tests of `libs/` and coverage needed for the refactor. “Invalid” means the assertion does not establish the behavior claimed by the test; it does not mean the test fails today.

### T01 — Blender concurrency test misses live handles

`tests/tests/test_blender.py`:L209–227: 🟡 invalid claim: the concurrency test waits until all four runs finish before calling `kill_current()`, so it cannot detect a lost handle while another run is live. Synchronize two live runs, let one finish, and check the remaining handle and kill behavior.

**Your comment:** _Add your note here._

### T02 — Orientation test does not prove call order

`tests/tests/test_repairer.py`:L224–239: 🟡 invalid claim: absence of `Step.ORIENT` and matching face counts do not prove orientation runs after splitting. Record the actual filter and split call order. The real inverted-third probe tests the effect of unconditional orientation, but this particular unit test does not establish order.

**Your comment:** _Add your note here._

### T03 — Failure test does not prove unchanged geometry

`tests/tests/test_repairer.py`:L289–308: 🟡 invalid claim: the failure test says the input is returned unchanged but checks only that geometry exists and two reported face counts agree. Use an input changed by an earlier step and compare returned geometry and measured result fields with the intended failure contract.

**Your comment:** _Add your note here._

### T04 — Converter exception regression missing

`tests/tests/test_converter.py`:L191–200,L230–236: 🔴 missing regression: the fake converter can return `False` but never raises; a thrown conversion error currently drops the file silently. Add a raising converter case asserting one failed result is emitted and counted.

**Your comment:** _Add your note here._

### T05 — Failed PyMeshFix result regression missing

`tests/tests/test_repairer.py`:L293–315: 🔴 missing regression: only a raising part tool is tested; the default tool can return an unsuccessful PyMeshFix result that the pipeline reports as success. Stub a failed `meshfix.repair` result and assert `Result.ok=False` with a failure reason.

**Your comment:** _Add your note here._

### T06 — Zero-face STL regression missing

`tests/tests/test_mesh_io.py`:L301–308: 🔴 missing regression: the truncated-file test does not cover a well-formed binary STL with zero triangles, which `probe` accepts and `load` crashes on. Add that exact 84-byte input and assert a consistent invalid result.

**Your comment:** _Add your note here._

### T07 — Welder distance boundary evidence missing

`tests/tests/test_welder.py`:L158–179: 🟡 missing boundary evidence: the test deliberately accepts a vertex 0.2 units off the edge, but no test states how to classify a much larger gap in an edge-connected shell. Add a representative fixture with an independently justified expected result before setting a rejection rule.

**Your comment:** _Add your note here._

### T08 — Decimation loss safety test missing

`tests/tests/test_processor.py`:L52–57,L72–102: 🟡 missing safety case: destruction tests change only repair input/output volumes; none make decimation remove volume while repair succeeds. `tests/probes/decimation_lost_appendage.stl` now demonstrates a clean decimation that shortens a thin feature, and its source geometry is guarded in `tests/tests/test_regression_fixtures.py`; the pipeline verdict test is still missing. Test that loss against the original source is detected before a clean output is approved.

**Your comment:** _Add your note here._

### T09 — Small functional shell safety test missing

`tests/tests/test_splitter.py`:L122–130: 🟡 missing safety case: the existing test confirms removal of a one-face speck. `tests/probes/small_valid_shell.stl` now provides a clean four-face component beside a 760-face body, with its input guarded in `tests/tests/test_regression_fixtures.py`; the pipeline preservation assertion is still missing. Assert that the pipeline does not silently approve its loss.

**Your comment:** _Add your note here._

### T10 — Pool selector exception test missing

`tests/tests/test_pool.py`:L265–270: 🟡 missing failure case: handler exceptions are covered, selector exceptions are not; a selector error currently terminates a worker while `start()` returns normally. Assert the chosen error-reporting contract for selector failures.

**Your comment:** _Add your note here._

### T11 — Legacy pipeline tests do not cover the refactor

`tests/tests/test_pipeline.py`:L41,L139: 🟡 refactor coverage gap: these end-to-end tests call the frozen `stl_batch_fix.process_file`, so their 36 passing cases do not verify that `libs/` produces the same output. Add an end-to-end test through the refactored entry point when it exists, using the same output checks.

**Your comment:** _Add your note here._

### T12 — Reversed welder path test missing

`tests/tests/test_welder.py`:L424–458: 🟡 missing algorithm case: the two-vertex fixture asserts the sorted result but never reverses the path's geometric order. `tests/probes/reversed_tjunction_chain.stl` and its characterization test now prove the reversed path; the repair assertion is still missing. Assert that repair either rejects a backward chain or leaves no new unmatched edges.

**Your comment:** _Add your note here._

### T13 — Zero, NaN, and zero-area decision tests missing

`tests/tests/test_processor.py`:L72–102: 🔴 missing decision cases: `tests/probes/opposite_volume_shells.stl` now supplies two clean shells whose signed volumes cancel, and its input is guarded in `tests/tests/test_regression_fixtures.py`. The pipeline decision assertion, NaN-volume case, and zero-area closed-topology case remain missing. Give each an explicit non-success verdict and keep topology and geometry evidence separate.

**Your comment:** _Add your note here._

### T14 — Source collision and copy-failure tests missing

`tests/tests/test_converter.py`:L292–313 and `tests/tests/test_indicators.py`:L128–157: 🟡 missing identity/failure cases: no folder contains both `model.obj` and `model.stl`, and no companion-copy failure is injected. Assert collision handling and the intended per-file copy-failure behavior.

**Your comment:** _Add your note here._

### T15 — Interrupted-write test missing

`tests/tests/test_processor.py`:L202–234: 🟡 missing interrupted-write case: tests check complete output and marker files only. Simulate a failure after a final path is created, then assert the next `indicators.check` cannot treat it as completed work.

**Your comment:** _Add your note here._

### T16 — Final face-budget test missing

**Not a finding under the owner's requirement.** `max_faces` is the
decimation target, not a hard post-repair ceiling. The configured `900,000`
target deliberately leaves headroom below the approximately 1M slicer limit;
repair may add faces, especially when the welder restores missing
subdivision. Re-decimating after repair would risk recreating the defects just
repaired.

**Owner decision:** no final-face-budget gate or T16 regression is required.
