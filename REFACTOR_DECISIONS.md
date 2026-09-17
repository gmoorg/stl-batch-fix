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

### D17 — One scanner, on arrays, with no ceiling

**Decided.** Defect counting becomes `libs/scanner.py`, working on a loaded
`Mesh`. Four questions — open/non-manifold counts, open-loop measurement,
winding seams, connected components — off one edge map.

**Why one module rather than four functions where they are used.** The old code
asked these questions at eight call sites across four stages (after optional
decimation, after Blender, and twice around PyMeshFix), each one re-reading the
file from disk and rebuilding the map. Building the map *is* the work; all four
questions are derived from it. Splitting them would mean building it repeatedly
again, which is the thing being fixed.

**What welded faces delete.** `_build_edge_counts` packed each vertex into a
192-bit int in a per-triangle Python loop, and that was not gratuitous — an
unwelded STL has no vertex sharing, so identity had to be recovered from
coordinate bytes, and one int per edge beat seven tuple/float objects five-fold
on peak RSS. With `faces` in hand the indices *are* the identity, so the packed
keys, the `-0.0` fold and the per-triangle loop all go at once.

**The ceiling goes with them, and that is the real win.** `scan_mesh_errors`
returned `(-1, -1)` above `_LARGE_MESH_TRI_LIMIT = 2_000_000`; `_post_verify`
translated that into "UNVERIFIED" — on exactly the meshes that are both hardest
to scan and most likely to be broken. A 2.1M-triangle scan is now a test case.

**Unscannable cannot be represented.** `_post_verify` needed a three-valued
return because `(0, 0)` for an unscannable mesh reads as verified-clean at
every call site (`nm > 0 or open_e > 0`), and files were written out as
finished having been checked by nothing. Here an unloaded mesh raises. The
third value is not needed because the state it described cannot occur.

#### The bug the differential test caught

Worth recording, because the process is the point rather than the bug.

The first vectorised `winding_seams` found edge pairs by comparing adjacent
rows after a lexsort, then masked with `forward[:-1] == forward[1:]` against a
`pair_first` of length `n`. Length `n` against length `n-1`: it raised
`ValueError` on **every mesh that had any seam candidate** — 200 of 200 random
trials.

It was caught by running a differential check against the original dict-based
implementation *before* writing any tests. Tests written first would have been
written against the broken behaviour and would have passed on the paths that
did not raise.

The fix stops slicing adjacent rows: `np.unique(..., return_inverse=True)` plus
a `bincount` of the direction flags asks the per-edge question directly, so
correctness can be read off the code rather than derived from index arithmetic.
400 random meshes now agree with the original exactly. The original is kept
verbatim in `test_scanner.py` as the oracle — it is slow, but it ran on the
whole collection, so agreement with it is the strongest statement available.

**General rule this supports**: when replacing a working implementation with a
faster one, the old one is the test oracle, and the differential check comes
before the unit tests — not after.

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

**Verified 2026-09-15** — the two measurable preconditions hold.

Tested on a 2562-vertex / 5120-face icosphere written as binary PLY with a real
vertex table, round-tripped through Blender 4.0.2:

| | verts | faces | v/f |
|---|---|---|---|
| written by us | 2562 | 5120 | 0.500 |
| round trip, no modifier | 2562 | 5120 | 0.500 |
| after decimate to 30% | 770 | 1536 | **0.501** |

A welded closed mesh sits at v/f ~ 0.5; fully split would be 3.0. **The vertex
table survives, decimation included** — no splitting even at the new creases
decimation creates, which was the case most likely to break it. `wm.ply_export`
writes binary little-endian, and the header it emits is bare (`x, y, z` plus a
face list, no normals, no UVs), which is *why* nothing splits: there are no
per-face attributes to disagree about, so no reason to duplicate a corner.

The concern was originally stated badly — as being about mapping new vertices
to old ones, which never happens, since Blender's output is loaded as fresh
geometry. The real risk was narrower: if Blender split vertices, its output
would need the same lexsort weld an STL needs, and PLY would buy nothing. It
does not split, so the weld genuinely disappears rather than getting faster.

One implementation note: Blender exports `property list uchar uint
vertex_indices` while the test wrote `int`. Both are legal PLY — a reader must
handle the type it finds rather than assume one.

#### Scope, settled 2026-09-15 — the Blender boundary only

**Do it**, and not on cost grounds. The refactor's justification has never been
runtime; it is that the old script accumulated things nobody can verify. The
hand-packed `struct.pack_into` loop in `decimate.blender` is exactly that —
about 100 lines of byte assembly inside a script no unit test can reach,
writing a format that needs re-welding on the way back. `wm.ply_export` deletes
that whole category. Declining it because it only helps six files would apply a
standard applied nowhere else in this refactor; the "do it when the scripts are
touched anyway" trigger was deferral dressed as discipline.

> **Superseded in part, 2026-09-16.** The justification above is
> `decimate.blender`'s hand-packed `struct.pack_into` loop — and D19 deleted
> that file outright when it removed the Blender decimation rung. The ~100
> lines of untestable byte assembly are gone, not replaced, so the reason
> recorded here no longer exists in the form stated.
>
> What remains is `convert.blender`, which is a weaker case: it already uses
> `bpy.ops.export_mesh.stl`, Blender's own C exporter, with no hand-packing at
> all. Switching it to PLY swaps one operator for another and saves a weld on
> ASCII/OBJ conversions — real, but not the "delete byte assembly nobody can
> test" argument that justified the work.
>
> **Still open, reconsidered when `repairer` lands**, since that is where
> Blender will next be used heavily and where the boundary's cost is felt
> again. The verification below stands regardless: the vertex table does
> survive a Blender round trip.

**Scope is the scratch boundary and nothing else.** A wider version was
proposed and dropped the same evening: supporting PLY as an *input* format, the
way OBJ is supported. It was rejected on the only ground that matters — there
has never been a PLY in the collection, so it solves a problem that does not
exist. Explicitly out of scope:

- `indicators` learning a `.ply` extension
- `converter` routing PLY through the conversion stage
- `mesh_io.kind()` recognising PLY as a source format
- parsing arbitrary PLY dialects — ASCII, big-endian, extra properties, quads

**In scope**: a narrow writer and reader for one boundary. It must handle our
own output and what Blender emits, which is now known exactly — binary
little-endian, bare `x`/`y`/`z`, and `property list uchar uint vertex_indices`.
Anything else may be refused rather than guessed at.

**Where it lives**: probably `mesh_io`, since `repairer` needs the same
boundary and will use Blender more than decimation does — but it is a private
internal format there, not general PLY support.

**The work**: `mesh_io` gains a PLY writer and reader; `decimate.blender` swaps
its packing loop for `wm.ply_import` / `wm.ply_export`; `_decimate_blender`
changes its temp extensions. Tests both sides, including a real Blender round
trip in `test_blender` rather than only the synthetic check already run.

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

---

## Where the refactor stands (2026-09-15)

Seven modules built, 229 tests, all green. `stl_batch_fix.py` is untouched and
still frozen; `main` is at `4c1c0c2` and `refactor` is five commits ahead.

| module | what it owns | tests |
|---|---|---|
| `pool` | worker loop, nothing domain-specific | 24 |
| `indicators` | what the filesystem says about a file | 20 |
| `blender` | launching Blender, killing it, reading its markers | 32 |
| `mesh_io` | probe, load, write — the only writer | 44 |
| `scanner` | defect counts off one edge map | 31 |
| `decimator` | the three-rung ladder | 22 |
| `converter` | the preparation walk | 20 |

Still to build: **`repairer`** (PyMeshFix, seam split, NM repair, open-edge
fill) and the **orchestration** that threads a file through all of it.

### Open question — is `open_loops_are_printable` worth keeping?

Raised and **not settled**. Recorded mid-discussion so it can be resumed
without re-reading the pipeline.

**What the check does.** Accepts a mesh with open edges, without repairing it,
when every open boundary is smaller than one printed layer.

**The case for it** — the 1st-body cascade, the reason it was written:

```text
after pymeshfix   879,332 faces  volume  99.99%  open=4    0.01mm, all at one point
  -> blender called to clear them (180s)
after blender     874,236 faces  volume 100.04%  open=142   28 NEW holes, 0.023-0.162mm
  -> pymeshfix called again to clear those
final             356,392 faces  volume  44.94%  open=0     head and torso gone
```

The check fires at the first line. It does not save a redundant call — it
declines to *start* a cascade that creates defects and ends by destroying the
model while reporting `open=0`, which is the pipeline succeeding by its own
standard.

**The case against it**, and the reason it is open: `MIN_LAYER = 0.6` is an
absolute constant. On the Princess Leia part, 5.6 mm across, a 0.6 mm hole is
**11% of the model** — the check would wave through a hole that is plainly
real. The constant is also already doing a second, unrelated job at
`stl_batch_fix.py:2853`, where a mean edge length below `MIN_LAYER` is read as
evidence the mesh is over-detailed.

**The recovery ladder as it actually is** (worth stating, because the
discussion assumed one rung fewer): a repair that loses volume does not simply
get dropped. `_repair_by_seam_split` cuts the mesh at its winding seams and
repairs each region separately — measured 100.0% and 100.2% volume on the two
regions of a mesh that lost 15% while joined. So it is
*repair -> volume check -> seam-split retry -> fall back to the pre-repair
mesh*, and the printability check sits ahead of all of it.

**The candidate fix, not yet agreed**: make the test scale-aware rather than
absolute — a hole is unprintable only if it is below the layer height *and*
below some fraction of the model's bounding-box diagonal. `mesh_io.diagonal`
already exists. That removes the tiny-model failure while keeping the cascade
guard. The alternative is to drop the check and always take the second repair
pass, accepting the cascade risk.

**Also agreed but not yet built**: `scanner` should own `volume(mesh)`.
`_mesh_volume` is already an `einsum` over `verts[faces]`; today it pays a full
weld from a path every time it is asked, twice per volume comparison.

---

## Splitting and debris — discussed 2026-09-15, partly settled

### Settled — the split moves from recovery to planning

**Decided in discussion.** Detection runs upfront on every file; the split
happens when detection calls for it, not after a repair has already failed.

**The reasoning that changed my objection.** I argued for split-on-failure on
runtime: 6 files of 768 ever needed it, and paying detection on all 768 looked
like doubling the run to help six. Two counters, both better than the argument
they answered:

- **Closed seam loops make the split unavoidable.** PyMeshFix deletes a region
  when regions disagree about winding, and that was learned the hard way. So
  split-on-failure does not avoid the split — it pays for a doomed PyMeshFix
  pass first, then splits anyway. Upfront removes the wasted pass, not work.
- **Detection makes the work orderable.** The pool already sorts by triangle
  count for memory admission. A file that needs splitting currently looks
  identical to one that does not until it fails; known upfront, it is
  schedulable rather than discovered.

And the cost is partly refunded: splitting lets debris shells be dropped rather
than repaired. On `whole-costume01` that is 443 of 444 shells under 100 faces —
work that was being spent on fragments nobody would print.

**The shape**: detect (`scan` + `winding_seams`, both cheap) -> split when
seams or multiple shells say so -> drop debris, recorded -> repair each part ->
merge. Volume-loss-then-seam-split stays as the backstop for what detection
misses.

### Idea, not yet designed — debris by bounding box, not face count

**Recorded as an idea. Not decided, not built.**

`_MIN_SHELL_FACES = 100` is a triangle count standing in for physical size. It
was measured honestly — the smallest real shell seen was 750 faces, so 100
keeps every real part with 7.5x margin — but a dense speck carries thousands of
faces in a sub-millimetre box. That is exactly the Leia shape: a million
triangles per millimetre of extent. A high-detail sculpt's debris sails past a
face floor.

**The insight that dissolves it**: after the split, each part *is* its own
mesh, so its bounding box can be measured directly — `mesh_io.dimensions()`
already exists. No fraction-of-the-parent arithmetic, no constant meaning
different things at different scales, because the measurement is on the part
itself.

Sketch, to be worked out later: drop a part whose bounding box is under some
small multiple of `MIN_LAYER` in **every** dimension. Open question whether
that multiple is a constant with its reasoning written down (something near
"under 2 mm in all three axes") or a per-run setting.

**Whatever is dropped must be recorded as dropped** — count, largest extent,
total volume. Two reasons. The volume guard exists to catch deleted geometry
and would otherwise see a deliberate drop as loss, sending the file down the
seam-split recovery path for something that was chosen. And a speck at 5 mm may
matter at 300%: "dropped 443 shells, largest 47 faces, total 0.02 mm^3" is
checkable later, "the volume changed" is not.

### Measured — `scanner.shells()` is too slow, and that is my bug

Timed on surviving collection meshes, load versus shell count:

| triangles | load | shell count | count as share of load |
|---|---|---|---|
| 309,555 | 0.20s | 2.03s | 1015% |
| 2,061,994 | 1.91s | 12.51s | 657% |
| 5,081,319 | 6.42s | 33.86s | 527% |

**Counting shells costs 5-10x the entire load.** I had claimed in discussion
that counting was "nearly free" now that it no longer goes through PyMeshLab's
file-writing split — reasoning from "it is just union-find" without measuring.
Wrong by an order of magnitude.

The synthetic curve shows it is linear, about 2x `scan()`, so it is not
pathological — just a Python-level `find()` loop over every face, which is the
same class of thing `_build_edge_counts` was criticised for and which `scan()`
avoids by being vectorised. **`shells()` needs a vectorised rewrite before any
ordering decision is argued from its numbers**; a proper implementation should
be near `scan()`, order 1s on a 2M mesh rather than 12.5s.

One real finding survives regardless: `Mandy_Body_Dinamuuu3D.stl` has **39
shells, all 39 above the 100-face floor** — matching the design doc's note
exactly, so the floor behaves as recorded on real data.

### Not yet measured

How many files in the collection carry **closed seam loops**. Detection being
upfront is settled, so this does not gate that — but if the answer is 200
rather than 6, the splitting stage needs a different design than if it is rare.

### Open — scratch files must not outlive an interrupted run

**Raised 2026-09-15, not yet built.** Applies to the Blender boundary temps
(the PLY input we hand Blender, and the output we read back).

**What is already handled.** `_decimate_blender` puts both temps in a
`tempfile.mkdtemp` and removes the directory in a `finally`, so an ordinary
failure — a timeout, a non-zero exit, an unreadable output — cleans up. There
is a test asserting no `decimate-*` directory survives. This is already better
than the old `dst + '.decimate.stl'`, which wrote into the *output tree* and
kept the file until the end of the pipeline.

**The gap: Ctrl+C.** D14 has the pool deliberately not catching
`KeyboardInterrupt`. A `finally` normally runs on interrupt, but if it arrives
while a thread is inside `Runner.run`, or during the `finally` itself, the
directory survives. That is also the likeliest moment for it: the run is
interrupted *because* something is wrong, which is when a large mesh is sitting
in scratch. Same exposure for an OOM kill or power loss, where no `finally`
runs at all.

**Size makes it more than cosmetic**: a 5M-triangle mesh as PLY is roughly
80 MB, and several workers interrupted mid-flight leave a few hundred MB
orphaned. Under `/tmp` it is at least invisible to the output tree and cleared
on reboot — but the machine has no swap and an M.2 SSD, so silently accumulating
scratch is not free.

**Candidate fix, not agreed**: a *known* scratch root rather than per-call
`mkdtemp` — swept once at startup, before any work begins. That covers every
exit path uniformly (interrupt, OOM kill, crash, power loss) instead of trying
to make each one orderly. Same reasoning as `~parts` in the old code: make
stale state structurally reachable so it can be cleaned, rather than relying on
an orderly exit.

Open sub-questions: where the root lives (a fixed subdirectory of the output
tree, or of the system temp directory), and whether a sweep must avoid deleting
scratch belonging to a *concurrently running* second instance.

#### Correction 2026-09-16 — the input file is not garbage

**The entry above frames every temp as a leak to be swept away. That is wrong
for half of them**, and the distinction matters more than the sweep does:

- **The file we hand Blender** — keep it when Blender is killed. It is a valid
  mesh in durable form; producing it cost a weld and a write. It is what a
  retry continues from and what a manual rescue opens. Deleting it on failure
  means re-reading and re-welding the source just to try again, and throws away
  the exact reproducer for whatever killed Blender.
- **The file Blender wrote** — this one *is* garbage on failure. Truncated or
  half-written, with no value, and dangerous precisely because something later
  could mistake it for a real result.

So `keep_temp_on_failure=False` had it backwards: keeping the input should be
what normally happens on failure, not an opt-in.

**This also undermines the startup sweep as described.** A blind sweep would
delete exactly the file that was deliberately kept. If a kept input is meant to
be resumed from, it is not scratch at all — it wants a known, predictable path
that a retry can find, which is a different thing from a temp directory nobody
outside the function knows about.

**Deferred to `repairer`**, because after D19 removed the Blender decimation
rung the only Blender left is `convert.blender`, and conversion has the same
shape: an input worth keeping, an output worthless if it failed. The question
is better settled once `repairer` shows what resuming actually needs.

### Decided — drop the Blender decimation rung; mark the failure instead

**Decided 2026-09-15.** The decimation ladder loses its Blender rung. Two rungs
remain — fast_simplification, then PyMeshLab — and a mesh that defeats both is
written out with a marker rather than handed to a third decimator.

**What the evidence actually says.** The collection run shows **88 of 88
decimations taking fast_simplification**, zero falling through. That is 88
meshes in one run, not a proof that failure is impossible, and it was nearly
over-read here as "fast_simplification always works" — the same shape as two
other claims made tonight without measurement. To be rechecked against the logs
before the change lands.

**What actually settles it is not that number.** There is a manual fallback:
Bambu Studio's own simplify. So a mesh that defeats both Python decimators is
not a lost model — it is a file that gets marked and handled by hand with a
tool already in use. That reduces the Blender rung from insurance against
losing a model to saving one manual step on a case that has not yet occurred,
against the cost of a script, a subprocess, temp files and a format boundary.

**PyMeshLab stays.** It is array-native and in-process, so it costs nothing to
keep and makes a fast_simplification failure a non-event rather than manual
work. It is Blender specifically that is expensive.

**The nice consequence**: with Blender gone from decimation, *both* remaining
rungs work on arrays, so **decimation never touches the disk at all**.
`decimate.blender` can be deleted, and the PLY work scoped above becomes
`repairer`-only.

#### The marker: `.undecimated.stl`

A new result indicator beside the existing ones, and
`Indicator.UNDECIMATED` alongside them in `libs/indicators.py`.

**Why a marker rather than a log line.** Decimation is a deliverable. A mesh
that silently ships undecimated gets re-decimated by the printer, which
reintroduces the non-manifold edges this tool exists to remove — so "not
decimated" must be a state the *filesystem* records, not a line in a log nobody
reads. It is also trivially checkable: the indicator scan already tests for
sibling markers, so one more suffix costs a single `os.path.exists`, and an
existing marker means the file is not retried.

**It is a full copy of the source, like every other signal file** — never an
empty marker, never a hardlink. These are fallback prints. An undecimated mesh
is a particularly good one: it is a complete, printable model that simply was
not reduced, so it can go to the plate or through Bambu Studio's simplify as
it stands.

**What must survive from the current code**: `decimate()` returns
`Rung.FAILED` with the input mesh unchanged and every attempt recorded in
`attempts`. The caller turns that into the marker. The distinction between
"not decimated" and "decimated badly" is the whole reason the input is returned
untouched rather than a partial result.

### Idea — nm ratio as a debris signal (unmeasurable from current logs)

**Raised 2026-09-16. A hypothesis with a clear test, not yet testable.**

**The idea**: distinguish debris from small-but-real parts by the *proportion*
of defective edges, not by face count. A legitimate part is a closed solid —
few non-manifold or open edges relative to its size. A fragment torn off a
sculpt is mostly boundary: a scrap of surface rather than an object, so its
defect ratio is high whatever its face count.

**Why it is worth pursuing.** It addresses the failure the existing floor is
known to risk. The comment at `stl_batch_fix.py:~895` records the worry
directly: a floor set too high "would silently drop a magnet peg, a locating
pin or a small accessory, which are parts, not debris." Those are small *and*
topologically clean. A speck is small *and* malformed. Face count cannot tell
them apart; a defect ratio can.

**It composes with the bbox idea rather than competing with it.** Bounding box
answers *too small to print*; nm ratio answers *not a solid object*. A magnet
peg is small with clean topology, a speck is small with bad topology — two
signals for two different failure modes, and a part should probably have to
fail both before being dropped.

**Why it cannot be checked against the collection logs.** Asked for, and the
data does not exist. Three independent reasons:

1. **Dropping is silent.** Both sites discard without logging — a bare
   `continue` in `split_at_seams` (line 1316) and a list filter in
   `split_shells` (~line 902). Neither takes `L`, neither writes a step row. A
   dropped part leaves no trace: not its face count, not its extent, not a
   defect count.
2. **Per-part defects are never measured before the drop.** The floor is
   applied to a boolean *face mask* over the parent mesh. Nothing scans it at
   that point. Scanning happens later, on parts that survived and were written
   to disk — so a dropped part's nm count was never computed by anything.
3. **The surviving sample is one part.** `~foot1.part.1.stl` is the only
   per-part mesh in the step log, across 78 rows of the same repeating cycle.

**This is the second reason for the "record what is dropped" note above**,
which was written for the volume guard's benefit. Recording a drop is not only
about stopping the volume check misreading a deliberate discard — it is the
only way the drop rule itself can ever be evaluated. A rule that discards
without recording cannot be checked against outcomes, only trusted.

**The test, whenever the split stage is built**: scan each part *before*
applying any floor, and record for every part — kept or dropped — its face
count, bounding box, nm count and open-edge count. One run over the collection
then answers whether the defect ratio separates specks from small real parts,
and where the threshold sits. Until that data exists this stays an idea.

### Discussion point — measured part data, and what it says about the drop rule

**Recorded 2026-09-16 for a later conversation. Nothing decided.**

The nm-ratio idea above was recorded as untestable because dropping is silent
and the logs hold nothing. Three orphaned `~parts` folders survived interrupted
runs, so a small amount of real per-part data does exist after all. Scanned
with `libs/scanner`:

| part | faces | nm% | bbox mm | what happened |
|---|---|---|---|---|
| Falcon part.0 | 90,712 | 0.0% | 117 x 36 x 155 | committed |
| Falcon part.1 | 156 | 0.0% | 5.7 x 5.8 x 3.2 | committed |
| Falcon part.3 | 38 | 0.0% | 0.72 x 0.52 x 3.08 | committed |
| Falcon part.4 | 36 | 0.0% | 9.3 x 1.0 x 4.2 | committed |
| Falcon part.2 | 160 | **12.5%** | 7.3 x 1.0 x 6.5 | **broken** |
| costume part.1 (original) | 250 | **9.7%** | 1.5 x 0.8 x 1.0 | repaired to **1 face** |
| costume part.2 (original) | 136 | 1.2% | 4.8 x 1.8 x 0.9 | repaired to 16 faces |

nm% is against total edge slots (3 x faces).

**What the numbers support.** nm% predicts *repair failure* cleanly on this
sample: the three parts with meaningful nm (12.5%, 9.7%, 1.2%) are exactly the
three that broke or were annihilated, and every 0.0% part committed. That is a
real signal.

**What they do not support — and this is the point to revisit.** nm% is not a
*debris* test:

- **Falcon parts 3 and 4 are 36 and 38 faces with 0.0% nm.** Both are below the
  100-face floor and would be dropped as debris today, yet both are
  topologically perfect and both committed. The nm test would correctly defend
  them.
- **But part 3's bbox is 0.72 x 0.52 x 3.08 mm** — a 3 mm sliver half a
  millimetre thick. Locating pin, or splinter? Clean topology cannot say. It is
  clean either way.
- **costume part.1 is 250 faces — above the floor — at 9.7% nm in a 1.5 mm
  box.** Debris by any reading, and the face floor would have *kept* it.

So the face floor and the nm ratio disagree in **both** directions on this
sample, which is more informative than agreement would have been.

**The shape this suggests**, to be argued later rather than assumed now:

- **nm% ~ "will repair survive this part?"** — possibly better used to *route* a
  part (repair it differently, or not at all) than to discard it.
- **bounding box ~ "is this worth printing?"** — the actual debris question, and
  the one the face count was always standing in for.

Falcon part 3 is the case that decides the design: clean topology does not make
a 0.5 mm-thick sliver worth printing, and a face count does not know its size.

**Caveats, stated so the table is not over-read.** Seven distinct parts from two
models — this suggests, it does not settle. And the sample is biased: these
parts survived *because* their runs failed. Parts that merged cleanly were
deleted by `_cleanup_parts`, so the data is drawn from the trouble cases.

**Related, unrelated to the drop rule**: those three `~parts` folders are todo
M4 sitting on disk — pipeline intermediates left behind when a run is killed
mid-file, 89 MB in the belly-dancer folder alone. `_SIGNAL_SUFFIXES` already
names them as such. The startup-sweep idea recorded for Blender scratch would
cover these too.

### Discussion point — a flawless small shell may still be a print hazard

**Raised 2026-09-16. To revisit. Nothing decided, nothing measured.**

Two questions have been treated as one and are not:

1. **Is this shell meaningful?** — about the shell itself: topology, size.
2. **Can it be printed where it sits?** — about the *arrangement*: where this
   shell is relative to the others and to the plate.

**The case that separates them.** A pin, an eyeball, a magnet peg is *supposed*
to be its own shell, unattached, and that is correct geometry. If it is
flawless, it should be kept however small it is — the earlier measurement
supports that: Falcon parts 3 and 4 are 36 and 38 faces, 0.0% nm, and both
committed fine. Small and clean is not debris.

**But position decides whether keeping it is safe.** An eyeball nested inside a
head prints fine — the head builds around it. The same eyeball sitting in empty
space 5 mm away has nothing beneath it: it needs support, or it drops. A
dropped part is not a lost part. It becomes a blob the nozzle drags through,
stringing across the plate, and it can destroy the entire print — the whole
plate, not just that piece.

**Same geometry, same topology, opposite outcomes.** Nothing measured so far
distinguishes them. `scan` sees a clean mesh either way; a face floor sees a
small one either way; the nm ratio sees 0.0% either way. Every signal discussed
so far is a property of the shell in isolation, and this failure is a property
of the *relationship between shells*.

**A cheap detectable version**, to be argued later rather than assumed: once
parts are separate meshes, bounding-box containment and overlap are trivial to
compute. A small shell whose box is **inside or intersecting** another shell's
is a feature — nested detail, an inset eye, a peg in its socket. A small shell
sitting in **clear space**, touching nothing, is either debris or something
that will need support. That is a different classification from either of the
signals recorded above, and it uses data we will already have.

**Open, and not to be guessed at**: whether this tool should act on it at all.
It may be that the right output is a *warning* rather than a drop or a fix —
the slicer is where support is decided, and a repair tool silently deleting a
correctly-modelled eyeball would be worse than saying "three loose shells, none
supported, check before printing". Deleting geometry because it might need
support is exactly the class of over-reach the volume guard exists to catch.

### D18 — Self-edges are not winding seams (and what that says about oracles)

**Fixed 2026-09-16.** `seam_edges` drops edges whose two endpoints are the same
vertex. They are degenerate-face artifacts, and `scan().degenerate` is where
they belong.

**The bug.** A face with two identical corners emits an edge `(v, v)`. Two such
faces sharing it satisfy the "exactly two faces, same direction" seam test — a
self-edge has no direction to disagree about — and then `adjacency[v] = [v, v]`
has length 2, which satisfies the "every vertex has exactly two seam edges"
closed-loop test. **A single point was reported as a closed loop.**

That is not a cosmetic miscount. A closed seam loop is the pipeline's strongest
signal that a mesh must be split before PyMeshFix touches it, so zero-area
triangles were about to route clean models down the seam-split path.

**Found on real data.** A collection scan produced rows where the seam-edge
count exactly equalled the loop count — 115/115, 106/106, 77/77. A closed loop
needs at least three edges, so those could not be real. On
`Hanna and Chewie/Hair.stl`: 106 seam edges, **all 106 self-edges**, 106 phantom
loops, on a mesh with 250 degenerate faces and no winding seam at all. Of the
rows reporting any seam, roughly half showed this 1:1 signature.

#### The methodological point, which matters more than the bug

**The original `find_winding_seams` has the same flaw**, so the 400-mesh
differential test could never have caught it. Both sides agreed, and both were
wrong.

That is a limit of oracle-based testing worth stating plainly, because D17
introduced the technique and recommended it: **a differential test proves
agreement, not correctness.** It is the right tool for "did my rewrite change
behaviour" and no tool at all for "was the behaviour right". When the oracle is
a working implementation, inherited bugs are exactly the class it cannot see.

Both oracle methods in `test_scanner.py` are now **deliberately corrected** to
filter self-edges, with a comment saying so, and `TestSelfEdges` pins the
behaviour directly rather than by comparison. Correcting the oracle rather than
weakening the assertion keeps the differential test meaningful for everything
else.

#### A second lesson, from the fix's own test

The first version of `test_a_real_seam_is_still_found_alongside_degenerate_faces`
expected 3 seam edges and got 2 — and **the code was right, the fixture was
wrong**. The degenerate faces had been placed on vertex 1, which the reversed
face also touches; `[1, 1, 2]` emits the ordinary edge `(1, 2)` as well as the
self-edge, pushing that edge to four users and legitimately out of the
"exactly two faces" test. The mesh genuinely had two seam edges.

Worth recording because the instinct on a red test is to suspect the code. Here
the test was asserting something untrue about its own fixture.

### Measured — seam frequency across the collection (2026-09-16)

575 meshes scanned in 18 minutes (11 skipped as over 2.5M triangles, a test
bound only — the real pipeline decimates first, so it never sees a mesh above
`MAX_FACES`).

| | count | share |
|---|---|---|
| no seam edges at all | 513 | 89% |
| genuine seam edges | ~29 | 5% |
| phantom (self-edge bug, D18) | ~33 | 6% |
| **genuine, with a closed loop** | **~10** | **under 2%** |

**This answers the question left open above** — whether closed loops affect 6
files or 200, which was recorded as the thing that would decide whether the
splitting stage needs a different design. It is about 10 of 575. Splitting
stays a narrow path, and upfront detection is cheap insurance rather than a
major cost centre.

The genuine hits, ranked:

| edges / loops | mesh |
|---|---|
| 922 / 129 | `Amidara_Blustmorn_1-12_base.stl` — an order of magnitude beyond anything else |
| 117 / 18 | `Amidara_Blustmorn_1-12_hands_2.stl` |
| 83 / 27 | `Princess_Leia .../Neck_Cuff.stl` — a cuff over a neck, the hair-over-scalp shape exactly |
| 15 / 1 | `belly-dancer/3rd-01.stl` |
| 7 / 1 | `Mandy_Body_Dinamuuu3D.stl` |
| 3 / 2 | `Skeletor - STL/Base.stl` |

**Caveat, stated so the numbers are not over-quoted**: this scan ran *pre-fix*
code. The `edges == loops` rows are provably phantom (a loop needs at least
three edges), but a genuine row may also carry some self-edges inflating its
count — Amidara's 922 could be 900 after filtering. The ranking almost
certainly holds; the exact figures do not. A re-run settles it and costs 18
minutes.

Incidentally: the whole `Boris` tree — 185 assembly parts, cut from larger
assemblies — returned **zero** seams. The cutting was done cleanly.

### Pending verification — two models the user will restore

`Mandy_Body_Dinamuuu3D.stl` and `whole-costume01.stl` are to be restored from
source and used as known-behaviour test cases, rather than trusting numbers
from meshes nobody has inspected. Expectations to check against, recorded now
so they are not reconstructed from memory:

**Mandy** — 39 real shells, smallest 750 faces after decimation (the
measurement `_MIN_SHELL_FACES = 100` was calibrated against). On seams the doc
records **5 edges / 0 loops straight from decimation, 40 edges / 7 loops after
Blender**; this scan found **7 edges / 1 loop undecimated**. Three states,
three answers.

**The question that matters**, and it may undercut a decision already taken:
the split-upfront design assumes detection fires *before* repair. If Mandy's
closed loops only appear once Blender has rebuilt the surface — which is what
the doc's 0-loops-after-decimation figure suggests — then upfront seam
detection will not catch the very case the seam split exists for, and the
volume-loss backstop is doing the real work. Worth settling on the restored
file before `repairer` is built around the assumption.

**whole-costume01** — 444 shells, 443 of them under 100 faces. From the
surviving `~parts` debris: 892,445 faces after decimation, 2,055 nm, 9 open,
and two split parts that repair reduced to 1 and 16 faces.

### CORRECTION — the split-upfront decision was taken against existing evidence

**Recorded 2026-09-16. The decision above ("the split moves from recovery to
planning") is re-opened, not settled.**

I argued for it and recorded it without reading the design doc passage that
already covers the question. The old code tried exactly that design, measured
it, and disabled it. The branch is still in the source behind
`if False and _seam_loops > 0`, kept for the measurements in its comments.

**Why it was disabled** (`STL_BATCH_FIX_DESIGN.md`, "The pre-emptive seam split
is disabled — read this before re-enabling it"):

> On a sphere with its cap reversed — 40 seam edges in 1 closed loop —
> PyMeshFix re-winds it correctly and returns the same 760 faces, where
> splitting first gives 880 faces and introduces 2 non-manifold edges. The
> Mandy mesh that *needed* the split had **exactly 40 seam edges too**. Nothing
> measurable before step E distinguishes "PyMeshFix will fix this" from
> "PyMeshFix will delete this".

Two meshes, identical seam counts, opposite correct actions. That is the whole
argument, and it is an argument from measurement rather than from reasoning.

**So closed-loop detection cannot be the trigger for splitting.** It can say
"this mesh has irreconcilable winding somewhere"; it cannot say "PyMeshFix will
destroy this". The volume check after the fact can, because by then the damage
is a measured fact rather than a prediction.

**A second error, in how the Mandy numbers were presented.** Three readings
were tabulated as though they were the same mesh at different times:

| source | mesh state | reading |
|---|---|---|
| design doc | after decimation | 5 edges / 0 loops |
| design doc | after decimation **and Blender repair** | 40 edges / 7 loops |
| our scan | **raw 2,061,994-triangle source** | 7 edges / 1 loop |

They are three different meshes. The doc's line is explicit: *"straight from
decimation the Mandy mesh has 5 seam edges in 0 closed loops and cannot be
separated; after Blender's repair it has 40 in 7 loops and splits cleanly."*
Blender rebuilding the surface is what creates the explicit boundary —
established in `2c524d8`, and the doc says it, not `MAX_FACES`, is what fixed
that model.

**What still stands from the earlier discussion**: detection upfront is cheap
(under 2% of meshes carry closed loops) and knowing a file's shape before
queuing it has value for ordering. What does not stand is using that detection
to *trigger* a split.

**To settle on the restored model, with fresh tools rather than from logs**:
does a closed loop exist in the raw mesh that our detector sees but the old
byte-level one missed, or does the boundary genuinely only appear after
Blender? The preserved logs cannot answer it — both runs record Mandy as
`skip`, so neither processed it.

**The process failure worth keeping**: this is the second time in two days that
a confident argument ran ahead of reading what was already recorded — after
`shells()` being called "nearly free" without measuring. Both times the
correction came from the user asking a question I could not answer. The
standing rule from that: a claim about cost or behaviour ships with a number or
an admission that there is none. Extend it — **a decision that reverses
existing behaviour ships with the reason that behaviour exists.**

### Verified on the restored Mandy — the detector is correct, and the seam is not the point

**Measured 2026-09-16** on the re-downloaded
`Mandy Dinamuuu3D/Complete model (Thingiverse version)/Mandy_Body_Dinamuuu3D.stl`,
with `libs/scanner` and `libs/decimator` rather than from logs.

**The new tools reproduce the recorded measurements exactly.** After decimation
to 900,000 faces via fast_simplification:

| | measured now | design doc |
|---|---|---|
| seam edges | 5 | 5 |
| closed loops | 0 | 0 |
| shells | 39 | 39 |
| smallest shell | 750 faces | 750 |

Every figure agrees, the smallest shell to the face. So the rewritten scanner
matches the old byte-level implementation on the mesh that motivated the whole
seam-split mechanism, and the D18 self-edge fix did not disturb a genuine
reading — Mandy carries only 10 degenerate faces, so it was never contaminated.

**Answering the question directly: the detection mechanism is not wrong.**

| mesh state | seam edges | closed loops |
|---|---|---|
| raw source (2,061,994 faces) | 7 | **1** |
| after decimation to 900k | 5 | **0** |

**Decimation destroys the loop.** Quadric edge collapse removes two of the
seven seam edges and the ring opens. The signal genuinely exists in the raw
mesh and is genuinely gone by the time the pipeline could act on it — so the
user's intuition that "the model should have a seam, otherwise it would not
lose the hair so cleanly" was right about the model, and the pipeline simply
never sees it. Under D13 decimation must come first (the face budget is spent
once, before anything is divided), so this is not a reordering that can be
undone.

Full raw-source figures, for reference: 266 non-manifold, 84 open, 10
degenerate, 39 shells, bbox 53.7 x 38.0 x 86.4 mm.

#### The finding that matters more: the head is already its own shell

Shell sizes after decimation begin: **562,288 · 121,537 · 63,073 · 60,932 ·
14,324 …**

The design doc records PyMeshFix returning *"one shell of 394,432 faces with
the head — a 121,537-face component — deleted"*. That second shell **is** the
head, and the measurement names it exactly.

**So the head was never fused to the body.** A plain connected-component split
— no seam logic whatsoever — separates it before PyMeshFix can see the whole
mesh. The doc's own conclusion agrees: what fixed that model was splitting
*after* decimation, not seam detection.

**This sharpens the re-opened split-upfront decision.** Seam detection is not
what saves Mandy; **shell splitting after decimation** is, and that is already
settled as D13. Seam detection earns its keep only where two regions are
genuinely fused into a single shell — hair over a scalp, a cuff over a neck.
On this collection that is `Amidara_Blustmorn_1-12_base.stl` (922 edges / 129
loops) and `Princess_Leia .../Neck_Cuff.stl` (83 / 27), not Mandy.

**Still open**: whether even those two need the seam path, or whether a shell
split handles them as well. Worth checking before `repairer` is built around a
mechanism that may have no remaining use case.


### Correction — `Done/` is not a pipeline output tree

An earlier entry above described Amidara as "already in `Done/`, meaning it
already went through the pipeline". **That was an inference from a folder name,
stated as fact, and it is wrong.**

`Done/` holds finished *source* archives — `Mandy Pinup Figurine.zip` (881 MB),
`mowmaw.zip` (1.8 GB) — models the user has finished printing. Pipeline output
goes to `Fixed/`.

The Amidara files are unprocessed sources: all 11 share one `Jun 8 13:19`
timestamp (an extract, not a run, which writes staggered times), no signal file
of any kind sits beside them, and no log — `repair_steps.tsv`,
`repair_summary.tsv`, or either preserved run — mentions Amidara at all.

So the Amidara measurements stand, on a clean source mesh. The claim about its
provenance did not.

Third instance in two days of a confident claim built from pattern-matching
rather than looking, after `shells()` "nearly free" and the split-upfront
reversal. Each was caught by the user asking a question rather than by any
check of mine.


### D19 — Done: the Blender decimation rung is removed

**Implemented 2026-09-16**, completing the decision recorded above. The log
recheck it was conditional on has been done: **104 of 104 decimate rows in the
step log took fast_simplification**, no failures, no fallbacks; the preserved
collection run shows the same on the 4 files it decimated. Consistent — but it
is still only the meshes fast_simplification happened to handle, which is why
that record is not the justification. The justification is the manual fallback:
Bambu Studio's own simplify.

**What went:**

- `Rung.BLENDER`, `_decimate_blender`, `_decimate_script`, `_SCRIPT_CACHE`,
  `_remove_tree`
- the `timeout`, `blender_executable` and `keep_temp_on_failure` parameters —
  `decimate(mesh, max_faces)` is now the whole signature
- the `blender` import, and `os`/`tempfile` with it
- `libs/blender_fx/decimate.blender`, deleted
- `TestBlenderRung` in the suite (22 tests → 19)

**What arrived:** `Indicator.UNDECIMATED` and the `.undecimated.stl` marker, a
full copy of the source like every other signal file — and a particularly good
fallback print, being a complete model that simply was not reduced.

**The consequence worth naming: decimation no longer touches the disk.** Both
remaining rungs are array-native, so the module has no file boundary at all.
Two tests assert this directly.

#### Two earlier entries this invalidates

Both were written assuming the Blender decimation rung exists, and both are now
annotated in place rather than deleted:

**The PLY deferral** rested on `decimate.blender`'s hand-packed
`struct.pack_into` loop — *"~100 lines of untestable in-Blender serialisation"*
that `wm.ply_export` would replace with one C call. That file is now deleted
outright, so those lines are gone rather than improved, and the stated
justification no longer exists. What remains is `convert.blender`, which
already uses Blender's own C exporter and needs no hand-packing — a much weaker
case. Reconsidered when `repairer` lands.

**The scratch-sweep entry** describes `_decimate_blender`'s `tempfile.mkdtemp`
and `finally`, which no longer exist. The concern survives and moves to
`convert.blender` and `repairer`.

#### And a correction to that scratch entry, from the user

It framed every temp file as a leak to be swept away. **That is wrong for half
of them.** The file handed *to* Blender is not garbage: it is a valid mesh in
durable form that cost a weld and a write to produce, it is what a retry
continues from, and it is the reproducer for whatever killed Blender. Deleting
it on failure means re-reading and re-welding the source just to try again.
Only Blender's *output* is worthless on failure — truncated, and dangerous
because something later could mistake it for a result.

So `keep_temp_on_failure=False` had the default backwards, and a blind startup
sweep would delete exactly the file that was deliberately kept. A kept input is
not scratch at all; it wants a known path a retry can find. Deferred to
`repairer`, which is where resuming will show what it actually needs.

### D20 — `shells()` uses scipy; a pure-numpy replacement was tried and rejected

**Decided and implemented 2026-09-16.** `scanner.shells()` delegates to
`scipy.sparse.csgraph.connected_components`, and scipy joins pymeshlab and
pymeshfix as a hard requirement in `install.sh`.

**The problem.** The original was a per-face Python `find()` loop — correct,
and measured at 5-10x the cost of *loading* the mesh. Recorded earlier as my
bug, after I had called shell counting "nearly free" without measuring it.

**The measurements**, on `Mandy_Body_Dinamuuu3D.stl`:

| mesh | union-find | scipy | speedup |
|---|---|---|---|
| raw, 2,061,994 faces | 12.75s | **0.42s** | 30x |
| decimated, 900,000 faces | 5.72s | **0.14s** | 41x |

Identical grouping in both cases, not merely identical counts.

**Why it is worth a dependency.** At 0.14s on the mesh the pipeline actually
sees — decimation runs first under D13 — shell counting costs less than
`scan()` itself (2.55s on the same mesh). It stops being something to ration
and becomes something a caller can ask on every file without thinking. scipy is
BSD-3, ~109 MB in a venv already at 1.3 GB, and not exotic in a project that
already carries numpy, pymeshlab and pymeshfix.

#### The failed attempt, recorded so it is not retried

**Pointer-jumping label propagation** was written first, to avoid the
dependency: give every vertex its own label, push the smaller label across
every face edge with `np.minimum.at`, then collapse label chains by indexing
the array into itself twice. Fully vectorised, no scipy.

It was **15-24x faster than union-find on synthetic meshes and 2x slower on a
real one**:

| mesh | union-find | pointer-jumping |
|---|---|---|
| synthetic strip, 1M faces | 4.43s | **0.29s** |
| Mandy raw, 2.06M faces | 12.71s | **25.9s** |

**The cause**, from instrumenting it: convergence is O(log diameter) passes
*only when label chains actually collapse*. Mandy's largest shell is 1.3M faces
of tangled organic geometry and took **194 passes**, each a fixed 0.13s over
6.2M edge entries. `np.minimum.at` is an unbuffered ufunc, so the per-pass cost
does not fall as the labels settle.

**The real failure was the benchmark, not the algorithm.** The fixture was a
long thin strip — which *sounds* like a diameter worst case and was described
as one in the code comments I wrote — but it has three components and converges
in 11 passes. I chose a fixture that flattered the approach, then recorded its
flattering number as an established fact.

It came within one step of being committed: the differential test passed 500 of
500 random meshes, the suite was green, and the synthetic numbers were
excellent. **Only running it against a real model caught it** — the same check
that caught the self-edge bug the day before.

**The rule this supports**, extending the earlier ones: a performance claim
needs a fixture whose *structure* resembles real input, not one chosen for how
bad it sounds. Correctness and fitness are different questions, and a
differential test only answers the first.

**Test coverage**: `TestShellsAgainstUnionFind` keeps the union-find as an
oracle in the suite — 500 random meshes, the largest-first ordering contract,
and the awkward cases where implementations diverge (degenerate faces, repeated
faces, disjoint components, unused high indices). Unlike the `winding_seams`
oracle it carries no inherited flaw, so agreement with it is meaningful.

### D21 — Intermediates live in the destination folder and die when the next step commits

**Decided 2026-09-16.** This replaces most of the "scratch files must not
outlive an interrupted run" entry above, which was built on a premise that
turned out to be wrong.

**The rule, whole:**

1. Every intermediate is written **into the destination folder**, beside the
   output it will become — not into `tempfile.mkdtemp`, not into `/tmp`.
2. **Both files are deleted when the next step succeeds *or* is not needed.**
   Not "on success": a step that was skipped leaves the input equally useless.
3. Anything left behind is therefore a record that the run stopped there.

**What this dissolves rather than answers.** The earlier entry left two open
sub-questions — where a deliberately-kept input should live so a retry can find
it, and how a startup sweep avoids deleting scratch belonging to a concurrent
instance. Neither survives the rule:

- A kept input is at a predictable path derived from the output path, so a
  retry finds it by construction rather than by searching.
- There is no separate scratch root, so there is nothing to sweep and no way
  for one instance to delete another's files. Cleanup is done by whichever
  step moves the pipeline forward, not by a janitor.

**The old design had this right and my rewrite deviated from it.**
`dst + '.decimate.stl'` wrote into the output tree, and `_SIGNAL_SUFFIXES`
already names `.decimate.stl`, `.repairnm.stl`, `.pymeshfix.stl`, `.merge.stl`
and `.partial` as pipeline intermediates. I moved temps to `mkdtemp` reasoning
that scratch should not pollute the output tree — without noticing that the
output tree is exactly where this state belongs, because it is the only place
the next run will look.

**A leftover intermediate is diagnostic, not litter.** `Body.decimate.stl` in
the output tree says decimation produced it and the next step never committed —
a crash, a kill, a power loss. It is simultaneously the last known good state
and a record of where the run stopped, and it makes the retry cheap: continue
from it instead of re-reading and re-welding the source.

**The constraint this carries**, and it is the reason the old suffix list
exists: anything written into the destination folder **must** be recognised as
not-an-input, or the next walk picks it up as a mesh to process.
`libs/indicators.py` already has the mechanism for marker suffixes; the
intermediate suffixes need adding alongside them.

**PLY is nothing special here.** It is another intermediate with another
extension, covered by whatever rule covers `.decimate.stl`. The earlier note
treating the Blender boundary's temps as a distinct problem was over-thinking
it.

**One consequence to accept knowingly**: a kept intermediate sits in the output
tree rather than `/tmp`, so it does not vanish on reboot. That is the point —
it is visible to the next run — but it does mean cleanup is deliberate rather
than something the OS eventually does.

### D22 — Shells are edge-connected, not vertex-connected

**Fixed 2026-09-16.** `scanner.shells()` joins two faces only when they share
an **edge** (two vertices). Surfaces meeting at a single point are separate
shells. The previous behaviour was documented, tested, and wrong for the job.

**Why it matters.** A vertex where two surfaces touch is non-manifold by
construction, and PyMeshFix rebuilds *one* manifold surface and discards the
rest. Handing it a vertex-joined pair as a single component gives it exactly
the input that makes it delete geometry — the failure the split exists to
prevent.

**The evidence**, on `Mandy_Body_Dinamuuu3D.stl` raw:

| criterion | components | largest |
|---|---|---|
| vertex-connected (was) | 39 | 1,315,986 |
| **edge-connected (now)** | **40** | 941,571 + 374,415 |
| PyMeshLab `generate_splitting_by_connected_components` | 40 | 941,571 + 374,415 |

The edge-connected answer matches PyMeshLab component for component and face
for face. Mandy's largest shell is two surfaces touching at one vertex.

**A detail worth not glossing**: after decimation the count is **39** again —
quadric collapse removes the single-vertex join. So the old behaviour was
accidentally right for the mesh the pipeline sees and wrong for the raw one.
That is a coincidence, not a defence.

**The cost of being correct**: edge connectivity needs a lexsort over
`3 * faces` rows to find which faces share an edge.

| | union-find | scipy vertex | scipy edge |
|---|---|---|---|
| raw | 12.75s | 0.42s | 2.52s |
| decimated | 5.72s | 0.14s | 0.92s |

Still 5x faster than the loop it replaced, and still cheaper than `scan()`
(2.58s) on the mesh that matters.

#### The third time a green suite agreed with itself

`test_faces_sharing_only_a_vertex_are_one_shell` **asserted the wrong
behaviour**, and the union-find oracle in `TestShellsAgainstUnionFind` unioned
vertices too — so 500 random meshes agreed, and all three of implementation,
test and oracle were wrong together.

This is now the third instance in this refactor:

1. `winding_seams` — self-edges counted as closed loops; the original had the
   same flaw, so the differential test could not see it.
2. `shells` pointer-jumping — 500/500 differential trials passed on an
   implementation that was 2x slower than what it replaced on real geometry.
3. `shells` vertex connectivity — implementation, test and oracle all agreed.

Every one was caught by running against a real model, never by the suite. The
oracle is now edge-connected, and PyMeshLab stands as the independent check.

**The rule**: an oracle derived from the same assumption as the implementation
tests consistency, not correctness. Where an independent implementation exists
— PyMeshLab here — check against that, on real geometry, at least once.

### Decided — scan the merged mesh as the final step

**Raised by the user 2026-09-16.** After parts are repaired and merged, run
`scanner.scan()` on the result before writing it out.

This is not re-checking work already checked. Repairing parts independently and
reassembling can introduce defects **no individual part had**: two parts
sharing a boundary can be re-wound differently from each other, and the merge
itself can leave the seam between them open. The merged mesh is geometry that
nothing has yet inspected.

It extends the principle `_post_verify` already encodes — *"PyMeshFix (and
Blender) self-report unreliably, so nothing is written out as 'ok' on a
library's word alone"* — to the one step that currently has no verification at
all.

### D23 — `Mesh` carries its destination, and a split gives each part its own

**Decided and implemented 2026-09-16**, at the user's prompting: *"the
indicator is built on fix name matching so destination should be defined a
single way."*

**`destination` is a required field on `Mesh`**, second after `path`. `path` is
where the mesh came from; `destination` is where its repaired result belongs.

**Why required rather than optional.** Every marker the indicator scan looks
for is derived from the destination:

```python
base, _ = os.path.splitext(output_file)
for suffix, indicator in _OUTPUT_MARKERS:
    if os.path.exists(base + suffix): ...
```

Two derivations would mean two spellings. A file written as `Body.stl` while
the scan tested `body.stl.failed.stl` would silently reprocess on every run,
and nothing would report it. One field, one source of truth — the same argument
as one-writer in `mesh_io`.

I had leaned optional, reasoning that probing happens before a destination is
known. That was wrong on inspection: `converter._output_for()` computes the
destination on line 126 and probes on line 149. There is no window where a
`Mesh` exists without a knowable destination.

**`write(mesh)` loses its path argument.** The mesh carries where it goes, so a
caller cannot write it somewhere the indicator scan will not look.

**`with_geometry` versus `with_destination`.** These are the two shapes a step
can have, and conflating them was the bug underneath the question:

- `with_geometry` — a step that *transforms* a mesh: same file, changed
  geometry, destination carried across. Decimation, repair.
- `with_destination` — a step that *divides* one: one input, several outputs,
  each needing its own identity. Only the splitter.

Before this, `_extract` used `with_geometry`, so every part inherited the
parent's path verbatim. Two parts of one model were **indistinguishable by
every field on the object** — same path, same destination — which broke
writing, logging, and the D21 resume-from-intermediate rule at once.

**Naming is injected, not derived here.** `splitter` takes a
`name(mesh, index, total)` function, defaulting to `<base>.part.<N>.stl` beside
the parent's output. Output layout is the caller's policy, exactly as
`converter` takes `convert` rather than importing `blender`.

**`merge` takes the parent's destination explicitly.** Inheriting `parts[0]`'s
would write the whole model to `<base>.part.0.stl` and hang the parent's
markers off a part's path. That was a real bug in the first draft, caught on
re-reading rather than by a test.

**A consequence worth noting**: a part with its own destination gets its own
`.failed.stl` for free, so the old `~<name>` pending-rename protocol becomes
unnecessary — the destination's existence *is* the commit.

### Measured — stored STL normals DO disagree with winding, on 62 files

**Scanned 2026-09-16**, all 665 readable binary STLs in `Fixing`, `Boris` and
`Done`, comparing each triangle's stored facet normal against one computed from
its winding.

| | |
|---|---|
| files scanned | 665 |
| files with at least one disagreement | **62** (9.3%) |
| total disagreeing faces | **1,107,820** |

Worst offenders, all from one set:

| faces | share | file |
|---|---|---|
| 118,760 | **22.6%** | `Fae/Aine Noon Fae/.../platform_supported.stl` |
| 271,455 | 14.2% | `Fae/Aine Noon Fae/.../UMesh_mushrooms.stl` |
| 19,895 | 12.2% | `.../upperPart_supported.stl` |
| 242,835 | 10.5% | `.../mushrooms1.stl` |

The Daki set disagrees consistently too, at 0.1-0.44% per file.

**Why this was measured.** `normal_vote` in `stl_batch_fix.blender` — which the
script review calls *"the cleverest thing in the file"* and says should survive
the refactor intact — uses the STL's **stored per-facet normals** as ground
truth for winding: it flood-fills per connected component and flips a component
when more of its faces disagree than agree. That only does anything when the
stored normals carry information the winding does not.

**A correction to record.** I checked three large files (Mandy body, Amidara
base, Mandy clothed head), found 100% stored normals and **zero** disagreement,
and concluded the signal "carries no independent information" and that
`normal_vote` could be dropped. That was wrong. Three files chosen for size are
not a sample; the clean ones are simply the common case. Fourth instance today
of generalising from an unrepresentative sample — after `shells()` "nearly
free", the pointer-jumping benchmark, and vertex-vs-edge connectivity.

#### What it does to the PLY-at-the-Blender-boundary plan

`wm.ply_export`'s normals are **per-vertex** — the RNA description is *"Export
specific vertex normals if available, export calculated normals otherwise"* —
so PLY cannot carry a per-face stored normal. Routing repair through PLY would
silently discard the winding evidence on 62 files, and `normal_vote` would
degrade to comparing winding against itself: it can then only ever report
`agree: N, disagree: 0` and flip nothing, while still printing as though it
ran.

So the boundary question splits, and the two halves are no longer one decision:

- **Decimation's boundary** — already gone with D19; no normals involved.
- **Repair's boundary** — three options, none obviously right:
  1. the script keeps parsing STL itself, retaining review problems #1-#3
     (buffer sized before filling, triangles assumed, header trusted);
  2. the normals travel separately alongside the PLY;
  3. **`normal_vote` moves to our side entirely** — it is flood-fill over
     connected components plus a per-component majority vote, and
     `scanner.shells()` already does the flood-fill. `mesh_io.load` would keep
     the stored normals beside the geometry, and the vote becomes an array
     function with no Blender involvement, testable like everything else.

Option 3 is the larger change and the only one that makes the boundary clean
rather than working around it. Not yet decided.

Raw scan output kept at
`scratchpad/keep_normalscan.tsv` (tris, stored%, disagreeing faces, %, path).

### Survey — most geometric repair runs on 0.8% of files

**Asked 2026-09-16**: *"who checks for inverted normals when Blender is not
involved?"*, then the same question for T-junctions and degenerate faces.
Answered by grep, not by memory.

| capability | Python side | in the Blender script | actually runs on |
|---|---|---|---|
| inverted normals (`normal_vote`) | **nothing** | yes | 6 files of 768 |
| T-junction split | `MERGE_DIST` constant only | 6 references | 6 of 768 |
| wire edges / fin vertices | **nothing** | 8 references | 6 of 768 |
| merge doubles | constant only | 5 references | 6 of 768 |
| degenerate faces | `scanner` **counts** them | 3 references | counted always, fixed on 6 |
| hole filling | `meshfix.repair()` | 5 references | wherever repair runs |

`grep -rn 'normal_vote\|inverted\|flip' --include='*.py'` returns **nothing**.

**Two of these are worse than absent.** `scanner.scan()` reports a `degenerate`
count on every file and nothing acts on it. `MERGE_DIST` exists in the config,
in the TUI, and is passed into the Blender script — no Python code uses it.
Both look like working features from the outside.

**`find_winding_seams` is not a substitute for `normal_vote`.** It finds edges
where two faces disagree *with each other*. A region that is internally
consistent and uniformly backwards has **zero** seam edges, because no two
neighbours disagree. The stored facet normals are the only evidence for that
case, and nothing on the Python side reads them.

**And the clean-copy shortcut makes it concrete:**

```python
if (nm_src == 0 and open_src == 0 and not _seam_loops
        and (MAX_FACES == 0 or n_tris <= MAX_FACES)):
    shutil.copy2(src, dst)
    L("result: ok (clean copy — no repair needed)")
```

A file with inverted normals, T-junctions, wire edges or degenerate faces — but
no holes and no non-manifold edges — is copied through, reported `ok`, and
never opened by Blender. The design doc names the symptom already: *"a region
renders black in viewers and in Bambu while every defect count reads zero."*

#### What this does to the repair design

The question was framed as "how does `normal_vote` survive the PLY boundary".
That framing was too small. The real finding is that **a whole class of
geometric repair is gated behind a door that opens for 0.8% of files**, and the
boundary question is downstream of deciding whether that should change.

Every one of these operates on arrays and would be testable on our side:
`scanner.shells()` already does the flood-fill `normal_vote` needs, degenerate
faces are already detected, and T-junction splitting and doubles-merging are
vertex-distance work of the kind `mesh_io.load` already does.

But that converts "port a Blender script" into "reimplement six BMesh
operations in numpy", which is a different size of commitment and a different
shape of project. **Not decided.** Recorded so the choice is made deliberately
rather than by porting momentum.

### Measured — an inverted mesh is invisible to every check, and PyMeshFix ignores it

**Tested 2026-09-16** on a synthetic fixture, after a real-file attempt proved
unreadable.

**The real file was the wrong subject, and the user said so first.**
`platform_supported.stl` was picked for having the highest disagreement in the
scan (22.6%) — but the name says it: a **resin model with printed supports**.
Its 4,526 shells are support pillars, and the normals at their tips are
unreliable by construction. PyMeshFix returned 266,918 of 525,254 faces and 1
shell, having eaten 4,525 pillars. Nothing about that is attributable to
inversion: too many defects at once, none of them isolated.

**The synthetic answer is unambiguous.** A closed sphere, and the same sphere
with every face reversed:

| | faces | nm | open | degenerate | seams | shells | signed volume |
|---|---|---|---|---|---|---|---|
| correct | 760 | 0 | 0 | 0 | 0/0 | 1 | **+4094.9** |
| inverted | 760 | 0 | 0 | 0 | 0/0 | 1 | **−4094.9** |

Identical on every check the pipeline has. **Signed volume is the only
discriminator**, and it is already computed for the volume-loss guard.

**PyMeshFix is a clean no-op**: 760 faces in, 760 out, volume unchanged at
−4094.9, `ok=True`. It neither detects nor fixes inversion. So the capability
cannot be delegated to it, which was worth establishing before designing around
either answer.

**`find_winding_seams` cannot see this, by construction.** It reports edges
where two faces disagree *with each other*; a uniformly reversed mesh has no
such edge. Zero seams, and correctly so.

#### The stored-normal signal is mostly junk

Re-examining `platform_supported.stl` with a threshold rather than a sign:

| dot(stored, winding) | faces | reading |
|---|---|---|
| > 0.9 | 280,738 | agrees |
| −0.1 … 0.1 | 26,093 | perpendicular — junk |
| −0.9 … −0.1 | 99,769 | skewed — junk |
| < −0.9 | **11,850** | genuinely opposite |

All normals are unit length, so none is zeroed — but 125,862 point at angles
that are neither with nor against the winding, and **zero shells are entirely
opposite**. The earlier "1,107,820 disagreeing faces across 62 files" counted
any negative dot product and so conflated real inversion with exporter noise.
That number should not be quoted; the scan wants re-running at `< -0.9`.

This weakens the case for porting `normal_vote`: on this file it would be
voting on noise, with no component actually needing a flip.

#### Two corrections

**`libs/meshfix.py` overstated its guarantee.** Its docstring implied that
omitting `remove_smallest_components` prevents shell deletion. It does not —
`clean()` alone discarded 4,525 shells on the file above. The two-tetrahedra
test that "verified" it is too small to show the behaviour. Docstring fixed;
splitting first is what prevents the loss.

**A new fixture**, `tests/fixtures/inverted.stl`, generated by
`make_fixtures.py`: a closed sphere with every face reversed. `check()` now
reports signed volume, because without it the inverted fixture is
indistinguishable from a correct one in that table.

### Settled — inverted normals are a NON-DEFECT; do not build detection for them

**Tested end to end 2026-09-16, and the answer retires the question.**

A closed sphere with **every** face reversed was put through the whole chain:

| stage | result |
|---|---|
| `scanner` (nm, open, degenerate, seams, shells) | all zero — identical to a correct sphere |
| `meshfix.repair()` | clean no-op: 760 faces in, 760 out, volume unchanged, `ok=True` |
| commercial online repair service | **"0 Inverted normals"** — its dedicated check reports nothing |
| Bambu Studio — render | renders as a normal opaque sphere |
| Bambu Studio — **slice** | **slices fine** (confirmed) |

Only signed volume distinguishes it: **+4094.9 against −4094.9**.

**Confirmed on all five stages**, the slice last and separately. (A note on
process: the table briefly claimed "slices fine" before it had been tested — I
read the user's confirmation that all four *rendered* as covering slicing too.
It was corrected while the test ran, and then the test agreed with it. Being
right by luck is not being right, and the correction was worth making.)

**Nothing downstream treats this as a defect, so it is not one.** A slicer
reconstructs orientation from geometry rather than trusting the file's winding,
which is the sane thing for it to do since it has to produce a solid either
way.

**Consequence: `normal_vote` is not ported, and not rebuilt on our side.** The
previous entry was heading toward reimplementing its flood-fill-and-vote in
numpy. That would have been machinery for a condition with no consequence
anywhere in the chain.

**The commercial tool has the same blind spot we do**, which is itself worth
recording: it offers an "Inverted normals" check and reports **0** on a mesh
that is entirely inside-out, while reporting **40** on `fixture_seam.stl`. So
its check means what `find_winding_seams` means — faces disagreeing *with each
other*. A uniformly reversed mesh is locally perfect everywhere and scores
clean. We are not missing a standard check that everybody else has.

#### The seam case is different and is NOT retired

`fixture_seam.stl` — one sphere with only its cap reversed — was flagged (40
inverted normals) and genuinely repaired: 40 seam edges / 1 closed loop and
volume +2146.2 became **0 edges / 0 loops and +4095.2**, matching the correct
sphere to rounding.

Worth noting how it repaired: it **re-wound 554 of 760 faces**, and the face
set is not even identical ignoring winding — so it rebuilt the surface rather
than flipping the offending cap. Vertex and triangle counts unchanged, so
nothing was added or lost. I had assumed from the log that it flipped 40 faces
in place; comparing the arrays showed otherwise, and the counts the log prints
would never have revealed it.

#### A design-doc note that now needs revisiting

The doc says of winding seams: *"a region renders black in viewers and in Bambu
while every defect count reads zero."* That symptom is real and recorded from a
real model, but it cannot describe the **uniform** case, which Bambu renders and
slices without complaint. It must describe the **partial** case — a region
disagreeing with its neighbours, where a renderer cannot resolve a consistent
surface. The note should say which.

#### Retired with it

The planned re-scan of the collection at `dot < -0.9`, to separate genuine
inversions from exporter noise, is **no longer worth running**. Whether 62 files
or 6 carry opposed stored normals does not matter if the condition has no
consequence.

### Measured — T-junctions: `scanner` detects them, PyMeshFix repairs them

**Tested 2026-09-16** on a synthetic fixture, the same method that settled the
inverted-normals question.

**The fixture**: a closed 760-face sphere with one vertex inserted at the
midpoint of one edge, used by the two faces on one side and *not* by the
neighbour across it. The surfaces stay geometrically flush — no measurable gap
— but are topologically unjoined, and no vertex merge closes it because no two
vertices are coincident. `tests/fixtures` equivalent written to
`_validate/sphere_tjunction.stl`.

**Unlike inverted normals, this one is visible to us:**

| | faces | verts | nm | open | seams | volume |
|---|---|---|---|---|---|---|
| control | 760 | 382 | 0 | **0** | 0/0 | +4094.9 |
| T-junction | 761 | 383 | 0 | **3** | 0/0 | +4094.9 |

Three open edges, so the file never takes the clean-copy shortcut and always
reaches repair. Volume is unchanged, which confirms signed volume is specific
to orientation and blind to this.

**A commercial repair service agrees with our detection**: *3 Naked edges,
1 Planar hole*. Worth recording after the inverted case, where we shared a
blind spot with the same tool — here our numbers match a mature implementation
exactly.

**PyMeshFix repairs it, and arguably better than the service does:**

| | faces | verts | approach |
|---|---|---|---|
| source | 761 | 383 | — |
| `meshfix.repair()` | **760** | **382** | removed the T-vertex, merged the split face back |
| online service | **762** | 383 | kept the T-vertex, added a triangle to patch the crack |

PyMeshFix returns *exactly* the control's counts with a volume change of
**+0.000** — it undid the T-junction rather than papering over it, without
moving any geometry. The service's answer is the more conservative one (it
removes nothing), and both are valid; for a spurious T-vertex, undoing it is
the better result.

#### What this means for the survey

Two of the capabilities that live only inside `stl_batch_fix.blender` now turn
out not to need porting, for **opposite** reasons:

| capability | detected by us? | needs building? | why |
|---|---|---|---|
| inverted normals | no | **no** | nothing downstream cares — renders and slices fine |
| T-junctions | **yes** (open edges) | **no** | `meshfix.repair()` already fixes it, on every repaired file |

The Blender script's T-junction splitter and the unused `MERGE_DIST` constant
may therefore be solving a problem PyMeshFix already handles — and handling it
on 0.8% of files where PyMeshFix handles it on all of them.

**Not yet settled**, and the honest limit of this test: the fixture is *one*
synthetic T-junction on an otherwise perfect sphere. The script's splitter
carries a `MAX_ITER = 200` cap, which suggests it was written for meshes with
hundreds. A harder fixture — many T-junctions, or T-junctions along a seam
between two welded surfaces, which is the shape the real cases take — would
test whether PyMeshFix still copes.

Still untested from the survey: wire edges, fin vertices, doubles-merging,
and whether `scanner`'s degenerate-face count needs anything acting on it.

### Measured — the rest of the survey, on synthetic fixtures (2026-09-16)

Same method throughout: one defect on an otherwise perfect 760-face sphere,
run through `scanner`, then `meshfix.repair()`, then a commercial online repair
service for an independent verdict. Fixtures and their `_pmf` outputs are in
`/mnt/sda2/STL/_validate/`.

| fixture | source | `scanner` sees | PyMeshFix result | volume | verdict |
|---|---|---|---|---|---|
| control | 760/382 | clean | — | +4094.9 | — |
| **fin** | 761/383 | nm=1, open=2 | **756/380** clean | +4093.1 | deleted the fin, trimmed slightly |
| **degenerate** | 761/382 | nm=1, open=1, deg=1 | **760/382** clean | +4094.9 | exactly the control |
| **t-junction x150** | 910/532 | open=450 | **1000/502** clean | +4085.9 | all 450 closed, 0.2% volume loss |
| **doubles** | 1520/764, 2 shells | **clean** | **418/211** | **+2047.4** | **destructive** |

**Independently confirmed clean**: the service reports all zeros on both
`sphere_fin_pmf.stl` and `sphere_tjunction_many_pmf.stl`. So PyMeshFix's
repairs are good by a mature implementation's judgement, not merely by ours.

**`MAX_ITER = 200` is not a limit in practice.** The Blender splitter's cap
suggested it was built for meshes with hundreds of T-junctions; PyMeshFix
handled 150 at once without difficulty. Note it *patches* rather than *undoes*
at that scale — 1000 faces out, not back to 760 — where on a single T-junction
it undid it exactly. Both give a clean mesh.

#### The doubles case is the one real gap

Control volume +4094.9. Two coincident spheres: +8189.7. PyMeshFix returns
**+2047.4** — *half of one sphere*, from 418 faces where a correct merge gives
about 760. It discarded one shell and then ate half the other.

This is the same shell-eating seen on `platform_supported.stl`, and it is what
`MERGE_DIST` and Blender's `remove_doubles` exist for. **PyMeshFix makes this
one worse, not better.**

Also worth noting: `scanner` reports the doubles fixture as **completely
clean** — nm=0, open=0, degenerate=0. Only the shell count (2) hints at
anything, and two shells is legitimate on a real model. So we cannot currently
distinguish "two parts" from "one part duplicated".

#### An observation about the online service

On `sphere_tjunction_many_pmf.stl` it reported **0 of everything** and then
changed the file anyway: +2 verts, +6 faces. So "0 defects" and "unchanged" are
not the same thing for that tool — its repair pass evidently does tidying its
analysis does not report. Worth remembering when reading its output as ground
truth.

#### Survey status

| capability | detected | needs building | why |
|---|---|---|---|
| inverted normals | no | **no** | nothing downstream cares |
| T-junctions | yes | **no** | PyMeshFix fixes them, at scale |
| fin vertices | yes | **no** | PyMeshFix fixes them |
| degenerate faces | yes | **no** | PyMeshFix fixes them exactly |
| **doubles / coincident** | **no** | **probably yes** | PyMeshFix is destructive; nothing detects it |
| wire edges | untested | — | — |

**The commercial service calls the wreckage clean.** Put
`sphere_doubles_pmf.stl` — half a sphere, 418 faces, volume +2047.4 against a
control of +4094.9 — through the same service and it reports **0 of
everything**, then adds 57 verts and 114 faces anyway.

It is not wrong about what it sees. A half-sphere is a perfectly valid
watertight mesh: no naked edges, no holes, one shell. The tool has no way to
know that half the model is missing, because **every defect check in this chain
is local**. None of them compares the file against what it should have been.

That is the general lesson, and it is worth more than the doubles case itself:

| check | question it answers |
|---|---|
| naked edges, holes, non-manifold, degenerate | is this mesh self-consistent? |
| **volume before vs after** | **did the repair destroy anything?** |

Only the second can catch a repair that succeeded by its own standard and
wrecked the model. It is already in the pipeline as the volume-loss guard, and
this is the strongest evidence yet for keeping it: three independent
implementations — `scanner`, PyMeshFix and a commercial service — all call
`sphere_doubles_pmf.stl` clean, and only the before/after comparison sees the
damage.

**Still to test in Bambu**, which decides whether the doubles gap matters at
all: if two coincident shells slice identically to one, the destruction never
happens because the file would never need repairing.

---

## Where the refactor stands (2026-09-16, end of session)

**Ten modules, 260 tests across nine suites, all green.**
`stl_batch_fix.py` untouched and still frozen; `main` at `4c1c0c2`.

| module | owns | tests |
|---|---|---|
| `pool` | the worker loop, nothing domain-specific | 24 |
| `indicators` | what the filesystem says about a file | 21 |
| `blender` | launching Blender, killing it, reading markers | 32* |
| `mesh_io` | probe, load, write — the only writer | 44 |
| `scanner` | defect counts off one edge map | 46 |
| `splitter` | cutting into parts and merging back | 27 |
| `meshfix` | PyMeshFix on the arrays | 23 |
| `decimator` | the two-rung ladder | 19 |
| `converter` | the preparation walk | 20 |
| — | `test_pipeline` (the frozen script) | 36 |

\* `test_blender` runs separately; it launches real Blender and takes ~20 s.

### Built this session

`splitter` and `meshfix` are new. `scanner` gained `seam_edges()` and had two
bugs fixed (self-edges counted as closed loops; vertex- instead of
edge-connected shells). `decimator` lost its Blender rung. `Mesh` gained a
required `destination`. scipy became a hard requirement.

### Still to build

- **`repairer`** — the ladder over `meshfix` and `blender`, with the volume
  check deciding when a repair destroyed rather than fixed. **Much thinner than
  assumed at the start of the session** — see the survey results above.
- **`blender_fx/repair.blender`** — and it is now unclear how much of the
  593-line original it needs to carry.
- **Orchestration** — the four-line pipeline `splitter`'s docstring describes.
- Intermediate suffixes in `indicators`, per D21.

### Open questions, in the order they would need answering

1. **Does the doubles case matter to Bambu?** If two coincident shells slice
   identically to one, the one real gap in the survey closes itself. Fixture is
   `_validate/sphere_doubles.stl`. *This is the cheapest open question and it
   decides whether anything needs building at all.*
2. **Wire edges** — the last untested survey item.
3. **How thin can `repair.blender` be?** Four of six capabilities need no
   porting. What remains is the 12-pass repair loop and the hole-filling
   escalation, and it is not established that those beat PyMeshFix either.
4. **The split-upfront decision** (re-opened) — closed-loop detection cannot
   predict what PyMeshFix will do, so the trigger has to stay the volume check
   after the fact.
5. **Debris by bounding box, and the nm-ratio idea** — both recorded as
   discussion points, neither designed.
6. **PLY at the repair boundary** — its original justification (deleting
   `decimate.blender`'s hand-packed byte loop) evaporated when D19 deleted that
   file. Weaker case now; reconsider when `repairer` lands.

### The methodological thread, since it recurred all session

Six times a confident claim of mine was wrong and a measurement corrected it:
`shells()` "nearly free" (was 5-10x the load), the pointer-jumping benchmark
(15x faster synthetically, 2x slower on a real mesh), vertex-vs-edge
connectivity (implementation, test and oracle all agreed and all wrong), stored
normals "carry no information" (62 files say otherwise), `Done/` being pipeline
output (it is source archives), and "slices fine" written before the slice was
tested.

Each was caught by the user asking a question I could not answer, or by running
against real data rather than a fixture I had chosen. The standing rules that
came out of it:

- a claim about cost or behaviour ships with a number or an admission there is none;
- a decision that reverses existing behaviour ships with the reason that behaviour exists;
- a differential test proves agreement, not correctness — where an independent
  implementation exists, check against that, on real geometry, at least once;
- a performance fixture must resemble real input in *structure*, not merely in size.

And the finding that generalises furthest, from the doubles case: **every
defect check in this chain is local.** Ours, PyMeshFix's, and a commercial
service's all ask "is this mesh self-consistent" — none asks "did the repair
destroy anything". Only volume before-vs-after answers that, and it caught what
three independent implementations missed.

### Probe meshes

`tools/make_probe_meshes.py` regenerates the eight single-defect meshes the
survey used. They are written into the collection (`/mnt/sda2/STL/_validate`),
which is deletable by design — the generator is the durable artefact, not the
meshes.

### CORRECTION — "PyMeshFix repairs it" meant topology, not surface quality

**Seen in Bambu 2026-09-16, after the survey above was recorded.** The user
loaded the `_pmf` outputs and looked at them. Three defects are visible that
every numeric check called clean:

| mesh | what the numbers said | what it looks like |
|---|---|---|
| `sphere_doubles_pmf` | clean; volume +2047.4 | **half a sphere — an open bowl** |
| `sphere_tjunction_many_pmf` | clean; volume within 0.2% | **visible dents and creases** |
| `sphere_fin_pmf` | clean; volume −0.04% | a small defect |

**The survey entry above is wrong where it says fins, degenerate faces and
T-junctions "need no new capability".** That verdict rested on defect counts
and volume, and it should have said: *PyMeshFix produces a topologically clean
mesh*. For `tjunction_many` that is true and insufficient — 450 open edges were
closed and the surface was deformed doing it.

The degenerate-face result still stands unqualified: PyMeshFix returned
*exactly* the control's 760 faces and 382 vertices, so there is nothing to be
deformed.

#### The third category of check

The doubles case already showed that every defect check here is **local** —
none asks whether the repair destroyed anything. The dents show a further gap:

| question | what answers it |
|---|---|
| is this mesh self-consistent? | nm, open edges, degenerate, shells |
| did the repair destroy anything? | volume before vs after |
| **does the surface still look like the original?** | **nothing we have** |

A bad patch preserves volume almost exactly — `tjunction_many_pmf` is within
0.2% — while visibly deforming the surface. Volume cannot see it, and neither
can any topological count.

Candidate measures, none tried: Hausdorff distance to the pre-repair mesh
(PyMeshLab has a filter), or per-vertex displacement, or dihedral-angle change
across the patched region. Any of them would need a threshold calibrated
against what the user considers acceptable, which is a judgement rather than a
measurement.

**Why this matters beyond the fixtures.** The pipeline currently accepts a
repair whenever the defect counts reach zero and volume holds. On this evidence
that is not sufficient to conclude the model is undamaged, and the failure mode
is silent — a dented print that slices without complaint.

**The method that found it**: loading the output and looking at it. Three
independent implementations reported these meshes clean. No amount of
cross-checking numeric tools would have caught it.

### NEXT SESSION — first task: run the probe spheres through Blender

**The control the survey never ran.** Every fixture was measured against
`scanner`, PyMeshFix, a commercial service and Bambu — but **not** against the
incumbent, `stl_batch_fix.blender`. Its 12-pass repair loop is the thing the
survey was implicitly arguing could be dropped, and it was never asked to
repair anything.

That is a hole in the reasoning, not merely a missing datapoint. The survey
concluded "PyMeshFix handles these, so the Blender capabilities need no
porting" without establishing what the Blender capabilities actually produce on
the same inputs.

**What to run.** Regenerate the probes if the collection was cleared:

```bash
.venv/bin/python tools/make_probe_meshes.py
```

Then put each through the frozen script's Blender path — `fix_stl(src, dst,
merge_dist=MERGE_DIST)` in `stl_batch_fix.py`, which renders `BLENDER_SCRIPT`
and parses `BLENDER_OK` / `BLENDER_OPEN` / `BLENDER_UNREPAIRED` /
`BLENDER_EMPTY`. Write results beside the `_pmf` ones as `_bl` so all three
versions of each mesh sit together.

**What to measure, and in this order** — the last one is the point:

1. `scanner.scan()` — nm, open, degenerate, shells
2. signed volume against the control's +4094.9
3. face and vertex counts against the control's 760 / 382
4. **load it in Bambu and look at it**

Step 4 is what found the dents that steps 1-3 all missed. It cannot be skipped
and cannot be delegated to a numeric check we do not have.

**The cases that matter most:**

| fixture | why |
|---|---|
| `tjunction_many` | PyMeshFix closed all 450 open edges and **dented the surface**. Does Blender's T-junction splitter — the thing `MERGE_DIST` feeds — do better? This is the direct comparison the survey lacked. |
| `doubles` | PyMeshFix returned half a sphere. `remove_doubles(dist=merge_dist)` is exactly the operation for this, so Blender should merge the two coincident spheres into one. If it does, the one real gap has an owner. |
| `fin` | PyMeshFix leaves a small visible defect. The script's fin-vertex removal targets this specifically. |
| `inverted` | Settled as a non-defect, but `normal_vote` should flip it — worth confirming the mechanism works even though the condition does not matter. |

**What the answers would change.** If Blender produces clean *and* undamaged
surfaces where PyMeshFix produces clean-but-dented ones, the survey's
conclusion inverts: those capabilities are not redundant, they are better, and
`repairer` needs them rather than needing to drop them. If Blender does no
better, the survey stands and `repairer` stays thin.

Either way this is one session's work with fixtures that already exist, and it
should happen **before** `repairer` is designed around either answer.

### CORRECTION — the survey's conclusion is inverted: PyMeshFix is the weaker tool

**2026-09-16, after putting the *source* fixtures through the commercial
service.** The survey only ever fed it PyMeshFix's *output*. Fed the sources
directly, it repairs all three to **perfect spheres**, verified by eye in
Bambu.

| fixture | PyMeshFix | the service, same source |
|---|---|---|
| `tjunction_many` | 1000 faces, **visibly dented** | 532 verts unchanged, +150 faces — **perfect** |
| `fin` | 756 faces, small visible defect | +2 verts, +5 faces — **perfect** |
| `doubles` | 418 faces — **half a sphere** | 1520 → **816** faces — **perfect** |

**`doubles` is the decisive one.** 816 faces is one sphere's worth: the service
**merged** the two coincident copies. PyMeshFix discarded one and ate half the
other. That is `remove_doubles(dist=merge_dist)` behaviour — precisely the
operation `MERGE_DIST` feeds in `stl_batch_fix.blender`, and precisely the
capability the survey proposed dropping.

**`tjunction_many` shows why the dents happened.** The service kept **all 532
vertices** and added exactly 150 faces — one per T-junction, patching each
without touching the surrounding surface. PyMeshFix restructured (532 → 502
verts) and deformed the sphere doing it.

#### What this does to the survey

The entry above concluded that fins, degenerate faces and T-junctions "need no
new capability" because PyMeshFix handles them. **That conclusion is wrong**,
and the correction two entries up — "it meant topology, not surface quality" —
did not go far enough. It is not merely that PyMeshFix's output was unverified
on surface quality; it is that **another tool does the same repairs correctly**,
so the capability is not redundant.

Only the degenerate-face case survives: PyMeshFix returned exactly the
control's 760 faces and 382 vertices, with nothing to deform.

**Root cause of the wrong conclusion.** I measured PyMeshFix against defect
counts and volume, both of which it satisfied, and never ran the incumbent — or
any second repairer — on the same inputs. A comparison needs two columns.

#### This makes the Blender run decisive, not a control

The next-session task recorded below was framed as a control. It is now the
test that decides the shape of `repairer`:

- **If Blender matches the service** — merges `doubles`, patches
  `tjunction_many` without denting — then its repair path is *better* than
  PyMeshFix's and those capabilities must be kept, not dropped. `repairer`
  becomes a ladder where Blender is not the last resort but the better tool for
  several defect classes.
- **If Blender is no better than PyMeshFix**, then neither is adequate, and the
  gap is real and unowned — the pipeline would be shipping dented repairs today
  and nothing measures it.

Either answer changes the design. Neither is knowable from what has been run.

### MEASURED — the three-way comparison: Blender wins `doubles` and `fin`, loses `tjunction_many`

**Run 2026-09-16.** Every probe sphere through `fix_stl(src, dst,
MERGE_DIST=0.01)` — the frozen script's Blender path — with PyMeshFix's and the
commercial service's outputs alongside. Control: **760 faces, 382 verts,
+4094.9**.

| fixture | Blender | PyMeshFix | service |
|---|---|---|---|
| `doubles` | **760 / 382 / +4094.9 — exactly the control** | 418 / 211 / +2047.4 | 816 / 410 / +4095.2 |
| `fin` | **760 / 382 / +4094.9 — exactly the control** | 756 / 380 / +4093.1 | 766 / 385 / +4095.2 |
| `degenerate` | 760 / 382 / +4094.9 | 760 / 382 / +4094.9 | — |
| `tjunction` | 762 / 383 / +4094.9 | 760 / 382 / +4094.9 | — |
| `tjunction_many` | 824 / **414** / +4083.8 | 1000 / 502 / +4085.9 | **1060 / 532 / +4095.2** |
| `inverted` | **unchanged** (−4094.9) | no-op | reports 0 defects |
| `seam` | **unchanged** (+2146.2) | — | **fixed** (+4095.2) |

**`doubles` settles the one real gap.** Blender returns *exactly* the control —
it merged the two coincident spheres. That is `remove_doubles(dist=merge_dist)`
doing precisely what `MERGE_DIST` exists for, and it is the capability the
survey proposed dropping. PyMeshFix returns half a sphere; the service is close
but carries a slight excess.

**`fin` likewise**: Blender exactly the control, where PyMeshFix loses volume
(the visible defect) and the service adds geometry.

**But Blender is the *worst* of the three on `tjunction_many`.** It dropped 118
vertices (532 → 414) and lost the most volume. The service kept all 532 and
matched the control. So the Blender T-junction path restructures harder than
either alternative at scale — the opposite of what the `doubles` and `fin`
results suggest, and a reminder that "which tool is better" has no single
answer.

#### Two defects nothing in the pipeline repairs

`inverted` and `seam` come back **byte-identical to their sources** — same
face/vertex counts, same volume. `inverted` is settled as a non-defect, so that
is correct behaviour. **`seam` is not.** It is the hair-over-scalp case the
entire seam-split mechanism exists for, and the incumbent repair path leaves it
untouched at +2146.2 against a control of +4094.9. The commercial service
repairs it to +4095.2.

That is a live gap in the current pipeline, not a refactor question.

#### `BLENDER_OK` means "the script ran"

All eight fixtures reported `BLENDER_OK`, including the two it did not repair
at all. The marker is not a verdict on the mesh — the same trap as PyMeshFix's
`ok=True`, and the reason `_post_verify` exists. Worth keeping in mind when
`repairer` reads these markers.

#### What this does to the design

No tool dominates. A ladder over both is justified, and **which rung to prefer
depends on the defect**:

| defect | best tool |
|---|---|
| coincident/duplicate geometry | **Blender** (`remove_doubles`) |
| stray fins | **Blender** |
| degenerate faces | either (tie) |
| many T-junctions | neither of ours — the service beat both |
| winding seams | neither of ours — split first, as the pipeline already does |

**Still to check by eye**: the `_bl` outputs in Bambu. Numbers missed the dents
on PyMeshFix's output once already, and `tjunction_many_bl` at 414 vertices is
the obvious candidate for the same problem.

### CONFIRMED BY EYE — `tjunction_many_bl` is dented; everything else is spherical

**Checked in Bambu 2026-09-16**, the step the numbers cannot replace.

Seven of the eight `_bl` outputs render as clean spheres. **`sphere_tjunction_many_bl.stl`
has a visible gash** — a wedge-shaped tear across the surface, the same failure
mode PyMeshFix produced on the same fixture.

**So `tjunction_many` is unanimous in the wrong direction:**

| tool | faces | verts | volume | by eye |
|---|---|---|---|---|
| Blender | 824 | 414 | +4083.8 | **dented** |
| PyMeshFix | 1000 | 502 | +4085.9 | **dented** |
| service | 1060 | 532 | +4095.2 | clean |

Both of our tools deform the surface closing 450 open edges; only the
commercial service patches without restructuring — it kept **all 532 vertices**
and added exactly 150 faces, one per T-junction.

**The good results are confirmed too.** `doubles_bl` and `fin_bl` measured
*exactly* the control (760 / 382 / +4094.9) and look right. So Blender's
`remove_doubles` and fin removal are genuinely correct, not merely
numerically plausible — which is the strongest evidence yet for keeping
`MERGE_DIST` and the capabilities the survey proposed dropping.

**Volume was a weak but real signal here.** Blender lost 11.1 units on
`tjunction_many` (0.27%) against 0.0 on the cases that came out clean. Not
enough to threshold on by itself — PyMeshFix lost 9.0 and was also dented,
while the service *gained* 0.3 and was fine — but the two dented outputs are
the two that lost volume, and the clean ones lost none. Worth keeping in mind
if a surface-quality measure is ever built: "volume changed at all" may be a
cheap first filter, with Hausdorff distance for the cases it flags.

**Method note.** Three tools, three numeric checkers and a slicer all called
`tjunction_many_bl` clean: nm=0, open=0, degenerate=0, one shell, volume within
0.3%. Looking at it took seconds. This is the second time in one session that
eye inspection overturned a conclusion every measurement supported.

### SOLVED (as a sequence, NOT as code) — the seam case

> **Read this first.** Nothing below is implemented. The sequence was run in a
> throwaway shell script and measured; the repo cannot do it. `splitter`,
> `mesh_io` and `scanner` provide every piece **except the flip**, which lives
> nowhere — the only `faces[:, ::-1]` in the tree is in `make_fixtures.py`,
> where it *builds* the inverted fixture. `stl_batch_fix.blender` and
> `stl_batch_fix.py` were not touched; both remain frozen. The flip belongs in
> `repairer` when that is built, because deciding a region is backwards is
> policy rather than geometry.

**Measured 2026-09-16.** `sphere_seam.stl` — one sphere with its cap reversed,
40 seam edges in 1 closed loop, volume **+2146.2** against a control of
**+4094.9** — is repaired to a **perfect reconstruction** by a sequence that
uses no repair tool at all.

| step | result |
|---|---|
| `splitter.by_seams()` | 760f → **520f + 240f**, each seam-free, each one shell, 40 open edges apiece |
| region volumes | +3120.5 and **−974.3** — the reversed cap is negative |
| flip the negative region | −974.3 → +974.3 |
| `splitter.merge()` | 760f / 422v / 80 open / 2 shells / **+4094.9** |
| `mesh_io.write` + `load` | **760f / 382v / nm=0 / open=0 / seams 0/0 / 1 shell / +4094.9** |

**Identical to the control on every measure.**

**Repairing the regions is what breaks it.** PyMeshFix caps each open region
into its own closed solid, which is correct in isolation and wrong here:

| approach | volume | vs control |
|---|---|---|
| split → repair (no flip) | +2936.5 | 71.7% — capped the reversed cap inside-out |
| split → flip → repair | +4355.9 | 106.4% — cap surfaces sit inside the sphere |
| **split → flip → merge → reload** | **+4094.9** | **100.0%** |

The regions must be left open so the merge rejoins them. A repair tool cannot
know that.

**The weld claim in `splitter.merge()`'s docstring is confirmed.** It says
*"coincident vertices at an old cut line are not welded — the next
`mesh_io.load` does that"*. Measured: 422 verts and 80 open edges collapse to
382 and 0 on write-and-reload. That claim was load-bearing here.

#### This corrects the morning's `normal_vote` conclusion

Recorded earlier: *"inverted normals are a non-defect; do not build detection
for them"*. That holds for a **uniformly** inverted mesh — nothing downstream
cares, Bambu renders and slices it fine.

**It does not hold after a seam split.** One region is then reversed *relative
to the other*, and flipping it is the entire repair. So the capability is
needed after all — but not as `normal_vote`'s per-component majority vote over
stored facet normals, which today's scan showed to be mostly exporter noise.
**Signed volume decides it**: a region with negative volume is reversed. Three
lines, using a measure already computed for the volume-loss guard, with no
dependence on stored normals at all.

That is a better mechanism than the one being considered for porting, and it
was reached from the opposite direction.

#### Where this leaves the seam defect

| tool | result |
|---|---|
| Blender `fix_stl` | **unchanged** — `BLENDER_OK`, +2146.2 |
| PyMeshFix alone | not applicable — no open edges to work on |
| commercial service | fixed, +4095.2 |
| **split → flip → merge → reload** | **+4094.9, exactly the control** |

Ours is now the best result available, and it needs nothing the modules do not
already have: `splitter.by_seams`, a signed-volume test, `splitter.merge`, and
a write/reload cycle.

**Confirmed by eye in Blender**: `_validate/sphere_seam_FIXED.stl` renders as a
clean sphere, including across the boundary ring where the two regions
rejoined. That check mattered — twice today a mesh with nm=0, open=0, one shell
and volume within 0.3% turned out to have a visible gash. This one matches the
control on every field including vertex count, and looks right as well.

### Note — an unrepaired seam mesh looks perfectly fine

**Observed 2026-09-16.** `fixture_seam.stl` — the **unrepaired** source, 40
seam edges in 1 closed loop, volume **+2146.2** against a control of +4094.9 —
renders as a clean sphere. Nearly half its enclosed volume is wrong and nothing
is visible.

Consistent with the inverted-normals finding: renderers reconstruct orientation
from geometry rather than trusting winding, and a reversed cap occupies exactly
the right space — same surface, traversed the other way. Nothing moves, so
nothing looks wrong.

**This contradicts the recorded symptom.** The design doc says of winding
seams: *"a region renders black in viewers and in Bambu while every defect
count reads zero."* This fixture reads zero on every count **and renders
normally**. Either the symptom needs something the fixture lacks, or it was
misattributed. Not resolved.

**The real justification for the seam mechanism is not visual.** It is that
**PyMeshFix deletes a region** when handed irreconcilable winding — 562,288
faces in, 394,432 out, a model's head gone. The failure is in the repair, not
the render, and it strikes a file that looks and measures fine going in.

Worth recording plainly, because "it looks fine" is exactly the observation
that would justify removing the machinery:

| | `fixture_seam.stl` |
|---|---|
| nm / open / degenerate | 0 / 0 / 0 |
| shells | 1 |
| renders in Bambu | **fine** |
| signed volume | **+2146.2 vs +4094.9 — 52% of control** |
| through PyMeshFix unsplit | **region deleted** |

Signed volume is the only check that sees anything wrong, and it is the same
measure that turned out to drive the repair (flip the negative-volume region).

#### Both seam repairs are idempotent

Re-running each repair on its own output changes nothing — checked because a
repair that keeps "fixing" the same file is a real failure mode, and the
pipeline reruns files.

**Ours**, three passes from the source:

| pass | faces | verts | seams | regions | flipped | volume |
|---|---|---|---|---|---|---|
| src | 760 | 382 | 40/1 | — | — | +2146.2 |
| 1 | 760 | 382 | **0/0** | 2 | 1 | **+4094.9** |
| 2 | 760 | 382 | 0/0 | **1** | 0 | +4094.9 |
| 3 | 760 | 382 | 0/0 | 1 | 0 | +4094.9 |

Pass 1 does the work. Passes 2 and 3 find **one** region — no seam to cut on —
so `by_seams` returns `(mesh,)` and nothing is flipped, merged or welded. That
is the "a mesh that does not split comes back as a list of one" contract doing
its job: a rerun costs one scan.

**The commercial service** likewise: `fixture_seam_fixed.stl` back through it
reports 0 of everything and returns 382 verts / 760 faces unchanged.

So both approaches are stable, and they agree on the answer:

| | detection | mechanism | result | idempotent |
|---|---|---|---|---|
| service | 40 inverted normals | flip the faces in place | +4095.2 | yes |
| **ours** | 1 closed seam loop | split → flip negative-volume region → merge → reload | **+4094.9** | yes |

**A possible simplification, not taken.** The service does not split at all —
it flips the reversed faces where they are, which is simpler than
split-merge-reload. Ours could do the same if the reversed region could be
identified *without* extracting it; `scanner.shells()` does not currently
distinguish regions across a seam boundary, so that would need new work. The
split path is built and tested, so this is recorded as an option rather than a
plan.

### Measured — decimation creates the defects on `whole-costume01`

**2026-09-16**, while testing the model the user recalled Blender breaking.

| | faces | nm | open | degenerate | shells | volume |
|---|---|---|---|---|---|---|
| source | 2,004,941 | **3** | 93 | 1 | 448 | +7,760.5 |
| after decimation to 900k | 900,000 | **2,263** | 20 | — | **491** | +7,758.7 |

`fast_simplification` takes a mesh with **three** non-manifold edges and
returns one with **2,263**, and adds 43 shells. Volume is preserved to within
0.02%, so nothing is deleted — the damage is topological, not geometric.

**This reframes the model's failure.** It does not reach repair carrying three
defects; it reaches repair carrying 2,263, and the step that created them is
upstream. Any comparison of repair tools on this file is measuring their
response to decimation's output, not to the model.

It is also consistent with the standing rule that decimation may leave
non-manifold edges and that the repair steps exist to clean up after it —
recorded in `decimator`'s docstring. What was not previously measured is the
*scale*: a 750-fold increase on this model.

**Not yet known**: whether this is particular to `whole-costume01` (444 of its
448 shells are debris under 100 faces, so decimation is collapsing a great many
tiny components) or general to fast_simplification on multi-shell meshes. One
model is not a sample — the mistake made four times already in this refactor.

The seam algorithm does **not** apply to this model, incidentally: it has
**0 seam edges and 0 closed loops**, so `by_seams` returns it unchanged. The
defect here is shells and non-manifold edges, not reversed winding.

### Measured — PyMeshLab is the strongest of the three tools we have

**2026-09-16.** Asked whether PyMeshLab has equivalents for the defects
PyMeshFix and Blender mishandle. It does, and it is exact on four of six —
including two that **nothing else we have can fix**.

| defect | Blender | PyMeshFix | **PyMeshLab** | online service |
|---|---|---|---|---|
| `doubles` | exact | **half a sphere** | **exact** | exact |
| `degenerate` | exact | exact | **exact** | — |
| `inverted` | unchanged | no-op | **exact — the only tool that fixes it** | reports 0 defects |
| `seam` | unchanged | n/a | **exact, without any split** | exact |
| `fin` | exact | lossy | 99.7%, 2 extra faces | exact |
| `tjunction_many` | **dented** | **dented** | **destroys or no-ops** | **exact — only tool** |

PyMeshLab is already a hard dependency, runs in-process, and takes numpy
directly — so this costs no subprocess and no new install.

#### The filter sequences that work

**`doubles`** — 1520f/764v/200% volume becomes **760f/382v/100%**, exactly the
control:

```python
meshing_merge_close_vertices(threshold=PercentageValue(0.1))
meshing_remove_duplicate_faces()
meshing_remove_unreferenced_vertices()
```

`merge_close_vertices` **alone makes it worse** — it collapses the vertices and
leaves both face sets, giving 1,140 non-manifold edges at 200% volume. All
three filters are needed.

**`seam`** — +2146.2 becomes **+4094.9**, exactly the control:

```python
meshing_re_orient_faces_coherently()
meshing_re_orient_faces_by_geometry()
```

`re_orient_faces_coherently` alone unifies the winding but picks the **wrong
direction** — it orients everything to the reversed cap, giving −4094.9.
`by_geometry` then turns the whole mesh outward.

**`inverted`** — `meshing_re_orient_faces_by_geometry()` alone takes −4094.9 to
+4094.9. The only tool in the chain that detects and corrects it.

**`degenerate`** — `meshing_remove_null_faces()`, exactly the control.

#### This simplifies the seam repair recorded as "solved" this morning

That entry describes split → flip the negative-volume region → merge → write →
reload. It is correct and it reaches the control. **Two PyMeshLab filters reach
the same answer with none of that machinery** — no split, no flip, no merge, no
weld.

The split-based sequence is still the more *general* mechanism (it isolates
regions for separate treatment, which matters when PyMeshFix would otherwise
delete one), but for the plain "a region is wound backwards" case the two-filter
version is what should be reached for first.

#### T-junctions are unfixed by anything we have

`meshing_remove_t_vertices` has a `threshold` defaulting to 40, which is why it
first appeared to be a no-op:

| threshold | Edge Collapse | Edge Flip |
|---|---|---|
| 40, 10 | no-op (450 open edges remain) | no-op |
| **1, 0.1** | **destroys the mesh — 910 faces to 0** | no-op |

`meshing_close_holes(maxholesize=30)` alone gets 450 open edges down to 70 but
introduces a non-manifold edge.

So on `tjunction_many` all three of our tools fail: Blender dents it, PyMeshFix
dents it, PyMeshLab either does nothing or deletes everything. **Only the
commercial service handles it**, keeping all 532 vertices and adding exactly
150 faces. That remains an unowned gap.

**A hazard worth naming**: `meshing_remove_t_vertices(method='Edge Collapse')`
at a low threshold silently reduced a 910-face mesh to zero faces with
`nm=0 open=0` — a "clean" empty mesh. Volume is the only check that catches it.

### Measured — the two orientation filters in isolation, and a correction

**2026-09-16.** `meshing_re_orient_faces_coherently()` +
`meshing_re_orient_faces_by_geometry()`, run alone on every fixture.

| fixture | before | after |
|---|---|---|
| `inverted` | −4094.9 | **+4094.9** |
| `seam` | +2146.2, 40 seam edges / 1 loop | **+4094.9, 0/0** |
| `correct` | +4094.9 | unchanged |
| `tjunction` (3 open) | 100% | unchanged, safe |
| `tjunction_many` (450 open) | 100% | unchanged, safe |
| `doubles` (2 shells) | 200% | unchanged, safe |
| `degenerate` (nm=1) | — | **RAISES** |
| `fin` (nm=1) | — | **RAISES** |

**The precondition is exactly `nm == 0`.** The error says why: *"Mesh has some
not 2-manifold faces, Orientability requires manifoldness"* — orientation is
undefined on a non-manifold surface. Open edges and multiple shells are fine;
only non-manifold edges block it.

So these filters are safe to run unconditionally **after** non-manifold edges
are resolved, and `scanner.scan().non_manifold` is the gate. Verified:
PyMeshFix on `fin` gives 756f/100%, and orient then runs as a clean no-op.

#### Correction — PyMeshFix fixes the seam fixture by itself

Recorded this morning, in the seam comparison table: *"PyMeshFix — not
applicable, no open edges to work on."* **Wrong.** Measured:

```
source     760f 382v vol +2146.2  seams 40/1
pymeshfix  760f 382v vol +4094.9  seams 0/0     <- exactly the control
same face set (ignoring winding): True
faces re-wound: 760 of 760
```

It re-wound the entire mesh to a consistent outward orientation. One call, no
split, no flip, no merge, no reload.

**So the split → flip → merge → reload sequence recorded as "SOLVED" is
unnecessary for this fixture.** It is correct and it reaches the control, but
`meshfix.repair(mesh)` alone does the same thing, and so does the PyMeshLab
pair. Three routes to the same answer, and I built the most elaborate one first
because I had assumed PyMeshFix could not help.

**The design doc's claim is not contradicted** — it says PyMeshFix *deletes a
region* on irreconcilable winding, 562,288 faces in and 394,432 out, on a real
model. That model evidently had something this fixture lacks (more shells, or a
seam that re-winding cannot resolve). **The fixture does not reproduce the
failure the seam mechanism was built for**, which is worth knowing before
trusting it as the test case for that mechanism.

#### Where this leaves the two filters

They are the **only** tool that fixes a uniformly inverted mesh. For seams they
are one of three options. Their value is therefore narrower than it first
appeared, but not zero — and they cost nothing, PyMeshLab being already a
dependency and in-process.

### Measured — the tool order: PyMeshFix first, PyMeshLab after

**2026-09-16.** Proposed order was PyMeshLab (inverted, seams, doubles) then
PyMeshFix. Tested on a fixture carrying **four defects at once** — a reversed
cap, a fin, a degenerate face — because every fixture until now had exactly
one, and the order only matters when defects interact.

Source: 762f, nm=2, open=3, seams 40/1, **51.3%** volume.

| order | result |
|---|---|
| **A — PyMeshLab first** | clean filters take nm 2 → 1, then orient **RAISES**: *"Orientability requires manifoldness"*. Pipeline stops at **51.3%**. |
| **B — PyMeshFix first** | **756f, nm=0, open=0, seams 0/0, 100.0%** in one call. PyMeshLab then runs as a safe no-op. |

**The order is structural, not a preference.** PyMeshLab's orientation filters
have a hard precondition of `nm == 0` — they raise, they do not degrade — and
PyMeshFix is good at establishing exactly that. PyMeshFix's own weaknesses
(`doubles` → half a sphere, `inverted` → no-op) are precisely what the
PyMeshLab filters clean up afterwards. They compose in one direction only.

#### The sequence

```
split by shells                       splitter.by_shells
  -> PyMeshFix                        nm, open edges, seams
  -> PyMeshLab clean                  remove_null_faces
                                      merge_close_vertices(0.1%)
                                      remove_duplicate_faces
                                      remove_unreferenced_vertices
  -> PyMeshLab orient                 re_orient_faces_coherently
                                      re_orient_faces_by_geometry
  -> verify volume
merge
```

**Split first, always.** PyMeshFix turns the `doubles` fixture into half a
sphere and ate 4,525 support shells on `platform_supported.stl`. Given
pre-split single shells it is excellent: costume01's three parts went from
**2,263 non-manifold edges to 0** in 201s with volume preserved to 100.0%.

**`meshing_remove_t_vertices` is excluded deliberately.** It destroyed both
T-junction fixtures at threshold ≤ 1 — 910 faces to **zero**, reporting
`nm=0 open=0`. A clean empty mesh, which only a volume check catches.

#### What the order does not solve

**T-junctions.** All three tools fail: PyMeshFix and Blender both dent the
surface (numerically 99.8% and 99.7% and "clean" — visually gashed), PyMeshLab
destroys the mesh. Only the commercial service handles it, keeping all 532
vertices and adding exactly 150 faces. Unowned.

**Where Blender fits** is still open. It is exact on `fin` and `doubles`, blind
to `inverted` and `seam`, and it **timed out at 420s** on costume01 — the
`TIMEOUT after 420s` in stdout being why the original run wrote `.failed.stl`
with no markers. A run with the cap lifted is in progress; until it returns,
whether Blender can repair a mesh of that shape at all is unknown.

### CORRECTION + the full sequence measured on all eight fixtures

**2026-09-16.** Two corrections to the entries above, both from running the
composed sequence rather than each tool alone.

#### 1. Only ONE of the two orientation filters refuses

Recorded above as *"PyMeshLab's orientation filters refuse when `nm > 0`"*.
Too broad. Tested individually:

| case | nm | open | `re_orient_faces_coherently` | `re_orient_faces_by_geometry` |
|---|---|---|---|---|
| clean tetra | 0 | 0 | ok | ok |
| **nm edge (fin)** | **1** | 2 | **RAISES** | **ok** |
| open (face removed) | 0 | 3 | ok | ok |
| **degenerate face** | **1** | 1 | **RAISES** | **ok** |
| two shells | 0 | 0 | ok | ok |
| vertex-joined | 0 | 3 | ok | ok |

`by_geometry` **never refuses**. The precondition belongs to `coherently`
alone, and the trigger is non-manifold *edges* — open edges, multiple shells
and vertex joins all pass. The original test ran the pair together, so
`coherently` raising masked that `by_geometry` was fine.

Practically: `by_geometry` alone fixes `inverted` and can run on anything.
`coherently` is needed for the seam case and carries the constraint.

#### 2. Clean filters must run BEFORE the split

First composition attempt — split, then repair each part, then clean — left
`doubles` at **200% volume and two shells**. The split separated the coincident
spheres, so `merge_close_vertices` never saw them together and had nothing to
merge. **Splitting first defeats deduplication.** Moving the clean filters ahead
of the split fixed it: 760f, one shell, 100.0%.

#### 3. ORIENT damages a mesh that does not need it

The sole remaining failure was `tjunction_many`: source has **0 seam edges**,
output had **153 in 34 closed loops**. Traced step by step:

```
source            910f  nm=0 open=450 seams=0/0   100.0%
after CLEAN       910f  nm=0 open=450 seams=0/0   100.0%
after pymeshfix  1000f  nm=0 open=  0 seams=0/0    99.8%   <- already correct
after ORIENT     1000f  nm=0 open=  0 seams=153/34 99.8%   <- damaged
```

The orientation filters **manufactured 34 closed seam loops** on a mesh
PyMeshFix had just rebuilt correctly. That is worse than the dents: a closed
seam loop is precisely the signal meaning *PyMeshFix will delete a region here*.

My earlier "safe no-op on clean meshes" conclusion came from simple fixtures.
On a mesh freshly patched over 150 T-junctions, they are not.

**The guard**: run ORIENT only when the mesh says it is misoriented —
`volume < 0` or a closed winding seam. With that, **all eight fixtures pass**.

#### The sequence that works

```python
mesh = pml(mesh, CLEAN)                 # BEFORE the split — dedup needs the whole mesh
for part in splitter.by_shells(mesh):
    part = meshfix.repair(part).mesh
    if scanner.volume(part) < 0 or scanner.winding_seams(part)[1] > 0:
        if scanner.scan(part).non_manifold == 0:
            part = pml(part, ORIENT)    # only when needed, and only when legal
merge(parts)
```

| fixture | faces | nm | open | seams | shells | volume |
|---|---|---|---|---|---|---|
| inverted | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| seam | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| doubles | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| degenerate | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| fin | 756 | 0 | 0 | 0/0 | 1 | **100.0%** |
| tjunction | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| tjunction_many | 1000 | 0 | 0 | 0/0 | 1 | 99.8% |
| correct | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |

**Eight of eight numerically clean.** Outputs written as `sphere_*_seq3.stl`.

**Not yet confirmed by eye** — and `tjunction_many` at 1000 faces is the same
shape that was visibly dented before, so that one especially needs looking at.
Numbers have been wrong about this fixture twice.

### Measured — Blender CAN repair costume01; it missed the timeout by 16%

**2026-09-16.** The original `.failed.stl` on this model was a **timeout, not a
failure**. `fix_stl` returned `TIMEOUT after 420s` with no markers and no output
file, which is why the caller had nothing to report.

Re-run with the cap lifted to 3780s: **Blender finished in 488s** — 68 seconds
past the 420s budget, a 16% overrun.

`_blender_budget()` gives 70% of a 600s budget, reserving 30% for post-Blender
work. That reserve is measured and justified (p95 of post-Blender PyMeshFix was
152.6s), so the cap is not arbitrary — this model simply needs more than the
whole budget allows.

#### But finishing is not the same as winning

| | faces | nm | open | shells | volume | time |
|---|---|---|---|---|---|---|
| decimated input | 900,000 | 2,263 | 20 | 491 | 100.0% | — |
| **Blender** | 887,019 | **0** | **493** | 450 | 99.5% | 488s |
| **PyMeshFix** (after split) | 835,426 | **0** | **28** | 3 | **100.0%** | 201s |

Both clear all 2,263 non-manifold edges. Blender leaves **493 open edges
against PyMeshFix's 28**, keeps 450 shells where the split reduced to 3, loses
0.5% volume against 0.0%, and takes **2.4x as long**.

Its final marker is `BLENDER_OPEN: open=493 max=0.7735mm` — and 0.7735mm
exceeds `MIN_LAYER = 0.6`, so the print-scale gate would not accept it either.

#### The pass log shows the cost

```
Repair pass  7: nm=237 boundary=392
Repair pass  8: nm=236 boundary=389
Repair pass  9: nm=236 boundary=389          <- stalled
Repair pass 10: wider NM deletion (2583 faces)
Repair pass 10: nm=26 boundary=460
Repair pass 11: nm=14 boundary=425
Repair pass 12: nm=14 boundary=419           <- loop limit, not success
```

Twelve passes, the stall counter firing at 9, the wider-NM escalation deleting
2,583 faces at pass 10, and termination at the loop limit rather than at
convergence. The escalation works — nm goes 236 to 26 — but it is deleting
geometry to get there, which is where the 0.5% volume and 493 open edges come
from.

**So the incumbent is slower, lossier, and ends short of clean on the one real
multi-shell model tested.** Its wins remain the simple single-shell cases —
`fin` and `doubles`, both exact.

### CORRECTION — the sequence is dented on `fin` and `tjunction_many`

**Confirmed by eye 2026-09-16.** Two of the eight `_seq3` outputs are visibly
dented, despite **every numeric check passing**: nm=0, open=0, seams 0/0, and
volume at 100.00% and 99.78%.

**The composition is not at fault.** Traced step by step, `CLEAN` changed
nothing on either fixture (no duplicates, no null faces), the split found one
part, and the ORIENT guard correctly declined to fire. `meshfix.repair()` on
the raw source gives byte-identical output to the whole sequence. The dents
come from PyMeshFix, inherited rather than introduced.

**Blender is exact on `fin` — literally.** Compared face-set against the
control:

| version | faces | verts | volume | identical to control |
|---|---|---|---|---|
| **`_bl`** | **760** | **382** | **100.00%** | **YES — 0 extra, 0 missing** |
| `_pmf` / `_seq3` | 756 | 380 | 99.96% | no — 527 extra, 531 missing |
| `_fixed` (service) | 766 | 385 | 100.01% | no |

Blender did not approximate the repair; it reconstructed the original mesh
exactly. PyMeshFix removed 4 extra faces, and that 0.04% is what shows as a
dent.

On `tjunction_many` **nothing matches the control** — every tool produces a
different surface, and only the commercial service's is visually clean.

#### What this changes

The sequence applies **PyMeshFix to everything**, and that is wrong where
another tool is exact. Routing by defect rather than by fixed order:

| defect | tool | evidence |
|---|---|---|
| doubles, degenerate | PyMeshLab CLEAN, before the split | exact |
| **fin / nm on a small mesh** | **Blender** | **identical to control** |
| nm on a large multi-shell mesh | PyMeshFix after the split | costume01: 2,263 → 0, 100.0% volume, 201s |
| inverted, seams | PyMeshLab ORIENT, guarded | exact |
| T-junctions | **nothing we have** | all three fail; only the service is clean |

**Three numeric all-clears on visibly wrong meshes, in one session.** `fin` at
99.96% and `tjunction_many` at 99.78% both passed nm, open, seam and shell
checks. Volume differences that small are indistinguishable from rounding, so
volume cannot be the gate either. The surface-comparison check recorded earlier
as "nothing we have" is now the blocking gap: without it, a repair ladder
cannot tell which tool produced the better mesh.

### Measured — CLEAN tears holes in `costume01`, and the threshold is not why

**2026-09-16.** The costume run showed open edges going **20 → 1,318** at the
CLEAN step. I attributed that to my `merge_close_vertices` threshold, having
used PyMeshLab's `PercentageValue(0.1)` — roughly 0.11 mm on this model —
against the project's own `MERGE_DIST = 0.01 mm`. **Wrong.**

| threshold | faces | nm | open | shells |
|---|---|---|---|---|
| input | 900,000 | 2,263 | **20** | 491 |
| 0.1% (~0.11 mm, what I used) | 898,616 | 2,018 | 1,318 | 489 |
| **0.01 mm = `MERGE_DIST`** | 895,230 | **2,359** ↑ | 1,506 | 487 |
| 0.001 mm | 898,636 | 2,016 | 1,318 | 491 |
| **no merge step at all** | 898,953 | 1,920 | **1,225** | 491 |

**Open edges go 20 → 1,225 with no merge step at all**, so the damage belongs to
`remove_duplicate_faces` or `remove_null_faces`, not to the merge or its
threshold.

And `MERGE_DIST = 0.01 mm` is the **worst** setting tried: the only one that
*increases* non-manifold edges, 2,263 → 2,359. The project's own constant is
tuned for Blender's `remove_doubles`, and does not transfer to PyMeshLab's
filter.

**The net result is still fine** — PyMeshFix closes those 1,225 holes back to
28 — so CLEAN's damage is real but recoverable. Whether it *earns its place* on
a mesh with no duplicate geometry is a separate question, now being tested by
running the sequence without it.

**A correction to note**: `pymeshlab.AbsoluteValue` does not exist. The
absolute-units class is **`PureValue`**; `PercentageValue` is the other. Both
are required — passing a bare float raises.

#### The costume result, for the record

| | faces | nm | open | shells | volume | time |
|---|---|---|---|---|---|---|
| decimated input | 900,000 | 2,263 | 20 | 491 | 100.0% | — |
| **full sequence** | 835,466 | **0** | **28** | 2 | **100.0%** | **198s** |
| PyMeshFix alone | 835,426 | 0 | 28 | 3 | 100.0% | 201s |
| Blender, uncapped | 887,019 | 0 | 493 | 450 | 99.5% | 488s |

The sequence and PyMeshFix-alone differ by **40 faces and one shell** — the
PyMeshLab work contributed almost nothing on this model. `part 1: oriented`
did fire, so the guard found a genuinely misoriented region on real data.

#### `seq4` — `by_geometry` before the repair

Tried at the user's suggestion, since `re_orient_faces_by_geometry` never
refuses and could run first. Result: **byte-identical to `seq3` on all eight
fixtures.** It only acts on a misoriented mesh, and by the time it runs the
orientation is either already correct or PyMeshFix will fix it. A no-op in
either position.

#### Vertex-set comparison detects what the eye detects

Comparing each output's sorted vertex array against the control's: six of eight
match exactly, and **the two that do not are precisely the two seen as
dented** — `fin` (756f/380v) and `tjunction_many` (1000f/502v).

So when a known-good reference exists, vertex-set comparison is the
surface-quality check that volume and topology counts cannot provide. It does
not help on real models, where no control exists — but it makes the fixture
suite able to catch dents automatically.

### Measured — CLEAN earns nothing on `costume01`; it should be conditional

| | faces | nm | open | shells | volume | time |
|---|---|---|---|---|---|---|
| **with CLEAN** | 835,466 | 0 | 28 | 2 | 100.0% | 198s |
| **without CLEAN** | 835,426 | 0 | 28 | 3 | 100.0% | 202s |

A **40-face** difference and 4 seconds, for a step that first tears **1,225
open edges** into the mesh which PyMeshFix then has to close. Damage and repair
for no net gain — because this model has no duplicate geometry to remove.

That does not make CLEAN useless: it is the only thing that fixes `doubles`,
where PyMeshFix returns half a sphere. It makes it **conditional**, like ORIENT
already is. Run it when there is duplicate geometry, not unconditionally.

Detecting that is the open part. `scanner` currently reports the `doubles`
fixture as entirely clean — nm=0, open=0, degenerate=0 — and only the shell
count hints at anything, which is useless because two shells is legitimate.

**A side effect worth noting**: without CLEAN the orient guard fired on **two**
parts; with CLEAN, on one. So CLEAN was masking a misorientation rather than
resolving it.

### The T-junction defect, precisely

Traced on `sphere_tjunction.stl` rather than described from memory, after an
explanation of mine turned out to be wrong in its details.

The T-junction vertex is **345** — not the last-added index, since PyMeshLab
renumbers on load. It has **3 edges and 2 faces**, and:

```
vertex 345 lies ON edge (339, 358) at t=0.500, distance 2.6e-23
```

Exactly the midpoint, and that edge is used by **one** face — the neighbour
that never got split. The three open edges are:

| edge | faces | what it is |
|---|---|---|
| (339, 358) | 1 | the unsplit neighbour's full-length edge |
| (339, 345) | 1 | left half, on the split side |
| (345, 358) | 1 | right half, on the split side |

The halves and the whole occupy the same line and are three distinct edges,
each with a single face. **Three open edges and zero measurable gap** — which
is why merging coincident vertices cannot help: nothing is coincident.

**The repair** is to split the neighbouring face at 345. One face becomes two,
345 gains a fourth edge, its fan closes, and every edge gets two faces. **One
extra face per T-junction** — precisely the +150 faces the commercial service
produced on the 150-T-junction fixture while leaving all 532 vertices intact.

**A correction**: I first described this as vertex M having 3 edges where the
neighbour contributes nothing, and checked the wrong vertex (382, which has 6
edges all properly shared). The shape of the explanation was right; the
specifics were invented. Checking took one query.

### SOLVED (as a method) — the T-junction repair is one face split

**2026-09-16.** The user sketched the fix and asked whether M–D should be a
face. It should be an **edge**, and adding it splits the neighbouring face in
two. Verified on `sphere_tjunction.stl`:

```
neighbour face 199 = [335, 358, 339]        D-C-A, with D = 335
replaced by        [358, 345, 335]          C-M-D
                   [345, 339, 335]          M-A-D

before: 761f  nm=0  open=3
after : 762f  nm=0  open=0  seams=0/0  shells=1  volume 100.00%
```

**+1 face, and the defect is gone.** Every edge now has two faces, M's fan is
closed, and the surface is untouched — no vertex moved, nothing deleted.

#### This is the repair nothing in the toolchain does

| | faces | outcome |
|---|---|---|
| **face split at M** | 761 → **762** (+1) | **clean, 100.00%, no surface change** |
| PyMeshFix | 910 → 1000 | numerically clean, **visibly dented** |
| Blender | 910 → 824 | numerically clean, **visibly dented** |
| commercial service | 910 → 1060 (**+150**) | clean |

The service's **+150 faces on a 150-T-junction fixture** is one face per
T-junction — the same operation, arrived at independently.

#### Why the other tools fail at it

Neither PyMeshFix nor Blender treats this as a topology problem. Both see
open edges and try to *close a hole*: PyMeshFix patches (adding 90 faces),
Blender restructures (removing 86 faces and 118 vertices). Both move geometry,
and that is where the dents come from. The defect has **zero measurable gap** —
there is no hole to close, only an edge that needs splitting.

#### It is implementable, and simply

The detection already exists in a scratch form: for each vertex, test whether
it lies on an edge it does not belong to. On the fixture:

```
vertex 345 lies ON edge (339, 358) at t=0.500, distance 2.6e-23
```

Then split that edge's face at the vertex, preserving winding by walking the
triangle and inserting M between A and C. About 30 lines over `scanner`'s
existing edge map.

**This is the first repair found today that our own code can do and no external
tool does correctly.** Worth building rather than delegating — and it removes
the one defect the survey listed as unowned.

**Caveat before building**: the fixture has one T-junction at an exact
midpoint. Real ones will sit anywhere along the edge, several may share a face,
and a vertex may lie on an edge only approximately. The collinearity tolerance
and the multiple-per-face case both need deciding on real data.

#### Confirmed trivial — a naive implementation handles 150 at once

The user's assessment, tested rather than assumed:

| fixture | before | after | splits | rounds | time |
|---|---|---|---|---|---|
| `tjunction` | 761f, 3 open | **762f, 0 open, 100.00%** | 1 | 2 | 0.01s |
| `tjunction_many` | 910f, 450 open | **1060f, 0 open, 100.00%** | 150 | 2 | **0.40s** |

**1060 faces is exactly what the commercial service produced** — same face
count, same 532 vertices preserved. Two implementations arriving at an
identical result independently.

Against the alternatives on `tjunction_many`:

| | faces | open | volume | surface |
|---|---|---|---|---|
| **face split (ours)** | **1060** | **0** | **100.00%** | untouched |
| commercial service | 1060 | 0 | 100.01% | clean |
| PyMeshFix | 1000 | 0 | 99.78% | **dented** |
| Blender | 824 | 0 | 99.73% | **dented** |

**Three worries, all unfounded.** Several T-vertices on one edge, several on
one face, and convergence all needed no special handling — the naive version
takes at most one split per face per round and re-derives the edge map each
round. Two rounds sufficed on 150 scattered T-junctions, the second only
confirming none remained.

**The one real open question is tolerance.** `1e-6` works because these
fixtures are exact to 2.6e-23 by construction. A T-junction from a boolean or a
decimation sits *near* an edge rather than on it, and the threshold becomes a
judgement: too tight misses them, too loose splits faces that should not be.
That is a parameter to calibrate on real data, not a design problem.

**Confirmed by eye**: `sphere_tjunction_tjfix.stl` and
`sphere_tjunction_many_tjfix.stl` both render clean, **no dents**. That check
mattered — numbers had been wrong about this fixture three times, and both
PyMeshFix's and Blender's outputs passed every numeric gate while being
visibly gashed.

So this is the first repair in the session that **our own code does better
than every external tool**, verified numerically and visually.
