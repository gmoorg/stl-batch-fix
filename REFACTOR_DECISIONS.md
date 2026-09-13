# Refactor decisions

A running record of what was decided and why, while the refactor is being
designed. Written because the reasoning is expensive to reconstruct and easy to
lose — the design doc went 13 commits stale in a day, and `TODO.md` went four
items behind.

`TODO.md` item 1 holds the *target* (the `Stl` DTO, tool modules, operation
modules, the guidelines). This file holds the decisions made since, and the
arguments that produced them.

Status: **design only. No code has moved.**

---

## D1 — The runner is not exempt

**Superseded:** an earlier claim that the pool should be left as explicit
imperative code.

That was wrong, and the measurements say so:

```text
run_progress_screen   696 lines   (TUI)  <- second-worst function in the codebase
  _fill                41 lines   nested closure, push-model admission control
  _watchdog            87 lines   nested closure
core runner           ~60 lines   a SECOND submit loop, with no restart handling
pool references:     171 in the TUI, 126 in the core
```

It is not one hard thing deliberately left alone — it is **the same concern
implemented twice, in two files, already drifted apart**. That drift is how the
bare script ran for months without child isolation. "Keep related things
together" applies harder here than to the mesh pipeline, because the knowledge
is duplicated rather than merely long.

---

## D2 — Worker-pull, not parent-push

**Decided.** The pool owns a queue; each worker loops `get_next()` → do it →
`get_next()` until drained.

The current code submits every file up front as a future. The TUI then
re-implements pull semantics *on top of* push, because it needs memory-aware
admission — `_fill` holds files back and submits more only as the running set
drops. It is already straining toward pull and getting there awkwardly.

Two things pull buys concretely:

- **`plan_worker_count` becomes natural.** A worker asking for work is exactly
  the moment to decide whether it should get any. Push has to guess ahead and
  re-check on completion.
- **Retry stops being special.** A failed file goes back on the queue.
  `_to_retry` + `_attempts` + requeue-at-back-not-front is machinery for
  putting things back into a model that did not expect them.

---

## D3 — Threads, not a process pool

**Decided**, and this is the one that deletes the most.

The mesh work already runs in a `--one-file` **subprocess**. The process *pool*
was a separate decision that brought failure modes the subprocess isolation had
already made unnecessary. With worker threads driving subprocesses:

| today | with threads |
|---|---|
| `BrokenProcessPool` fails every pending future | a thread raising affects only itself |
| `_abandon_pool` — SIGKILL the pool, shutdown on a daemon thread, never join | nothing to abandon |
| `_worker_status` is a `Manager()` proxy; a worker SIGKILLed mid-read deadlocks the parent in `futex_do_wait` | plain dict under a lock |
| `_status_snapshot` daemon-thread workaround for that deadlock | unnecessary |
| `max_tasks_per_child` forces `spawn`, incompatible with the fork design | irrelevant |
| watchdog kills a *worker* it cannot otherwise reach | the thread holds the `Popen`; `kill()` is direct |
| pool restart, `_to_retry`, `_attempts`, requeue-at-back | `pool.requeue(item)` |

**The GIL does not matter**: every worker thread is blocked in `communicate()`
waiting on a subprocess, not computing.

**Nothing crosses a process boundary** except argv and a return code. An earlier
worry about pickling lambdas was an artifact of `ProcessPoolExecutor` — it does
not apply to this design. Measured, for the record: a lambda cannot be pickled,
a closure cannot, a bound method can. None of it is relevant once the pool is
threads.

### Survives unchanged

- **`_child_pids_of`** — killing a `--one-file` child must still walk `/proc`
  for Blender one level deeper. That is the process tree, not the pool.
- **The `--one-file` child** and everything in it.
- **`plan_worker_count` / `estimate_peak_bytes` / `mesh_is_too_large`** — the
  memory model is real; it moves from `_fill` into an `admit` callback.

---

## D4 — The Pool interface

**Sketched, runnable**: `design/pool_sketch.py` — 130 lines;
`python design/pool_sketch.py` runs a demo with three workers, eight files and
two induced failures.

```python
class Pool:
    def __init__(self, items, n_workers, admit=None)
    def get_next(self, timeout=None)   # None = stop
    def status(self)                   # what each worker is on, for the display
    def stop(self)
    def start(self, work)              # threads, each running work(self)
```

The whole worker the caller writes:

```python
def repair_one(pool):
    while (src := pool.get_next()) is not None:
        report(run_one_file(src))       # spawns --one-file, waits, reads rc
```

No failure bookkeeping at all. A mesh that fails repair is just a result, the
same as one that succeeds.

**`timeout` is for a future caller with a queue that can grow**, not for this
one. It defaults to `None` and passing it here means nothing useful: every wait
in this workload is on another worker finishing a mesh, which always resolves.
An earlier draft used it as an escape from `admit` blocking forever — that was
papering over a self-inflicted problem, and it made `get_next` return a file it
had been told to withhold, leaving the caller unable to tell approval from a
clock running out.

**The sketch in `design/pool_sketch.py` predates D5 and still has `requeue`,
`attempts` and a `timeout=5` worker.** It has not been updated — treat this
section as the current interface and the sketch as the earlier draft.

---

## D4b — Admission is a condition variable and a caller-supplied callback

**Decided**, replacing an open question (O1) that turned out not to be one.

`get_next` waits on a `threading.Condition` while `admit` refuses the head item,
and a worker finishing signals it. Standard mechanism, already in the sketch. An
earlier draft treated "a blocked thread does nothing" as a problem worth
designing around — it is not. Doing nothing is exactly what a thread with
nothing to do should do, and an idle stack is not a cost.

The forever-block case is not a locking problem either, just a clause in the
predicate: **if no other worker is running there is nothing to wait for**, so the
item is handed over regardless. Either it runs alone or it never runs, and
`mesh_is_too_large` already identifies the never-fits case before queueing.

`admit` stays a **caller-supplied callback**, `admit(item, running)`. The pool
never learns what a triangle is; the mesh-specific memory model
(`estimate_peak_bytes`, `_run_memory_budget`, the 890 bytes/triangle constant)
lives in the function passed in. Same injection pattern as `scan` elsewhere in
the design. That resolves what looked like a dilemma — putting `admit` in the
pool would make a generic pool know about meshes, putting it in the workers
would scatter one decision across threads that cannot see each other — because
the callback is neither.

---

## D7 — The watchdog does not survive

**Decided**, replacing O2.

### What it does today

```text
every 0.25s, for each worker pid in worker_status:
    limit = _effective_timeout()            # 3600, or 86400 when TIMEOUT=0
    skip if limit <= 0, or (pid, started) already fired
    skip if now - started < limit * grace   # grace=3.0 at the only call site
    -> fire
```

The real threshold is **10,800 s**, not 3600, and it measures how long a worker
has held one file — not how long any operation took. On firing it: records
`(pid, started)`; logs the kill; **writes `.timeout.stl` from the parent**,
because the worker is about to be SIGKILLed and will never reach its own
marker-writing code; SIGKILLs the worker's children via `/proc` so a Blender
grandchild cannot reparent to init and keep its memory; then SIGKILLs the
worker.

It exists for one thing: **a worker wedged inside a GIL-holding C++ call.**
`fast_simplification`, `pymeshfix` and `pymeshlab` all ran in-process, no
Python-level timer could interrupt them, and pymeshfix is ~80 % of runtime. The
parent was the only process positioned to act.

### Why nothing is left for it

With threads the whole sequence is local to one thread:

```text
thread: spawn --one-file child
        communicate(timeout=budget)
        -> expires
        kill grandchildren (/proc walk), kill the child
        write .timeout.stl          <- it knows the file; it sent it
        record the result, get_next()
```

There is no worker *process* to kill, and nothing outside that thread needs to
observe the timeout. `communicate(timeout=)` is not a partial answer leaving a
gap for a watchdog to cover — it *is* the mechanism, and the thread is already
the right place to act because it holds both the `Popen` and the filename.

An earlier draft asked whether a worker thread could wedge *outside* the
subprocess call, and kept D7 open on that. The question was an artifact of the
old shape, where the timeout enforcer and the file's owner were different
processes and the parent had to reach across. Once they are the same thread it
stops existing. (Third time machinery was carried over from the design being
replaced, after the pickling detour and `requeue`.)

### What moves rather than disappears

- **Marker writing** stops being special: the killer and the marker-writer are
  the same thread, so `.timeout.stl` is written on the ordinary path.
- **`_child_pids` before the kill** stays — a `--one-file` child must still have
  its Blender grandchild reaped. Process-tree logic, already noted in D3.

### Stale justifications retired with it

The docstring claimed the kill is safe because "the file is retried once, then
set aside" — D5 removed retry. It also carried the `_status_snapshot`
workaround and the `Manager()` deadlock commentary, both of which D3 deletes
along with the proxy.

---

## D5 — No retry. If it failed, it failed

**Decided**, replacing an earlier "the pool counts attempts, the caller decides
retry policy".

**The rerun is the retry.** A file that fails gets its marker, and the operator
reruns the script with different settings — a lower `MAX_FACES`, a longer
`TIMEOUT_PART` — which is a deliberate choice about that file rather than the
pool guessing on its own.

This is consistent with everything else in the script: indicators already
suppress reprocessing until deleted, and skip reasons name the marker to remove.
Retry inside the pool was the one place that decided by itself to have another
go.

### What retry was actually for

Worth recording, because it was never "the mesh might work next time". Its only
trigger was `BrokenProcessPool`:

> A worker killed by the OOM killer breaks the whole `ProcessPoolExecutor`, not
> just its own task: every not-yet-completed future — including files still
> sitting in the queue, unassigned to anyone — fails with `BrokenProcessPool`.
> **One 7M-triangle mesh therefore cost 40 untouched files in the last run.**

So retry meant "this file never ran at all", not "try the repair again". A mesh
that genuinely failed repair was never retried; that path has no retry logic.

### Why it does not come back

Two independent reasons, and the policy one is the stronger:

1. **Policy (D5):** failed is failed. This survives changes to the failure
   modes — if some new spurious failure appears later, the answer is still
   "rerun it", not "add retry back".
2. **Mechanism (D3):** with threads there is no future, so a file has only
   three states — queued, held by a worker, done. "Failed without running" stops
   being expressible, so the forty-innocent-files problem is not handled better,
   it cannot occur.

`requeue` and `attempts` were in the first sketch because machinery was ported
from the old model without asking whether the new one needed it — the same
error as an earlier detour into pickling constraints that only existed because
of `ProcessPoolExecutor`.

### What is lost, and what replaces it

`_attempts` distinguished "died once, probably innocent" from "died twice, is
the culprit", producing the `killed a worker twice (likely out of memory)`
diagnosis. That distinction goes.

What replaces it is more precise: an OOM-killed `--one-file` child gives its
thread a negative return code directly, so the file is reported as failed with
the actual signal, and no sibling is affected.

---

## D6 — A preparation stage that normalises everything to binary STL

**Decided.** A first pool fills the queue for the main one.

### Why it exists

The main pool cannot order work it has not measured, and today it cannot
measure two of the three formats:

| format | `_read_stl_header` | admission check |
|---|---|---|
| binary STL | real count | runs |
| ASCII STL | **-1** — "header claims 1,919,252,000 tris" | **skipped** |
| OBJ | **-1** — "file shorter than STL header" | **skipped** |

That `1,919,252,000` is the ASCII text `solid` read as a little-endian uint32 at
offset 80. A size cross-check catches it and returns -1, so nothing breaks — but
by accident of a sanity check, not by design. `mesh_is_too_large` is guarded by
`if n_tris > 0`, so for ASCII and OBJ the admission check simply does not run and
the real memory cost is, in `measure_files`' own words, "discovered on the way".

ASCII STL is worse than OBJ here: OBJ at least gets an approximation for queue
ordering (`filesize // 60`), while ASCII STL returns 0 and sorts **first**, as
the cheapest thing in the queue. An ASCII STL is roughly 6-8x larger on disk
than its binary equivalent.

### The rule

Exactly the skip logic the main pipeline already uses — existence is the cache,
deleting the file is the invalidation:

```text
for each collected file:
    binary STL                      -> queue it
    ASCII/OBJ, export already there -> queue the export
    ASCII/OBJ, no export            -> export it, then queue the export
```

Exports go to a `stl-exported/` folder **in the source tree**, and the collector
must skip that folder.

**No mtime check.** An export could in principle go stale if its source were
replaced under the same name, but that does not happen in this workflow —
sources arrive and stay put. Existence alone decides, exactly as it does for the
repaired output; deleting the export is the way to force a re-export.

### Why writing to the source tree is acceptable here

The "source is never modified" rule came from a specific worry: *our repair
output* polluting the source folder, where a buggy script leaves files that are
hard to tell from originals and hard to manage.

A Blender format export is not that. It is a lossless container change — same
triangles, same coordinates, binary instead of text — so even a buggy run leaves
the source mesh in a different encoding. A dedicated `stl-exported/` folder is
one directory to delete, obviously not originals, and the export is a standard
Blender operation, not something implemented here.

### The collector exclusion is not optional

Without it the next run collects the exports as inputs, and every OBJ is
processed twice — once as OBJ, once as its export — producing two outputs under
different names for the same model. The same failure the `~parts` and
`__MACOSX` exclusions exist for, and easy to miss because it only appears on the
**second** run.

### What it simplifies downstream

Stage two stops having an `is_obj` / `is_ascii` branch. Today those files set
`blender_src = src` and bypass the Python pipeline entirely — no pre-scan, so no
nm count, no open-edge count, no print-scale gate. They go straight to Blender
and take whatever comes back. After preparation they are ordinary binary STLs
and get the full pipeline.

The conversion is not extra work: Blender already does it in step F today.
Preparation moves it earlier and keeps the result.

### Ordering

Once every file is binary STL with a real count, the queue is sorted by **face
count**, which is free from the header.

**Decided, and the reason settles it:** the sort exists to feed memory
admission, memory peak is driven by decimation, and decimation cost scales with
face count. nm count predicts *runtime* — far better than size does (33,353
defects → 3,080 s against 2,055 → 289 s) — but runtime is not what the ordering
is for, and getting nm would cost a `scan_mesh_errors` pass over every file.

Supporting measurement: across every log, **189 decimations, all handled by
`fast_simplification`, zero fallbacks and zero failures** — including the
14.1 M-triangle file. So decimation cost is not merely predictable from face
count, it is predictable from face count *through one implementation*, with no
branch to a differently-scaling decimator. (The pymeshlab and blender rungs of
the ladder have therefore never executed. They are not proven dead — rung one
simply never failed — but nothing is known about how they scale.)

### Worker shedding

`get_next` returns `None` for worker N when there is no longer room for it, and
that worker exits and frees its resources — rather than blocking and holding a
stack and a status slot while doing nothing. Strictly better than the blocking
admission in D4.

Ordering and shedding interact: smallest-first means workers shed late,
largest-first means they shed early and the tail runs wide.

---

## Open, not yet decided

**O3 — status reporting.** `_worker_status` becomes a plain dict, but the TUI
reads it every 0.25 s from the render loop while workers write. Needs a lock;
`_status_snapshot`'s daemon-thread workaround becomes unnecessary.

**O4 — can `_worker_status` leave the Manager proxy entirely?** It is the main
reason a worker can wedge today. If the child reported its own status, or status
travelled the existing result pipe, the restart machinery becomes genuinely
vestigial rather than arguably so. (Partly answered by D3, which removes the
proxy — but the question of *who* reports status is still open.)

**O8 — can a shed worker come back?** If worker N exits because the remaining
files are large, and the queue later returns to small files, concurrency stays
narrow for the rest of the run unless the pool can spawn a replacement. Sorting
by monotonically increasing cost makes shedding always final and the question
disappears — which is an argument for a specific ordering rather than a free
choice.

**O5 — where does the shared budget arithmetic live?** Carried over from
`TODO.md` item 1. Decimation, repair and Blender draw on one mesh budget and
Blender's share depends on what earlier steps spent. Cross-cutting: if each
module owns its own timeout policy the arithmetic has no home.

**O6 — where does the seam-recovery trigger sit?** Also carried over. It fires
when PyMeshFix *succeeded but deleted geometry* — a fact about the transition,
not about the mesh before or after. The case the module shape handles worst, and
not a corner case: it is the Mandy fix.

---

## Evidence that has not been gathered

Stated so it is not mistaken for settled:

- The sketch's `admit` blocking path **never fired in the demo run** — the
  trace shows the happy case only. Admission control is written, not exercised.
- The watchdog and the pool restart have **never fired in any real run**, so
  removing them is reasoning, not measurement.
- Nothing has been measured about thread-vs-process memory behaviour under the
  real workload. The claim that threads are fine rests on every worker being
  blocked in `communicate()`, which is true of the current design but has not
  been profiled.
