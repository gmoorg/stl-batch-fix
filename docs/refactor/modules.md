# Current module reference

Checked against `libs/` on 2026-09-28. Read only entries relevant to the task,
then inspect code for exact signatures. Modules do not depend on the legacy script.

## Contracts

Loaded `Mesh` values carry identity plus `Geometry` arrays. Every mesh step
has the uniform signature `(mesh, config: pipeconfig.StepConfig | None =
None) -> (ok, mesh, detail)`; `ok` describes execution, not printability. A
failed step stops the sequence it is in. `StepConfig` carries `faceCount`
(read by decimation) and `whole_model_diag` (read by alpha wrap); other
steps ignore it. A collection step (shell/seam split) instead has the shape
`(tuple[mesh, ...], config) -> (ok, tuple[mesh, ...], detail)` — see
`execstep`'s entry below. The uniform-step refactor
([TODO](TODO.md#uniform-step-refactor)) is implemented; [Pipeline](orchestration.md)
owns the execution-order reference.

**Custom `part_steps`**: a caller supplying `part_steps=` to `repairer.repair`
gets exactly that sequence, run as given — nothing is auto-appended.
Decimation and conditional MeshFix are ordinary entries in
`repairer.DEFAULT_PART_STEPS`, not a hidden mandatory tail; a caller that
wants them lists them itself, the same as any other step. This is a
deliberate behavior change from the pre-refactor pipeline, settled by the
user: "final decimate and meshfix after should be treat as any other stand
alone step what we could add or remove from list of steps."

## Geometry and tools

| Module | Scope / main interface | Limitations and edge cases |
|---|---|---|
| `mesh_io` | `Mesh`, `Geometry`; classify/probe/load/write; PLY exchange; `staged_write` | Rejects empty/non-finite input. Binary STL output; PLY preserves indexed geometry for Blender. Publish via temporary sibling + atomic replace; partial final files must not retire work on rerun. |
| `scanner` | Measurement only: `scan`, `shells`, `winding_seams`, `volume`, `component_volume`, bounds and open loops | Clean edge counts do not prove a valid solid or preserved shape. `volume` is signed; component volume sums shell magnitudes to avoid opposite-winding cancellation. Winding seams inspect edges with exactly two incident faces; consistent winding does not prove outward orientation. |
| `decimator` | `decimate(mesh, max_faces) -> Result` using fast_simplification | Zero disables that call; target is best-effort. Can lose detail, alter topology, or exceed requested count. No alternative decimator fallback; `Rung.FAILED` retains input and failure evidence. |
| `alphawrap` | CGAL `wrap`; `step_alpha_wrap(mesh, config)` | Reads `config.whole_model_diag`; default reconstruction uses it with 0.15/0.06 caps. Requires finite positive parameters and nonempty output. Reconstructed topology does not guarantee fidelity; face count and memory may grow dramatically. |
| `splitter` | `by_shells`, `by_seams`, `merge`; `make_shell_split_step`/`make_seam_split_step` build the collection-step wrappers | Shells connect through edges, not single shared vertices. Default shell floor 100 can discard meaningful parts; if none qualify, original is retained. Seam split returns every cut region, including triangles; absent from the default split composition because independent repairs can destroy shape. Merge concatenates, never welds or unions. |
| `meshfix` | `repair` returns rich result; `step_meshfix_repair` adapts it | Used conditionally after per-part decimation. May delete geometry, especially with disconnected shells/self-intersections; execution success still needs judging. Native FD capture is process-wide: isolated batch children avoid cross-job capture races; direct concurrent calls need isolation/serialization. |
| `meshlab` | `apply_filters`, CLEAN and orientation step wrappers | Explicit-use tools, absent from default sequence. Close-vertex merge and orientation damaged measured models; do not re-enable without evidence. Wrappers run when called; no removed ENABLE flags. |
| `blender` | Timeout-bound `Runner`, `convert`, `repair`, `step_blender_repair`; scripts in `blender_fx/` | Conversion is active; repair is explicit-use. Exchange repair geometry through PLY, not STL re-welding. Accepted incomplete repair output is not proof of cleanliness. Direct Runner cleanup kills its process; batch repair children additionally have group cleanup. |
| `welder` | `find`, `repair`, `step_weld_close_tjunctions` | Explicit-use T-junction repair adds faces without moving vertices. Reversed/non-monotone paths are refused; no defensible distance bound for far bent paths. Do not sort chains as a substitute for valid topology. |

## Pipeline and configuration

| Module | Scope / main interface | Limitations and edge cases |
|---|---|---|
| `pipeconfig` | `StepConfig(faceCount, whole_model_diag)` — plain config data, no step lists, no tool imports | No on/off flags any more; a step runs because it appears in the sequence that composes it. Tool tuning values still live in individual modules. |
| `execstep` | Shared execution: `Entry`/`mesh_entry`/`collection_entry`, `ConditionStep`, `run_step`, `run_collection_step`, `run_sequence`, `run_merge_step`, `StepResult` | Owns config delivery, `step_logger` invocation, recording, and stop-on-failure for every stage. Imports nothing from `repairer`/`processor`/any concrete composition — those import this, not the reverse. `run_sequence` rejects a sequence mixing mesh-kind and collection-kind entries. |
| `repairer` | `repair(mesh, ..., part_steps=None)`; `WHOLE_MESH_STEPS`, `_split_stage`, `DEFAULT_PART_STEPS` — the visible composition; measurements | Default part sequence is alpha wrap → decimate → conditional MeshFix, ONE `Step.PART` record per entry. A custom `part_steps` replaces the sequence entirely — nothing auto-appended (see the "Custom `part_steps`" note above); an empty `part_steps=()` runs no per-part steps. Closing checks guard non-finite output. `is_already_clean` exists but is not wired in. |
| `processor` | `process(mesh, max_faces, part_steps=None) -> Outcome`; `write` | Initial decimation runs through the SAME `decimator.make_step`/`execstep.run_step` pair `repairer` uses for per-part decimation — one decimator step implementation in both positions. No final whole-mesh decimation or `final_decimation` field. Judge checks volume retention/topology/solid volume; it does not fully protect original-to-first-decimation detail, meaningful components, or final winding. Lost vertices are diagnostic only. |
| `steplog` | `open_step_log`, `logged_step`, `timed_info`; callback-based incremental TSV (7 fields: `...\tsource\tevent\tstep\tpart\tduration\tdetail`) | Append/line-buffered child logs survive ordinary child failure up to last emitted event. No power-loss durability guarantee. `source` is an absolute path (set by `batch_repair._process_one_file`) and every event carries a `part` (`'N/M'` or `'-'`), so same-basename files and per-part events are distinguishable. Format migration (old 6-field rows alongside new 7-field ones in a reused `--output`) is self-describing by field count. Not every scan has its own logged timing. |

## Intake and execution

| Module | Scope / main interface | Limitations and edge cases |
|---|---|---|
| `indicators` | `check`, `export_path`, marker suffixes; `Indicator`, `Finding` | Output/marker names govern skip-on-rerun. Preserve naming and atomic publication semantics. |
| `converter` | `prepare`: walk, copy companions, normalize OBJ/ASCII STL, emit jobs | Preserves relative paths; caches conversions in `stl-exported/`. Copy failures count and continue; conversion failures emit invalid jobs. Same-stem OBJ/STL conversion collision remains accepted low priority. |
| `pool` | `Pool.start`, serialized selector, concurrent handlers | Handler errors go to selector; selector errors rethrow after workers stop. Stop prevents new work, not active native work. Caller owns subprocess cancellation. |
| `runstate` | Locked admission, child lifecycle, publication authorization, cancellation, completion | Memory reservations with oversized-alone override. Reporting attempted once; failed report remains unresolved. Unconfirmed cleanup cancels the run. Factor 3 for alpha-wrap memory is uncalibrated. |
| `proctree` | `terminate_and_confirm` | Linux `/proc` + process-group kill and reap. Zombies cannot write; unreadable/unparseable entries prevent confirmation. Descendants escaping the group are outside its guarantee. |
| `publication` | Expected paths, preflight collisions, baseline reconciliation | Recovery trusts one new expected publication after cleanup; multiple are inconsistent. Does not inspect mesh quality. Assumes preflight/baseline discipline, not arbitrary concurrent writers. |
| `childresult` | Atomic JSON `ChildResult` writer; bounded validated reader | Requires matching source and recognized fields; 64 KiB limit. Missing/invalid result triggers recovery. Exit code alone does not invalidate a committed result. `clean` derives from category/indicator. |

`libs/__init__.py` is package plumbing. `tools/batch_repair.py` owns CLI,
child dispatch, counters, and terminal reporting; it is not a geometry module.

## Evidence worth preserving

- Never infer shape preservation from clean topology alone, or use summed
  signed volumes as retained size for oppositely wound shells.
- Do not replace indexed PLY exchange with lossy STL round trips.
- Seam-region repair and aggressive cleanup have destroyed real models.
- Keep the model-loss fixtures and tests even when historical prose is archived.

For exact measurements only: [alpha-wrap/tool investigation](../../archive/docs-refactor-2026-09-22/discovered-bugs.md),
[MeshFix destruction](../../archive/docs-refactor-2026-09-22/amidara-clean-destroys.md),
[seam-region loss](../../archive/docs-refactor-2026-09-22/mandy-volume-loss.md).
