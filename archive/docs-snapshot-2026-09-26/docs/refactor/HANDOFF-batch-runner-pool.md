# Handoff: batch_repair.py per-file isolation architecture

**Status 2026-09-23:** PLAN phase complete and agreed with Codex (8 rounds).
**Implemented and under REVIEW** — `libs/runstate.py`, `libs/proctree.py`,
`libs/publication.py`, `libs/childresult.py`, `tools/batch_repair.py`
rewritten, full new test suite (`test_runstate.py`, `test_proctree.py`,
`test_publication.py`, `test_childresult.py`, `test_batch_repair_unit.py`,
`test_batch_repair_pool.py`, `test_batch_repair_cli.py`,
`test_batch_repair_run.py`, `test_batch_repair_smoke.py`), 633 tests passing
plus 2 real end-to-end smoke tests.

**Review status: 2 rounds complete, 1 pending.** Round 1 found 5 real
implementation bugs (all fixed — see "Round 1 findings, fixed" below).
Round 2 confirmed the fixes but flagged 2 test-coverage gaps (also fixed —
see "Round 2 findings, fixed" below). **Round 3 (re-verification of the
round-2 fixes) could not complete: Codex hit its usage limit mid-review,
resets 2026-09-25 15:28.** No new findings were raised before the limit hit.
The message sent for round 3 is preserved below verbatim — resend it (or an
updated version if anything changes first) once Codex is available again,
using `tools/ask_codex.sh` with the same `PHASE: REVIEW` session (do NOT
run `tools/reset_codex.sh` first — the review session, distinct from the
planning session, holds all prior review context).

**Do not treat this implementation as approved for merge/use until round 3
completes with a PASS or equivalent.** The code is believed correct — round
1's bugs are fixed and round 2 found nothing further wrong with the fixes
themselves, only test coverage — but per COLLABORATION.md this task is not
finished until the reviewer explicitly signs off on the final diff.

### Round 3 message, ready to resend once Codex is available

```text
PHASE: REVIEW
Follow-up. Fixed both coverage findings from your last review.

1. Regression coverage for the removed exit-code gate: added test_batch_repair_pool.py's test_valid_result_trusted_despite_nonzero_exit_code -- a fake child that writes a FULLY VALID result JSON (published/PROCESS) and THEN calls sys.exit(3). Asserts the trusted result (not a reconciled one) is what's reported, with recovered=False. This fails loudly if the exit-code gate is reintroduced, since the old code would have skipped validation entirely on nonzero exit and produced a different (crash-reconciliation) outcome instead. Also added test_batch_repair_run.py's test_recovered_clean_publish_through_real_run_forces_diagnostic_and_exit -- runs the REAL _run() (real Pool, real _Runner, real spawned crash-after-publish child), asserting the actual printed 'Diagnostic:' line and exit code 1, not just the intermediate dict the earlier pool-level test checked.

2. SIGINT timing races: fixed both remaining sleep-based tests with real synchronization instead of guessed delays.
   - test_batch_repair_run.py's repeat-SIGINT test: the fake child now writes a marker file the instant it's actually running (proving Pool.start() dispatched it before the first SIGINT is sent), and the helper script wraps signal.signal itself to write a second marker the exact moment _run installs SIG_IGN for its cleanup window -- the second (repeat) SIGINT is only sent after that marker confirms the cleanup-ignoring window is genuinely active, not after a fixed 0.05s guess.
   - test_batch_repair_pool.py's test_sigint_kills_in_flight_children_and_marks_incomplete: replaced the shared no-marker fake child with a dedicated one that writes a per-mesh marker file the instant each of the two hanging jobs is truly running; the test waits for BOTH markers before sending SIGINT, so it can no longer pass by accident because a job hadn't been dispatched yet.

Full suite: 633 tests (up from 631), all passing, including these newly-synchronized SIGINT tests run repeatedly without flake. Smoke tests unaffected (not re-touched this round).

Anything else outstanding, or is the implementation ready?
```

### Round 1 findings, fixed

1. **Recovered clean publications silently exited 0.** `_run`'s consumer
   only diagnosed `not clean` results, so a crash-recovered `PROCESS`
   outcome (child died after publishing, before reporting) looked identical
   to an ordinary clean run. Fixed: every result-builder now carries an
   explicit `recovered: bool`; `_run` diagnoses `not clean or recovered`.
2. **A reporting failure could be retried, double-invoking `report_fn`.**
   `handler()`'s exception path unconditionally retried `complete_once` for
   the same token. Fixed structurally in `RunState`: `_report_attempted`
   set, marked before calling `report_fn`, checked at the top of
   `complete_once` — a second attempt now raises `AssertionError` rather
   than re-invoking the callback.
3. **Valid results were gated on the child's exit code.** The
   `if cause == 'exited' and proc.returncode == 0:` guard skipped
   validation entirely on nonzero exit, discarding a legitimately
   committed result. Removed — `childresult.read_and_validate` now always
   runs after cleanup confirmation, matching its own documented contract.
4. **`UnboundLocalError` in `proctree.terminate_and_confirm`.** The
   `ProcessLookupError` branch never set `kill_error`, which a later
   uncertainty path referenced unconditionally. Fixed: initialized before
   the `try`.
5. **Cancelled-but-cleanly-reaped jobs vanished from the summary.** `_run`
   discarded `category == 'cancelled'` results entirely, so an interrupted
   run could report "0 unresolved, 0 queued" while never mentioning how
   many jobs were actually interrupted. Fixed: `cancelled_count` tracked
   and printed explicitly in the `Run INCOMPLETE` line.

### Round 2 findings, fixed

1. **No regression test actually proved the exit-code gate stayed removed.**
   The existing "recovered clean" test used a child that crashed *before*
   writing any result file, so it couldn't detect a regression where a
   *valid* result with a nonzero exit code got rejected again. Fixed: added
   a dedicated test with a fake child that writes fully valid JSON and
   *then* exits nonzero, asserting the trusted (not reconciled) result is
   what's reported.
2. **SIGINT tests used fixed sleeps instead of real synchronization,**
   both in the original round-1 fix and in two further tests only found by
   Codex's second pass — no proof the target process/window had actually
   been reached before a signal was sent. Fixed with genuine handshakes:
   marker files the child/cleanup code writes at the exact moment being
   tested for, polled by the test rather than guessed at with a sleep.

Do not re-run `tools/reset_codex.sh` before resuming this task — the
planning session (`01a0caf8-fd51-7832-92fb-429f19088edc` at time of writing,
confirm via `tools/codex_session.py` state) holds all the reasoning below and
should be resumed for the review phase, not started fresh. Only reset if
starting genuinely new, unrelated work first.

## Original task

User: "Let fix the main script. Order, pool workers" — confirmed via
AskUserQuestion to mean: build the full per-file isolation architecture
(worker thread pool driving isolated one-file child subprocesses) for
`tools/batch_repair.py`, per the recovered design at
`archive/docs-before-compact-2026-09-19/refactor/pool/d1.md`–`d11.md`. This
also fixes the known Ctrl+C-does-not-interrupt-alpha-wrap bug (CGAL's
`alpha_wrap_3` runs in-process and blocks Python's SIGINT delivery).

`libs/pool.py`'s `Pool[T]` class already exists, matches D4's final decided
interface, and already has 27 passing tests — it is not being redesigned,
only wired in.

## Scope, confirmed with the user mid-planning

- Memory admission: **in scope**, using the confirmed-real `890 bytes/triangle`
  figure as a decimator-peak-memory budget (user confirmed this specific
  number was measured for the decimator, separate from the retired
  PyMeshFix-era pipeline). Alpha-wrap's own memory need is larger and
  unmeasured — handled via an explicit, labeled-as-unvalidated multiplier
  (see `ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED` below), not a fabricated precise
  number.
- Correctness bar: **keep iterating to full correctness** (user's explicit
  choice when asked whether to descope after several rounds of Codex finding
  real lifecycle bugs) — do not ship a version with known data-loss/zombie/
  silent-overwrite windows.
- Single-writer assumption **accepted**: `batch_repair.py` is not designed to
  defend against two concurrent invocations writing the same output tree.
  This materially simplified the publication-reconciliation design (see
  below) — do not relitigate this unless the user changes it.
- Out of scope: `RunConfig`/`Job`/`RunEvent`/`RunSummary` typed contracts, an
  event stream, TUI, the D7 preparation/conversion pool (intake stays serial
  via existing `converter.prepare`).

## Agreed design (verbatim summary of the final accepted plan)

### `--one-file` child mode

New CLI branch in `tools/batch_repair.py`: `--one-file SRC --destination DST
--max-faces N --result-file PATH` (mutually exclusive with `--input
--output`). Runs the existing per-file body (load → `processor.process` →
`processor.write`) for exactly one file. Writes its outcome as JSON to
`PATH` via `mesh_io.staged_write` (same atomic-rename primitive
`processor.write()` already uses for markers) — **not** a pipe/fd (an
earlier draft proposed `--result-fd`; rejected because `communicate()`
cannot safely drain an arbitrary pipe without a deadlock/ordering risk).
Child exit code is **never** part of the parent's trust decision — only
"does a valid result file exist" is (see Result validation below). **This
does not make an OOM kill invisible**: an OOM-killed child never reaches the
line that writes its result file (that write only happens on a graceful
exit), so the parent sees "no valid result file" regardless of exit code —
which is exactly what routes it into the crash/reconciliation path below and
produces a `.failed.stl` marker. Exit code is redundant with that check, not
a substitute for it; that redundancy is why it's safe to ignore.

### `RunState` — the single piece of shared run-level state

One class, one lock, owns: cancellation flag + reason, per-token job state,
per-token `Popen` handles, per-token memory reservations, and a
"already completed" set for idempotency. Replaces every earlier draft's
separate `ChildRegistry` + selector-closure-owned memory total (those had an
unsynchronized two-lock race, caught by Codex — fixed by merging into one).

Job states: `reserved` → `running` → `reaped` → (`publishing`) →
completed-and-removed, OR `stuck` (permanent, never removed) OR
`launch_failed` (terminal, no process ever existed).

Key methods (see the full pseudocode in the Codex planning session — not
re-transcribed here in full, this doc is a resumption pointer, not a spec
replacement):

- `start(estimate_bytes, budget_bytes) -> (token|None, refusal, override)` —
  combined admission check + reservation, one locked call, no second call
  needed for the "admit alone" override (D5's rule: an unrunnable-alone job
  is admitted regardless rather than blocked forever — logged when it
  happens).
- `spawned(token, proc) -> bool` — False means cancellation raced the launch;
  caller must kill-and-reap immediately, still via its own thread.
- `mark_reaped(token)`, `mark_launch_failed(token)`, `mark_stuck(token,
  detail)` — state transitions.
- `authorize_publication(token) -> bool` — must be called immediately before
  ANY parent-side filesystem write (synthetic marker, or trusting a
  recovered finding). False means cancellation won the race; report
  "cancelled", touch nothing on disk. This is a **commitment point**, not a
  real-time write-in-progress guarantee: cancellation blocks *new*
  authorizations; an already-authorized write may still finish.
- `complete_once(token, report_fn, result)` — the **only** way any job ends.
  Idempotent per token. Calls `report_fn(result)` first, only marks the
  token completed (and removes it from tracking) if that succeeds — a
  raising `report_fn` leaves the token genuinely unresolved (visible in the
  final incomplete-run summary), not retried.
- `cancel(reason)` — idempotent, signals (`os.killpg(SIGKILL)`) every
  currently-live registered process once, sets the cancellation flag so
  every subsequent `selector` call and admission check refuses new work
  immediately.
- `has_unresolved()`, `has_incomplete_reason()`, `snapshot_unresolved()` —
  read for the final run summary.

### Process-tree kill: process groups, not a `/proc` walk

Each child spawned with `subprocess.Popen(..., start_new_session=True)`
(== `setsid`), putting it and every descendant (including a Blender
grandchild) in one process group keyed by the child's own PID.
`os.killpg(child.pid, signal.SIGKILL)` reaches the whole tree in one call.
Documented limitation (accepted, not closed): a descendant that calls its
own `setsid()`/`setpgid()` escapes this — known not to apply to this
project's actual descendant (`libs/blender.py`'s `Runner.run()` uses plain
`Popen`, confirmed by reading `libs/blender.py` — it has **no** reusable
process-tree kill logic itself, `Runner._kill` is literally just
`proc.kill()`; do not assume there's something to extract from it).

`_terminate_group_and_confirm(proc, deadline=REAP_DEADLINE) ->
(confirmed: bool, detail: str|None)`: kills the group, reaps the **direct**
child via `proc.wait()` (a `/proc` scan alone is not sufficient — Codex
caught that an early draft skipped direct-child reaping entirely), then
polls `/proc/[0-9]*/stat` for any remaining process whose pgid matches
`proc.pid` and whose state isn't `Z` (zombie). A live match, or a
malformed/permission-denied `/proc` entry, blocks confirmation (uncertainty
counts against confirmation). A vanished entry (ENOENT/ESRCH mid-read) is
ordinary process exit, not counted. Called on **every** exit path
(normal/timeout/exception), not just failure paths — a cleanly-exiting
direct child can still leave a live grandchild.

An unconfirmed `_terminate_group_and_confirm` result → `run_state.mark_stuck`
→ the whole run is cancelled (every other live child signalled) and the run
ends reporting "INCOMPLETE", never "Run complete." — full correctness was
chosen over "assume dead and continue with reduced capacity" (an earlier,
rejected draft).

### Result validation

Bounded read (reject/treat-as-invalid over ~64KB), full field type checking,
job-identity check (`path` in the result must equal the job's own
`mesh.path`), category/indicator must be known enum values, `clean` is
**computed by the parent** from category+indicator rather than trusted as an
independently-supplied field (removes the "contradictory values" question by
construction).

### Publication reconciliation (crash/timeout with no trusted result)

**Preflight, before any dispatch**: for every valid emitted mesh, compute its
full expected-publication-path set (destination + every marker path
`indicators._OUTPUT_MARKERS` could derive from it). Build a path→owning-job
map across all jobs; any path claimed by more than one job → every owner
rejected (`intake_failure`, reason names the collision) before any child
spawns. Separately, any job whose own path set already has an existing file
on disk at preflight time is also rejected the same way (residual stray
marker case — ordinary rerun-skipping already happens earlier via
`indicators.check` inside intake and never reaches here as a "fresh" job).

This preflight step is why post-crash reconciliation can be simple: every
**dispatched** job is guaranteed to start with an empty path set. After a
crash/timeout with no trusted result (and after `_terminate_group_and_confirm`
has run, so no descendant can still be writing), the parent re-snapshots the
same path set:

- **Zero** new paths exist → nothing was published. Parent writes a
  synthetic marker (`.failed.stl` for a crash, `.timeout.stl` for a timeout —
  `cause` is tracked through the handler specifically so this choice is
  correct) via `mesh_io.staged_write` + `shutil.copy2` of the **source**
  file (never empty/hardlink — see the `signal-files-are-fallback-prints`
  memory rule). Success → `category='published'`,
  `indicator=FAILED`/`TIMED_OUT`. The write itself raising →
  `category='write_failure'`.
- **Exactly one** new path exists → recovery. Trust it: `category='published'`,
  indicator taken from whichever marker/path was found. Always added to the
  diagnostics list regardless of indicator (even a recovered clean PROCESS
  outcome is diagnosed as "child crashed after publishing, recovered via
  filesystem" — this is a genuine behavior change from earlier drafts that
  tried to make a clean recovery exit 0 silently; Codex's ruling was that
  the abnormal exit itself is worth surfacing even though the file is fine).
- **Two or more** new paths exist → `INCONSISTENT`. **Do not** write yet
  another marker into an already-inconsistent set of files (an explicit
  correction from Codex — an earlier draft wrongly proposed adding a
  synthetic marker here too). `category='write_failure'`, reason names every
  unexpected path.

`authorize_publication(token)` must gate entry to this whole reconciliation
block (both the marker-write and the recovery-trust paths) — a cancellation
arriving after the crash is detected but before this runs must produce a
"cancelled" result and touch nothing on disk, not a synthetic marker.

### Admission / memory / ordering

Queue sorted ascending by `mesh.triangles` (from `mesh_io.probe()`'s header
read at intake — free, no new scan). `BUDGET_BYTES_PER_TRIANGLE = 890`
(confirmed-real decimator figure). `ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED = 3`
— **explicitly an unvalidated placeholder**, commented as such in code, not
claimed as a safety bound; replace once real alpha-wrap peak-RSS-vs-input-
triangle measurements exist (this is exactly the kind of thing
`docs/refactor/TODO.md` should track once this lands). Available budget:
`os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_AVPHYS_PAGES')` ×
`--memory-budget-fraction` (default `0.7`, validated in `(0, 1]`); falls back
to requiring an explicit `--memory-budget-bytes` if `sysconf` is unavailable,
rather than guessing.

### CLI defaults (Codex-agreed)

| Setting | Default | Validation |
|---|---:|---|
| `--workers` | `min(4, os.cpu_count() or 1)` | positive integer |
| `--per-file-timeout` | `3600` seconds | finite, positive |
| `--reap-deadline` | `10` seconds | finite, positive |
| `--memory-budget-fraction` | `0.7` | finite, in `(0, 1]` |

### SIGINT handling

Do **not** install a custom `signal.signal(SIGINT, ...)` handler — keep
Python's default (it raises `KeyboardInterrupt` in the main thread, which is
where `Pool.start()` runs and already doesn't catch it — confirmed by reading
`libs/pool.py`: its `start()` joins have no `try/finally`, so a
`KeyboardInterrupt` from the *main* thread exits immediately without joining;
all cleanup must happen in `_run`'s own `except KeyboardInterrupt:` block, not
relied on from Pool). `_run` calls `run_state.cancel('SIGINT')` then waits
(bounded, `wait_for_drain`-style) for `has_unresolved()` to clear, printing
"INCOMPLETE" either way. Repeat-SIGINT during that bounded wait is
temporarily deferred (`signal.signal(SIGINT, SIG_IGN)`, restored in a
`finally`) so a second Ctrl+C during cleanup doesn't corrupt in-progress
kill/reap bookkeeping.

### Category/marker vocabulary: no new top-level categories

Reuses the existing `intake_failure` / `load_failure` / `process_failure` /
`write_failure` / `published` categories and the existing `Indicator` enum
(including `TIMED_OUT`, `FAILED`, which already existed but were unused by
the parent before this). "Cancelled" jobs are the one exception: no marker,
no category, not in the terminal counters at all — they're meant to remain
eligible for a plain rerun (existing indicator-based skip-on-rerun
convention), reported only via a distinct "N files still in flight when
interrupted" summary line, not as a per-file terminal outcome.

### Error-path corrections from the final two Codex rounds (do not lose these)

1. Launch failure (`Popen()` itself raising) needs its own terminal state
   (`launch_failed`) since no process ever existed to reap.
2. A handler-level exception (bug, not a normal per-job outcome) must first
   try to clean up **its own** child via `_terminate_group_and_confirm`
   before falling back to `mark_stuck` — only mark stuck if that cleanup
   itself can't be confirmed; if it CAN be confirmed, `mark_reaped` first
   (state must be valid before `complete_once` will accept it), then
   `run_state.cancel(...)` (stop the whole run) and complete this job as a
   reported `write_failure`.
3. The `selector` callback itself must be wrapped: any exception inside it
   (not just the override-log line, which is separately swallowed) must
   call `run_state.cancel(...)` **before** re-raising, and account for a
   token that was granted but never handed to a worker (`mark_stuck` on it)
   — otherwise `Pool` (per its own documented behavior) waits for all
   already-dispatched handlers to finish, up to the full
   `PER_FILE_TIMEOUT`, before surfacing the selector's error, during which
   new work could otherwise still be dispatched by other threads if
   cancellation weren't set immediately.

## Files that will change

- `tools/batch_repair.py` — the bulk of the change (new `RunState` class,
  `--one-file` mode, new `_run` body driving `libs.pool.Pool`).
- `tests/tests/test_batch_repair.py` — update the 15 existing tests for the
  new internal shape (no more single in-process serial loop to reason
  about), add tests for: `--one-file` mode per terminal category; real
  parallelism (observable overlap, not mocked); ascending dispatch order;
  admission shedding under a synthetic memory budget; the "alone" override;
  SIGINT mid-run → INCOMPLETE summary, nonzero exit, untouched queue items
  stay rerunnable; a `mark_stuck` event cascading to cancel the rest of the
  run; `_terminate_group_and_confirm` correctly distinguishing a live
  grandchild from a zombie from unparseable `/proc` data (real subprocess
  fixtures preferred over mocks, matching this project's existing testing
  style); bounded/malformed result-file handling; the three RECOVERED /
  INCONSISTENT / NOTHING reconciliation outcomes; preflight collision and
  pre-existing-artifact rejection.
- `docs/refactor/orchestration.md` — Build order status list, once
  implemented.
- `docs/refactor/TODO.md` — "Runner and concurrency" section updated; add the
  `ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED` follow-up item explicitly.

## Not yet done, do next

1. Confirm the Codex planning session is still resumable
   (`tools/codex_session.py`), or re-supply this doc's content if it has to
   restart.
2. Implement `tools/batch_repair.py` per the design above.
3. Run the full test suite via `tools/project_python.sh`.
4. Send the actual diff to Codex via a **fresh `PHASE: REVIEW`** call (not
   `PHASE: PLAN` — planning is finished and agreed) per COLLABORATION.md's
   Phase 3, including this handoff's content as context since the reviewer
   session does not inherit the planning session's history.
5. Update `docs/refactor/orchestration.md` and `docs/refactor/TODO.md` to
   reflect what landed.
