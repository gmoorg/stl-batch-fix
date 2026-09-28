# Handoff: per-part decimate-and-fix (NM-after-decimation), logging rework, skip-gate spike

**Status 2026-09-23:** Implemented and self-reviewed by Claude only.
**Codex has not reviewed any of this** — the whole segment below happened
with "Codex is out of token..let work together on that" (user's explicit
instruction), suspending COLLABORATION.md's normal Phase 1-3 flow for this
task. Nothing here has gone through `PHASE: INTERPRETATION` / `PLAN` /
`REVIEW`. Treat this doc as the review-context package: send it (or a
summary) via `tools/ask_codex.sh` with `PHASE: REVIEW` once Codex is
available, using the original prompts and agreed decisions below verbatim.

Full suite: 627 tests passing (`tools/project_python.sh -m unittest`,
81s). Verified end-to-end on real geometry (`tests/fixtures/foot1.stl`,
`--one-file`) — see the real step log excerpt under "Real-world
confirmation" below.

## What changed, in order

### 1. Step-log rework (foundation for everything after)

User: "No logs of any kinds. hard to tell if script run or dead" →
`tools/batch_repair.py` gained timestamped stderr logging.

User: "Will I be able to separate steps by looking into the log... avg.
time spent for alpha wrapping... I should be able to separate steps
easily" → redesigned from prose to tab-delimited:
`HH:MM:SS\tsource\tevent\tstep\tduration\tdetail`, `step` a stable
identifier (`decimate`, `scan_volume_in`, `split`, `scan_shells`,
`scan_seams`, `step_alpha_wrap`, `scan`, `merge`, `judge`,
`decimate_to_target`) — filterable with `awk -F'\t'`, no prose regexing.
New file `libs/steplog.py`.

User: "There is multiple places where scanner called. Please be sure you
add log in all of them" → instrumented every real `scanner.*` call site
across `libs/repairer.py` / `libs/processor.py`.

### 2. Duplication self-review

User: "look into the changes you did today. See if there duplicated code
introduced what should be extracted as function." → found real
duplication in the step-logging code (hand-written `mark = time.monotonic()`
/ `time.monotonic() - mark` pairs repeated at every call site). Fixed via
two new context managers in `libs/steplog.py`:

- `logged_step(step_logger, source_name, step, start_detail='')` — logs
  'start' immediately, yields `end(detail)` which logs 'end' with
  auto-computed elapsed time.
- `timed_info(step_logger, source_name, step)` — same idea but logs a
  single 'info' line, for sub-events inside an already-logged step's
  window (e.g. `scanner.shells()`'s work inside SPLIT's own window).

Applied throughout `repairer.py` / `processor.py`, replacing every
hand-written timing pair.

User, mid-review: "Problem with repeatable code is like joke you hear 10
times, it just not fun anymore. Also if some slightly changed, you would
not notice it because you would not even read the code if familiar
pattern found" — rationale for why this mattered, not just a style
preference.

### 3. Skip-gate spike (`is_already_clean`) — built, NOT wired in

User: "let implement skip-gate first. But let implement it as separate
function what return Boolean flag, so, if we would need to
tweak/disabled it would be in single place."

Added `repairer.is_already_clean(mesh: Mesh) -> bool`, stateless:

```python
def is_already_clean(mesh: Mesh) -> bool:
    scan = scanner.scan(mesh)
    if not scan.is_clean:
        return False
    seam_edges, _ = scanner.winding_seams(mesh)
    return seam_edges == 0
```

Exists to close a specific previously-identified gap: `Scan.is_clean`
alone (NM=0, open=0) does **not** check winding consistency — this killed
a 2026-09-21 attempt at the same skip-gate idea. Caught and fixed a bug
of my own here mid-session: I initially checked `winding_seams(mesh)[1]`
(`closed_loops`) instead of `[0]` (`seam_edges`, the actual defect count —
confirmed against the archived Amidara-base case where "922 winding-seam
edges" was specifically the `seam_edges` figure). User confirmed the fix:
"It was. We good on that."

5 synthetic tests added (`TestIsAlreadyClean` in `test_repairer.py`),
including `test_inverted_winding_is_not_clean_even_though_scan_is_clean`
— constructs a mesh that passes `Scan.is_clean` but fails the new gate,
proving the gap is actually closed.

**Deliberately not called from `repair()`/`process()` yet** — built and
tested in isolation first, per the user's own framing of the task ("as
separate function... single place" to tweak/disable later). Confirmed
with user that when wired in, it should log at the call site (this
codebase's convention — user asked whether their own apps' convention of
"log in the function that produces the result" applied here; confirmed it
doesn't, this codebase logs at the call site, and `is_already_clean`
should follow suit) and should not get a special-cased logging path
("Do not produce unique path of it not needed").

### 4. NM-after-decimation fix (the main fix this segment)

**Confirmed root cause:** alpha-wrap guarantees manifold output, but
`fast_simplification` decimation run on the merged mesh *afterward*
(the old whole-mesh "second pass" in `processor.process()`) could
reintroduce non-manifold edges. Confirmed on a real Monkey King model
batch run, and independently by the user's own observation that Bambu
Studio's mesh-simplify feature does the same thing to this pipeline's
output.

User (design collaboration, Codex unavailable): "What we would need to do
this? Since we will reuse pymeshfix to do fixes, we need to pass to it a
shell, not a merged model. That mean what we would need to decimate each
shell to it to the size the shell had imideatelly after splitting. Then
check for nm and run the fixing step already exists."

Confirmed via AskUserQuestion:
- Decimation target = **each part's own pre-alpha-wrap face count**
  (captured right after split, before alpha-wrap inflates it).
- The old whole-mesh second decimation pass in `processor.process()` is
  **removed entirely**, replaced by this per-part approach (not kept as a
  fallback/dead path).

**Design:** new `repairer._decimate_to_target_and_fix(mesh, target_faces)
-> (ok, mesh, detail)`, run as its own `Step.PART` entry immediately after
each part's alpha-wrap step, inside the PART loop:

1. Decimate this one part back to `target_faces` (its own pre-alpha-wrap
   size — a size it's already known to have held cleanly).
2. If decimation itself fails → `ok=False`.
3. Scan the decimated part. If clean → done, `ok=True`.
4. If not clean and PyMeshFix unavailable → `ok=False` (reports NM/open
   counts in the detail string).
5. If not clean and PyMeshFix available → run `meshfix.repair()` (the
   low-level call, not the uniform-step wrapper `step_meshfix_repair`,
   specifically to avoid double-applying step-contract detail formatting
   on top of `_decimate_to_target_and_fix`'s own). Success → `ok=True`,
   detail notes the fix; failure → `ok=False`.

**Non-uniform step contract, called out explicitly:** unlike every other
per-part step (all `(mesh) -> (ok, mesh, detail)`, bound once via
`functools.partial` before the PART loop because their extra arguments —
e.g. `whole_diagonal` for alpha-wrap — are the same for every part),
`_decimate_to_target_and_fix` needs `target_faces` bound **fresh per
part** inside the loop, since it differs per part. User: "Do not like
non uniform step. Let implement that but add todo task to make step to
accept the optional argument of specific type we could modify if needed
later on." — implemented pragmatically now (separate `_run_step` call per
part, `functools.partial` rebound each iteration), with a TODO recorded
in `docs/refactor/TODO.md`'s "Configuration" section for a general typed
per-call context argument mechanism, explicitly not blocking the actual
bug fix on that redesign.

**Consequence: every part now produces TWO `Step.PART` `StepResult`
entries** — the named tool call (e.g. alpha-wrap) and
`_decimate_to_target_and_fix`'s own, distinguishable by
`'decimate:' in s.detail` (its `detail_prefix` is always
`f"part {index} decimate: "`).

**Full removal of the old whole-mesh second pass** (user, via
AskUserQuestion: "Fully remove final_decimation and simplify _judge" —
Recommended option, confirmed, not kept as a shim):

- `processor.Outcome.final_decimation` field — removed entirely.
- `processor._decide()` — `final_decimation` parameter removed (now 3 args,
  was 4).
- `processor._judge()` — `final_decimation` parameter removed; the ~40-line
  `if final_decimation is not None: ... else: ...` branch collapsed to the
  single remaining path (always `scan = scanner.scan(repaired.mesh)` etc.
  directly, no second-pass special case). Every `Outcome(...)` call site's
  `final_decimation=...` kwarg removed.
- `processor.process()` — the entire second decimation pass after merge
  deleted; now just decimate → repair → `_decide`.
- `processor.write()` — `final_mesh` selection simplified (no longer a
  ternary checking `outcome.final_decimation`).
- `import numpy as np` removed from `processor.py` (no longer used).

### Real-world confirmation (foot1.stl, `--one-file`)

```
17:07:24|foot1.stl|start|decimate_to_target||531582 faces in
17:07:25|foot1.stl|end|decimate_to_target|1.121|decimated to 1586f, clean
17:07:41|foot1.stl|start|decimate_to_target||130794 faces in
17:07:41|foot1.stl|end|decimate_to_target|0.216|decimated to 128f introduced nm=21 open=0, pymeshfix fixed it
```

Part 1's decimation came out clean (PyMeshFix skipped). Part 2's
decimation introduced 21 NM edges, which the new step detected and
PyMeshFix genuinely fixed — the exact target scenario, seen live, not
just in a unit test.

## Test changes

- `tests/tests/test_processor.py`: deleted `TestFinalDecimation` (6 tests,
  ~90 lines) — tested the now-removed second-pass feature.
- `tests/tests/test_repairer.py`: added `TestIsAlreadyClean` (5 tests, see
  above). Updated `TestSequence.test_every_step_is_reported_in_order`
  (`[SPLIT, PART, PART, MERGE]`, was `[SPLIT, PART, MERGE]`) and
  `test_the_tool_sees_one_call_per_part` (PART count `4`, was `2` — 2
  parts × 2 entries each).
- `tests/tests/test_repair_pipeline.py`: added helper `tool_call_parts()`
  (filters to the named tool-call PART entries only, excluding the
  decimate-step's own). Updated
  `test_orientation_runs_on_every_part_of_every_fixture` and
  `test_one_inverted_shell_among_correct_ones_is_turned_outward` to use
  it. `test_merge_puts_the_parts_back` needed the **opposite** filter
  (MERGE consumes the decimate-step's output, not the tool call's) —
  fixed inline, documented in the test's own docstring.

Root cause of all 6 failures: tests assumed one `Step.PART` entry per
part; there are now two. User confirmed fix approach via
AskUserQuestion: "Update each assertion to account for 2 PART entries
per part (Recommended)" — not weakened, assertions made accurate to the
new (intentional) shape.

Also fixed, incidentally found while working in this area:
`test_a_repair_returning_non_finite_geometry_is_a_failed_outcome`'s
`poison` helper returned a 2-tuple instead of the required 3-tuple
`(ok, mesh, detail)` — was accidentally "passing" because the malformed
unpacking raised inside `_run_step`, caught by `repair()`'s outer
exception handler, producing the expected `FAILED` indicator for the
wrong reason. Fixed to return a proper 3-tuple.

## Docs updated

- `docs/refactor/TODO.md` — NM-after-decimation entry rewritten from
  "CONFIRMED" to "**FIXED, 2026-09-23**" with full design description,
  the real foot1.stl confirmation, test changes, and remaining open
  items (see below). New entry added for the typed per-call context
  argument TODO.
- `docs/refactor/orchestration.md` — pipeline table's steps 14/14a (the
  removed whole-mesh second decimation pass and its face-budget gate)
  replaced with 10a (`_decimate_to_target_and_fix`, per-part, always
  runs) and 10b (conditional PyMeshFix on that part only), positioned
  between step 10 (alpha-wrap) and step 11 (part failure gate, whose
  condition text was updated to cover 10a/10b failures too). Judge
  section's stale cross-reference row (previously line 92, pointed at
  "the second decimation pass (step 14/14a above)") removed, replaced
  with a closing paragraph stating there is only one decimation-plus-
  judge pass now.

## Not done / explicitly deferred

1. **`is_already_clean` is not wired into `repair()`/`process()`.** Exists
   and is tested in isolation only (see section 3 above).
2. **General typed per-call step-context argument mechanism** — recorded
   as a TODO per user's explicit request, not implemented. Current fix
   uses a pragmatic non-uniform special case instead (user approved this
   as the near-term tradeoff).
3. **Corpus-wide measurement of how often NM-after-decimation actually
   fired** before this fix — not measured, only confirmed to occur (once
   on a real batch run, once via Bambu Studio's independent behavior,
   once live via foot1.stl above). Flagged as still-open in the TODO.md
   entry.
4. **No Codex review of any of this segment** — this is the primary
   reason this handoff doc exists. Next step once Codex is available:
   send a `PHASE: REVIEW` call per COLLABORATION.md covering the diff
   described above (`libs/steplog.py`, `libs/repairer.py`,
   `libs/processor.py`, and the four test files), using this doc as the
   "agreed interpretation and plan" context substitute, since no
   `PHASE: INTERPRETATION` / `PLAN` calls were made for this task.

## Files touched this segment

- `libs/steplog.py` — new file (`logged_step`, `timed_info`).
- `libs/repairer.py` — step-log rework throughout; added
  `is_already_clean`; added `_decimate_to_target_and_fix`; PART loop in
  `repair()` now runs a second `_run_step` per part.
- `libs/processor.py` — step-log rework; removed `final_decimation`
  field/parameter/branch throughout `Outcome`/`_decide`/`_judge`/
  `process`/`write`; removed unused `numpy` import.
- `tests/tests/test_repairer.py` — `TestIsAlreadyClean` added; sequence
  tests updated for 2-entries-per-part.
- `tests/tests/test_processor.py` — `TestFinalDecimation` deleted.
- `tests/tests/test_repair_pipeline.py` — `tool_call_parts()` helper
  added; 3 tests updated.
- `docs/refactor/TODO.md`, `docs/refactor/orchestration.md` — updated as
  described above.
