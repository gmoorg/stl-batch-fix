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
>
> The same applies to this file. It went five commits stale on 2026-09-13 with
> four of its seven items already shipped.

---

## 1. Refactor into single-purpose modules — Code Design Guidelines

The one substantial item left.

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
runner       16 fns  1455 lines   <- the hard part
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

### The design lives in `REFACTOR_DECISIONS.md`

The target shape — the `Stl` DTO, tool modules, operation modules, the predicate
and injection rules — and the eleven decisions that refine it are recorded
there, not here. **This file holds what is left to do and how to go about it;
that one holds what was decided and why.**

A copy of the design used to sit here and went stale within a day: it still said
the runner was exempt (D1 says the opposite) and listed two questions as open
that D9 and D10 had closed.

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
they catch broken geometry, not broken integration. See the note at the bottom.

`_process_file_impl` also carries shared mutable state across its 23 exit points
(`working`, `nm_src`, `open_src`, `stats`, `temps`, `dst`). Deciding what each
extracted step reads and writes is the actual work; getting it wrong produces a
mesh that looks fine and is subtly wrong.

---

## 2. Loose ends, not investigations

**The two real Falcons have not been regenerated.** `d1e6ccd` made a partial
split merge what repaired into `<name>.open.stl` instead of discarding the file,
so both would now produce a ~99.2% model instead of a broken source copy.
Rerunning them means deleting their `.failed.stl` fallbacks — the user's call.

**`_MIN_SHELL_FACES` behaved unexpectedly once.** The 2026-09-12 Falcon split
kept parts of 38 and 36 faces against a floor of 100. Demoted from an
investigation on 2026-09-13: every observation rests on one pathological file —
393,198 triangles, nm=147,448, one real body plus four specks — and reasoning
about "part versus debris" from a model with no real parts is backwards. What
would make it a real question is a model with genuine small parts (magnet pegs,
locating pins) where dropping a 38-face shell visibly loses something. Cheap to
check: every `split: N shells →` line in the backup logs records what came out.

**Test-log pollution.** ~376 lines in `/mnt/sda2/STL/Fixed/repair_log.tsv` are
from `/tmp` test runs (fixture names, `~`-prefixed parts). The 70 real lines are
the four path-prefixed collection files. The 2026-09-12 run is preserved in
`_logbackup/`.

---

## Done (2026-09-13)

Kept short — the reasoning lives in `STL_BATCH_FIX_DESIGN.md` and the commit
messages.

| was | outcome |
|---|---|
| Two runners with different timeout behaviour | `f7623ff` — `TIMEOUT_PART` back to 600 s, bare script now submits `process_file_subprocess` like the TUI, and `--one-file` children call `retarget_logs()` |
| PyMeshFix invisible while it runs | `04d7666` + `1620e78` — a start line stating size, defects and expected duration, then `repair()` run as its three logged sub-steps |
| Skip reasons computed and discarded | `442c9f7` — a `reason` column, appended last so old summaries still parse |
| Bbox flag absolute in mm | `94ad2f4` — `BBOX_TOLERANCE_PCT` = 0.7 % of the bbox diagonal, floored at 0.1 mm, because the user rescales models after repair |

**What is not proven about them:**

- `TIMEOUT_PART = 600` diverts one known outlier (`Default_SubTool7`, 3,080 s).
  Whether that is the only such file is unmeasured — the `.timeout.stl` markers
  after the next full run are the answer.
- The PyMeshFix sub-step timings read `0.0s` on every fixture, so which phase
  dominates a slow mesh is still unknown. The only large mesh available
  collapses in `fill_holes`.
- Only the `already fixed` skip reason was exercised end to end; the other four
  need a signal file on disk to trigger.

---

## Note on how the above was verified

Claims that were wrong on 2026-09-12/13 shared one shape: a mechanism verified
in isolation, the integration asserted rather than checked.

- the SIGALRM cap (unit test passed; armed nowhere in a real run)
- `result['stdout']` KeyError (isolated `process_file` passed; the runner's
  `open` branch indexes it directly and would have crashed)
- `_time_mod`, `_time_now`, a missing function signature, and `shutil` in the
  TUI — all passed `ast.parse` and failed at runtime
- `_BBOX_TOLERANCE_FLOOR` referenced by the CLI block before its definition —
  import, parse and all 36 tests green, `NameError` on every real invocation
- "Blender finished and the part was failed anyway" — the old log made a *kill*
  look like a completion, and the diagnosis repeated the lie
- "the Falcons are unsalvageable" — repeated several times; 99.2 % repairs
- a PyMeshFix sub-step order "verified" against a mesh that collapses to zero
  faces either way, in an order `repair()` does not use

Four consecutive verification probes also failed on their own mistakes — a grep
for a string that never appears, a process snapshot of a run that had already
finished, a launch from a drifted working directory, and a `sed` address that
did not match. Each looked like evidence about the code.

Everything checked against the path actually run — the pool, a real result dict,
a real mesh, an argument-bearing invocation — held up. Everything checked in
isolation had a hole.
