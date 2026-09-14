# Refactor decisions

A running record of what was decided and why, while the refactor is being
designed. Written because the reasoning is expensive to reconstruct and easy to
lose — the design doc went 13 commits stale in a day, and `TODO.md` went four
items behind.

**This file holds what was decided and why.** `TODO.md` holds what is left to do
and how to go about it — the guidelines, how small to make each step, what the
tests do not catch.

Decisions are grouped by topic. Numbers are global and stable across topics, so
a commit message citing D7 keeps meaning D7 when a new topic is added.

Status: **design only. No code has moved.**

> **Renumbered 2026-09-13.** Entries were previously numbered in the order they
> were written, and each new one was inserted before D5 — so the file read
> D1–D4, D4b, D7, D8, D9, D10, D5, D6. They are now in decision order. Three
> contradictions were fixed at the same time: D2 and D3 still described `requeue`
> and a surviving watchdog, both of which later entries had abolished. Earlier
> commit messages refer to the old numbers.

---

## Target — the module design

Worked out in discussion before the numbered decisions began; they refine it.
A direction, not a specification — deviate where the code argues back.

**`Stl` — a plain-data DTO.** Facts and paths: format, triangle count, defect
counts, bounds, volume. **No geometry.** A 900k-face mesh is ~45 MB of
triangles, and memory is already the binding constraint (`_BYTES_PER_TRIANGLE`,
`auto_worker_count`, the OOM killer taking workers); an immutable value carrying
arrays would double peak memory at every handoff. Vertex arrays are loaded and
discarded inside each operation, as they are today.

Values are overwritten as newer data arrives. Where a step genuinely needs the
prior value, the DTO simply holds both — `volume` and `volume_before`, `bounds`
and `bounds_before`. Two decisions need that: volume loss after repair (the
Mandy seam recovery) and bbox drift. No append-only history mechanism; the cases
are few and known. See D9.

Keep it serialisable. The `--one-file` child reports its result to the thread
that spawned it as a plain dict over a JSON pipe, so either the DTO is plain
data by construction or it gains an explicit `to_dict()` at that boundary.

**That is the only boundary.** Under D3 the worker is a thread in the parent
process, not a separate process, so nothing has to be serialised between a
worker and the parent — only between the child and the thread that owns it.

**Tool modules** — `blender_handler`, `pymeshfix_handler`, `pymeshlab_handler`.
One tool each, no policy. This is where the invisible-Blender bug came from:
four call routes, timing recorded at one of them.

**Operation modules** — `decimator`, `repairer`, `splitter`, `scanner`. Each
owns its fallback ladder *and* its `isRequired…` predicate, so `Stl` never
learns `MAX_FACES` or which tool does what. `decimator` owns
fast_simplification → pymeshlab → blender; `repairer` owns pymeshfix → blender
plus seam recovery.

- **A predicate must be cheap and side-effect-free.** If it is not, it is a
  process and gets named as one: `scanner.scan(stl)` returns an `Stl` carrying
  defect counts, after which `repairer.isRequiredRepair(stl)` is free because it
  reads facts already held. Two current functions are processes wearing
  predicate clothing — `_open_loops_are_printable` re-scans, `_will_decimate`
  recomputes a condition decided elsewhere.
- **Inject capabilities, not control flow.** Where a module needs a fact it
  cannot cheaply obtain, pass the processor in (`isRequiredRepair(stl,
  scan=scanner.scan)`) rather than duplicating the logic or re-scanning. Give
  the parameters defaults so the common path stays prose. Injected callables
  answer questions or perform named operations; they never make decisions the
  module owns. Applied to admission in D5 and to budgets in D10.

**Pipeline** — reads as prose, orders the steps, and documents why the order is
load-bearing. The steps look independent and are not: the split is deferred
until after decimation, Blender runs before the seam split, the print-scale gate
precedes the Blender fallback. State those constraints in the module docstrings
or someone will tidy the sequence and silently regress it.

**The runner.** An earlier version of this section said the runner was *exempt*
— 1,455 lines of pool management that should stay explicit imperative code.
**That was wrong; see D1.** It is not one hard thing, it is the same concern
implemented twice in two files and already drifted apart, and D2–D11 specify
what replaces it.

---

## Worker pool

Eleven decisions, no open questions. `design/pool_sketch.py` is a runnable
sketch of D4, predating D6.

### D1 — The runner is not exempt

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

### D2 — Worker-pull, not parent-push

**Decided.** The pool owns a queue; each worker loops `get_next()` → do it →
`get_next()` until drained.

The current code submits every file up front as a future. The TUI then
re-implements pull semantics *on top of* push, because it needs memory-aware
admission — `_fill` holds files back and submits more only as the running set
drops. It is already straining toward pull and getting there awkwardly.

What pull buys concretely: **`plan_worker_count` becomes natural.** A worker
asking for work is exactly the moment to decide whether it should get any. Push
has to guess ahead and re-check on completion.

It also removes the reason retry existed at all — see D6, which abolishes it.
(An earlier version of this entry said "retry stops being special: a failed file
goes back on the queue". That was written before D6 and is wrong: nothing goes
back on the queue.)

---

### D3 — Threads, not a process pool

**Decided**, and this is the one that deletes the most.

The mesh work already runs in a `--one-file` **subprocess**. The process *pool*
was a separate decision that brought failure modes the subprocess isolation had
already made unnecessary. With worker threads driving subprocesses:

| today | with threads |
|---|---|
| `BrokenProcessPool` fails every pending future | a thread raising affects only itself |
| `_abandon_pool` — SIGKILL the pool, shutdown on a daemon thread, never join | nothing to abandon |
| `_worker_status` is a `Manager()` proxy; a worker SIGKILLed mid-read deadlocks the parent in `futex_do_wait` | plain dict under a lock (D11) |
| `_status_snapshot` daemon-thread workaround for that deadlock | unnecessary |
| `max_tasks_per_child` forces `spawn`, incompatible with the fork design | irrelevant |
| the watchdog, which kills a *worker* the parent cannot otherwise reach | gone entirely — the thread owns the `Popen`, so `communicate(timeout=)` is the mechanism (D8) |
| pool restart, `_to_retry`, `_attempts`, requeue-at-back | gone entirely — the failure they handled cannot occur (D6) |

**The GIL does not matter**: every worker thread is blocked in `communicate()`
waiting on a subprocess, not computing.

**The only process boundary is child↔thread**: argv and `--result-fd` going
down, a JSON result dict and a return code coming back. Worker and parent are
the same process, so nothing is serialised between them. An earlier
worry about pickling lambdas was an artifact of `ProcessPoolExecutor` — it does
not apply to this design. Measured, for the record: a lambda cannot be pickled,
a closure cannot, a bound method can. None of it is relevant once the pool is
threads.

#### Survives unchanged

- **`_child_pids_of`** — killing a `--one-file` child must still walk `/proc`
  for Blender one level deeper. That is the process tree, not the pool.
- **The `--one-file` child** and everything in it.
- **`plan_worker_count` / `estimate_peak_bytes` / `mesh_is_too_large`** — the
  memory model is real; it moves from `_fill` into an `admit` callback.

---

### D4 — The Pool interface

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

**The sketch predates D6 and still has `requeue`, `attempts` and a `timeout=5`
worker.** It has not been updated — treat this section as the current interface
and the sketch as the earlier draft.

---

### D5 — Admission is a condition variable and a caller-supplied callback

**Decided**, replacing an open question that turned out not to be one.

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

### D6 — No retry. If it failed, it failed

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

#### What retry was actually for

Worth recording, because it was never "the mesh might work next time". Its only
trigger was `BrokenProcessPool`:

> A worker killed by the OOM killer breaks the whole `ProcessPoolExecutor`, not
> just its own task: every not-yet-completed future — including files still
> sitting in the queue, unassigned to anyone — fails with `BrokenProcessPool`.
> **One 7M-triangle mesh therefore cost 40 untouched files in the last run.**

So retry meant "this file never ran at all", not "try the repair again". A mesh
that genuinely failed repair was never retried; that path has no retry logic.

#### Why it does not come back

Two independent reasons, and the policy one is the stronger:

1. **Policy:** failed is failed. This survives changes to the failure modes — if
   some new spurious failure appears later, the answer is still "rerun it", not
   "add retry back".
2. **Mechanism (D3):** with threads there is no future, so a file has only
   three states — queued, held by a worker, done. "Failed without running" stops
   being expressible, so the forty-innocent-files problem is not handled better,
   it cannot occur.

`requeue` and `attempts` were in the first sketch because machinery was ported
from the old model without asking whether the new one needed it — the same
error as an earlier detour into pickling constraints that only existed because
of `ProcessPoolExecutor`.

#### What is lost, and what replaces it

`_attempts` distinguished "died once, probably innocent" from "died twice, is
the culprit", producing the `killed a worker twice (likely out of memory)`
diagnosis. That distinction goes.

What replaces it is more precise: an OOM-killed `--one-file` child gives its
thread a negative return code directly, so the file is reported as failed with
the actual signal, and no sibling is affected.

---

### D7 — A preparation stage that normalises everything to binary STL

**Decided.** A first pool fills the queue for the main one.

#### Why it exists

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

#### The rule

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

#### Why writing to the source tree is acceptable here

The "source is never modified" rule came from a specific worry: *our repair
output* polluting the source folder, where a buggy script leaves files that are
hard to tell from originals and hard to manage.

A Blender format export is not that. It is a lossless container change — same
triangles, same coordinates, binary instead of text — so even a buggy run leaves
the source mesh in a different encoding. A dedicated `stl-exported/` folder is
one directory to delete, obviously not originals, and the export is a standard
Blender operation, not something implemented here.

#### The collector exclusion is not optional

Without it the next run collects the exports as inputs, and every OBJ is
processed twice — once as OBJ, once as its export — producing two outputs under
different names for the same model. The same failure the `~parts` and
`__MACOSX` exclusions exist for, and easy to miss because it only appears on the
**second** run.

#### What it simplifies downstream

Stage two stops having an `is_obj` / `is_ascii` branch. Today those files set
`blender_src = src` and bypass the Python pipeline entirely — no pre-scan, so no
nm count, no open-edge count, no print-scale gate. They go straight to Blender
and take whatever comes back. After preparation they are ordinary binary STLs
and get the full pipeline.

The conversion is not extra work: Blender already does it in step F today.
Preparation moves it earlier and keeps the result.

#### Ordering

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

#### Worker shedding

`get_next` returns `None` for worker N when there is no longer room for it, and
that worker exits and frees its resources — rather than blocking and holding a
stack and a status slot while doing nothing.

Ordering and shedding interact: smallest-first means workers shed late,
largest-first means they shed early and the tail runs wide.

**A shed worker never needs to come back.** Sorting by face count means cost only
rises as the queue drains, so a worker that exits because the head item is too
large will never meet a smaller one afterwards. There is nothing to come back to
— not an unlikely scenario, an impossible one — so the pool needs no mechanism
for respawning a shed worker.

The caveat, recorded rather than hidden: the sort is by *predicted* cost, and
`estimate_peak_bytes` is a linear extrapolation from a single 7M-triangle
calibration, so a later file could turn out cheaper than an earlier one. That
does not revive the question, because shedding follows sort position rather than
measured memory — the queue order is what it is whether or not the prediction
was accurate.

---

### D8 — The watchdog does not survive

**Decided.**

#### What it does today

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

#### Why nothing is left for it

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
subprocess call, and kept this open on that. The question was an artifact of the
old shape, where the timeout enforcer and the file's owner were different
processes and the parent had to reach across. Once they are the same thread it
stops existing. (Third time machinery was carried over from the design being
replaced, after the pickling detour and `requeue`.)

#### What moves rather than disappears

- **Marker writing** stops being special: the killer and the marker-writer are
  the same thread, so `.timeout.stl` is written on the ordinary path.
- **`_child_pids` before the kill** stays — a `--one-file` child must still have
  its Blender grandchild reaped. Process-tree logic, already noted in D3.

#### Stale justifications retired with it

The docstring claimed the kill is safe because "the file is retried once, then
set aside" — D6 removed retry. It also carried the `_status_snapshot`
workaround and the `Manager()` deadlock commentary, both of which D3 deletes
along with the proxy.

---

### D9 — Volume is measured at open, like any other fact

**Decided.**

#### The problem as it was framed

Seam recovery fires when PyMeshFix reports success — nm=0, open=0, every defect
count clean — and has quietly deleted part of the model:

```python
_vol_before = _mesh_volume(working)      # before PyMeshFix
_vol_after  = _mesh_volume(_pmf_tmp)     # after
if _vol_after < _vol_before * _VOLUME_LOSS_LIMIT and not _is_seam_piece:
    -> _repair_by_seam_split(...)
```

Mandy's head: 13,730 mm³ → 11,676, a 121,537-face component gone, reported `ok`.
Invisible to everything else — the deleted region sits inside the model's own
bounding box, so the bbox check stays quiet too.

That looked like a problem for the module design, because every other predicate
asks about *a mesh* (`isRequiredDecimation` reads a face count) while this one
asks about *a transition*: 11,676 mm³ is not suspicious on its own, only beside
13,730.

#### Why it is not a special case

**Volume is a property of the mesh, measured at open, like triangle count and
defect counts.** The DTO carries it because that is what the DTO is for — not
because seam recovery asked for it. `repairer` then compares what it measures
now against what the DTO already holds, and there is no transition fact needing
a home.

The awkwardness came from meeting volume first *inside* the recovery logic and
concluding it belonged to it. Every operation that transforms a mesh produces a
new `Stl` with fresh measurements; comparing against the previous one is
available to anyone, stored for no one.

#### Heavier state

Scalars live on the DTO. Vertex maps and edge counts are megabytes and do not —
but they are already computed and discarded repeatedly:
`scan_mesh_errors(return_edges=True)` builds a packed edge map that
`_open_loops_are_printable` recomputes a few lines later.

Those can be kept on disk beside the working file and reloaded, so a step can be
skipped or re-entered without redoing the scan. The mechanism already exists:
`--one-file` writes `.decimate.stl` and `.pymeshfix.stl` intermediates and
deletes them in a `finally`. Treating them as resumable checkpoints is a change
of intent, not of machinery.

**Caution, from a bug already hit once.** A cache keyed only by filename goes
stale when the file is rewritten. The parts protocol solved that with
`~`-prefixed pending names, so the convention exists — but a scan cache is
easier to get subtly wrong than a whole-file rename, because a stale *number*
still looks plausible where a missing file does not.

---

### D10 — The budget arithmetic stays in `--one-file`

**Decided.** Same logic as now, unchanged.

#### It was never cross-cutting

The question claimed the arithmetic had no home because decimation, repair and
Blender all draw on one mesh budget and Blender's share depends on what earlier
steps spent. Traced against the code, every budget computation **already runs
inside the child process**:

| site | function | process |
|---|---|---|
| 2633, 2867, 2989 | `_process_file_impl` → `_part_cap` + `_arm_mesh_alarm` | child |
| 2101, 2158, 2172 | `_run_blender_script` / `fix_stl` / `blender_decimate` → `_blender_budget` | child |
| 3726 | `--one-file` entry → `_arm_mesh_alarm(TIMEOUT_PART or _effective_timeout())` | child |

So the arithmetic is not scattered across modules needing a shared owner. It is
one process tracking its own elapsed time, which is exactly what "decimation,
repair and Blender share one budget" means in practice. `elapsed` is the child's
own clock.

Fifth time in this discussion a difficulty came from importing the old design's
framing rather than from the new design — after the pickling detour, `requeue`,
the watchdog gap, and volume-as-a-transition-fact.

#### The one parent-side budget decision

`stl_batch_fix.py:3436`, in `process_file_subprocess`:

```python
_budget = int(budget) if budget else _effective_timeout()
```

That is the parent deciding **how long to wait for a child before killing it**.
Under D3 and D8 it becomes the worker thread's `communicate(timeout=)`.

The split is clean rather than complicated:

- the **parent** owns *when to give up on a child*
- the **child** owns *how to spend its own time*

#### What it means for the operation modules

`decimator`, `repairer` and `blender_handler` do not each own a timeout policy.
They receive the remaining time, or ask the child's clock for it — so the
injected-capability pattern from D5 covers this too, and no budget owner is
needed.

---

### D11 — Status is a plain dict under a lock, written by the owning thread

**Decided.**

#### What the state actually is

Four fields per worker, written once when a file is picked up and removed when
it finishes:

```python
_worker_status[pid] = {'rel':     rel,            # which file
                       'started': _t.monotonic(), # when it was picked up
                       'bytes':   _bytes,         # size on disk
                       'tris':    _tris}          # triangle count from the header
```

Never updated mid-file, which is why the panel can say "on this file for 41m"
but nothing about progress within it.

Three consumers: the worker panel at 4 Hz (all four fields), the watchdog
(`started` only — dies with D8), and a stale-row sweep.

#### Why it needs so much machinery today

All of it follows from the dict living in another process with killable writers:

- **`_status_snapshot`** reads the proxy on a throwaway daemon thread it never
  joins, because a `Manager` proxy call has no timeout and a worker SIGKILLed
  mid-read blocks the parent forever — observed as `futex_do_wait`, 0 % CPU,
  the TUI still redrawing stale rows.
- **The stale-row sweep** exists because a SIGKILLed worker never reaches the
  line that pops its own entry, so its row would count up forever against a file
  nobody is working on.
- **`_pid_is_live`** reads `/proc/<pid>/stat` because `os.kill(pid, 0)` succeeds
  for a zombie.

#### What replaces it

A plain `dict` guarded by a `threading.Lock`. Writers are threads in the same
process; a thread is never SIGKILLed mid-write, and a thread that finishes
always runs its own cleanup. So:

| today | with threads |
|---|---|
| `_status_snapshot` daemon-thread read | `with lock: return dict(status)` |
| stale-row sweep | deleted — entries cannot be orphaned |
| `_pid_is_live` on this path | deleted |

The owning thread writes its entry on pickup and removes it on completion, both
under the lock; the TUI copies the dict under the same lock at 4 Hz. Four
fields, a sub-microsecond critical section, no contention worth designing
around.

The question of whether status could leave the `Manager` proxy was already
answered by D3 — removing the process pool removes the proxy. What was left of
it, *who reports status*, is answered here: the thread that owns the file.

#### What becomes possible

Status can now be updated **mid-file**, because the writer is not a process that
must survive being killed. The step E start line already knows a mesh is about
to spend minutes inside PyMeshFix; that could appear in the panel rather than
only in the log.

---

### Open — worker pool

**Nothing.** Every question raised during the pool design is closed: five became
decisions (D5, D8, D9, D10, D11), two were folded into D7, and one turned out to
have been answered already by D3.

---

## Mesh pipeline

The work is extracting the steps of `_process_file_impl` (826 lines, nesting
depth 8, 91 if-statements, 23 return points) into the operation modules the
Target section describes. `TODO.md` item 1 holds the approach — how small to
make each step, and what the 36 tests do not catch.

### D12 — `decimator` is one implementation, required at startup

**Decided.** Drop the three-rung fallback ladder. `fast_simplification` becomes a
hard dependency, checked once when the run starts rather than per file.

#### The ladder never did what its shape suggests

It looks like failure recovery. It is not: `run_fast_decimate` has essentially no
failure surface. It returns `None` in exactly one case — `n_in <= target_faces`,
meaning there is nothing to decimate — and the caller correctly falls through.
Anything genuinely wrong raises, and the `try` around it catches that.

What the lower rungs actually guard is **absence**, not failure.
`fast_simplification` is a soft import, and `install.sh` tolerates a failed
install with "decimation falls back to PyMeshLab/Blender (slower, more memory)".
That is an install-time condition, knowable at startup, and it cannot change
between files — yet the per-file `if not _dec_done` chain re-asks it 761 times.

#### The dependency is not fragile

```text
fast_simplification-0.2.0-cp312-cp312-manylinux_2_24_x86_64...whl
287 KB · Requires-Python >=3.9 · Requires-Dist: numpy
```

A prebuilt manylinux wheel, 287 KB, one dependency the script already has. No
compiler, no source build. The realistic ways it could be missing — no network,
an architecture with no wheel, a Python version the project has not published
for yet, glibc older than 2.24 — are all install-time and all visible
immediately.

Every one of those would equally break `pymeshfix` and `pymeshlab`, which are
**also optional imports and have no alternative implementation at all**. The
script logs "unavailable" and skips those steps. Keeping a decimation ladder
while doing that for repair is inconsistent, and it is expensive: two unexercised
paths, one of which (`blender_decimate`) carries its own script template, timeout
budget, and the `RLIMIT_AS` history.

#### Measurement

189 decimations across every log, **all `fast_simplification`, zero fallbacks,
zero failures** — including the 14.1 M-triangle file. The pymeshlab and blender
rungs have never executed. They are not proven dead (rung one never failed, so
they were never reached) but nothing is known about how they behave either.

The pymeshlab rung is additionally gated on `_LARGE_MESH_TRI_LIMIT`, so on a
>2 M-triangle mesh with `fast_simplification` absent, the ladder skips straight to
Blender — the least-tested path, reached only on the largest files.

#### Missing decimator is a hard failure, not a skip

This is where decimation differs from every other optional step. It is a
**deliverable, not an optimisation**: a too-large repaired file decimated later
in Bambu immediately regains non-manifold edges and needs repairing again.

So "no fallback" must mean **the run refuses to start** without a decimator —
not that it silently produces undecimated output. PyMeshFix being absent degrades
a repair but still yields a file; a missing decimator yields a file that will
fail in the slicer.

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
