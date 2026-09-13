# Open items

Things worth investigating, with the evidence that prompted them. Nothing here
is a known bug — those get fixed. These are questions where the right answer is
not yet clear.

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
