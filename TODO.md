# Open items

Things worth investigating, with the evidence that prompted them. Nothing here
is a known bug — those get fixed. These are questions where the right answer is
not yet clear.

---

## 1. The timeout configures nothing, and that is currently load-bearing

`process_file_subprocess` — 100+ lines of child-spawn, `communicate(timeout=)`,
`/proc` child-walk and SIGKILL-tree machinery — **is called from nowhere**. The
pool submits `process_file_safe` directly:

```python
future_to_src = {pool.submit(process_file_safe, src): (i, src)   # line ~3540
```

Consequences, all confirmed by process tree on the 2026-09-12 run (bash →
parent → three pool workers, no `--one-file` child anywhere):

- `TIMEOUT_PART` and `TIMEOUT` bind on nothing during a batch run.
- The SIGALRM cap added in `0f1712b` no-ops: `_arm_mesh_alarm` returns early
  unless `_ONE_FILE` is set, and only the `--one-file` entry point sets it. It
  was verified in a unit test and armed in **zero** processes in a real run.
- `_kill_own_children` and the SIGKILL tree teardown are unreachable from the
  pool path.

**Do not simply revive it.** `Default_SubTool7.stl` needed **3,106 s** and
succeeded (nm=33,353 → 0, 18.5 MB written, clean). A working 600 s cap would
have killed it and written a `.timeout.stl` for a file that repairs correctly.
The dead code is the only reason that output exists.

**So the question is what the timeout is FOR**, before making it work:

- 600 s has no measurement behind it.
- PyMeshFix runtime tracks defect count more than face count (33,353 nm →
  3,080 s; 2,055 nm → 289 s), so a flat per-mesh number is the wrong shape.
- A timeout should stop a *hang*, not cap honest work — and nothing observed
  so far was actually hung.

**What would settle it:** cost-per-defect figures from several large meshes,
then decide between a scaled budget, a much larger flat cap, or dropping the
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
