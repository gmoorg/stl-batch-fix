# Final batch orchestration

**Status, 2026-09-23:** `tools/batch_repair.py` (milestone 2, this session)
implements every step of this diagram. Intake (`converter.prepare`) stays
serial by design; from there, jobs are admitted and dispatched through
`libs.pool.Pool` driving worker threads, each of which owns one isolated
`--one-file` child process (a `--one-file` mode on `batch_repair.py` itself)
via `subprocess.Popen(start_new_session=True)`. `libs.runstate.RunState` is
the shared, lock-guarded bookkeeping that lets a `KeyboardInterrupt` in the
main thread find and kill every live child's whole process group
(`libs.proctree.terminate_and_confirm`), which also fixes the Ctrl+C-ignored-
by-alpha-wrap bug this milestone was built to close. Each child writes a
JSON `ChildResult` (`libs.childresult`) to a parent-owned temp file rather
than a pipe or its own exit code. `libs.publication` handles preflight
collision/pre-existing-artifact rejection and post-crash reconciliation
(exactly one newly-published path → trust it; two or more → report
inconsistent; zero → the parent writes the fallback marker itself).

Full design history — 8 rounds of Claude/Codex PLAN-phase review, including
every rejected earlier draft and why — is in
[docs/refactor/HANDOFF-batch-runner-pool.md](HANDOFF-batch-runner-pool.md).
See [Build order](#build-order) below for what remains (typed
`RunConfig`/`Job`/`RunEvent`/`RunSummary` contracts, an event stream, TUI).

## Module order

```text
CLI or TUI → RunConfig
  → converter.prepare
       indicators.check
       blender.convert only for input normalization
  → sort/admit Jobs with pool.Pool
  → isolated child per file
       mesh_io.load
       processor.process
         decimator → repairer → scanner decision
       atomic processor.write
  → FileResult over JSON
  → one reporting/event component
  → CLI/TUI display + durable summary
```

## What happens to one model, step by step

Every call the pipeline makes to a single mesh, in order, with what each step
is for and when it runs. Steps marked **conditional** are skipped when their
condition is not met. This table reflects the alpha-wrap default sequence
adopted 2026-09-22 (see [discovered bugs](../archive/docs-refactor-2026-09-22/discovered-bugs.md#a-two-pass-recipe-that-did-work-2026-09-22)
and [the one-file pipeline](../archive/docs-refactor-2026-09-22/pipeline.md)) — the earlier weld/CLEAN/orient/
Blender/PyMeshFix sequence this table used to describe is no longer the
default; those tools remain in the codebase and importable, only unwired.

### Intake

| # | step | goal | condition |
|---|---|---|---|
| 1 | `converter.classify` | decide whether the file is a usable mesh, a companion to copy, or needs conversion | always |
| 2 | `blender.convert` | normalize OBJ or ASCII STL to binary STL, because nothing downstream reads other formats | **conditional** — only when step 1 says the format is not binary STL |
| 3 | `mesh_io.probe` | read the header for a triangle count, so jobs can be admitted smallest-first without loading | always |
| 4 | `mesh_io.load` | read vertices and faces into arrays, welding exactly identical coordinates into one shared vertex table | always |

### Reduce

| # | step | goal | condition |
|---|---|---|---|
| 5 | `decimator.decimate` | bring the face count under the printing budget; an over-budget file gets reduced by the slicer instead, which reintroduces the defects this tool removes | **conditional** — `Rung.NOT_NEEDED` when already under `max_faces`; `max_faces <= 0` disables it |
| 6 | *decimation failure gate* | stop with `UNDECIMATED` rather than shipping an over-budget mesh | **conditional** — only when step 5 reports `FAILED` |

### Repair — `repairer.repair`

| # | step | goal | condition |
|---|---|---|---|
| 7 | *(none)* — `WHOLE_MESH_STEPS` is `()` | weld and the four CLEAN filters ran here through 2026-09-21; removed from the default sequence, tools remain importable (`welder.step_weld_close_tjunctions`, `meshlab.step_clean_*`) | *(unwired — no longer runs by default)* |
| 8 | `splitter.by_shells` | separate edge-connected components before repair runs, since alpha-wrap operates on one part at a time | **conditional** — returns the mesh unchanged when there is one component; components under `MIN_SHELL_FACES=100` are **dropped** |
| 9 | `splitter.by_seams` | separate regions whose winding contradicts itself | *(not implemented in the default sequence)* — exists and is tested; `repairer.repair` calls it only when `pipeconfig.ENABLE_SPLIT_SEAMS` is explicitly set True (default False) |
| 10 | `alphawrap.step_alpha_wrap` — CGAL Alpha Wrapping | reconstruct each retained part as a watertight, manifold, self-intersection-free solid at `alpha=min(diag/800, 0.15)`, `offset=min(diag/2000, 0.06)`, where `diag` is the **whole pre-split mesh's** bounding-box diagonal, bound once per `repair()` call via `functools.partial` — not each part's own diagonal | always, per part — **conditional** on `pipeconfig.ENABLE_ALPHA_WRAP` (default True); orient/Blender/PyMeshFix (`meshlab.step_orient`, `blender.step_blender_repair`, `meshfix.step_meshfix_repair`) ran here through 2026-09-21 and remain importable but unwired |
| 10a | `repairer._decimate_to_target_and_fix` | alpha-wrap can produce far more triangles than it was given; decimate this ONE part back to its own pre-alpha-wrap size (`target_faces`, captured right after step 8's split, before step 10 inflated it) — replacing the old whole-mesh second decimation pass (removed 2026-09-23; see `docs/refactor/TODO.md`'s "alpha-wrap's second decimation pass reintroduces non-manifold edges" entry) | always, per part, unconditionally — a second, separate `Step.PART` result per part (see `docs/refactor/modules.md`'s `repairer` entry) |
| 10b | `scanner.scan` (per-part) + `meshfix.repair` | check whether step 10a's decimation reintroduced NM/open-edge defects; if so, run PyMeshFix on THIS part only, before it ever reaches merge | **conditional** — PyMeshFix runs only when the post-decimation scan is not clean |
| 11 | *part failure gate* | stop the whole file rather than merging back a part that could not be repaired | **conditional** — only when step 10 or 10a/10b returns `ok=False` (decimation failed, or PyMeshFix failed/unavailable on a part that needed it) |
| 12 | `splitter.merge` | concatenate the repaired parts into one mesh. Does **not** weld coincident vertices at former cuts | **conditional** — returns the single part unchanged when there is one |
| 13 | *finite check* | reject NaN or infinite coordinates before any measurement touches them | always |

### Judge — `processor._decide`, in this order

| # | step | goal | condition |
|---|---|---|---|
| 15 | measurability gate → `FAILED` | refuse an unmeasurable volume instead of comparing it, since every `<` test below is False against NaN and would wave it through | **conditional** — when `volume_in`, `volume_out` or their ratio is not finite |
| 16 | destruction gate → `DESTROYED` | catch a repair that kept less than `MIN_VOLUME_KEPT=0.90` of the volume. **Asked before the topology tests**, because a half-model is a valid closed surface and would otherwise pass them | **conditional** — when `volume_kept < 0.90` |
| 17 | `scanner.scan` → `UNREPAIRED` | reject remaining non-manifold edges | **conditional** — when `scan.non_manifold > 0` |
| 18 | → `OPEN_EDGES` | reject remaining holes | **conditional** — when `scan.open_edges > 0` |
| 19 | `scanner.component_volume` → `BROKEN` | reject a surface that encloses nothing. Four faces on a line own every edge twice and score `nm=0, open=0, is_clean=True` while being no solid at all | **conditional** — when the enclosed volume is not finite and positive |
| 20 | → `PROCESS` | accept | when every gate above passed |

There is only ever ONE decimation-plus-judge pass now — no separate
whole-mesh "second pass" with its own face-budget/finite/volume/topology
validation the way there used to be (removed 2026-09-23 along with
`Outcome.final_decimation`). By the time a merged mesh reaches this judge,
every part it is built from has already been decimated to its own target
and fixed if that decimation left defects, at steps 10a/10b above.

### Commit

| # | step | goal | condition |
|---|---|---|---|
| 21 | `processor.write` | write the repaired STL, or the marker naming why not, atomically so an interrupted write cannot look finished | always |

## Turning steps off

Every repair step still reachable from the default sequence has an on/off
switch in `pipeconfig`, so what a step contributes can be measured instead
of argued about. All default to the shipping behaviour; a disabled step
still appears in `Result.steps` with `detail` saying it was skipped, so a
log never silently omits a stage.

| step | switch | default | in default sequence? |
|---|---|---|---|
| alpha wrap | `pipeconfig.ENABLE_ALPHA_WRAP` | True | **yes** |
| shell split | `pipeconfig.ENABLE_SPLIT_SHELLS` | True | **yes** |
| seam split | `pipeconfig.ENABLE_SPLIT_SEAMS` | **False** | yes (as the off-by-default case) |

`ENABLE_SPLIT_SEAMS` is the one switch that is off by default and turns a step
*on*: when True, `repairer.repair` does call `by_seams` on each shell part.
Enabling it repairs each seam region independently, which is measured as
destructive on real models.

**The weld/CLEAN/orient/Blender/PyMeshFix flags were removed 2026-09-23**
(user: "since we control what need to be executed in the repairer, we do
not need the Boolean flags what enable/disable steps") — confirmed none of
them gated anything reachable from `WHOLE_MESH_STEPS`/`PART_MESH_STEPS`
regardless of their value, so the flag was controlling nothing real. The
step functions themselves (`welder.step_weld_close_tjunctions`,
`meshlab.step_clean_*`/`step_orient`, `blender.step_blender_repair`,
`meshfix.step_meshfix_repair`) remain fully importable and unconditional —
call one directly and it runs, with no flag to disable it. See
`docs/refactor/TODO.md`'s Configuration section for the removal record and
`docs/refactor/modules.md`'s per-module "why unwired" entries for the
measurements that took each one out of the default sequence in the first
place — those hold regardless of the flag's removal.

The old weld/CLEAN/orient/Blender/PyMeshFix flag table, and the measurement
table that used to compare their on/off combinations on `Amidara_..._base`,
described the pre-2026-09-22 default sequence — see
[discovered bugs](../archive/docs-refactor-2026-09-22/discovered-bugs.md) and
[pipeline.md](../archive/docs-refactor-2026-09-22/pipeline.md#why-this-order) for the alpha-wrap era's own
measurements (the settled `alpha=diag/800, offset=diag/2000` recipe,
capped at 0.15/0.06).

### What the judge cannot see

Steps 15–20 are the entire verdict, and they do not test winding directly —
though alpha-wrap's own watertight/manifold/self-intersection-free guarantee
(unconditional, regardless of input winding) sidesteps the specific failure
mode this section originally documented (a mesh with hundreds of inverted
faces reaching `PROCESS` unnoticed). See [discovered bugs](../archive/docs-refactor-2026-09-22/discovered-bugs.md)
for what alpha-wrap does and does not guarantee — topology, not fidelity or
triangle budget.

## Required behavior

1. Validate configuration and dependencies once.
  `fast_simplification` is required for startup; PyMeshLab remains required
  for the separate shell split/merge operations.
2. Classify every source once; copy companions and normalize OBJ/ASCII STL to probed binary STL.
3. Preserve identity and relative output paths; reject destination collisions.
4. Admit jobs by worker and memory limits, preferably smallest measurable first.
5. Run each file in a child so timeout, native crash, OOM, or Blender descendants cannot kill the pool.
6. Return exactly one structured result per source. The parent creates timeout/crash markers when needed.
7. Commit outputs and markers atomically.
8. Emit one event stream for CLI/TUI and return nonzero when actionable failures remain.

## Why other structures failed

- Separate TUI/script pools duplicate scheduling, status, and cancellation.
- A process pool does not preserve pending work when a worker dies; stable threads should own disposable children.
- Mutable globals let CLI, tests, and children disagree; pass immutable values.
- Logs written by several layers lose exactly-once accounting; one runner owns final results.
- A killed child cannot write its timeout result; the surviving parent must.
- Direct final-path writes make interrupted files look finished.

## Build order

Define `RunConfig`, `Job`, `FileResult`, `RunEvent`, and `RunSummary`; build a serial runner; add child isolation; add scheduling/admission; add reporting; then point CLI and TUI at the shared runner. Do not port legacy `_process_file_impl` repair logic.

**Status, 2026-09-23:**

1. **Partially done.** No `RunConfig`/`Job`/`RunEvent`/`RunSummary` classes —
   `batch_repair.py` still uses plain function arguments and in-memory
   counters/lists for those. `ChildResult` (`libs/childresult.py`) *is* now a
   real, validated value type, matching `FileResult`'s role in the diagram
   above (a JSON payload the child writes and the parent strictly validates).
2. **Done.** Serial intake via `converter.prepare`.
3. **Done.** Each job runs in an isolated `--one-file` child process, spawned
   by a worker thread via `subprocess.Popen(start_new_session=True)`.
   `communicate(timeout=)` bounds the wait;
   `libs.proctree.terminate_and_confirm` is the actual kill/confirm
   mechanism, reached both on timeout and on `Ctrl+C` (via
   `libs.runstate.RunState`, which a `KeyboardInterrupt` in the main thread
   uses to find and kill every live child's process group) — closing the
   2026-09-22 bug where CGAL's `alpha_wrap_3` ignored Ctrl+C.
4. **Done, with a stated limitation.** `pool.Pool` drives `batch_repair.py`'s
   per-file dispatch. Admission combines a `--workers` ceiling with a
   memory-aware policy (`RunState.start()`): ascending face-count ordering,
   a running-total budget check, and D5's "admit alone" override for a job
   that cannot fit even by itself. The per-triangle byte estimate
   (`libs.runstate.BUDGET_BYTES_PER_TRIANGLE = 890`) is a confirmed-real
   decimator figure; the multiplier applied on top for alpha-wrap's own
   memory use (`ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED = 3`) is an explicitly
   unvalidated placeholder — see
   [TODO.md](TODO.md#runner-and-concurrency).
5. **Not done.** No structured event stream; only printed summary text (now
   including an explicit `Run INCOMPLETE` / unresolved-job report when
   cancellation or an unconfirmed child kill leaves anything unresolved,
   rather than ever claiming `Run complete.` in that case).
6. **Not applicable yet.** No CLI/TUI beyond `batch_repair.py`'s own
   `argparse` interface; `stl_batch_fix_tui.py` still drives the legacy
   script, untouched.

Full design reasoning — 8 rounds of PLAN-phase review with Codex, including
every rejected earlier draft (a pipe-based result channel, a `/proc`-walk
kill mechanism, single-Finding reconciliation, and others) and why each was
replaced — is preserved in
[HANDOFF-batch-runner-pool.md](HANDOFF-batch-runner-pool.md) rather than
re-derived here.

The original 2026-09-18 plan for this build order, including the
`RunConfig`/`Job`/`FileResult`/`RunEvent`/`RunSummary` shared contracts and
the full phase-by-phase implementation plan, survives at
`archive/docs-before-compact-2026-09-19/refactor/final-behavior.md` — it
was dropped from the live doc set during the 2026-09-19 compaction without
being carried forward. Its **batch-level architecture is still the target**
(thread pool driving isolated `--one-file`-style child processes, not a
process pool); only its **mesh-level step sequence** (the old weld → CLEAN →
split → orient → PyMeshFix → Blender → merge order) is superseded by the
alpha-wrap sequence this file's own step table now describes.
