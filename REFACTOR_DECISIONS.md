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

> **Amended by D15.** The type became `mesh_io.Mesh` and it *can* carry
> geometry, in an optional field filled only by an explicit `load()`. The
> constraint above still holds and is what D15 is built to satisfy — it is met
> by "every operation returns a new mesh" rather than by "the type cannot hold
> arrays". The handoff this paragraph feared does not happen: what sits in the
> queue is always the probed mesh, and the loaded one is a different value that
> a worker creates and drops.

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
load-bearing. The steps look independent and are not: the split runs after
decimation (D13), Blender runs before the seam split, the print-scale gate
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

#### Implemented 2026-09-15 as `libs/pool.py` — final shape

The interface below records how it landed after review. The three subsections
that follow are the intermediate drafts, kept because the reasoning that killed
each one is the part worth not repeating.

```python
class Pool[T]:
    def __init__(self, n_workers, item_selector, item_handler)
    def stop(self)
    def start(self)

item_selector(done, error) -> T | None   # next item, or None to shut this
                                         # worker down
item_handler(item)                       # do the work
```

**The pool owns the loop.** There is no `get_next`: the caller supplies work,
not control flow. `item_selector` runs with the lock held; `item_handler` runs
without it, or every worker would serialise and the pool would be pointless.

**One call, one place, one outcome.** A separate `item_error_handler` existed
briefly and was removed: the failed item reached both it and the selector, so a
caller releasing in both freed twice and a budget drifted upward. `error` is
the exception itself rather than a flag, so nothing is lost by collapsing them.

**The pool owns nothing but a lock and a shutdown flag.** Not the items, not
the ordering, not the admission rule, and not even which item each thread
holds — the worker passes its finished item back as `done`, because the worker
is the only thing that knows. Everything else moved to the caller, which
resolved four problems at once:

- **`admit`, `alone` and the wait loop are gone.** The queue is sorted
  cheapest-first, so if the head does not fit now it never will: waiting cannot
  help and the honest answer is to shed the worker. The three-way
  ADMIT/WAIT/SHED signalling I designed was solving a problem the sort order
  had already eliminated.
- **`n_workers` became a ceiling rather than a computed number.** Sizing the
  pool to the queue would require the pool to know the queue. The user's point
  settled it: *"if you spawn 10 threads and 8 immediately close themselves,
  what the harm?"* — a surplus thread asks once, is told `None`, and exits.
- **The per-thread map went too.** The pool kept `dict[thread name → item]`
  so it could work out `done` itself, and exposed it as `holding()`. But that
  duplicated a value the worker already had in a local variable, and its one
  observable use reported progress keyed by `"w3"` — a thread name, meaningless
  to any display. The user's objection was exact: *"keeping item in both places
  is confusing (you have no idea what item belongs to what thread) and
  unneeded."* Passing `done` back removed the map, `holding()`, and the use of
  thread identity altogether.
- **The `stop()` leak disappeared rather than being fixed.** See below.

**The combined call is the load-bearing decision.** `select(done)` both reports
a completion and hands out the next item, which is what lets a budget policy
release before it decides. It also once caused a genuine bug: when `start()`
called `select` itself to release a worker's final item, `stop()` over-committed
by one item per worker, because a combined call cannot release without also
acquiring — every compensating call re-committed what it had just freed. Three
patches failed on that before the right answer appeared, which was to stop the
pool calling `select` on its own behalf at all. A worker's last item is still
reported: it finishes, asks once more, is told `None`, and exits.

`test_pool.py` — 17 tests, ~0.8 s, including a budget policy end to end and an
assertion that a running total returns to zero after a full run.

---

#### Earlier draft: `admit` + `alone` (superseded)

**Everything below describes an interface that no longer exists.** It is kept
because each item records a wrong turn and why it was wrong — `admit` and
`alone` are gone, `status()` and `pending()` are gone with the queue, and the
pool no longer takes `items` at all — nor does it track which thread holds
what. Read it as history.

It was built to this interface, with three changes the implementation forced:

```python
class Pool[T]:
    def __init__(self, items, n_workers, admit=None)
    def get_next(self) -> T | None      # None = shut this worker down
    def status(self) -> dict[str, T]
    def pending(self) -> int
    def stop(self)
    def start(self, work)
```

1. **`admit` gained an `alone` parameter** — `admit(item, running, alone)`.
   The sketch, when `admit` refused but nothing else was running, handed the
   item over **anyway** and logged an override. For a module that claims to be
   policy-free that is wrong: it silently violates the caller's rule. But
   removing it outright reintroduces the deadlock the escape existed to
   prevent. Asking a second time with `alone=True` puts the choice where it
   belongs — a resource rule says yes (nothing is competing), a "never run
   this" rule says no and the pool sheds the worker. Both paths are tested.
2. **`timeout` is gone from `get_next`.** D4 already argued it means nothing
   for this workload; with the `alone` handshake there is no waiting that
   cannot resolve, so nothing was left for it to escape from.
3. **`pending()` added**, because the tests needed to assert that a refused
   item stays queued rather than being dropped.
4. **Selection is injectable — `select=`.** `get_next` keeps only the
   synchronisation: take the lock, release the finished item, notify, loop,
   wait. *Which* item to hand out moved into `get_next_default`, and passing
   `select=` to the constructor replaces it entirely.

   ```python
   select(items, running, admit) -> (item, wait)

       (item, _)      hand it out; select has already popped it
       (None, True)   nothing now, but work is in flight — block and retry
       (None, False)  nothing, and waiting cannot help — shed this worker
   ```

   It is called **with the lock held and receives the real list**, so it can
   reorder or extend the queue. That is deliberate: an earlier draft of this
   passed an index and let the pool do the popping, so a caller's bug could
   never corrupt the queue. The user's call was that the safer shape is
   speculative generality for a module only this project uses — *"if we decide
   a lambda should add elements to the queue, we should have a really important
   reason for that."* Recorded because the reasoning, not the ruling, is what
   would be re-litigated: the risk is real but the caller is us.

`log` is also gone: a domain-free module has nothing worth saying that the
caller cannot observe through `status()`.

**Tests: `test_pool.py`, 12 cases, ~0.6 s.** This is the half of the system
`test_pipeline.py` cannot reach — no meshes, no subprocesses, fake work is a
sleep. Covers every item handled exactly once under 16-way contention,
admission actually gating concurrency, both outcomes of the `alone` ask,
`stop()`, and a raising worker not stranding the pool.

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

> **Detection half built 2026-09-15** as `libs/indicators.py` — `export_path()`
> derives the location and `check()` reports `EXPORT_READY` when one exists.
> The module detects only; exporting, collecting and skipping remain to do.

#### Companion files belong in the same queue

Raised while reviewing what the old script does with images. It copies them —
`.png .jpg .gif .bmp .webp .tiff .svg .pdf .txt .md .readme .zip .7z .rar` —
from the source tree to the output tree, preserving relative paths, skipping
any already present.

Two defects in how, both fixed by treating a copy as ordinary work rather than
a startup chore:

```python
for src, dst in companions:          # serial, on the main thread,
    ...                              # before a single mesh is touched
    shutil.copy2(src, dst)           # unguarded
```

- **One failure aborts the whole run.** A permission error or a full disk on a
  single JPEG raises before any mesh is processed. Every other I/O path in the
  pipeline is defensive; this one is not.
- **It is serial and blocking**, scaling with file count on the main thread.

`check()` now reports `COPY_AS_IS` / `ALREADY_COPIED`, so the preparation pool
handles three kinds of item — copy, convert, queue — which is one coherent job
rather than three. A failed copy then fails one item, not the run.

The user's framing settled where it goes: *"indicator sounds more correct, it
indicates what file should be just copied; mesh_io opens mesh."* An image is
not a mesh, so `mesh_io` has no business being asked about it.

**Still to do:** the design doc's companion list omits `.zip .7z .rar`, which
are in `COMPANION_EXTENSIONS`. Minor drift, left until the collector moves.

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

**Dropping the pymeshlab rung does not drop pymeshlab.** It stays a hard
dependency for a different job: `split_shells` and `_merge_parts` are the only
implementations of shell splitting and merging, with no fallback of any kind.
"Remove the pymeshlab decimation rung" must not be read as "remove pymeshlab" —
the `pymeshlab_handler` tool module survives D12 intact.

Both pymeshlab and pymeshfix are now **checked at startup**
(`require_mesh_libraries`, called from both entry points), for the same reason
D12 requires a decimator: their absence used to surface as a mesh *outcome*
rather than a setup error. A missing pymeshlab made `split_shells` return `[]`,
indistinguishable from "single shell", so a run silently stopped splitting and
the log said nothing.

#### Worker shedding

**`None` is how the pool tells a worker to shut down.** The worker loop is
the pool's own, and `item_selector` returning `None` ends that thread — which
then releases its stack and any memory it was holding.

There are exactly **two reasons** the selector returns `None`, and they are
different questions that happen to share an answer:

1. **The queue is drained.** Nothing left to hand out, so every worker that
   asks is told to stop. This is ordinary shutdown.
2. **The next file needs more memory than is free while N workers are
   running.** The file itself is fine — it just cannot run *alongside* the
   others. So the pool sheds workers until the remaining set leaves enough
   headroom, and the big file then runs with fewer threads beside it.

Case 2 is the whole point. The alternative is a worker that blocks waiting for
room, holding a stack and a status slot while doing nothing, and — worse —
holding memory that is exactly what the blocked file is waiting for. Shedding
converts a deadlock-shaped wait into "run the big one narrow, then finish".

Note what is *not* happening: the file is never rejected and the queue is never
reordered. A worker leaves so the next file can have the room.

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

#### Open proposal — the preparation stage could detect and correct authoring scale

**Not decided. Raised 2026-09-14, recorded before it is lost.**

The Princess Leia set is authored in some unit that is not millimetres: its
parts measure 1.7–19.4 mm, a head is 2.5 × 3.2 × 2.6 mm, and `Sizer.stl` — a
part whose whole purpose is to be printed as a size test — is 19.4 mm. Nobody
intended a 2.5 mm head.

Earlier in the same conversation auto-scaling was dismissed, correctly, on the
grounds that the script cannot know an assembly's combined geometry: it sees
`Hand_L.stl` and `Lower_Body.stl` as unrelated files and has no idea what the
finished figure should measure. **The preparation stage dissolves that
objection** — it already reads every header in the folder to fill the queue, so
it sees the set, not the file. A folder whose every member is a few millimetres
is evidence in a way that any single file is not.

**Why this is worth more than a standalone rescaling script.** Scaling *after*
repair cannot undo what repair already did — Leia's fused hip seam is baked in,
because decimation ran at authoring scale. Scaling *before* repair means
`MIN_LAYER`, `MERGE_DIST` and `_VOLUME_MIN_MEANINGFUL` finally mean what they
were set to mean. That is most of the constants-at-scale problem solved as a
side effect, without changing a single constant.

**Open questions, none of them answered yet:**

1. **What triggers it?** Probably not a threshold on size. The real tell is not
   that Leia's parts are 2.5 mm but that they are 2.5 mm carrying ~800,000
   triangles per millimetre — a combination nobody authors deliberately.
   `tris_per_mm` is now logged and may be the better signal than dimensions.
   A legitimately tiny, legitimately simple part must not trip it.
2. **What factor, and derived how?** A fixed 25.4 does not survive inspection:
   it puts Leia's head at 64 × 81 × 66 mm and a hand at 94 × 76 mm — a hand
   larger than the head — and `Sizer.stl` at 493 × 521 × 267 mm, past any
   Bambu plate. 12.7 (half-inch) gives a 32 × 41 × 33 mm head, right for a
   1/6-scale figure. Deriving the factor from the largest part reaching a
   plausible figure height is more defensible than either constant, but
   "plausible" needs defining.
3. **One factor per folder, necessarily.** All 19 Leia parts share a coordinate
   system; a per-file factor would break the assembly. So the decision is the
   folder's, not the file's — which is exactly why it belongs in the
   preparation stage and nowhere else.
4. **How is it made visible and reversible?** A silent 12.7x on a folder that
   was meant to be small is worse than today's behaviour. Scaled copies want
   their own folder, plus a log line stating the factor and what triggered it.
   This interacts with D7's `stl-exported/` convention.
5. **Does it belong in the same folder as the format conversion?** D7 already
   writes normalised binary STL to `stl-exported/`. Scaled output is the same
   kind of artifact — a derived source — and probably shares that mechanism.

**Not in scope:** knowing the correct real-world size of a model. The pipeline
can detect *"these numbers are not millimetres"* far more safely than it can
guess *"this should be 180 mm tall"*, and only the first is needed.

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

### D13 — decimate first, then split: one order, no deferral

**Decided:** the pipeline is `if > MAX_FACES: decimate` then `split`. There is
no pre-decimation split and no deferral step.

**This is not a behaviour change.** The current code already does it, by a
route that hides the fact:

```python
_will_decimate = MAX_FACES > 0 and n_tris > MAX_FACES
_split_deferred = (... and (n_tris > _LARGE_MESH_TRI_LIMIT or _will_decimate))
```

Any file that will be decimated defers. So step B only ever runs on files
already under `MAX_FACES` — files with no decimation to come, where the
ordering question is vacuous. For every file where the order *means* something,
the split already happens after decimation.

**Why the order is right, not merely current.** Splitting first gives each part
its own `MAX_FACES` budget, so N parts can claim N × 900k faces. A figurine
whose fingernail is a separate shell would decimate that fingernail to the same
budget as the torso. The merge then exceeds the very limit decimation exists to
enforce — measured: every merged output in the logs lands just under 900k
(`Mandy_Clothed_SeamlessHipLegs` exactly *on* it) precisely because the budget
was spent before anything was divided.

Any fix for that would have to divide the budget proportionally by face count,
which is what decimate-first already is. The reorder cannot be made to work
without reimplementing the thing it replaces.

**What changes is structure, not behaviour.** B and B2 are one operation reached
two ways. Under the single order they collapse into one call site and
`_split_deferred` disappears. The `n_tris > _LARGE_MESH_TRI_LIMIT` trigger stays
as a memory guard on the scan, but it no longer needs to move the split.

**Origin.** Raised as "is the pre-decimation split too fancy?", and settled by
two arguments from the user rather than by measurement: the fingernail case
above, and that merging independently-decimated parts breaks the face ceiling.
An A/B run on `Lufy/simpl/Assembly.stl` was started and abandoned — that file is
several unrelated models merged for testing, so it could not represent the real
case, which is a single model whose shells belong together. Its one useful
result: part 0 repaired correctly at 200,546 faces, visually confirmed, which
is evidence *for* decimate-then-split producing sound geometry.

### D14 — Ctrl+C kills; the pool does not catch it

**Decided.** `KeyboardInterrupt` propagates out of `Pool.start()`. The pool does
not catch it, does not call its own `stop()`, and does not wait for in-flight
work. The caller installs the signal handler and owns the whole shutdown
sequence.

**Why killing mid-flight is safe, which is the load-bearing part.** A part
killed while running leaves its `~`-prefixed pending file, and the next run
reads that as unprocessed and redoes it. Nothing is half-committed and nothing
is lost — so "let the current item finish" protects nothing. The user's
framing: *"the reason for a Ctrl+C would be something went drastically wrong
and I do not need to wait for item to complete — I probably clicked Ctrl+C
because it already took too long. Also, if we kill mid-flight, the consequent
run would pick up unprocessed items."*

**Why `stop()` is not the answer to an interrupt.** `stop()` only prevents the
*next* selection. A worker already inside a 20-minute PyMeshFix call keeps
going, so catching the interrupt and calling `stop()` would make Ctrl+C look
like nothing happened for a long while — the opposite of what pressing it
means. `stop()` remains for genuine graceful cases (a `--max-files` limit, say).

**Why the pool cannot own the handler at all.** `signal.signal()` works only
from the main thread and allows one handler per signal per process, so a pool
that installed one would fight any other pool and override the caller's own
needs — a module reaching for a process-wide global, against the `libs/` rule.
And the real shutdown work is entirely outside the pool's knowledge:

```text
--one-file child   kill Blender, SIGKILL own children, _exit(130)
TUI                first Ctrl+C  -> graceful shutdown
                   second Ctrl+C -> restore SIG_DFL, _exit(130) immediately
```

The pool knows nothing about Blender, child PIDs, or what a second interrupt
should mean. Its entire contribution to shutdown is `stop()`, and the caller
decides whether to use it.

**Keep the second-interrupt escape.** If the first interrupt's own cleanup
wedges, there must still be a way out — the TUI's `_shutdown_started` flag plus
`os._exit(130)` is the shape to preserve.

**Carried forward:** `_child_pids_of` must survive the refactor unchanged.

**Measured 2026-09-15, and the wording above was misleading.** "Blender is a
grandchild" is true of one relationship and false of the other, and building
the wrapper on the wrong one would have moved `/proc` walking into a module
that cannot use it:

```text
Case 1 — within one process (what libs/blender.py does)
    spawner 1210131 -> blender 1210132        DIRECT child
    proc.kill() reaches it; blender has no children of its own

Case 2 — across the --one-file boundary (what the runner does)
    me 1210200 -> middle 1210201 -> blender 1210202
    blender is NOT a direct child of me
    kill only the middle process:
        blender still alive : True
        its PPid becomes    : 1      (reparented to init)
```

So the responsibility splits:

- **`libs/blender.py`** spawns Blender as a direct child and kills it directly
  on overrun. No `/proc` walk, because there is no intermediate process.
- **The runner**, killing a `--one-file` child, must walk `/proc` *first* or it
  orphans a Blender that keeps its memory until it finishes on its own. That is
  what `_child_pids_of` is for, and it belongs with whatever supervises child
  processes — not with the Blender wrapper.

### Proposed pipeline — `_process_file_impl` restructured

**Draft, for review.** Four changes agreed 2026-09-14 are marked **[NEW]**;
everything else restates current behaviour with its real condition, because the
prose descriptions of this function have repeatedly described the source order
rather than the execution order.

Entered once per mesh. Parts re-enter it with `is_part=True`.

#### 0. Skip checks — **[NEW: extracted]**

```text
if is_part:                         skip this phase entirely
if exists(dst):                     -> skip "already fixed"
if exists(dst_base.broken.stl):     -> skip "previously broken"
if exists(dst_base.failed.stl):     -> skip "previously failed"
if exists(dst_base.unrepaired.stl): -> skip "previously unrepaired"
if exists(dst_base.open.stl):       -> skip "previously open-edges"
```

Pure filename predicates — no mesh is read. Extracted as
`should_skip(dst, dst_base) -> reason | None`, this removes **9 of the 23
exits** from the main function on its own.

#### 1. Integrity

```text
n_tris, is_ascii, err = check_stl_integrity(src)
if err:                             -> corrupt, copy to .broken.stl, exit
```

Also the first point where `fmt` and `dims_mm` are known, so they are recorded
here.

#### 2. Decimate — **[NEW: moved ahead of the scan]**

```text
if MAX_FACES > 0 and n_tris > MAX_FACES:
        decimate to MAX_FACES        (fast_simplification)
        working = <decimated temp>
```

The limit test is free — the triangle count is bytes 80–84 of the header. The
scan is the expensive part, so testing the cheap condition first is strictly
better.

**What this fixes.** Today a 5.1 M-triangle file is scanned *before*
decimation, exceeds `_LARGE_MESH_TRI_LIMIT` (2 M), and returns
`nm=-1 open=-1` — unmeasurable. That scan is wasted work, and its `-1` sentinel
then threads through the rest of the function as a special case. Decimate first
and nothing over `MAX_FACES` ever reaches the scanner, so
**`_LARGE_MESH_TRI_LIMIT` largely dissolves as a concept.**

**What it does not fix, and must be stated:** defect counts remain
post-decimation. `Lower_Body.stl` records `nm 134 -> 0` and nobody knows what
the source had. The new order does not recover that — it only stops the
pipeline from pretending otherwise, because there is now exactly one scan.

#### 3. Scan

```text
nm, open              = scan_mesh_errors(working)
seam_edges, seam_loops = find_winding_seams(working)
```

Always runs, on whatever `working` now points at. A file under `MAX_FACES`
skipped step 2 and is scanned at source size — so this is *decimate if needed,
then scan, always*, not *decimate instead of scanning*.

#### 4. Clean-copy shortcut

```text
if nm == 0 and open == 0 and seam_loops == 0:
        copy working -> dst          -> ok (clean copy)
```

The seam condition is load-bearing: a mesh can be `nm=0 open=0` and still hold
a reversed region. Without it such a mesh takes the shortcut past every repair
stage, which is exactly what happened to the model that prompted the seam work.

#### 5. Split — **[NEW: one call site, no B2]**

```text
if not is_part and pymeshlab available:
        parts = split_shells(working)
        if parts:
                for each part: repair inline (recursive, is_part=True)
                all ok    -> merge            -> ok
                some ok   -> merge what repaired -> open (+ source kept)
                none      -> failed
```

**B and B2 collapse into this.** They were the same operation reached two ways:
step B ran first in source order, tested `_split_deferred` and skipped itself;
B2 did the real work later. Execution order was already decimate-then-split
(D13) — the source order merely disguised it. `_split_deferred`, the `_why`
message and the `step B: deferred` log line all disappear.

**[NEW] A partial merge is not `open`.** The branch currently returns
`status='open'` and writes `<name>.open.stl`. Measured on the only two files
that took it this run — both Falcons, `split5+partial4`:

```text
Millenium_Falcon.open.stl   90,942 tris   nm=0  open=0
Millenium_Falcon.open.stl   90,942 tris   nm=0  open=0
```

**Both are perfectly watertight.** Every part that reaches the merge passed its
own repair, so a merge of four clean shells is clean. The label is asserted by
the code path and never measured, and it is false. What is true about those
files is that **4 of 5 shells are present** — incompleteness, not open edges.

Three consequences:

- **`status='open'` currently means two unrelated things** — *has open edges*
  and *is missing parts*. That is why the run's summary shows 2 `open` files
  with no open edges. Separating them makes both countable.
- **The status is derivable, not diagnosable.** `n_ok` and `len(parts)` are
  both in hand when the file is written; `4/5` needs no scan. It is already
  carried on the result as `'partial': "4/5"` and then contradicted by the
  status beside it.
- **The suffix should match**, since the skip logic keys on it: writing to
  `<name>.open.stl` makes the file itself claim open edges, and a rerun then
  skips it for the wrong stated reason.

**A bbox condition would not work**, though it was the natural proposal. Both
files recorded `drift=[]`: the dropped shell was debris well inside the
Falcon's silhouette, so removing it moved no extent. That is the same blind
spot that let PyMeshFix delete Mandy's head unflagged — volume catches it,
bounding box cannot.

The recursion stays: each part gets the full pipeline, and depth is capped by
construction because the whole block is guarded by `not is_part`.

#### 6. Repair — PyMeshFix — **[NEW: the availability branch is dead]**

```text
needs_repair = nm > 0 or open > 0 or seam_loops > 0

if not needs_repair:          skip "nothing-to-repair"
else:
        fill_holes -> remove_smallest_components -> clean
        working = <repaired temp>;  nm, open = new counts
```

`require_mesh_libraries()` exits at startup when PyMeshFix or PyMeshLab is
missing, so every `_PYMESHFIX_AVAILABLE` / `_PYMESHLAB_AVAILABLE` test inside
the pipeline is now testing a condition that cannot be false. The run confirms
it: **23 `pymeshfix skip` rows, all `nothing-to-repair`, zero `unavailable`.**

Ten sites, and they do not all go:

```text
957, 959    inside require_mesh_libraries() itself        KEEP — the check
872         split_shells' own early return                KEEP — library boundary
2932, 2941  split guards (also part of the B/B2 collapse) dead
3117        the pymeshlab decimation rung (see D12)       dead
3269        step E "skip: pymeshfix unavailable"          dead
3366        step E's own guard                            dead
3625        post-Blender PyMeshFix guard                  dead
3656        "pymeshfix unavailable" reason string         dead
```

The distinction worth keeping: a module-level guard inside `split_shells` is a
library boundary and stays honest on its own terms; a guard in the *pipeline*
is now dead weight that implies a fallback path which no longer exists.

#### 7. Seam recovery (E0)

```text
if volume_after < 0.95 * volume_before
   and volume_before >= _VOLUME_MIN_MEANINGFUL
   and not is_seam_piece:
        split at closed winding-seam loops, repair each region, merge
        if recovered:                -> ok
```

Triggered by **enclosed volume**, not bounding box — Mandy lost its head with
the bbox unchanged, because the head sat inside the silhouette. Measured
2-for-20 this run, on the wrong population: only ~6 of 20 were real destruction
(27–34% retained), twelve retained 85–94%, and the threshold cannot simply be
tightened because Mandy itself sits at 85%.

**[NEW] Both volume constants should be configurable.** Several constants lack
a CLI flag (`_BBOX_TOLERANCE_FLOOR`, `REVIEW_RATIO`, `LOG_KEEP`,
`_BLENDER_MIN_RUN`), so that alone is not the argument. What distinguishes
these two is that **the run data actively disputes their values**:

```text
_VOLUME_LOSS_LIMIT     = 0.95   20 triggers: ~6 real destruction (27-34%
                                retained), 12 ordinary repair drift (85-94%),
                                and Mandy — the case the route exists for — at
                                85%, in the middle of the noise.
_VOLUME_MIN_MEANINGFUL = 50.0   silently disables the check entirely on Leia's
                                small parts (Neck_Cuff 12mm3, Head_Without
                                21mm3), which are the meshes least able to
                                survive an undetected deletion.
```

No fixed value for 0.95 both catches Mandy and excludes a dozen legitimate
repairs — the populations overlap. That makes it a parameter to sweep across a
rerun, not a number to pick better: the point of a flag here is finding out
whether a good value exists at all, which cannot be settled by argument.

`MERGE_DIST`, `MIN_LAYER`, `MAX_FACES`, `TIMEOUT`, `TIMEOUT_PART`,
`BBOX_TOLERANCE_PCT`, `BLENDER_RESERVE_PCT` and `WORKERS` all already have
flags, so the mechanism exists and this is two `add_argument` lines.

#### 8. Post-verify

```text
if nm == 0 and open == 0:
        nm, open = independent rescan    # pymeshfix self-report is unreliable
```

#### 9. Print-scale gate

```text
if nm == 0 and open > 0 and MIN_LAYER > 0:
        measure every open boundary loop
        largest < MIN_LAYER  ->  open = 0           (accept, skip Blender)
        else                 ->  record "above-print-scale"
```

`nm > 0` deliberately never reaches here: those are topology errors, not holes,
and a slicer can genuinely mis-fill them. Open issue: `MIN_LAYER = 0.6 mm` is
14.6% of the Neck_Cuff's diagonal — on a 4 mm part this waves through a gap
spanning a seventh of the model.

#### 10. Success, or Blender fallback

```text
if nm == 0 and open == 0:
        move working -> dst          -> ok

else:   Blender (step F)
        ok           -> ok
        open_only    -> PyMeshFix once more -> ok, else .open.stl
        unrepaired   -> PyMeshFix once more -> ok, else .unrepaired.stl
        neither      -> .broken.stl or .failed.stl
```

#### What the rewrite buys

| | now | proposed |
|---|---|---|
| exits in the main function | 23 | ~14 |
| split call sites | 2 (B, B2) | 1 |
| scans of an oversized mesh | 2 (one useless) | 1 |
| `-1` sentinel threading through | yes | no |

#### Still unresolved

- **The state baton.** `working`, `working_tris`, `nm`, `open`, `stats` and
  `temps` are read and written by nearly every step. Extracting a step means
  deciding what it takes and returns, and for most the honest answer is "the
  whole state, and the whole state back" — which is the `Stl` DTO from the
  target design, not a parameter list. **This draft does not solve it.**
- **Source defect counts are unknowable** for anything decimated. Worth
  deciding whether that is acceptable or whether a cheap pre-scan is wanted.
- **Does seam recovery earn its place?** 2-for-20, and both successes are the
  same model duplicated in the collection.

### Float drift was tested and ruled out (2026-09-14)

**Do not re-propose quantised vertex welding.** Reading
`_weld_binary_stl`'s bit-sort comments naturally suggests it — sort on
coordinates rounded to 4 decimals and cast to int64, rather than on raw float
bits. It would appear to fix float drift, fold `-0.0` for free, save memory via
a packed key, and flatten coplanar faces. Measured on `Millenium_Falcon.stl`
(393 k tris) and `whole-costume01.stl` (2.0 M tris), all four claims fail:

```text
Falcon           exact 48,745 verts, 0 degenerate
                 4/5/6 decimals: +0 merges, +0 degenerate

whole-costume01  exact 999,976 verts, 1 degenerate
                 4 decimals: +29 merges, +37 degenerate
                 5 decimals:  +1 merge,   +2 degenerate
                 6 decimals:  +0 merges,  +0 degenerate
```

- **Drift merging**: 29 extra merges in 6 M vertex slots — 0.0029% — on the
  messiest model in the collection, and none at all on the Falcon. The 24.2x
  duplication in the Falcon is the STL format writing identical bits
  repeatedly, not drift.
- **Flatness**: degenerate faces went *up*, not down. Geometry never moved in
  this test (sort-key-only variant); collapsing two vertices 0.1 µm apart onto
  one index turns a sliver into a zero-area triangle.
- **Memory**: the packed-int64 win needs all three axes in 63 bits. The Falcon
  spans 155 mm and needs 22 bits/axis — 66 > 63, so it does not pack, leaving
  three int64 columns at 2x the current key size.
- **`-0.0`**: genuinely free, but already handled in one line.

The probe is `design/quantweld.py` (read-only, self-contained; run it against
any mesh to re-check). Float drift is not a problem this collection has.

### Step evidence from the 2026-09-14 full run

902 files, 897 ok, 2 open, 2 failed, zero exceptions. 916 step rows.

```text
step                ran   ok  fail  skip   rate   med     p95     max
pymeshfix           570  554    16    23    97%  23.4s  148.9s  950.1s
decimate            237  237     0     0   100%     -       -       -
split                34   32     2     0    94%     -       -       -
blender              21   14     7     0    67%  12.8s  105.1s  105.1s
seamsplit            20    2    18     0    10%     -       -       -
printscale            9    6     3     0    67%     -       -       -
pymeshfix2            2    2     0     0   100%   9.2s    9.5s    9.5s
```

**`decimate` is 237-for-237**, including Leia's ~1 M-triangles-per-mm parts.
D12's premise holds on fresh data; the fallback rungs have still never run.

**`seamsplit` is the weakest step in the pipeline, 2-for-20.** Both successes
are `Mandy_Body_Dinamuuu3D.stl` part 0 — the model the route was built for —
appearing twice because it is duplicated in the collection. So: one real save
for 20 Blender invocations. Every attempt found seam edges but no closed loop
to cut on, which is why the Blender-first fallback runs at all; it produced a
usable loop twice, left 15 still unseparable, and returned no mesh 3 times.

The trigger fires on the wrong population. Of 20 volume-loss triggers, only
~6 were real destruction (27–34% volume retained); twelve retained 85–94%,
which is what filling holes and dropping fragments legitimately costs. But the
95% threshold cannot simply be tightened — **Mandy itself sits at 85%**, near
the bottom of the mild cluster. Several triggers were on meshes of a few mm³
(`66 -> 62`, `183 -> 165`), which `_VOLUME_MIN_MEANINGFUL` is meant to filter
and evidently does not.

**`blender` mixes two jobs.** Of 21 runs: 19 repair-fallback (12 ok, 7 fail =
63%) and 2 ASCII conversion (2 ok). Conversion cannot meaningfully "fail to
repair", so one success rate over both muddies each. D7's preparation stage
already proposes moving conversion out of the repair path, which would separate
them.

### Absolute constants mean different things at different scales

Raised by the user after inspecting the Leia output: *"it feels weird to use the
same value for Leia and big models like Mandy."* The measurement backs it — the
same constant spans **41x** as a fraction of the model:

```text
model                   diag mm   MERGE 0.01   FLOOR 0.1   MIN_LAYER 0.6
Leia/Neck_Cuff              4.1      0.2430%     2.4296%         14.578%
Leia/Head_Without           4.8      0.2074%     2.0739%         12.443%
Leia/Branches               8.6      0.1158%     1.1577%          6.946%
Mandy_Body                108.6      0.0092%     0.0921%          0.553%
whole-costume01           113.7      0.0088%     0.0880%          0.528%
Millenium_Falcon          197.7      0.0051%     0.0506%          0.303%
```

**`_BBOX_TOLERANCE_FLOOR = 0.1` inverts its own purpose.** It exists to stop
`BBOX_TOLERANCE_PCT = 0.7%` flagging sub-micron noise on small models. On the
Neck_Cuff it is 2.43% — more than three times looser than the percentage it is
meant to floor. On small models it is not a floor, it is a much wider ceiling.
It should be *derived* from the percentage, not compete with it.

**`MIN_LAYER = 0.6` is 14.6% of the Neck_Cuff's diagonal.** The print-scale
gate accepts any open boundary below one layer height as unprintable-anyway; on
a 4 mm part that waves through a gap spanning a seventh of the model. The
constant is genuinely a printer property, so the value is right — but its
*meaning* changes with scale, which is the real defect.

**`_VOLUME_MIN_MEANINGFUL = 50.0` switches the safety check off entirely.**
Neck_Cuff's bbox volume is 12 mm³ and Head_Without's is 21 mm³, both below the
threshold, so the volume-loss check that catches PyMeshFix deleting a region
**never runs** on them. Mandy, the model it was built for, sits 3,526x above
it. That is not a tolerance being wrong; it is a guard being absent on exactly
the models least able to survive without it.

**`MERGE_DIST = 0.01` is about authoring noise, not printing**, so it should
scale with the model or with `min_feature` rather than being fixed in mm.

Nothing broke on Leia despite all four being wrong for it — the parts are dense
enough (`min_feature` 0.003–0.011 mm) that PyMeshFix had ample real geometry to
work with, and all 19 returned `ok`. The constants were wrong and the meshes
were good enough to absorb it.

### The fused seam on Leia is decimation, not repair

The user noticed the Leia output looked *"slightly distorted — two touching
parts have a fused line between them, like hip and leg stitched together rather
than being clear body parts."* Traced on `Lower_Body.stl` (12.5 x 6.6 x 7.7 mm):

```text
decimate       5,136,808 -> 899,999   (5.7x, flagged in review_decimated.tsv)
fill holes       899,999 ->  899,859   -140
drop fragments   899,859 ->  899,648   -211
clean            899,648 ->  899,374   -274
```

**PyMeshFix is not the cause**, which was the first hypothesis and it was
wrong: every sub-step *removed* faces. A stitched web between two surfaces
would show the count going up. It closed 96 open edges by deleting boundary
geometry, not by spanning a gap. Blender never ran on any Leia part at all, so
`MERGE_DIST` was never applied either.

**Decimation is what produces the fused seam.** Quadric edge collapse optimises
silhouette error, and a crease where a hip and a leg touch barely changes the
silhouette — so it is cheap to collapse across, and QEC does. At 5.7x on a part
whose features are already ~85x below the layer height, that is the visible
result.

**The same limit lands very differently by scale.** `MAX_FACES = 900_000`
applies identically to a 12.5 mm hip and a 200 mm Falcon. The Falcon, at 393 k
triangles, was never decimated at all; Leia's `Lower_Body` lost 82% of its
geometry to reach the same ceiling.

That reads like an argument for a resolution-derived target — decimate only
until features reach printable size — and it is wrong. See the next section:
`MAX_FACES` is Bambu's complexity threshold, and models are scaled to printable
size *after* repair, so a target computed from `min_feature` at authoring scale
is wrong by the scale factor. **The fused seam is a trade-off of reaching 900 k
at all, not a badly chosen number.**

**Also exposed by this trace:** `Lower_Body.stl` scanned as `nm=-1 open=-1` —
over the 2 M limit, so never measured. The `nm 134 -> 0, open 96 -> 0` recorded
against it are **post-decimation** counts. Those 134 non-manifold edges may be
decimation artifacts rather than source defects, and nothing in the current
pipeline can tell the difference.

### `MAX_FACES = 900_000` is a slicer constraint, not a memory ceiling

Recorded because the source does not say so and the wrong reading is easy to
reach: Bambu Studio warns that a model is too complex above roughly this
triangle count, **regardless of the model's physical size**. That is the entire
reason for the number.

Two consequences follow, and both kill an otherwise attractive idea:

- **A resolution-derived target does not work.** Deriving the decimation target
  from `min_feature` vs `MIN_LAYER` — decimate only until features reach
  printable size — was proposed and rejected. The user scales models to
  printable size *after* repair, so any target computed at authoring scale is
  wrong by the scale factor. The constraint is on triangle count as such, which
  scaling does not change.
- **The fused seam is therefore a trade-off, not a bug.** Reaching 900 k from
  5.1 M means something must go, and QEC gives up creases first because a crease
  between two touching surfaces barely affects the silhouette. The only lever
  that does not require knowing the final scale is weighting creases more
  heavily *within the same budget* — a decimator quality setting, not a
  different target. Whether `fast_simplification` exposes such a weighting is
  unknown and worth checking before assuming it.

### Proposed — log what each absolute constant meant on this mesh

Not a change to any constant. The values are right; what is missing is any
record of what they implied for a given model, which is what made the Leia
distortion take a full trace to diagnose.

Each step that consults an absolute constant should record its relative value:

```text
printscale     applied at 14.6% of model extent  (MIN_LAYER 0.6mm, diag 4.1mm)
merge_dist     0.24% of diagonal
volume check   SKIPPED — 12mm³ below the 50mm³ floor
```

The third line matters most. A guard that silently does not run is the same
failure mode as every logging gap found on 2026-09-13/14: *never ran* and
*ran and found nothing* must not look identical afterwards.

### Proposed — derive `_BBOX_TOLERANCE_FLOOR` from the percentage

The one item here that is straightforwardly a bug rather than a design tension.
`_BBOX_TOLERANCE_FLOOR = 0.1` exists to stop `BBOX_TOLERANCE_PCT = 0.7%`
flagging sub-micron noise on small models, but on a 4.1 mm part it *is* 2.43% —
three times looser than the percentage it is meant to floor. It should be
derived from that percentage rather than competing with it.

**Explicitly not proposed:** making `MIN_LAYER` or `MERGE_DIST` relative. They
are physical properties of the printer; a 4 mm part and a 200 mm part are
printed by the same machine. The defect is the missing record, not the values.

### The instrument has known holes

Stated so the tables above are not over-trusted:

- **Three steps carry no timing at all** — `decimate`, `split`, `seamsplit`.
  "How long does decimation take" is still unanswerable, and `seamsplit` cannot
  be priced against the one save it produced.
- **`pymeshfix2` still logged no baseline.** `nm_in`/`open_in` came out empty
  on both rows, because they read `_pv_nm`, which is only assigned when Blender
  *reports success* — and the two rows that fired are precisely the case where
  it did not (`BLENDER_OPEN`). The numbers were recoverable from the step log
  (`open=12 -> 0`) but the column added for this question stayed blank.
- **The archived 13-for-13 for `pymeshfix2` was survivorship.** Before
  2026-09-13 the failure path wrote nothing, so "never helps" and "never ran"
  were the same observation.
- **`bbox_drift` is last-writer-wins and does not cross the part boundary.**
  28 `bbox changed` events in the run produced 9 summary rows. Both Falcons
  show empty drift because part 2 drifted under PyMeshFix and back under
  Blender, to three decimals — the excursion exists only in the step log.

### Review of `stl_batch_fix.blender` (2026-09-14)

Findings, not decisions — the script is frozen until the refactor, so none of
this was changed. Ordered by confidence. The file is 593 lines and runs in
six steps: load → merge doubles → T-junction split → normal vote → repair loop
→ final cleanup and validate.

**1. `write_stl_from_bm` assumes every face is a triangle.** It packs exactly
three vertices into each 50-byte record. Both paths that reach it triangulate
first (the early exit, and step 6a), so this is not live — but a quad would
have its fourth vertex silently dropped, producing a corrupt-but-parseable
STL. A `len(f.verts) != 3` assertion would make it impossible rather than
merely unlikely.

**2. The output buffer is sized before the loop that fills it.** `buf` is
allocated from `len(bm.faces)` captured up front. Nothing enforces that the
iteration yields the same count; a mismatch either raises inside `pack_into`
or leaves trailing zeros that parse as degenerate triangles at the origin.
Writing the count after the loop removes the coupling.

**3. `load_binary_stl` trusts the header's triangle count** — `f.read(n_tris *
50)` before any validation, so a corrupt header claiming 4 G triangles asks for
a 200 GB read. The per-triangle loop guards against a *short* read, but the
allocation happens first. `check_stl_integrity()` validates this on the Python
side, so it is defence-in-depth, not a live bug.

**4. Two thresholds answer the same question.** `_open_tol = 0.5` mm here
decides `BLENDER_OK` vs `BLENDER_OPEN`; `MIN_LAYER = 0.6` mm drives the Python
print-scale gate. A 0.55 mm gap is "fine" to one and "significant" to the
other. The Falcon's 0.7209 mm cleared both, so it has never bitten. These
should be one value, passed in the way `merge_dist` already is.

**5. `normal_layer` is re-fetched five times, defensively.** After
`remove_doubles`, after `split_t_junctions`, and in both import branches. The
repetition suggests uncertainty about when BMesh invalidates custom layers; it
is harmless, but it hides whether any one of those re-fetches is load-bearing.

**6. The boundary-loop walk picks `nexts[0]` with no geometric criterion.**
Where three or more boundary edges meet a vertex, it takes the first unvisited
one. A wrong branch yields a loop that is not the hole outline — the fill is
then rolled back by the NM check, so it fails safe, but it can silently fail to
close a hole that was fillable. The `seen_junctions` handling in step 4d
suggests the case is known.

**7. `except Exception: pass` around the face fill** makes a genuine error
indistinguishable from "this loop was not fillable" — the same shape as every
logging gap found on 2026-09-13/14.

**8. `MAX_ITER = 200` in the T-junction splitter is an undiagnosed cap.** One
split per iteration with a full re-scan; a mesh with 500 T-junctions silently
keeps 300. `total_splits == 200` is the signature and is never checked.

**Deliberately not flagged.** The 12-pass repair loop, the stall counter and
the wider-NM escalation all look sound, and this run supports them: 14 of 16
Blender invocations returned `BLENDER_OK`. Using the STL's own stored normals
as ground truth for winding (`normal_vote`, flood-filling per connected
component and flipping a component when more faces disagree than agree) is the
cleverest thing in the file and should survive the refactor intact.

**Priority if any of this is acted on:** #1 and #4. The first is a real
corruption path however narrow; the second is two files disagreeing about what
"too small to matter" means.

---

## Mesh data

### D15 — One `Mesh`, geometry optional, every operation returns a new one

**Decided.** `mesh_io.Mesh` carries both the cheap facts and — after an
explicit `load()` — the welded geometry. There is one writer, `mesh_io.write`.

**The requirement this answers**, in the words it was set in: *"I just want
data in one place and somehow uniformly, so we would not need to create 3
different `write_stl` methods."*

#### What was rejected, and why

**A separate `writer` module** was the first proposal and was rejected on
inspection: there is only ever one `write_binary_stl(path, verts, faces)`
whether the arrays arrive loose or on an object, so a module for it would have
been a module containing one function that every other module imports. The
three call sites in the old script already called one function; they were never
three methods.

**A mutable DTO updated as work progresses** was the second, and was rejected
for two reasons. It recreates the `_process_file_impl` shared-mutable-state
pattern — the "state baton" that is the hardest thing in the old script to
follow. And it couples cost to identity: the object in the queue and the object
holding 383 MB would be the same object, so nothing could be said about what a
queue costs.

#### The shape

```python
mesh   = mesh_io.probe(path)              # ~200 bytes
loaded = mesh_io.load(mesh)               # a NEW Mesh, geometry attached
small  = decimator.decimate(loaded, cap)  # a NEW Mesh, via with_geometry()
mesh_io.write(small, destination)
```

`Mesh` stays `frozen=True`. `load` does not fill geometry in on the mesh it is
given; it returns a second mesh that has it. `with_geometry` is how an
operation reports a changed mesh, and it re-derives `triangles` from the faces
rather than carrying the old count over — the whole point of decimation and
repair is that the count changed, so carrying it would be carrying a lie.

**This satisfies the Target section's memory constraint rather than abandoning
it.** That section says the DTO must hold no geometry, because "an immutable
value carrying arrays would double peak memory at every handoff". The handoff
it feared does not occur: what sits in the queue is always the probed mesh, and
the loaded one is a different value a worker creates and drops. The constraint
is met by *every operation returning a new mesh*, not by *the type being unable
to hold arrays*.

**Loading is a call, never a property.** Measured: 174 MB peak for 1M
triangles, 383 MB for 2.55M. A worker's memory budget is decided before it
starts, so a geometry that materialised on first attribute access could blow
that budget from inside what reads as a field access.

**Bad data is a result; only a bad request raises.** `load` on a truncated file
returns an invalid `Mesh` with a `problem`, because a corrupt file is expected
input for this tool. `load` on an ASCII STL raises, and so does `write` on an
unloaded mesh — those are programming errors, the caller having skipped the
conversion stage or the load.

#### What the tests caught

Porting `_weld_binary_stl` introduced a read-only-buffer bug that the original
did not have. `np.frombuffer` returns a read-only array; the original wrapped
the vertex slice in `np.ascontiguousarray`, which copies — *except* when the
slice is already contiguous, which happens only for a single-triangle mesh, and
then it returns the read-only view unchanged and the `-0.0` fold fails on it.
The fix is a plain `.copy()`. Worth recording because of which input exposed
it: the degenerate-face test, a one-triangle mesh no real model contains. The
bug would otherwise have waited for the strangest file in a collection.

### D16 — Steps pass meshes, not paths; only Blender round-trips

**Decided.** Every processing step is `Mesh -> Mesh`. The mesh is read once at
the start of a file's pipeline and written once at the end.

#### What this replaces

The old pipeline threaded a *path* between steps. Each one wrote
`dst + '.<step>.stl'`, reassigned `working`, and the next read it back; the
`temps` list existed to clean up that trail at the end. The file was not an
output — it was the only carrier of the mesh between two in-process steps.

With geometry in the DTO (D15) that reason is gone. `working` is a value.

#### Why not validate by writing and re-reading between steps

Considered seriously, because a round trip *sounds* like the safer option, and
rejected on two grounds:

**It costs CPU, not just I/O.** A binary STL has no vertex table, so writing
means expanding welded arrays back to 6x duplicated triangles and reading means
re-welding them — the lexsort that costs 1.75s on a 2.55M-triangle mesh. The
SSD is not the bottleneck; the weld is. On a collection where 763 of 768 files
do nothing but decimate, that is the whole run.

**It validates less, not more.** Edge counting is what validation means here,
and it is strictly better on welded faces than on raw bytes:
`_build_edge_counts` packs each vertex into a 192-bit int in a per-triangle
Python loop *because* it has no vertex sharing to work with; with `faces` in
hand the same answer is two vectorised numpy calls. And `scan_mesh_errors`
returns `(-1, -1)` above `_LARGE_MESH_TRI_LIMIT` — so disk validation silently
gives up on exactly the meshes that most need checking.

So validation stays, and moves in-memory. It is the round trip that goes.

#### The one forced boundary

Blender is a separate process with its own interpreter and its own mesh
database. Our numpy arrays do not exist in its address space and there is no
shared-memory channel, so the only way to hand it geometry is to serialise it —
which is what a file is. That rung writes a temp, runs, loads the result back,
and deletes both, inside the module.

**The measurement that made this a small problem.** From the preserved
collection logs (768 files finished):

| | count | share |
|---|---|---|
| files invoking Blender at all | 6 | 0.8% |
| Blender wall time | 163s of 3974s | 4.1% |
| decimations taking the first rung | 88 of 88 | 100% |
| files needing no repair path at all | 763 of 768 | 99.3% |

Blender never ran as a *decimator* in that run. The asymmetry is real but it is
on a path taken by fewer than one file in a hundred.

**PyMeshLab does not need the boundary**, which was checked rather than
assumed: `pymeshlab.Mesh(vertex_matrix=, face_matrix=)` takes numpy in and
`vertex_matrix()` / `face_matrix()` give it back, so the middle rung stays
array-native. The old code's `load_new_mesh` / `save_current_mesh` was a disk
round trip inside our own process for no reason.

#### On a better interchange format

Raised: if we must write a file for Blender, STL is a poor choice — no vertex
table, 6x duplication, a re-weld on every read. Correct, and **PLY is the right
answer if this is ever worth changing**: binary, a real vertex table, natively
imported and exported by Blender (verified on 4.0.2: `wm.ply_import`,
`import_mesh.ply`, `wm.ply_export` all present), and it maps almost exactly onto
`Geometry` — a vertex block and a face-index block, which is what `verts` and
`faces` already are.

Not done now: it is a 0.8% path, and the format must be one Blender already
parses, which rules out anything custom. Revisit if Blender's share rises.

**Where a custom format would actually pay**, recorded so it is not confused
with the above: `.npy` for *caching a welded mesh between runs* —
`np.save`/`np.load`, memory-mappable, no parsing at all. That is a
resume-a-run feature, not an interchange one, and nothing has asked for it.

#### On releasing memory before the Blender call

Python is refcounted, so clearing the last reference frees the arrays
immediately — but the callee cannot release the *caller's* reference, and
`Mesh` is frozen so it cannot empty its own field. Rebinding
`mesh = decimate(mesh, ...)` does free the old geometry, but only when the call
*returns* — which is after Blender has finished allocating its own copy.

A `take()` returning `(facts_only_mesh, geometry)` would be the explicit move.
Not built: it is one mesh's arrays on a 0.8% path, and the pool's admission
control already refuses work that does not fit in free memory. If a measurement
ever shows the overlap mattering, `take()` is the answer.

### Deferred — PLY for the Blender boundary

**Not a decision yet.** Recorded with its trigger so it is not rediscovered
from scratch. Extends D16's closing note; the reasoning for *why* the boundary
exists at all is there, this is about what crosses it.

**The change**: use binary PLY instead of binary STL for the temporary files
that go to and from Blender. Only the scratch boundary — the deliverable the
slicer opens is a separate question (see below).

**Why it is better**, on the merits:

| | STL | PLY |
|---|---|---|
| write | expand welded arrays back to 6x duplicated triangles | `verts.tobytes()` + `faces.tobytes()` |
| read back | `frombuffer` + lexsort weld (~1.75s / 2.55M tris) | `frombuffer`, reshape, done |
| size | 50 bytes/triangle | ~12/vertex + 13/face, roughly a third |

PLY carries a vertex table, so the weld on the way back does not get faster —
it *disappears*, because the sharing is already in the file. `Geometry` is
already PLY's model: a vertex block and a face-index block.

**Why it is not worth doing for the speed.** ~2s on 0.8% of files — about 12
seconds across a 768-file collection. That is not a reason.

**The reason that would justify it.** `blender_fx/decimate.blender` does not
use Blender's STL exporter on the path that matters: it hand-packs the bytes in
a per-polygon Python loop (`struct.pack_into`, three calls per face, 900k
iterations on a large mesh). `bpy.ops.export_mesh.stl` appears only in the
"already within limit" branch. That loop exists *because* STL needs manual
assembly; `bpy.ops.wm.ply_export` would replace it with one C call and delete
~100 lines of untestable in-Blender serialisation. The win is removing
hand-rolled byte packing from a script no unit test can reach, not the seconds.

**Verify before committing to it** — three things, none assumed:

1. that `wm.ply_export` writes **binary little-endian** PLY, not ASCII;
2. that a PLY round trip through Blender **preserves vertex count** — Blender
   may split vertices on import for normals or UVs, which would defeat the
   entire point and must be measured, not hoped;
3. whether `mesh_io` grows PLY support or it stays private to `decimator`.

**Trigger**: do it when the Blender scripts are being touched anyway. That is
expected during `repairer`, which uses Blender far more than decimation does
(6 files of 768 invoked Blender, none of them as a decimator).

**Not to be confused with the output-format question.** What the slicer opens
is a different decision on a different path: Bambu Studio's import dialog lists
`.3mf .stl .oltp .stp .step .svg .amf .obj .gltf .glb .fbx` — **no PLY**. So
PLY can never be the deliverable. If the deliverable is ever revisited, 3MF is
the candidate (vertex table, zip-compressed, carries units explicitly — which
the Leia model, authored in non-mm units, would have benefited from), weighed
against STL being what every slicer and sharing site accepts and what the
sources already are.

**One correction worth keeping**, because it was believed briefly and would
have distorted this decision: writing float32 does **not** drift. The bytes we
hold are the bytes we write are the bytes we read back, bit-identical —
asserted by `test_a_written_mesh_reloads_identically`. STL's weakness is the
missing vertex table, not precision. Drift enters only when some *other* tool
nudges a coordinate between writes, which splits one vertex into two that no
longer weld. See also "Float drift was tested and ruled out (2026-09-14)".

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
