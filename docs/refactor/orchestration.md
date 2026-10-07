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
   OBJ/ASCII STL in-process (`textmesh`, one at a time in the runner's main
   thread, so Ctrl+C stops a conversion and removes its staged export), and
   collect probed binary meshes. Only `.stl` and `.obj` (any letter case,
   `converter.MESH_EXTENSIONS`) are meshes; files that are neither a mesh
   nor a companion are counted as `ignored` before any indicator check and
   never touched. Intake precedes isolated repair dispatch.
2. Preflight rejects invalid jobs, colliding destination/marker paths, and
   pre-existing publication paths among emitted jobs. A malformed source
   (`textmesh.Malformed`: a 5+-vertex OBJ face, a facet without three
   vertices, a bad index, a truncated facet) takes part in the collision and
   pre-existing checks, then gets its FAILED marker (full source copy) here
   when it passed both; otherwise it is rejected without one. Sort by
   triangle count.
3. Two sequential passes over the same `_Runner` (same pool, timeouts,
   cleanup and Ctrl+C handling). `pool.Pool` threads reserve memory via
   `RunState` and spawn one isolated `batch_repair_child.py` subprocess per
   admitted mesh; the parent passes it every value it needs, and the child
   never reads the TOML file. An oversized job may run alone.
   - **Prepare** (`--mode prepare`), reserved by `jobmemory.prepare_bytes`
     (source triangles): load with `mesh_io.load` (it drops triangles with
     NaN/inf coordinates or coincident corners; the step text says how many
     of each, once, in the pass that loaded the STL: a repair pass with a
     prepare handoff and a cache hit do not repeat it), run the initial
     decimation (step 1 below) and save it atomically as a PLY
     (`mesh_io.write_ply`) to `<input>.decimated/<job source path relative
     to the input>.<max_faces>.<decimator.settings_tag()>.ply` — beside the
     input folder, never inside it (a converted job is keyed by its
     `stl-exported/` copy; the tag changes with the decimator's
     parameters). The relative path is lexical: a source that is a symlink
     to a file outside the input keeps its own name, so its cache stays
     under `<input>.decimated` and inside the startup overlap checks. The PLY keeps the decimator's vertex table, so the repair
     pass gets it without re-welding, with the decimator's float64
     coordinates unrounded. Our loader, not PyMeshLab's STL reader, which holds ~4.5x the
     mesh's memory (docs/errors/decimation-memory-path.md). The cache is
     reused by later runs (sources are assumed unmodified) and rebuilt if
     unreadable; it is read back with `mesh_io.read_ply` and
     `jobmemory.repair_bytes` computed on it. It is written whenever the
     handoff names it, even if dropping non-finite triangles left nothing to
     decimate. Decimator failure publishes `UNDECIMATED` (source copy) and
     ends the job; a failed save is a write_failure; neither leaves a cache.
     The old `<input>/stl-decimated/` STL caches are not read (the converter
     still skips that folder). A success is a handoff (`childresult.PREPARED`:
     the mesh to load and the estimate), not a job result.
   - **Repair**, only if pass 1 was not cancelled and left nothing
     unresolved, reserved by the handoff's estimate: the child loads the
     prepared mesh (`--load-from`) with `max_faces 0` (decimation already
     ran) and runs steps 2–9; markers still copy the original source.
   Each job gets exactly one terminal result, carrying both passes' steps
   and elapsed time.
4. Each child probes/loads, processes, atomically writes output or marker
   (or, in prepare, the cache), then atomically writes its JSON `ChildResult`.
5. Parent waits with timeout, kills/confirms the process group, and validates
   the bounded result file regardless of exit code. With no trusted result:
   one new publication is recovered; multiple are inconsistent; none causes
   a source-copy timeout/failure marker, subject to cancellation authorization.
   That reason says how the child ended: `killed by SIGSEGV`, `exit status 3`,
   `exited normally without a valid result`, or, when the wait itself failed,
   `exit cause unknown`. A timeout says only `timed_out`: the status after our
   own SIGKILL is not the child's. No out-of-memory claim is made (owner,
   2026-10-05: the signal is enough; check the kernel log for an OOM kill).
   The same reason is appended to the model log as a `parent:` line.
6. Exactly-once reporting and counters produce the terminal summary. Recovered
   results get diagnostics even if the recovered output was clean.

Ctrl+C cancels admission and kills registered groups; a second Ctrl+C is
ignored during bounded cleanup. Unconfirmed cleanup cancels the run and keeps
jobs unresolved. Cancelled jobs receive no synthetic marker. Already committed
outputs can remain, so rerun behavior still follows existing output indicators.
Incomplete runs, diagnostics, and companion-copy failures return nonzero.

A runner that dies without cleanup (`kill -9`, the OOM killer) takes its
children with it: each child gets the runner's PID (`--parent-pid`) and,
before loading any library, asks the kernel for SIGKILL on parent death
(`proctree.exit_with_parent`, `PR_SET_PDEATHSIG`). A child whose runner is
already gone by then exits at once with status 75. Before this (found
2026-10-06), a SIGKILLed runner left its children running with no timeout
or memory admission. Limits: the signal is tied to the worker thread that
spawned the child, which reaps it before taking another job, so it never
fires early; a child whose kill could not be confirmed dies when that worker
exits. It reaches the child only, not its group: the default pipeline has
no descendants. Intake conversion runs in the runner itself, so it dies with it. Leftovers are
those of a timeout kill (pending marker, temp files); the next run redoes
the job.

## One mesh

| Order | Invocation | Condition / parameters |
|---|---|---|
| 1 | `decimator.make_step()` via `execstep.run_step` (`processor.decimate_initial`; in a batch it runs in the prepare child, and the repair child gets `max_faces 0`) | `max_faces` from `batch_repair.toml` via `StepConfig.faceCount`; zero or already within target skips; failure → `UNDECIMATED`. PyMeshLab `meshing_decimation_quadric_edge_collapse` with default parameters, one call. Same step implementation as 5b. |
| 2 | `repairer.repair` | Measure input component volume; compute `whole_model_diag` (`scanner.diagonal`) after decimation, before split; `WHOLE_MESH_STEPS` currently empty |
| 2a | Model gate (opt-in `skip_clean = true`) | After `WHOLE_MESH_STEPS`: if `is_already_clean`, skip 3–6 and go to 7 with the decimated mesh; verdict logged as `clean_gate` |
| 3 | `repairer._split_stage` → `splitter.make_shell_split_step` | Always present; shells with fewer than `min_shell_faces` faces are dropped (`batch_repair.toml`, default 100 = `splitter.MIN_SHELL_FACES`, 0 keeps all; passed child argv → `processor.process` → `repairer.repair`); if all shells are below floor, retain original mesh |
| 4 | (seam split) | Absent from the default split composition; add a `collection_entry` in `_split_stage` to enable, not a flag |
| 4a | Part gate (same `skip_clean`, model not clean) | Per retained part: if `is_already_clean`, merge it as split, bypassing 5 (default or custom `part_steps`); verdict logged as `clean_gate` with the part id. NM fast path: if NM edges are the only scanned defect (open 0, degenerate 0, winding seams 0) and there are at most `repairer.NM_FAST_PATH_MAX_PERCENT` NM edges per 100 faces of the part (0.05, provisional; a ratio, so no absolute cap on a very large part), MeshFix alone runs as step `nm_meshfix`; its result is merged instead of running 5 when finite, with no NM/open edges or winding seams left and component volume within `NM_FAST_PATH_VOLUME_BAND` (98–102% of the part's, provisional). Otherwise — including over the limit, unmeasurable input volume, MeshFix failure or any error — 5 runs on the original part; never a failure. Decision logged as `nm_fast_path` with the NM count, part faces, percentage and outcome |
| 5 | Per-part `DEFAULT_PART_STEPS`, via `execstep.run_sequence` | `(winding, decimate, meshfix)` by default; caller may replace `part_steps` entirely — nothing appended |
| 5a | `winding.step_winding_reconstruct` | Rebuilds the part as a solid: grid spacing `h = min(whole_model_diag/800, 0.15)` (alpha-wrap's alpha); block count from `StepConfig.reconstruct_memory_budget_bytes` (`batch_repair.toml` `reconstruct_memory_budget_gb`, default 10). A part that rebuilds to nothing (an open sheet such as debris, or a closed part thinner than the grid) is dropped, with `dropped` and the reason in the step detail; the model is merged without it, and the judge's retained-volume check still catches gross loss. Fails the part if no block count fits the estimate, or if the result is not closed/manifold/non-degenerate. Replaced `alphawrap.step_alpha_wrap` (2026-10-03), which remains available as an explicit entry |
| 5b | `decimator.make_step()` | Reads `faceCount` (target captured after splitting, before wrapping) from `StepConfig`; same implementation as step 1, ONE call (PyMeshLab defaults; the former `decimate_again` round was removed with fast_simplification, 2026-10-04 — see [reconstruction](reconstruction.md#decimation-after-reconstruction-2026-10-04)). Missing the target is **never** a reason to fail the part or the mesh (owner decision, 2026-10-03): the other steps decide whether the model is repaired, and extra faces only make slicing and printing slower. |
| 5c | `execstep.ConditionStep(scanner.scan, scanner.has_defects, meshfix.step_meshfix_repair)` | MeshFix only if the decimated part has open or non-manifold edges (decimating any rebuilt surface adds a few); unavailable/failed tool fails the step |
| 6 | `execstep.run_merge_step` → `splitter.merge` | Concatenate successful parts; no welding or boolean union |
| 7 | Closing repair checks | Finite coordinates, component volume, lost-vertex measurements; exceptions fail repair |
| 8 | `processor._judge` | Ordered gates below |
| 9 | `processor.write` | Atomically write accepted mesh or a full-mesh failure marker |

Gate check on real models (2a/4a; 2026-10-06, `skip_clean = true`,
`max_faces = 900000`, outputs under `/mnt/sda2/STL/GateSample/out/`): five
models were skipped whole, four had some clean parts merged unrepaired, and
every output passed the repair tool Bambu recommends, which found nothing the
scan missed. The Amidara base (no NM or open edges, 922 winding seams) fails
the gate and is repaired normally (100.00% volume, 0 seams). Slicing and
printing were not confirmed; the task was closed by the owner (2026-10-06)
and `skip_clean` stays off by default.

Reconstruction uses `h=min(diag/800, 0.15)` (alpha-wrap, when listed explicitly,
uses that as alpha with `offset=min(diag/2000, 0.06)`); `diag` belongs to the
whole mesh **after initial decimation**, not each part.
The captured per-part target is best-effort, including when CLI max faces is
zero. There is no post-merge decimation. Each entry in a part's sequence
produces its own `Step.PART` record — three by default (winding, decimate,
meshfix; a skipped step still records), one per entry in
whatever sequence actually ran.

Judge order: failed repair → `FAILED`; non-finite volume/ratio → `FAILED`;
retained component-volume ratio below 0.90 → `DESTROYED`; non-manifold edges
→ `UNREPAIRED`; open edges → `OPEN_EDGES`; no finite positive enclosed volume
→ `BROKEN`; otherwise `PROCESS`. Rejection precedes acceptance even if topology
looks clean. Source markers preserve the source; topology-failure markers can
contain the repaired mesh. Tool execution success alone is not acceptance.

## Job memory (calibration, 2026-10-04)

`libs/jobmemory.py` sets each pass's reservation. Measured with
`/usr/bin/time -v` (max RSS) on the real child, one model at a time,
`reconstruct_memory_budget_gb` 10, `max_faces` 900,000 unless noted.

The previous estimate (source triangles × 890 B × 3) was wrong both ways:
foot1 (1,232 faces) reserved 3 MB and peaked at 2.6 GB; a 6,160-face sphere
of radius 132 reserved 16 MB and peaked at 13.9 GB; join_complication
(3.8 M faces) reserved 9.5 GB and peaked at 2.0 GB. Face count does not
predict the peak. Two things do:

- the part's reconstruction, which `winding.plan` sizes to fit the budget
  (its estimate held on every run, ≥ 1.34× on the sphere's winding phase);
- PyMeshLab decimation of the rebuilt surface, with ~3·A/h² rebuilt faces
  (measured 2.6–3.2) and no budget at all (the sphere's 13.9 GB).

Per-face costs, each tool alone in a fresh process on winding-rebuilt
spheres (h 0.15):

| Input faces | PyMeshLab decimation, added | MeshFix, added |
|---|---|---|
| 300,612 | 475 B | 531 B |
| 996,332 | 476 B | 520 B |
| 2,952,028 | 504 B | 516 B |
| 9,923,104 | 477 B | — |

Constants are ~1.3× these (decimation 650 B, MeshFix 700 B per face; 3.5
rebuilt faces per A/h²).

Acceptance (estimate ≥ 1.25× measured peak):

| Model | Prepare peak | Repair estimate | Repair peak | Ratio |
|---|---|---|---|---|
| foot1 (2 parts) | 122 MB | 4.04 GB | 2.75 GB | 1.43 |
| sphere r 132 (6,160 faces, 29 M rebuilt) | 123 MB | 24.2 GB | 14.6 GB | 1.62 |
| Mirko (300 k, not decimated) | 223 MB | 3.23 GB | 1.74 GB | 1.81 |
| join_complication (3.8 M → 900 k, 9 parts) | 1.80 GB | 2.79 GB | 1.47 GB | 1.86 |
| join_complication, cache hit | 307 MB | — | — | — |
| join_complication, `max_faces` 0 (10 parts) | 858 MB | 3.40 GB | 1.88 GB | 1.76 |

Prepare reserves 400 MB + 700 B per source face (join: 3.06 GB for 1.80 GB,
1.70×). The decimation cache round-trips exactly: join (900 k) and Mirko
(100 k) reloaded with the same vertex, shell, part, non-manifold and
open-edge counts and the same volume as the in-memory result. These are
fits to these runs, not bounds; MeshFix-heavy multi-part models are not yet
in the table.

**Re-measured with float64 vertices (2026-10-05).** Same method, both code
trees on the same machine in one session (before: `daa5687`, float32
`Geometry`). The constants were kept: every peak moved by 0–4 %, and every
estimate still exceeds its peak by at least 1.44×. Mirko is no longer on
disk; its row above was not re-measured.

| Model | Prepare peak, before → after | Repair peak, before → after | Estimate | Ratio after |
|---|---|---|---|---|
| foot1 | 130 → 130 MB | 2.77 → 2.82 GB | 4.04 GB | 1.44 |
| sphere r 132 | 130 → 130 MB | 14.90 → 15.07 GB | 24.2 GB | 1.61 |
| join_complication (→ 900 k) | 1.89 → 1.91 GB | 1.61 → 1.68 GB | 2.79 GB | 1.66 |
| join_complication, cache hit | 326 → 331 MB | — | — | — |
| join_complication, `max_faces` 0 | 0.90 → 0.92 GB | 1.94 → 1.97 GB | 3.40 GB | 1.72 |

Prepare on join: 3.06 GB reserved for 1.91 GB, 1.60×. The "before" column
differs from the 2026-10-04 table (join repair 1.47 → 1.61 GB) because the
code changed in between (PLY cache, planarquadric decimation, chunked
load); compare within this table only.

## Limits and observability

- Initial-decimation loss, meaningful small-part loss, and final winding are
  not fully guarded by the judge. See [modules](modules.md).
- Each model has a raw log beside its output (`foo.log`): intake conversion
  and every repair attempt append a dated header; a repair child's stdout and
  stderr go straight into it (all tools, crash messages, faulthandler
  traceback), with a separator line before and after each step. Intake
  conversion adds one line with what it wrote and dropped, or why it failed. A log that cannot be written
  only produces a warning.
- Incremental step logging exists (`batch.log`), and `progress.log` persists
  run/progress/job/final records; the terminal summary is printed separately.
- Admission estimates are calibrated on measured runs (see "Job memory"),
  not enforced limits; an oversized job admitted alone can exceed the budget.
  The automatic budget is `memory_budget_fraction` × `MemAvailable`
  (`/proc/meminfo`), falling back to free pages.
- Post-reconstruction decimation is bounded by no budget: memory and time
  follow the rebuilt face count (~3·A/h²). A 6,160-face sphere of radius 132
  rebuilt to 29 M faces and decimated for 353 s at 13.9 GB. Admission now
  reserves for it; reducing it is an open owner decision.
- Process-group cleanup cannot cover descendants that deliberately leave the
  group.
- Startup checks every library once (`libs.dependencies.check_all`, from
  `_check_environment`): NumPy, SciPy, libigl, PyMeshLab and PyMeshFix
  required (Blender is retired since 2026-10-06, see modules.md), CGAL
  optional (alpha-wrap is explicit-use; its flag
  flips when alpha-wrap is wired back in). One line each: `[debug]` when
  available or an optional one is missing, `[error]` when a required one is,
  and the run stops. Library modules import their packages plainly, so a
  missing required package usually fails earlier, at import. No step
  handles a missing library (owner, 2026-10-05).
- No TUI is planned: runs are configured by editing `batch_repair.toml`.
