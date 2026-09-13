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
    def get_next(self, timeout=None)   # None = stop; blocks while admit() refuses
    def requeue(self, item)            # BACK of the queue, never the front
    def attempts(self, item)
    def status(self)                   # what each worker is on, for the display
    def stop(self)
    def start(self, work)              # threads, each running work(self)
```

The whole worker the caller writes:

```python
def repair_one(pool):
    while (src := pool.get_next(timeout=5)) is not None:
        result = run_one_file(src)          # spawns --one-file, waits, reads rc
        if result.failed and pool.attempts(src) < 2:
            pool.requeue(src)
```

**Requeue goes to the back**, not the front: front would make the next worker
retry the poisonous file immediately and burn the budget on it.

---

## D5 — The pool counts, the caller decides

**Decided.** `requeue` and `attempts` are mechanism. "Retry once then set
aside", and "a file the watchdog killed is known guilty and must not be
retried", are judgements about meshes rather than about pools, so they live in
the worker function.

---

## Open, not yet decided

**O1 — `get_next` blocking on `admit`.** Blocking keeps the worker loop trivial,
but a blocked thread does nothing, and a file too large to ever be admitted
would block forever. The sketch has two escapes — a timeout, and "if nothing
else is in flight, run it alone" — both of which are the author's judgement, not
the user's decision yet. `mesh_is_too_large` already identifies the
never-fits case.

**O2 — does the watchdog survive at all?** With threads, `communicate(timeout=)`
in the worker *is* the timeout, and that already exists in
`process_file_subprocess`. The separate watchdog uniquely catches a worker
wedged *outside* the subprocess call, which with threads is a much smaller
surface. It has never fired in any run, so there is no evidence about what it
would have caught.

**O3 — status reporting.** `_worker_status` becomes a plain dict, but the TUI
reads it every 0.25 s from the render loop while workers write. Needs a lock;
`_status_snapshot`'s daemon-thread workaround becomes unnecessary.

**O4 — can `_worker_status` leave the Manager proxy entirely?** It is the main
reason a worker can wedge today. If the child reported its own status, or status
travelled the existing result pipe, the restart machinery becomes genuinely
vestigial rather than arguably so. (Partly answered by D3, which removes the
proxy — but the question of *who* reports status is still open.)

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
