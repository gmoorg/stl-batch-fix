# TODO

Live task list. Refreshed 2026-09-22 from the archived
[open-issues.md](../../archive/docs-refactor-2026-09-22/open-issues.md), which
predates alpha wrapping and is mostly resolved history now — only what is
still open against the current architecture is carried forward here. New
items go here directly; do not add a fourth tracking file.

## Runner and concurrency

- **Per-file isolation, scheduling, and reporting: done, 2026-09-23.**
  `tools/batch_repair.py` now dispatches through `libs.pool.Pool` driving
  worker threads, each owning an isolated `--one-file` child process
  (`libs.runstate.RunState` + `libs.proctree.terminate_and_confirm` +
  `libs.childresult` + `libs.publication`). This also fixed the Ctrl+C bug
  (previously listed here as open): `RunState` lets a `KeyboardInterrupt` in
  the main thread find and kill every live child's process group. Full
  design history: [HANDOFF-batch-runner-pool.md](HANDOFF-batch-runner-pool.md).
  Still open from that design:
  - **`ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED = 3`** (`libs/runstate.py`) is an
    explicit placeholder, not a calibration — alpha-wrap can multiply
    triangle count well past its input and no real peak-RSS-vs-input-
    triangle measurement exists. Replace once that measurement exists. The
    `890` bytes/triangle figure it multiplies is confirmed-real for the
    decimator specifically (user-confirmed 2026-09-22), not a guess.
  - No shared "reporting/event" component beyond printed summary text — see
    [orchestration.md](orchestration.md#build-order) item 5.
  - `RunConfig`/`Job`/`RunEvent`/`RunSummary` typed contracts still not
    built; `batch_repair.py` uses plain function arguments and in-memory
    counters/lists. `ChildResult` is the one contract from that original set
    that does now exist as a real, validated type.
  - **No progress logging during a run: fixed, 2026-09-23.** Found the same
    day (user-reported, mid real batch run) after the pool rewrite lost the
    old serial runner's `[index/total] path` lines and never replaced them.
    `_Runner` now prints (to stderr, flushed): a `Scanning ...` line before
    intake, an `Intake done: N job(s) ..., W worker(s)` line after, a
    `[start] path` line the moment each job's child is actually spawned
    (`_run_one`), and a `[done/total] status: path [step summary]` line on
    every completion (`_report`) — the two print sites share
    `_Runner._results_lock` so concurrent worker threads can't interleave
    mid-line. `repairer.Result.steps` (per-step name + detail — split/merge
    counts, alpha-wrap's actual alpha/offset — computed but previously
    discarded entirely downstream of `repairer.repair()`) now rides along
    on `ChildResult.steps` and appears in that completion line's bracketed
    suffix. A fuller structured version is still the "shared
    reporting/event component" listed below as not built.
- **Logging is still inadequate for real investigation — found 2026-09-23,
  user-reported after a real batch run.** Four separate problems, not one:
  1. **`decimator.decimate()`'s outcome is never logged.** `_process_one_file`
     (`tools/batch_repair.py`) only reads `outcome.repair.steps` into
     `ChildResult.steps` — it never reads `outcome.decimation` (first pass)
     or `outcome.final_decimation` (second pass), even though
     `decimator.Result` already carries `Rung`/`attempts` data (which rung
     succeeded, what failed and why). The decimation stage is entirely
     invisible in the log despite the data existing on `processor.Outcome`.
  2. **The terminal window is the only log — nothing persists.** Once the
     terminal closes (or scrolls past its buffer, or the session ends),
     every `[start]`/`[done]` line from the fix above is gone; there is no
     way to investigate a run after the fact. Needs a `--log-file` (or
     always-on log file under the output tree) that mirrors stderr, at
     minimum.
  3. **No per-file processing time is logged: partially fixed, 2026-09-23.**
     Every stderr line (`[start]`/`[done/total]`/`Scanning`/`Intake done`/
     etc., all now routed through a single `_log_line` helper in
     `tools/batch_repair.py`) carries an `HH:MM:SS` timestamp, so elapsed
     time between a file's `[start]` and its `[done/total]` line can be read
     off by hand. Still missing: the duration is not COMPUTED and printed as
     its own number (e.g. `12.4s`) — an investigator has to subtract two
     timestamps themselves. `repairer.StepResult.second_elapsed` and
     `decimator`'s own timing (if it has one) already exist per-step and
     could be summed/printed directly instead.
  4. **Step logging is buffered until the whole file finishes, so a crash
     leaves no record at all — found 2026-09-23, user-reported, more
     serious than a formatting complaint.** `ChildResult.steps` (added
     2026-09-23) is built entirely inside the `--one-file` child's own
     memory (`outcome.repair.steps`) and only serialized into the result
     JSON once the child reaches the very end of `_process_one_file`. If
     the child crashes mid-repair (CGAL segfault, OOM kill, timeout kill) —
     exactly the file an investigation is most likely to be about — nothing
     it was doing gets logged at all: no record of which step it was on,
     how far it got, or how long it had already been running. User's
     explicit spec: log **before and after each step**, with that step's
     duration, written incrementally as it happens — not buffered until
     success. Concretely: each step function (or the loop in
     `repairer.repair`/`_run_step` that calls them, per
     `docs/refactor/modules.md`'s "uniform step interface" section) needs to
     emit a line at start and a line at end-with-duration, and that emission
     has to reach durable storage (a file, not just an in-memory list)
     immediately, from inside the child process, before the step's own
     possible crash — not deferred to the final JSON write. This also folds
     in the earlier, narrower complaint that one file's whole step history
     was crammed into a single `;`-joined line: before/after-per-step lines
     are naturally one-line-per-event and solve both problems together. Needs
     design: where the incremental writes go (a per-job temp file the parent
     can inspect even after a crash, similar in spirit to `--result-file` but
     written to across the run rather than once at the end; a raw stderr
     line per step, timestamped, would also satisfy "not buffered" even
     without a separate file) and how it interacts with `--result-file`'s
     existing atomic-write contract (that one must stay all-or-nothing per
     `childresult.write`'s docstring; the incremental step log is a
     deliberately different, non-atomic stream).
  ~~5. The completion line's status word is an internal category name, not
     a plain success/fail verdict.~~ **Withdrawn, 2026-09-23** — user: "I
     wrote it when log was barely there. The tech outcome is good enough."
     Written before the `libs/steplog.py` rework; the per-step tab-delimited
     log (`decimate`/`split`/`step_alpha_wrap`/`scan`/`merge`/`judge`, each
     with its own stable field and the real outcome — e.g. `judge`'s `end`
     line carries the actual `PROCESS`/`DESTROYED`/`FAILED` indicator
     directly) already gives enough structure that the `_run`-level
     completion line's bare category word is no longer the only way to see
     what happened. No longer tracked as its own item.
  All four remaining point the same direction: `_run`'s printed summary was
  never meant to be the durable record, and the "shared reporting/event
  component" already listed above as not built is where a real fix belongs
  — a structured, persisted, per-step, timed log rather than more ad hoc
  print lines layered onto stderr.
- **FIXED, 2026-09-23: alpha-wrap's second decimation pass reintroducing
  non-manifold edges — replaced the whole-mesh second decimation pass with
  per-part decimation and conditional PyMeshFix.** Root cause confirmed on a
  real batch run (`/mnt/sda2/STL/Fixing/Monkey King/stl/Merged_ESM_MK_STL.stl`,
  `100 non-manifold edge(s) remain`) and independently on Bambu Studio's own
  simplify feature — "decimation after a topology-guaranteeing repair step
  is not safe" is a real, general failure mode. The gate that caught it
  (`UNREPAIRED`, not silently shipped) was always working correctly; there
  was just no fix, until now.

  **Design, agreed 2026-09-23 (user, working without Codex — token limit):**
  decimate each PART back to its own pre-alpha-wrap size individually
  (`repairer._decimate_to_target_and_fix`, called from `repair()`'s PART
  loop with `target_faces = len(part.geometry.faces)` captured immediately
  after `splitter.by_shells`, before alpha-wrap inflates it), scan the
  result, and if that decimation reintroduced NM/open-edge defects, run
  PyMeshFix (`meshfix.repair`, the low-level call — cheaper than Blender,
  per stated cost preference) on that PART only. Replaces the old whole-mesh
  `decimate_final` pass in `processor.process()` entirely — removed, along
  with the `final_decimation` field/branch it fed through `Outcome`/`_decide`/
  `_judge`/`write()` (full simplification, not a shim; `_judge` collapsed
  from a two-branch function to one).

  **Non-uniform step, deliberately.** `target_faces` differs per part
  (unlike `alphawrap.step_alpha_wrap`'s `whole_diagonal`, one value for the
  whole file), so it cannot be bound once via `functools.partial` before the
  PART loop the way `whole_diagonal` is — it is bound fresh inside the loop,
  as a second, separate `_run_step(Step.PART, ...)` call per part, right
  after the named `part_steps` tool call. User: "Do not like non uniform
  step" — accepted as the pragmatic implementation, with a follow-up
  recorded above ("Give the uniform step contract an optional, typed
  per-call context argument") for a cleaner general mechanism later, rather
  than blocking this fix on designing that first.

  **Real confirmation, 2026-09-23**, on `tests/fixtures/foot1.stl` through
  the actual `--one-file` child (not a mock): part 2's decimation to its
  128-face target introduced 21 non-manifold edges, and
  `decimate_to_target` immediately fixed them via PyMeshFix before merge —
  `decimate_to_target: decimated to 128f introduced nm=21 open=0, pymeshfix
  fixed it`, judge reached `PROCESS`. Part 1 decimated clean (no defects),
  so PyMeshFix correctly never ran for it — confirming the conditional gate
  works both ways, not just the fix path.

  Every `Step.PART`-position-dependent test updated for the new "two PART
  `StepResult` entries per part" shape (the named tool call, then
  `_decimate_to_target_and_fix` — distinguishable by `'decimate:' in
  s.detail`): 2 in `test_repairer.py`, 3 in `test_repair_pipeline.py` (a
  shared `tool_call_parts()` helper added there to avoid repeating the
  filter). `TestFinalDecimation`'s 6 tests in `test_processor.py` (all
  specifically testing the removed second pass) deleted outright, matching
  this session's `tool=` removal precedent — not repurposed, since they
  tested behavior that no longer exists. Full suite: 627 tests passing
  (down from 633 — the 6 deleted), plus the real end-to-end confirmation
  above.

  Still open: how often this actually fires on a real corpus (one observed
  case plus one reproduced case, not yet a measured rate); the PLY-not-STL
  constraint for any FUTURE design needing disk-persisted part intermediates
  (not needed here — `_decimate_to_target_and_fix` stays entirely in
  memory, no per-part disk round-trip); whether
  `docs/refactor/orchestration.md`'s step 14/14a documentation (which still
  describes the old whole-mesh second pass) needs updating to match.
- **Resolve OBJ/STL same-stem collisions — downgraded, not critical.**
  User, 2026-09-23: not an issue in practice, since the source folder is
  user-controlled and this collision (a same-stem OBJ/STL pair in
  `converter.prepare`'s intake, distinct from the batch runner's own
  preflight collision check which only covers distinct jobs colliding on
  their eventual output path) would require an unusual input tree to
  trigger. Left here as a known, low-priority gap rather than removed.
- Serialize PyMeshFix output capture or isolate it (only matters if/when
  PyMeshFix is re-wired into a step; currently unwired).
- Track concurrent Blender processes and kill descendants on
  timeout/interruption — likely superseded now that `libs.proctree` exists
  and already does exactly this for the batch runner's own children; check
  whether `libs/blender.py`'s own `Runner` should be moved onto the same
  mechanism rather than keeping its separate plain `proc.kill()`.

## Configuration

- **Uniform steps and explicit execution conditions — agreed direction,
  2026-09-26; not implemented.** Supersedes the 2026-09-23 open-ended
  optional step-context proposal ("Do not like non uniform step").

  **Owner's rationale:** "our modules [are] too complicated and polluted by
  logic injected in unexpected places"; "I want a clear view of what we
  invoke and when." Reading the pipeline definition must reveal the
  operations, their order, and their execution conditions in one place.
  Adding classes alone does not satisfy this request if hidden tool calls,
  special branches, or duplicated execution paths remain.

  **Shared configuration and mesh-step contract:** define a `StepConfig`
  class in `pipeconfig`, initially with only these properties:

  ```python
  @dataclass
  class StepConfig:
      faceCount: int = 0
      whole_model_diag: float | None = None

  # Mesh-transform step contract:
  # step(mesh, config: StepConfig | None = None) -> (ok, mesh, detail)
  ```

  Every pipeline step accepts the optional configuration. Decimation reads
  `faceCount`; alpha wrap reads `whole_model_diag`; other steps accept and
  ignore the configuration. Compute the diagonal once from the whole mesh
  after initial decimation and before splitting, preserving current alpha
  sizing. Carry that same diagonal into each part's configuration. Remove
  alpha wrap's special `partial` binding for `whole_diagonal`.

  **One decimator step, invoked in two positions:** the initial whole-model
  call receives the CLI face count. The post-alpha-wrap call receives that
  part's face count captured immediately after splitting, before wrapping.
  `--max-faces 0` disables only initial decimation; post-wrap decimation
  still runs with the part's own target. This is a best-effort target:
  document that a different returned face count alone must not reject the
  model, per the owner's review decision. Retain execution-failure and
  geometry checks. Both positions use the same decimator step implementation
  and invocation contract.

  **Separate collection steps:** shell splitting and seam splitting each
  become an explicit step accepting and returning an array/collection of
  meshes, with the optional `StepConfig` argument. Each applies its split
  operation to the incoming meshes and returns the flattened collection.
  Keep this collection-to-collection signature distinct from the
  mesh-to-mesh contract; changing cardinality is explicit. Preserve existing
  shell filtering and the default-disabled seam split.

  **Separate operations:** remove `_decimate_to_target_and_fix` from
  `repairer.py`. Decimation and MeshFix are independent sequence entries.
  Decimation owns reduction; MeshFix owns its tool execution and specific
  failure handling. Repair orchestration must not hide one inside the other.

  **`ConditionStep`:** encapsulate three components, in this order:
  a condition-data provider (such as `scanner.scan`), a predicate consuming
  its result, and the operation to execute. Obtain the condition data from
  the current input when the entry is reached; execute the operation only
  if the predicate says so. A false predicate passes the input through.
  Unconditional entries use the same entity with the first two components
  set to `None`. Illustrative composition (predicate name not implemented):

  ```python
  ConditionStep(scanner.scan, scanner.needs_repair, meshfix.step_meshfix_repair)
  ConditionStep(None, None, decimator.step_decimate)
  ```

  General predicates interpreting scanner results belong in `scanner`;
  predicates specific to a repair tool belong in that tool's module.
  `ConditionStep` handles conditional invocation, not tool-specific policy.
  The executor handles configuration passing, logging, result recording,
  and failure propagation consistently. Measurement and predicate errors
  must not be treated as a successful skip.

  **Intended visible sequence:**

  ```text
  Decimate whole model       CLI face count; zero skips
  Split by shells
  Split by seams             disabled by default
  For each part:
      Alpha wrap             whole-model diagonal
      Decimate               part's pre-wrap face count
      MeshFix                only when the scan says repair is needed
  Merge
  Judge
  ```

  **Acceptance:** both decimation positions use one step implementation;
  step configuration needs no tool-specific argument binding; split steps
  expose their collection contract; decimation does not call MeshFix;
  conditions and operation order are visible in the pipeline definition;
  shared execution records each operation and its condition consistently.
  Preserve default ordering, repair decisions, geometry validation, and
  failure propagation while changing the structure. Do not enable the
  already-clean skip gate or seam splitting as part of this refactor.
  The source/part identity fixes from
  [the review](REVIEW-2026-09-26.md) remain required for unambiguous logs.
- **Move the value-parameters that tune enabled steps into `pipeconfig`.**
  Owner decision, 2026-09-21: only binary `ENABLE_*` switches were extracted
  so far. Still inline in code:
  - `meshfix.CLEAN_MAX_ITERS`, `meshfix.CLEAN_INNER_LOOPS`,
    `fill_small_boundaries(0, True)`'s arguments (all unwired currently, but
    not deleted).
  - `repairer.LOST_VERTEX_TOLERANCE = 1e-4`.
  - `splitter.MIN_SHELL_FACES = 100` (partially exposed as
    `repairer.repair()`'s `min_shell_faces` default).
  - `welder.MAX_ROUNDS = 10`, `welder.MAX_CHAIN = 12`.
  - `processor.MIN_VOLUME_KEPT = 0.90`.
  - `blender.convert`/`blender.repair`'s `timeout: float = 600` defaults,
    `blender.STEP_TIMEOUT = 600`, `blender.is_available()`'s inline
    `timeout=30` — one policy, three places.
  - Excluded as out of scope: format/plumbing constants
    (`mesh_io.BYTES_PER_TRIANGLE`, `HEADER_BYTES`, PLY magic bytes,
    `blender.py`'s script-path constants).
- **Move step ordering into `pipeconfig`, with per-step IDs — lowest
  priority, user 2026-09-23.**
  Owner proposal, 2026-09-21. Landed so far: `Step` collapsed to 4 members
  (PREP, SPLIT, PART, MERGE); `WHOLE_MESH_STEPS`/`PART_MESH_STEPS` both now
  `tuple[tuple[str, Callable], ...]`. **`tool=` removed 2026-09-23**, per
  user: "when should not pass tool, this was a stupid idea from the
  beginning, when should pass the list of steps instead" — confirmed
  `tool=` was only ever used by tests (production always passed `None`),
  and it threw away each step's own name, forcing `_run_step`'s logging to
  fall back to a generic `'part'` label. `repair()` now takes
  `part_steps: tuple[tuple[str, Callable], ...] | None = None`, the same
  shape `PART_MESH_STEPS` itself is — replacing it for one call rather than
  handing in an opaque callable. `whole_diagonal` binding for
  `alphawrap.step_alpha_wrap` is applied per-entry and computed lazily
  (only when a `step_alpha_wrap` entry is actually present), so a fully
  custom `part_steps` sequence still pays nothing for it, matching what
  `tool=` used to guarantee. Every `test_repairer.py`/`test_processor.py`
  caller updated to the new shape. Not done: moving `WHOLE_MESH_STEPS`/
  `PART_MESH_STEPS` into `pipeconfig`; a separate per-step ID enumerator; a
  `SPLIT_STEP` entry (open question — SPLIT/MERGE change cardinality, so
  the uniform one-in-one-out step contract may not fit them).
- **Remove the vestigial `ENABLE_*` boolean flags — user, 2026-09-23:
  "since we control what need to be executed in the repairer, we do not
  need the Boolean flags what enable/disable steps."** Confirmed by
  reading the code: `WHOLE_MESH_STEPS = ()` (empty) and `PART_MESH_STEPS =
  (('step_alpha_wrap', alphawrap.step_alpha_wrap),)` are what actually
  determine what `repairer.repair()` runs — a step not IN one of these
  tuples never executes, regardless of its `ENABLE_*` flag's value. Of the
  13 `ENABLE_*` flags in `libs/pipeconfig.py`
  (`ENABLE_WELD`, `ENABLE_CLEAN_NULL_FACES`, `ENABLE_CLEAN_MERGE_CLOSE`,
  `ENABLE_CLEAN_DUPLICATE_FACES`, `ENABLE_CLEAN_UNREFERENCED`,
  `ENABLE_ORIENT`, `ENABLE_BLENDER_PART`, `ENABLE_PART_TOOL`,
  `ENABLE_FILL_BOUNDARIES`, `ENABLE_CLEAN`), only `ENABLE_ALPHA_WRAP`
  gates a step actually reachable through the tuples — every other flag
  gates a step function that is unreachable regardless of the flag's own
  value (welder/CLEAN/orient/Blender/PyMeshFix — all absent from
  `WHOLE_MESH_STEPS`/`PART_MESH_STEPS`). `ENABLE_SPLIT_SHELLS` and
  `ENABLE_SPLIT_SEAMS` are a separate case — checked directly in
  `repair()`'s own body, not via the steps tuples — and are NOT part of
  this observation; they still meaningfully gate behavior today.

- **DONE, 2026-09-23: removed the 10 dead `ENABLE_*` flags.** User: "since
  we control what need to be executed in the repairer, we do not need the
  Boolean flags what enable/disable steps." `ENABLE_SPLIT_SHELLS`/
  `ENABLE_SPLIT_SEAMS`/`ENABLE_ALPHA_WRAP` stayed (still live, gate real
  behavior). Resolved the open question the same way it leaned: the guard
  is gone entirely, not replaced with anything — a step unreachable from
  `WHOLE_MESH_STEPS`/`PART_MESH_STEPS` now simply runs unconditionally
  when called directly (test, or `part_steps=` override), with no
  "disabled" concept left for these 10 steps. Every read site removed
  (`libs/welder.py`, `libs/meshlab.py` ×5, `libs/meshfix.py` ×3,
  `libs/blender.py`), each module's now-unused `pipeconfig` import removed
  too. `meshfix.repair()`'s `fill_small_boundaries`/`clean()` calls (gated
  by `ENABLE_FILL_BOUNDARIES`/`ENABLE_CLEAN`, which tuned behavior INSIDE
  one call rather than skipping a whole step) now run unconditionally,
  restoring their pre-flag behavior. Tests that specifically asserted on
  the disabled-skip behavior removed from `test_welder.py`,
  `test_meshlab.py`, `test_meshfix.py`, `test_blender.py`, and
  `test_repairer.py` (3 tests: `test_disabling_blender_alone_still_runs_
  pymeshfix`, `test_disabling_pymeshfix_alone_still_runs_blender`,
  `test_disabling_both_runs_neither`) — each file's now-unused imports
  cleaned up alongside. `docs/refactor/orchestration.md`'s flag table and
  `docs/refactor/modules.md`'s per-module entries updated to match. Full
  suite green after (628 tests — down from 636, the 8 removed disabled-skip
  tests).

## Algorithm questions

- **SPIKE, 2026-09-23: does an already-clean mesh (NM=0, open=0,
  winding-consistent) need alpha-wrap at all, given self-intersections alone
  don't fail a print?** User's framing: a mesh with no non-manifold edges
  and no open edges might still be safely skippable — self-intersections by
  themselves don't break slicing/printing, only inverted normals and real
  topology defects (NM/open edges) do, so if winding is ALSO confirmed
  consistent, alpha-wrap may be pure cost with no benefit on such a mesh.

  **This exact idea was proposed and rejected before** — see the archived
  "Do not repair a part that is already clean and within the face budget"
  (owner decision, 2026-09-21,
  [open-issues.md](../../archive/docs-refactor-2026-09-22/open-issues.md)) —
  and rejected in the very next paragraph of that same doc: `Scan.is_clean`
  (`open_edges == 0 and non_manifold == 0`) says nothing about winding, and
  Amidara's real `base.stl` had 1,506 inverted normals and 922
  winding-seam edges while reading as fully `is_clean`. A skip-gate built on
  `is_clean` alone would have let that broken model through untouched.

  **What's different this time:** explicitly adding a real winding check to
  the gate, closing the exact hole that killed the idea in 2026-09-21 —
  `is_clean` alone tested NO winding at all, so Amidara base's 1,506
  inverted normals were invisible to that gate by construction, not merely
  missed by an imperfect check.

  **Correction, 2026-09-23 (user caught this):** `scanner.winding_seams`'s
  known blind spot — it only examines edges with exactly two incident
  faces, missing winding inconsistency on edges shared by 3+ faces — does
  NOT apply to this gate. A gate requiring `NM == 0` as one of its own
  conditions already guarantees no edge has 3+ incident faces (that is
  what non-manifold means), so on any mesh that actually passes this gate,
  the blind spot's precondition can never occur. `scanner.winding_seams` is
  therefore sufficient here — no need for PyMeshLab's
  `meshing_re_orient_faces_coherently` or any other tool, and no new
  dependency question. The Amidara base case that motivated bringing in a
  heavier check doesn't transfer: that mesh had BOTH inverted normals AND
  922 winding-seam edges, so it would have failed a `winding_seams == 0`
  check directly, on the same simple test already in `scanner.py` — the
  extra tool was never actually needed to catch it.

  **Spike plan, revised 2026-09-23:** measure real models (the existing
  probe corpus — Amidara base/hands_2, Mandy, others already used
  throughout this project's alpha-wrap tuning) for: (a) which have
  self-intersections present but `NM == 0`, `open_edges == 0`, AND
  `winding_seams == 0` (all three via `scanner.py`'s existing functions,
  no new tool) — these are the candidates a skip-gate would actually apply
  to; (b) confirm on at least one such model that self-intersections alone
  genuinely don't cause a slicing/printing failure (this is asserted, not
  yet verified within this project — may need an actual slice/print test,
  not just a topology argument). Confirm Amidara base itself correctly
  FAILS this three-way gate (it should, via `winding_seams` alone, per the
  correction above) as a sanity check that the gate isn't accidentally
  weaker than intended.

  **Gate function built, 2026-09-23** — user: "Write it in repairer. Make
  it stateless - read mesh as argument." `repairer.is_already_clean(mesh)
  -> bool` (right before `repair()` in `libs/repairer.py`): checks
  `scanner.scan(mesh).is_clean` (NM=0, open=0) AND
  `scanner.winding_seams(mesh)[0] == 0` (seam edges, not the narrower
  `closed_loops` second value — matches how Amidara base's "922
  winding-seam edges" was originally measured). Takes only the mesh; reads
  no `pipeconfig` flag or other module state, so if this gate ever needs
  tweaking or disabling, that is a one-function change, not a flag spread
  across callers. 5 synthetic unit tests in
  `tests/tests/test_repairer.py`'s `TestIsAlreadyClean` (clean tetrahedron,
  open edge, non-manifold edge, LOCALLY inverted winding — the exact case
  `Scan.is_clean` alone misses, proving the gate actually closes that gap —
  and a fully/globally inverted mesh, which the gate correctly still calls
  clean since global orientation is a different question from local
  winding consistency). **Not wired into `repair()`/`process()` yet** —
  building the function was itself judged not to need the "prototype first"
  caution this spike's plan originally called for (user: "not sure why you
  want to do a prototyping for a stuff you already have"), since
  `scanner.scan`/`scanner.winding_seams` already exist and are
  well-understood; but wiring it into the live pipeline is still a
  separate, later decision, and the measurement plan above (real probe
  corpus, confirm Amidara base fails the gate, verify self-intersections
  alone don't fail a print) has not been run yet.

- Handle reversed/non-monotone T-junction paths (`welder.py`).
- Distinguish a far bent junction from a hole; no defensible distance bound
  exists yet.
- Decide when seam recovery (`splitter.by_seams`) and
  `open_loops_are_printable` are safe to wire into the default sequence.
  `by_seams` itself works (regions preserved, no floor); nothing calls it from
  `repairer` yet, and independent per-region repair before rejoin is known
  destructive on real models — see the archived
  [Mandy volume loss](../../archive/docs-refactor-2026-09-22/mandy-volume-loss.md)
  notes if this is picked up again.
- Replace remaining absolute geometry tolerances where scale tests require it.

## Missing regression coverage

- **Review and simplify the test suite — requested 2026-09-26.** Optimize
  for what each test proves and the maintenance it costs, not an arbitrary
  reduction in test count. The 629-test run includes unit, integration,
  native geometry, subprocess, and legacy coverage; these are not all unit
  tests. Audit candidates before removing them; this task does not authorize
  weakening behavior checks simply to make the step refactor pass.
  - Separate legacy and historical-tool suites from current default-pipeline
    verification. `test_pipeline.py` covers the legacy script;
    `test_repair_pipeline.py` explicitly exercises the historical
    orient/Blender/PyMeshFix route. Preserve useful retained-tool regressions,
    but make clear which pipeline each suite actually proves.
  - Review low-value implementation checks and overlapping assertions,
    including frozen-dataclass checks and exact sequence-tuple assertions.
    Keep those that protect an intentional contract; remove or consolidate
    those that merely repeat implementation details already covered by
    meaningful behavior tests.
  - Correct misleading test scope. For example,
    `test_clean_keeps_all_four_filters` explicitly invokes four functions
    in its own helper and checks their calls; it does not prove the live
    pipeline includes those functions. Place tool-contract checks with the
    owning tool and name them for what they actually verify.
  - With the uniform-step/`ConditionStep` refactor, consolidate shared
    executor tests around order, passing transformed output forward,
    conditional execution/skipping, configuration delivery, and stopping
    on failure. Tool tests should prove each tool's distinct behavior,
    rather than repeat the same executor scenarios for every tool.
  - Preserve substantive coverage of cancellation, crash recovery, atomic
    publication, exactly-once reporting, malformed inputs, and geometry
    preservation. Similar-looking cases can protect different failure
    modes; check that before treating them as duplicates.
  - Address the imbalance between fixture checks and outcome checks:
    `test_regression_fixtures.py` verifies model-loss probe properties,
    while `docs/refactor/tests.md` identifies missing pipeline-verdict
    coverage. Add meaningful outcome checks for intended behavior without
    blessing known model loss as correct.
  Acceptance: clearly distinguish current-pipeline and historical coverage;
  justify each removal or consolidation by the remaining protection;
  preserve meaningful failure and geometry checks; update the test guide
  and run the affected suites. A smaller count alone is not success.
- Reversed T-junction and far bent path.
- Selector/converter/copy exceptions and source collisions.
- Interrupted write.
- Full runner: markers, crash, timeout, Ctrl+C, summary, and exit code are
  now covered (`tests/tests/test_batch_repair_pool.py`,
  `test_batch_repair_cli.py`, `test_batch_repair_smoke.py`) — no TUI/layout
  exists yet to cover, and "retry" is deliberately absent by design (D6: no
  retry, a failed file is reported once, a rerun is the retry mechanism).

Do not turn an unresolved geometry judgment into a passing expectation merely
to make the suite green.
