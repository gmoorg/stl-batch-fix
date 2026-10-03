# Current pipeline

Implemented behavior, checked 2026-09-28. The uniform-step refactor
(`StepConfig`/`ConditionStep`/`execstep`, described in
[TODO](TODO.md#uniform-step-refactor)) is implemented.

## Batch execution

`batch_repair.py` takes no arguments. It loads `batch_repair.toml` from its own
folder (`libs.runconfig`: unknown keys, missing required keys, wrong types and
out-of-range values are errors; relative paths resolve against that folder),
resolves automatic values (workers, log path, memory budget), checks the
input/output folders and dependencies — all before any output or log is
created — then:

1. `converter.prepare`: walk input, check indicators, copy companions, convert
   OBJ/ASCII STL with Blender, and collect probed binary meshes. Intake uses
   one conversion worker; it precedes isolated repair dispatch.
2. Preflight rejects invalid jobs, colliding destination/marker paths, and
   pre-existing publication paths among emitted jobs. Sort by triangle count.
3. `pool.Pool` threads reserve memory via `RunState` and spawn one isolated
   `batch_repair_child.py` subprocess per admitted mesh; the parent passes it
   every value it needs, and the child never reads the TOML file. An oversized job may run alone.
4. Each child probes/loads, processes, atomically writes output or marker,
   then atomically writes its JSON `ChildResult`.
5. Parent waits with timeout, kills/confirms the process group, and validates
   the bounded result file regardless of exit code. With no trusted result:
   one new publication is recovered; multiple are inconsistent; none causes
   a source-copy timeout/failure marker, subject to cancellation authorization.
6. Exactly-once reporting and counters produce the terminal summary. Recovered
   results get diagnostics even if the recovered output was clean.

Ctrl+C cancels admission and kills registered groups; a second Ctrl+C is
ignored during bounded cleanup. Unconfirmed cleanup cancels the run and keeps
jobs unresolved. Cancelled jobs receive no synthetic marker. Already committed
outputs can remain, so rerun behavior still follows existing output indicators.
Incomplete runs, diagnostics, and companion-copy failures return nonzero.

## One mesh

| Order | Invocation | Condition / parameters |
|---|---|---|
| 1 | `decimator.make_step()` via `execstep.run_step` | CLI face target via `StepConfig.faceCount`; zero or already within target skips; failure → `UNDECIMATED`. Same step implementation as 5b. |
| 2 | `repairer.repair` | Measure input component volume; compute `whole_model_diag` (`scanner.diagonal`) after decimation, before split; `WHOLE_MESH_STEPS` currently empty |
| 2a | Model gate (opt-in `skip_clean = true`) | After `WHOLE_MESH_STEPS`: if `is_already_clean`, skip 3–6 and go to 7 with the decimated mesh; verdict logged as `clean_gate` |
| 3 | `repairer._split_stage` → `splitter.make_shell_split_step` | Always present; default floor 100 faces; if all shells are below floor, retain original mesh |
| 4 | (seam split) | Absent from the default split composition; add a `collection_entry` in `_split_stage` to enable, not a flag |
| 4a | Part gate (same `skip_clean`, model not clean) | Per retained part: if `is_already_clean`, merge it as split, bypassing 5 (default or custom `part_steps`); verdict logged as `clean_gate` with the part id |
| 5 | Per-part `DEFAULT_PART_STEPS`, via `execstep.run_sequence` | `(alpha_wrap, decimate, decimate_again, meshfix)` by default; caller may replace `part_steps` entirely — nothing appended |
| 5a | `alphawrap.step_alpha_wrap` | Reads `whole_model_diag` from `StepConfig` |
| 5b | `decimator.make_step()` | Reads `faceCount` (target captured after splitting, before wrapping) from `StepConfig`; same implementation as step 1. Missing the target is **never** a reason to fail the part or the mesh (owner decision, 2026-10-03): the other steps decide whether the model is repaired, and extra faces only make slicing and printing slower. |
| 5c | `decimator.make_step()` again (`decimate_again`) | The same step as 5b, run once more on 5b's output: a single fast_simplification call can plateau short of target on alpha-wrap output. Already within target → `not_needed` (decimate's own guard), no library call. Still above target afterwards is not a failure; a decimator error fails the part like 5b. No loop. |
| 5d | `execstep.ConditionStep(scanner.scan, scanner.has_defects, meshfix.step_meshfix_repair)` | MeshFix only if the decimated part has open or non-manifold edges; unavailable/failed tool fails the step |
| 6 | `execstep.run_merge_step` → `splitter.merge` | Concatenate successful parts; no welding or boolean union |
| 7 | Closing repair checks | Finite coordinates, component volume, lost-vertex measurements; exceptions fail repair |
| 8 | `processor._judge` | Ordered gates below |
| 9 | `processor.write` | Atomically write accepted mesh or a full-mesh failure marker |

Alpha wrap uses `alpha=min(diag/800, 0.15)`, `offset=min(diag/2000, 0.06)`;
`diag` belongs to the whole mesh **after initial decimation**, not each part.
The captured per-part target is best-effort, including when CLI max faces is
zero. There is no post-merge decimation. Each entry in a part's sequence
produces its own `Step.PART` record — four by default (alpha wrap, decimate,
decimate_again, meshfix; a skipped step still records), one per entry in
whatever sequence actually ran.

Judge order: failed repair → `FAILED`; non-finite volume/ratio → `FAILED`;
retained component-volume ratio below 0.90 → `DESTROYED`; non-manifold edges
→ `UNREPAIRED`; open edges → `OPEN_EDGES`; no finite positive enclosed volume
→ `BROKEN`; otherwise `PROCESS`. Rejection precedes acceptance even if topology
looks clean. Source markers preserve the source; topology-failure markers can
contain the repaired mesh. Tool execution success alone is not acceptance.

## Limits and observability

- Initial-decimation loss, meaningful small-part loss, and final winding are
  not fully guarded by the judge. See [modules](modules.md).
- Each model has a raw log beside its output (`foo.log`): intake conversion
  and every repair attempt append a dated header; a repair child's stdout and
  stderr go straight into it (all tools, crash messages, faulthandler
  traceback), with a separator line before and after each step. Blender's
  output is copied in after each Blender run. A log that cannot be written
  only produces a warning.
- Incremental step logging exists (`batch.log`), and `progress.log` persists
  run/progress/job/final records; the terminal summary is printed separately.
- Memory estimate: 890 bytes/input triangle × **unvalidated** alpha-wrap factor 3.
- Process-group cleanup cannot cover descendants that deliberately leave the
  group. Conversion intake and direct Blender use have separate lifecycle limits.
- Startup checks CGAL, fast_simplification, Blender, PyMeshFix, and
  PyMeshLab at startup. Splitting itself uses NumPy/SciPy, not PyMeshLab.
- No refactor TUI is planned: runs are configured by editing `batch_repair.toml`.
  The existing TUI drives the legacy code only.
