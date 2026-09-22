# Final batch orchestration

**Status, 2026-09-22:** `tools/batch_repair.py` (milestone 1, committed
2026-09-22) implements steps 1–4 and 6–8 of this diagram — intake via
`converter.prepare`, dependency validation, and commit — but processes
jobs **serially in the main process**, not through `pool.Pool` with
isolated per-file children. Steps 5 ("sort/admit Jobs with `pool.Pool`")
and the isolated-child boundary ("isolated child per file", `FileResult`
over JSON) are the next planned milestone, not yet built. See
[Build order](#build-order) below and
[open issues](open-issues.md#runner-and-concurrency).

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
adopted 2026-09-22 (see [discovered bugs](discovered-bugs.md#a-two-pass-recipe-that-did-work-2026-09-22)
and [the one-file pipeline](pipeline.md)) — the earlier weld/CLEAN/orient/
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
| 11 | *part failure gate* | stop the whole file rather than merging back a part that could not be repaired | **conditional** — only when step 10 returns `ok=False` |
| 12 | `splitter.merge` | concatenate the repaired parts into one mesh. Does **not** weld coincident vertices at former cuts | **conditional** — returns the single part unchanged when there is one |
| 13 | *finite check* | reject NaN or infinite coordinates before any measurement touches them | always |
| 14 | `decimator.decimate` (second pass) | alpha-wrap can produce far more triangles than it was given; bring the merged, repaired mesh back under the same `max_faces` the first pass used | **conditional** — only runs once the repair-stage measurability/destruction checks below (steps 15–16) already passed on the pre-decimation repair result, so a later operation cannot conceal an already-rejected repair; `max_faces <= 0` disables it, same as the first pass |
| 14a | *final face-budget gate* | reject rather than silently ship an over-budget mesh — the only one of the two decimation passes with this hard postcondition | **conditional** — only when the second pass's actual output exceeds a positive `max_faces` |

### Judge — `processor._decide`, in this order

| # | step | goal | condition |
|---|---|---|---|
| 15 | measurability gate → `FAILED` | refuse an unmeasurable volume instead of comparing it, since every `<` test below is False against NaN and would wave it through | **conditional** — when `volume_in`, `volume_out` or their ratio is not finite |
| 16 | destruction gate → `DESTROYED` | catch a repair that kept less than `MIN_VOLUME_KEPT=0.90` of the volume. **Asked before the topology tests**, because a half-model is a valid closed surface and would otherwise pass them | **conditional** — when `volume_kept < 0.90` |
| 17 | `scanner.scan` → `UNREPAIRED` | reject remaining non-manifold edges | **conditional** — when `scan.non_manifold > 0` |
| 18 | → `OPEN_EDGES` | reject remaining holes | **conditional** — when `scan.open_edges > 0` |
| 19 | `scanner.component_volume` → `BROKEN` | reject a surface that encloses nothing. Four faces on a line own every edge twice and score `nm=0, open=0, is_clean=True` while being no solid at all | **conditional** — when the enclosed volume is not finite and positive |
| — | *the second decimation pass (step 14/14a above) and its own face-budget/finite/volume/topology validation happen here too, before* | `PROCESS` *is reached — see* [pipeline.md](pipeline.md) *for the exact order* | |
| 20 | → `PROCESS` | accept | when every gate above passed |

### Commit

| # | step | goal | condition |
|---|---|---|---|
| 21 | `processor.write` | write the repaired STL, or the marker naming why not, atomically so an interrupted write cannot look finished | always |

## Turning steps off

Every repair step has an on/off switch in `pipeconfig`, so what a step
contributes can be measured instead of argued about. All default to the
shipping behaviour; a disabled step still appears in `Result.steps` with
`detail` saying it was skipped, so a log never silently omits a stage.
The weld/CLEAN/orient/Blender/PyMeshFix flags below still exist and still
work — they gate tools that remain fully importable — but none of them are
in the default sequence any more; only `ENABLE_ALPHA_WRAP`,
`ENABLE_SPLIT_SHELLS`, and `ENABLE_SPLIT_SEAMS` affect what a default run
actually does.

| step | switch | default | in default sequence? |
|---|---|---|---|
| alpha wrap | `pipeconfig.ENABLE_ALPHA_WRAP` | True | **yes** |
| shell split | `pipeconfig.ENABLE_SPLIT_SHELLS` | True | **yes** |
| seam split | `pipeconfig.ENABLE_SPLIT_SEAMS` | **False** | yes (as the off-by-default case) |
| weld | `pipeconfig.ENABLE_WELD` | True | no — unwired 2026-09-22 |
| null faces | `pipeconfig.ENABLE_CLEAN_NULL_FACES` | True | no — unwired 2026-09-22 |
| merge close | `pipeconfig.ENABLE_CLEAN_MERGE_CLOSE` | True | no — unwired 2026-09-22 |
| duplicate faces | `pipeconfig.ENABLE_CLEAN_DUPLICATE_FACES` | True | no — unwired 2026-09-22 |
| unreferenced verts | `pipeconfig.ENABLE_CLEAN_UNREFERENCED` | True | no — unwired 2026-09-22 |
| orient | `pipeconfig.ENABLE_ORIENT` | True | no — unwired 2026-09-22 |
| Blender part | `pipeconfig.ENABLE_BLENDER_PART` | True | no — unwired 2026-09-22 |
| part tool (PyMeshFix) | `pipeconfig.ENABLE_PART_TOOL` | True | no — unwired 2026-09-22 |
| fill boundaries | `pipeconfig.ENABLE_FILL_BOUNDARIES` | True | no — unwired 2026-09-22 |
| `clean()` | `pipeconfig.ENABLE_CLEAN` | True | no — unwired 2026-09-22 |
| `clean()` arguments | `meshfix.CLEAN_MAX_ITERS`, `CLEAN_INNER_LOOPS` | 10, 3 | no — unwired 2026-09-22 |

`ENABLE_SPLIT_SEAMS` is the one switch that is off by default and turns a step
*on*: when True, `repairer.repair` does call `by_seams` on each shell part.
Enabling it repairs each seam region independently, which is measured as
destructive on real models.

The weld/CLEAN/orient/Blender/PyMeshFix rows above, and the measurement
table that used to compare their on/off combinations on `Amidara_..._base`,
described the pre-2026-09-22 default sequence — see
[discovered bugs](discovered-bugs.md) and
[pipeline.md](pipeline.md#why-this-order) for the alpha-wrap era's own
measurements (the settled `alpha=diag/800, offset=diag/2000` recipe,
capped at 0.15/0.06).

### What the judge cannot see

Steps 15–20 are the entire verdict, and they do not test winding directly —
though alpha-wrap's own watertight/manifold/self-intersection-free guarantee
(unconditional, regardless of input winding) sidesteps the specific failure
mode this section originally documented (a mesh with hundreds of inverted
faces reaching `PROCESS` unnoticed). See [discovered bugs](discovered-bugs.md)
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

**Status, 2026-09-22:**

1. ~~Define `RunConfig`, `Job`, `FileResult`, `RunEvent`, `RunSummary`~~ —
   **not done**. `tools/batch_repair.py` uses plain function arguments and
   in-memory counters/lists instead of these named value types.
2. **Done, informally.** `tools/batch_repair.py` is a serial runner:
   `converter.prepare` intake, then a serial per-mesh loop
   (`mesh_io.load` → `processor.process` → `processor.write`), with
   per-file try/except, mutually-exclusive terminal-result categories,
   and a printed run summary. Milestone 1, deliberately scoped narrower
   than this document — no `RunConfig`/`FileResult` types, no event
   stream, no child isolation.
3. **Not done — next milestone.** Move one job into a child process
   (matching the legacy `--one-file` entry's role, described in the
   archived design at
   `archive/docs-before-compact-2026-09-19/refactor/pool/d3.md`,
   `d8.md`, `d10.md`): a worker thread owns a `Popen`, and
   `communicate(timeout=)` is the sole timeout/kill mechanism — there is
   no watchdog process, because the thread that spawned the child is
   the thread that can kill it. This also fixes a real problem found
   2026-09-22: CGAL's `alpha_wrap_3` runs in-process and cannot be
   interrupted by Ctrl+C while it is running (no Python bytecode
   boundary for the signal to land on); putting it behind a subprocess
   boundary makes it killable the same way Blender already is.
4. **Not done.** `pool.Pool` is not yet driving `batch_repair.py`'s
   per-file loop; there is no scheduling, admission, or memory-aware
   worker sizing.
5. **Not done.** No structured event stream; only printed summary text.
6. **Not applicable yet.** No CLI/TUI beyond `batch_repair.py`'s own
   `argparse` interface; `stl_batch_fix_tui.py` still drives the legacy
   script, untouched.

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
