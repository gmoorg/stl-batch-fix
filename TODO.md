# Open items

Things worth investigating, with the evidence that prompted them. Nothing here
is a known bug — those get fixed. These are questions where the right answer is
not yet clear.

> **When a change lands in the script, update `STL_BATCH_FIX_DESIGN.md` in the
> same commit.** That file carries the reasoning — measured parameter values,
> why the steps run in the order they do, what was tried and failed — and none
> of it is recoverable from the source. It fell 13 commits behind by 2026-09-13
> and still documented `TIMEOUT` at 1200 s, a debris rule that had been
> replaced, and a pipeline order that no longer matched. Stale rationale is
> worse than none, because it gets trusted.

---

## 1. Two runners with different timeout behaviour — partly fixed 2026-09-13

**The 2026-09-12 version of this entry was wrong.** It claimed
`process_file_subprocess` is "called from nowhere". It is called — the TUI does
it, passing the function as a value:

```python
fut = _pool.submit(_fix.process_file_subprocess, src)   # stl_batch_fix_tui.py:915
```

An AST walker looking for call nodes and bare `Name` nodes missed it (the
reference is an `ast.Attribute`), and plain grep found it. The same failure
shape as the note at the bottom of this file.

There are **two runners**:

| path | submits | child per file | watchdog | SIGALRM |
|---|---|---|---|---|
| TUI (normal use) | `process_file_subprocess` | `--one-file` | yes | arms |
| bare script | `process_file_safe` | none | no | no-ops |

So `Default_SubTool7.stl` ran 3,106 s uncapped because the **bare script** was
used to launch that run, not because the machinery is dead. Under the TUI it
would have been capped.

### Fixed in this session

- **Arming widened the cap it was meant to enforce.** `_arm_mesh_alarm`
  overwrote any pending alarm, so a split replaced the file budget with
  `_part_cap(n)` — a larger number. Measured on Millenium_Falcon at
  `TIMEOUT_PART=20`: `t=0.0s ARM 20s (whole mesh)` → `t=3.5s ARM 100s (split)`.
  A file-level budget could never fire on any file that split, and more shells
  meant more allowance. Now tightens only.
- **Nothing cancelled the alarm.** No `signal.alarm(0)` existed anywhere.
  Harmless under the TUI (each file has its own process) but the bare-script
  pool reuses a worker across files with `max_tasks_per_child` deliberately
  unset, so an alarm armed for file N fired during file N+1 and would mark an
  innocent file `.timeout.stl`. Demonstrated, then closed with
  `_cancel_mesh_alarm()` on the single exit path of `process_file_safe`.

### Still open

- **The bare-script path still has no cap at all** — it arms nothing, because
  `_ONE_FILE` is None there. Deliberately left alone: arming it would have
  killed SubTool7 at 600 s, and that file repairs correctly at 3,106 s.
- **So: what is the timeout FOR?** 600 s has no measurement behind it.
  PyMeshFix runtime tracks defect count more than face count (33,353 nm →
  3,080 s; 2,055 nm → 289 s), so a flat per-mesh number is the wrong shape. A
  timeout should stop a *hang*, and nothing observed so far was actually hung.

**What would settle it:** cost-per-defect figures from several large meshes,
then choose between a scaled budget, a much larger flat cap, or dropping the
concept and relying on the user noticing a stuck run.

---

## 2. PyMeshFix is invisible while it runs

The most expensive step logs one line on entry and one on exit. Measured gaps
on the 2026-09-12 run:

```text
3,079.8 s   Default_SubTool7.stl        step E -> pymeshfix: nm=0 open=0   (succeeded)
  420.5 s   whole-costume01.stl         part: ...part.0.stl -> next line
  289.3 s   ~whole-costume01...part.0   step E -> pymeshfix result
```

During a 51-minute silence a reader cannot tell working from hung — the exact
judgement the timeout question above depends on. At minimum: a start line
carrying mesh size and defect count so the wait is predictable. Better: a
progress or heartbeat line.

---

## 3. Skip reasons are computed and then discarded

757 skip rows in the summary, **one** `skip` line in the whole step log, and
the summary row carries no reason:

```text
NoirSpider_BUST_Stand.stl|skip|0.0||||||none||
```

`_process_file_impl` computes a precise reason (`already fixed: …`,
`previously failed — delete X to retry`, and three more) and returns it in the
result dict, but `log_summary` has no column for it. Add `reason` to
`_SUMMARY_COLUMNS` — appended last, the same trick that kept `blender_runs`
backward-compatible with older summary files.

---

## 4. `_MIN_SHELL_FACES` admits and drops inconsistently

`_MIN_SHELL_FACES = 100`, yet the 2026-09-12 Falcon split kept parts of **38**
and **36** faces as real parts while the floor should have dropped them as
debris. `part.2` at 160 is correctly admitted. Unexplained — the floor is
applied inside `split_shells`, and the reason these survived has not been
traced. Worth knowing before trusting the split to decide what is a part and
what is debris.

---

## 5. Millenium_Falcon — REOPENED and fixed, was wrongly closed

The previous entry here said "Closed — the user prefers to repair such files by
hand", and its part table was wrong. Corrected measurements (2026-09-12, after
the split):

```text
part.0   90,712 tris   nm=0  open=0   117.45 x 35.60 x 155.00 mm   <- whole model
part.1      156 tris   nm=0  open=0     5.67 x  5.76 x   3.15 mm
part.2      160 tris   nm=60           7.25 x  1.01 x   6.46 mm   <- fails
part.3       38 tris   nm=0  open=0     0.72 x  0.52 x   3.08 mm
part.4       36 tris   nm=0  open=0     9.28 x  0.98 x   4.20 mm
```

`part.0` carries the entire model bbox and repairs cleanly. The old "nm=147,448,
unsalvageable" framing described the *undecimated source*, before the split —
after splitting, 99.2% of the geometry is fine and one 160-triangle fin was
failing the file.

`part.2` is **not** debris: zero degenerate triangles, no sub-micron edges,
median edge 0.84 mm, 108 mm² of surface. A real fin, genuinely unrepairable.

Fixed in `d1e6ccd`: a partial split now merges what repaired into
`<name>.open.stl` (status `open`), drops the failed part, and still copies the
source to `.failed.stl`. Verified: 90,942 tris, nm=0, open=0, bbox matching the
source to 0.01 mm.

**Still open:** the two real Falcons in the collection have not been
regenerated — that means deleting their `.failed.stl` fallbacks, which is the
user's call.

---

## 6. Scale-aware bbox flag

Unbuilt. The bbox-drift flag is absolute, so sub-layer movement on small models
is reported alongside real damage. Reusing `MIN_LAYER` would have cut the
2026-09-12 run's 5 flags to 3, dropping two `imp_stand` entries at 0.236 mm and
0.181 mm.

---

## 7. Refactor into single-purpose modules — Code Design Guidelines

### The destination

A pipeline that reads as prose, with the details hidden behind well-named
functions. The user's sketch:

```python
stlFile = readFileAsStl(...)
if stlFile.needDecimation():
    stlFile = decimateFile(stlFile)
```

Single-purpose modules — `blender_handler`, `pymeshfix_handler`, and so on — so
that learning how a file is read means opening `readFileAsStl`, and finding
*that* is not a wall of text either.

### Where the current code already fits that shape

Grouping the 75 existing functions by what they touch:

```text
runner       16 fns  1455 lines   ← the hard part
split/merge  13 fns   447 lines
(other)      18 fns   428 lines
mesh_io      11 fns   331 lines
blender       4 fns   163 lines
logging      10 fns   116 lines
decimate      2 fns    41 lines
pymeshfix     1 fn     33 lines
```

`blender`, `mesh_io`, `split/merge` and `logging` are already cohesive and could
move almost as-is. Two honest caveats:

- **A handler module per tool is not symmetric.** `pymeshfix` is one function of
  33 lines; a `pymeshfix_handler` module would be a file with one function in
  it. Worth grouping it with the other repair passes instead, or leaving it
  until it grows.
- **`runner` is 1,455 lines and will not decompose by grouping.** It holds the
  pool, the child spawn, the alarms, the budgets and the watchdog — the genuine
  complexity. It needs designing, not sorting.

### Start with the one function, not the module split

```text
_process_file_impl   826 lines, nesting depth 8, 91 if-statements, 23 returns
                     — 27% of the module in a single function
```

That function *is* the pipeline the sketch describes, written out longhand.
Extracting its steps produces the readable version directly; moving files around
first would relocate it unchanged. So: functions first, then modules.

### The agreed design (2026-09-13)

Worked out in discussion. A direction, not a specification — deviate where the
code argues back.

**`Stl` — a plain-data DTO.** Facts and paths: format, triangle count, defect
counts, bounds, volume. **No geometry.** A 900k-face mesh is ~45 MB of
triangles, and memory is already the binding constraint (`_BYTES_PER_TRIANGLE`,
`auto_worker_count`, the OOM killer taking workers); an immutable value carrying
arrays would double peak memory at every handoff. Vertex arrays are loaded and
discarded inside each operation, as they are today.

Values are overwritten as newer data arrives. Where a step genuinely needs the
prior value, the DTO simply holds both — `volume` and `volume_before`, `bounds`
and `bounds_before`. Three decisions need that: volume loss after repair (the
Mandy seam recovery), bbox drift, and whether Blender actually ran. No
append-only history mechanism; the cases are few and known.

Keep it serialisable. Results cross worker→parent as plain dicts over a JSON
pipe, so either the DTO is plain data by construction or it gains an explicit
`to_dict()` at the boundary.

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
  module owns.

**Pipeline** — reads as prose, orders the steps, and documents why the order is
load-bearing. The steps look independent and are not: the split is deferred
until after decimation, Blender runs before the seam split, the print-scale gate
precedes the Blender fallback. State those constraints in the module docstrings
or someone will tidy the sequence and silently regress it.

**Runner — exempt.** 1,455 lines of pool management, child spawning, SIGALRM,
watchdogs, OOM attribution and pool restart. No `Stl` flows through it and no
predicate is meaningful (`isRequiredKill(worker)` is nonsense). It is a state
machine over processes; forcing handler shapes onto it would be worse than
explicit imperative code with good names.

### Still open in this design

- **Where the shared budget arithmetic lives.** Decimation, repair and Blender
  draw from one mesh budget, and Blender's share depends on what earlier steps
  spent (`budget − elapsed − reserve`). That is cross-cutting: if each module
  owns its own timeout policy the arithmetic has no home, or gets duplicated.
- **Where the seam-recovery trigger sits.** It fires when PyMeshFix *succeeded
  but deleted geometry* — a fact about the transition, not about the mesh before
  or after. It is the decision this shape handles least naturally, and it is not
  a corner case: it is the Mandy fix.

### Guidelines (from the user — guidelines, not hard rules; use judgment)

**1. Self-sufficient functions**

- If functionality can be logically separated and has a single purpose,
  consider extracting it into a function.
- Function names should clearly describe what the function does.
- If an `if`, loop, or similar control-flow block contains multiple lines of
  meaningful logic, consider moving that logic into a function.
- Avoid splitting code when doing so makes it harder to understand.

**2. Cohesive modules**

- Functions related to the same logical entity or concept are candidates to be
  grouped into a module.
- Keep related data and the behavior operating on that data in the same logical
  place.
- The goal is encapsulation of knowledge and responsibility, not simply
  restricting access.

**Guiding principle:** keep related things together, separate distinct
responsibilities, and make the code's intent clear through structure and naming.

### How these apply here

Depth-8 nesting means control-flow blocks holding substantial logic — squarely
guideline 1. The step boundaries are already marked by comments
(`# Step C — decimate…`), which is the code saying where the functions want to
be. Suggested order, each its own commit with the tests run between:

1. step B / B2 split-and-repair
2. step C decimation
3. step E0 seam recovery
4. step F Blender fallback

Guideline 2 pays off unevenly. `mesh_io` (read / weld / write / bounds) and
`blender` (script running, budget, route labelling) are genuinely cohesive —
data and the behaviour on it together. A `pipeline` module would just be the
826-line function relocated, so it is worth nothing until guideline 1 has done
its work.

### The caveat matters more than usual here

"Avoid splitting code when doing so makes it harder to understand" is the
binding constraint in this file: **32% of it is comments and docstrings**
carrying measured rationale — why step D was removed, the `RLIMIT_AS` history,
the seam measurements, the decimation cost figures. An extraction that separates
a 20-line explanation from the 5 lines it explains makes the file shorter and
the codebase worse.

### Sizing the steps

Keep each extraction small enough that the 36 end-to-end tests are a meaningful
gate, and run a real mesh through the pool between steps. The tests stayed green
through an entire day during which the SIGALRM cap was armed in zero processes —
they catch broken geometry, not broken integration. See the note below.

`_process_file_impl` also carries shared mutable state across its 23 exit points
(`working`, `nm_src`, `open_src`, `stats`, `temps`, `dst`). Deciding what each
extracted step reads and writes is the actual work; getting it wrong produces a
mesh that looks fine and is subtly wrong.

---

## Note on how the above was verified

Five claims made on 2026-09-12 were wrong in the same way: a mechanism was
verified in isolation and the integration asserted rather than checked.

- the SIGALRM cap (unit test passed; armed nowhere in a real run)
- `result['stdout']` KeyError (isolated `process_file` passed; the runner's
  `open` branch indexes it directly and would have crashed)
- `_time_mod`, a missing function signature, and `shutil` in the TUI — all
  three passed `ast.parse` and failed at runtime
- "Blender finished and the part was failed anyway" — the old log made a *kill*
  look like a completion, and the diagnosis repeated the lie
- "the Falcons are unsalvageable" — repeated several times; 99.2% repairs

Everything checked against the path actually run (the pool, a real result dict,
a real mesh) held up. Everything checked in isolation had a hole. Treat a fix
as unverified until it has been watched working on the pool path.
