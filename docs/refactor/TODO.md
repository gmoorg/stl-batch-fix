# TODO

Open tasks only. Implemented behavior: [modules](modules.md) and
[pipeline](orchestration.md). Historical review evidence and test results are
[archived](../../archive/README.md). Remove completed tasks; update the owning reference.

## Runner and concurrency

- [ ] Measure alpha-wrap peak memory and replace the unvalidated factor 3.
  The 890 bytes/triangle estimate covers decimation, not reconstruction.
- [ ] Define shared run/event/reporting contracts for the refactor runner.
- [ ] Connect a future refactor TUI to the shared runner without duplicating
  scheduling. The existing TUI still drives legacy code.
- [ ] Audit descendant cleanup in direct Blender use and conversion intake;
  batch repair groups already use `proctree`.
- [ ] Isolate or serialize native output capture for direct concurrent MeshFix
  calls; default batch repairs already run in separate processes.
- [ ] Address same-stem OBJ/STL conversion collisions when warranted
  (owner-accepted low priority). Runner destination preflight already exists.

## Geometry and configuration

- [ ] Validate preservation of meaningful small components and detail through
  initial decimation; global retained volume alone is insufficient.
- [ ] Evaluate the existing `is_already_clean` gate on real candidates before
  wiring it in: NM=0, open=0, winding seams=0; confirm Amidara base fails and
  verify candidate slicing/printing. Consistent winding can still be globally
  inverted or self-intersecting. Leave the gate disabled pending this work.
- [ ] Measure how often post-decimation MeshFix is required across a real corpus.
- [ ] Investigate reversed/non-monotone T-junctions and far bent paths;
  establish evidence before introducing a distance bound.
- [ ] Replace absolute geometry tolerances where scale tests justify it.
- [ ] Decide whether seam recovery and `open_loops_are_printable` are safe
  defaults; independent seam-region repair has caused real model loss.
- [ ] Move step tuning values into `pipeconfig`: MeshFix clean/fill parameters,
  lost-vertex tolerance, shell floor, welder rounds/chain limits, retained-volume
  threshold, and Blender timeouts. Keep format constants in their owning modules.
- [ ] Consider moving step ordering into `pipeconfig`, per-step IDs, and explicit
  split/merge entries (lower priority). Do not restore removed ENABLE switches.

## Test-suite cleanup and gaps

- [ ] Separate legacy/historical-tool suites from current default-pipeline coverage;
  preserve useful tool regressions and make each suite's scope explicit.
- [ ] Audit frozen-dataclass checks, exact tuple assertions, and overlapping tests.
  Justify removals by remaining behavior coverage, not a target test count.
- [ ] Rename or relocate misleading checks such as `test_clean_keeps_all_four_filters`:
  its helper calls four wrappers itself; it does not prove pipeline composition.
- [ ] Consolidate executor tests for order, transformed-output forwarding,
  conditional skip/run, configuration delivery, and stopping on failure.
  Keep tool-specific behavior tests with the tool.
- [ ] Preserve meaningful cancellation, crash recovery, atomic publication,
  exactly-once reporting, malformed-input, and geometry-preservation coverage.
- [ ] Extend model-loss fixture checks into pipeline-outcome checks where expected
  behavior is established; never bless known geometry loss to make tests pass.
- [ ] Add missing reversed-junction and far-bent-path regression coverage.
- [ ] Compare proposed exception/interrupted-write tests with existing converter,
  CLI, publication, and pool coverage before adding duplicates.
- [ ] Update the test guide and run affected suites after cleanup; document the
  remaining protection for each removed or consolidated test.
