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
| `decimator` | `decimate(mesh, max_faces) -> Result` using fast_simplification | Zero disables that call; target is best-effort. Can lose detail, alter topology, or exceed requested count. A mesh within target returns `not_needed` without a library call; that guard is why the same step can run twice per part (`decimate`, `decimate_again`: a fresh call continues where fast_simplification plateaued on alpha-wrap output). Exceeding the target never fails; a decimator error does. No alternative decimator fallback; `Rung.FAILED` retains input and failure evidence. |
| `alphawrap` | CGAL `wrap`; `step_alpha_wrap(mesh, config)` | Reads `config.whole_model_diag`; default reconstruction uses it with 0.15/0.06 caps. Requires finite positive parameters and nonempty output. Reconstructed topology does not guarantee fidelity; face count and memory may grow dramatically. |
| `splitter` | `by_shells`, `by_seams`, `merge`; `make_shell_split_step`/`make_seam_split_step` build the collection-step wrappers | Shells connect through edges, not single shared vertices. Default shell floor 100 can discard meaningful parts; if none qualify, original is retained. Seam split returns every cut region, including triangles; absent from the default split composition because independent repairs can destroy shape. Merge concatenates, never welds or unions. |
| `meshfix` | `repair` returns rich result; `step_meshfix_repair` adapts it | Used conditionally after per-part decimation. May delete geometry, especially with disconnected shells/self-intersections; execution success still needs judging. Native FD capture is process-wide: isolated batch children avoid cross-job capture races; direct concurrent calls need isolation/serialization. |
| `meshlab` | `apply_filters`, CLEAN and orientation step wrappers | Explicit-use tools, absent from default sequence. Close-vertex merge and orientation damaged measured models; do not re-enable without evidence. Wrappers run when called; no removed ENABLE flags. |
| `blender` | Timeout-bound `Runner`, `convert`, `repair`, `step_blender_repair`; scripts in `blender_fx/` | Conversion is active; repair is explicit-use. Exchange repair geometry through PLY, not STL re-welding. Accepted incomplete repair output is not proof of cleanliness. `Runner(own_process_group=True)` is the default for every unmodified caller (`convert()`, `repair()`, a bare `Runner()`): it launches Blender in its own session and kills/confirms the WHOLE process group on every exit path (timeout, any `BaseException` including `KeyboardInterrupt`, and ordinary success) via a shared `_cleanup` routine — closing the earlier gap where a descendant Blender spawns and outlives a killed or even cleanly-exited leader. `own_process_group=False` is for a caller that KNOWS this Blender runs nested inside another, enclosing `proctree`-managed process group (only `step_blender_repair`, gated by `pipeconfig.StepConfig.nested_process_group`) — it does not take its own session, so the enclosing worker's own group-kill still reaches it. `Runner` tracks every concurrently in-flight run in `self._active` (keyed by run id), supports genuinely concurrent `run()` calls on one instance, and exposes `cancel()` (permanent: refuses all future `run()` calls and kills every currently tracked run) separately from `kill_current()` (unchanged, best-effort, single-target, non-permanent). `wait_for_idle`/`reap_unresolved` give a caller (intake) a bounded way to wait for and retry cleanup of runs a `cancel()` could not immediately confirm. `Result.cleanup_confirmed` is `True`/`False` in owned mode, always `None` in delegated mode; `convert()`/`repair()` raise `BlenderCleanupUnconfirmed` whenever it is `False`, regardless of the conversion/repair's own `ok`. |
| `welder` | `find`, `repair`, `step_weld_close_tjunctions` | Explicit-use T-junction repair adds faces without moving vertices. Reversed/non-monotone paths are refused; no defensible distance bound for far bent paths. Do not sort chains as a substitute for valid topology. |

## Pipeline and configuration

| Module | Scope / main interface | Limitations and edge cases |
|---|---|---|
| `pipeconfig` | `StepConfig(faceCount, whole_model_diag, nested_process_group)` — plain config data, no step lists, no tool imports | No on/off flags any more; a step runs because it appears in the sequence that composes it. Tool tuning values still live in individual modules. `nested_process_group` (default `False`) is read by `blender.step_blender_repair` to decide whether its own internal `Runner` should get its own process session (`False`/absent) or stay part of an enclosing `proctree`-managed group (`True`) — threaded in from `repairer.repair`/`processor.process`'s own `nested_process_group` parameter, ultimately from the explicit `--managed-child` marker `batch_repair._spawn_child` passes to `batch_repair_child.py` (see below). |
| `execstep` | Shared execution: `Entry`/`mesh_entry`/`collection_entry`, `ConditionStep`, `run_step`, `run_collection_step`, `run_sequence`, `run_merge_step`, `StepResult` | Owns config delivery, `step_logger` invocation, recording, and stop-on-failure for every stage. Imports nothing from `repairer`/`processor`/any concrete composition — those import this, not the reverse. `run_sequence` rejects a sequence mixing mesh-kind and collection-kind entries. |
| `repairer` | `repair(mesh, ..., part_steps=None)`; `WHOLE_MESH_STEPS`, `_split_stage`, `DEFAULT_PART_STEPS` — the visible composition; measurements | Default part sequence is alpha wrap → decimate → `decimate_again` (the same decimate step again; `not_needed` when already within target) → conditional MeshFix, ONE `Step.PART` record per entry. A custom `part_steps` replaces the sequence entirely — nothing auto-appended (see the "Custom `part_steps`" note above); an empty `part_steps=()` runs no per-part steps. Closing checks guard non-finite output. `is_already_clean` (NM=0, open=0, winding seams=0) is applied by the opt-in `skip_clean` keyword arg (off by default): a clean mesh skips split/parts/merge (still measured and judged); otherwise clean retained parts are merged unprocessed. Globally inverted or self-intersecting meshes pass the gate. |
| `processor` | `process(mesh, max_faces, part_steps=None) -> Outcome`; `write` | Initial decimation runs through the SAME `decimator.make_step`/`execstep.run_step` pair `repairer` uses for per-part decimation and its second round — one decimator step implementation in every position. The initial pass is a single round. No final whole-mesh decimation or `final_decimation` field. Judge checks volume retention/topology/solid volume; it does not fully protect original-to-first-decimation detail, meaningful components, or final winding. Lost vertices are diagnostic only. |
| `steplog` | `open_step_log`, `logged_step`, `timed_info`; callback-based incremental TSV (7 fields: `...\tsource\tevent\tstep\tpart\tduration\tdetail`) | Append/line-buffered child logs survive ordinary child failure up to last emitted event. No power-loss durability guarantee. `source` is an absolute path (set by `batch_repair._process_one_file`) and every event carries a `part` (`'N/M'` or `'-'`), so same-basename files and per-part events are distinguishable. Format migration (old 6-field rows alongside new 7-field ones in a reused `--output`) is self-describing by field count. Not every scan has its own logged timing. |

## Intake and execution

| Module | Scope / main interface | Limitations and edge cases |
|---|---|---|
| `indicators` | `check`, `export_path`, marker suffixes; `Indicator`, `Finding` | Output/marker names govern skip-on-rerun. Preserve naming and atomic publication semantics. |
| `converter` | `prepare`: walk, copy companions, normalize OBJ/ASCII STL, emit jobs | Preserves relative paths; caches conversions in `stl-exported/`. Copy failures count and continue; conversion failures emit invalid jobs. Same-stem OBJ/STL conversion collision remains accepted low priority. `prepare`'s own generic `try/except Exception` around its `convert_one` callback already converts `blender.BlenderCleanupUnconfirmed` (like any other raised exception) into the standard invalid-job path — no special handling needed here. `batch_repair.py`'s own `_run` shares ONE `blender.Runner` (via `convert=functools.partial(blender.convert, runner=intake_runner)`) across every conversion `prepare` drives, so a single `cancel()` on that Runner reaches every conversion in flight, including ones launched after the call that triggered it. |
| `pool` | `Pool.start`, serialized selector, concurrent handlers | Handler errors go to selector; selector errors rethrow after workers stop. Stop prevents new work, not active native work. Caller owns subprocess cancellation. `KeyboardInterrupt` is deliberately not caught inside `Pool.start()` — it propagates to the caller. `batch_repair.py`'s `_run` now applies this SAME pattern to `converter.prepare`'s own internal `Pool.start()` call (not just its own top-level dispatch `Pool.start()`): a `KeyboardInterrupt` during intake `cancel()`s the shared intake `Runner`, bounds a wait via `wait_for_idle`/`reap_unresolved`, and returns early (nonzero) without reaching `_preflight`/dispatch. |
| `runstate` | Locked admission, child lifecycle, publication authorization, cancellation, completion | Memory reservations with oversized-alone override. Reporting attempted once; failed report remains unresolved. Unconfirmed cleanup cancels the run. Factor 3 for alpha-wrap memory is uncalibrated. |
| `proctree` | `terminate_and_confirm`; `live_group_members` (public — the `/proc`-sweep helper, formerly `_live_group_members`) | Linux `/proc` + process-group kill and reap. Zombies cannot write; unreadable/unparseable entries prevent confirmation. Descendants escaping the group are outside its guarantee. `terminate_and_confirm` itself is unchanged — its own reaping assumptions (a fresh `Popen` the caller is actively waiting on) do not match `blender.Runner`'s different situation (a process already `communicate()`-d or independently managed), so `Runner` builds its own bounded confirmation loop (`_confirm_group_dead`) around the shared `live_group_members` primitive rather than reusing the whole function. |
| `publication` | Expected paths, preflight collisions, baseline reconciliation | Recovery trusts one new expected publication after cleanup; multiple are inconsistent. Does not inspect mesh quality. Assumes preflight/baseline discipline, not arbitrary concurrent writers. |
| `runconfig` | `RunConfig` (frozen), `load(path)`, `resolve(config)`, `ConfigError`, `required_keys()` | Reads `batch_repair.toml`; documented by `batch_repair.example.toml`, whose keys and default values a test keeps equal to the dataclass. Unknown keys, missing required keys (`input`, `output`, `max_faces`), wrong types (bool is never an int) and out-of-range values raise `ConfigError` naming the key. Relative paths resolve against the config file's folder. TOML has no null, so `workers = 0`, `memory_budget_bytes = 0` and `log_file = ""` mean automatic; `resolve` replaces them in the parent only. |
| `childresult` | Atomic JSON `ChildResult` writer; bounded validated reader | Requires matching source and recognized fields; 64 KiB limit. Missing/invalid result triggers recovery. Exit code alone does not invalidate a committed result. `clean` derives from category/indicator. |

`libs/__init__.py` is package plumbing. `batch_repair.py` takes no
command-line arguments: it owns config loading (via `runconfig`), environment
checks, child dispatch, counters, and terminal reporting; it is not a geometry
module. `batch_repair_child.py` is the internal per-file entry point the
parent spawns; its arguments are internal, never user-facing, and it never
reads the TOML file. `_spawn_child` (the only code that KNOWS a child it
launches will live inside a `proctree`-owned process group) always adds
`--managed-child` to that child's argv — an explicit presence/absence marker,
since the child script can also be run directly (diagnostics, tests) and
cannot otherwise infer which context it is running in. The child's
`run_one_file` passes it straight through as `nested_process_group` into
`processor.process` (via `batch_repair._process_one_file`); absent means
`False`, the safe default.

## Evidence worth preserving

- Never infer shape preservation from clean topology alone, or use summed
  signed volumes as retained size for oppositely wound shells.
- Do not replace indexed PLY exchange with lossy STL round trips.
- Seam-region repair and aggressive cleanup have destroyed real models.
- Keep the model-loss fixtures and tests even when historical prose is archived.

For exact measurements only: [alpha-wrap/tool investigation](../../archive/docs-refactor-2026-09-22/discovered-bugs.md),
[MeshFix destruction](../../archive/docs-refactor-2026-09-22/amidara-clean-destroys.md),
[seam-region loss](../../archive/docs-refactor-2026-09-22/mandy-volume-loss.md).
