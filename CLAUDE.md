# Claude Code instructions

> **Temporary handoff (2026-10-07).** Read this, then delete this note when
> the next task's first commit lands.
>
> - **State:** branch `error-fixes`; `c914a25` (the spread investigation)
>   and this note are committed, **not pushed**. Option (a) from the
>   previous handoff is done, Codex-reviewed (PASS). No production code
>   changed.
> - **Finding:** the sphere r 132 5³ peak spread (3.62–3.81 GB) follows
>   glibc malloc's policy. The weld alone is deterministic (24 runs,
>   2.641 GB) and its inputs hash identically; with
>   `GLIBC_TUNABLES=glibc.malloc.mmap_threshold=131072` four interleaved
>   whole runs all peak at 3.239 GB, peak moved ~22 s before the end (out
>   of the weld), ~12 s slower. Mechanism not isolated (the tunable also
>   freezes the trim threshold); weld variation under it is hidden; why
>   the old code was steady is unmeasured. Details: reconstruction.md "The
>   spread follows glibc malloc's policy"; TODO.md winding entry.
> - **Open decisions for the owner, one at a time (none started):**
>   1. set the tunable for the batch (production change: lower, steady
>      peak vs ~12 s per large run; would need a decision where to set it —
>      env of the batch process or its children)?
>   2. then option (b): reindex the weld in place/in chunks. Judge it by a
>      probed weld phase (under the tunable the whole-run peak is not the
>      weld) or against the normal 3.62–3.81 range.
>   Not wanted now: a C/numba rewrite of the numpy glue (discussed; keep
>   numpy, chunk where a measured peak warrants it); the STL load weld's
>   `srt`/`ids` temporaries (~420 MB at 7 M triangles, not the peak).
> - **Tools:** `tools/experiments/weld_spread.py` (interleaved fresh-child
>   runs, 2 ms sampler, swap/THP counters, output hashes, `weld-inputs`
>   diagnostic with mallinfo2) and `weld_alone.py` (`save` the weld inputs
>   from one run, `run` the weld alone); usage in their docstrings, data in
>   the `*_2026-10-07.jsonl` beside them. Saved weld inputs lived in the
>   session scratchpad; regenerate with `weld_alone.py save` (~8 min). A
>   full sphere r 132 5³ run takes ~7.5 min; never run two at once.
> - **Codex:** this task's planning and review sessions are finished; run
>   `tools/reset_codex.sh` before the next task.

Treat the directory containing this file as the project root. Return to it before project commands; do not derive project paths from the shell's inherited working directory. Start with [README.md](README.md) and follow only the compact documentation needed.

Read only task-relevant sections linked from README; do not preload every
reference or recursively read the archived history in `../stl-batch-fix.old/`
(`archive/`, `design/`, outside this repository) or historical handoffs.

Review the user's proposals honestly and independently, not with automatic
agreement. Understand the whole proposal and its purpose, then explain any
evidence-backed concerns and alternatives. Follow the shared
[honest-review rule](COLLABORATION.md#honest-review-of-user-proposals).

For every non-trivial task, follow [COLLABORATION.md](COLLABORATION.md). Act as lead orchestrator. Before planning, ask Codex to validate your interpretation with `tools/ask_codex.sh`. Then have Codex challenge the plan. Proceed automatically when agreement is reached. Assign exactly one implementer and require independent review by the other agent. Ask the user only for unresolved material ambiguity or disagreement, consequential preference, or required approval for destructive/high-risk action.

Run Python through `tools/project_python.sh`, which resolves `/mnt/sda2/python/.venv/bin/python` independently of the caller's working directory.

Before each new collaboration task, run `tools/reset_codex.sh`. Do not reset
between phases or fix reviews. Interpretation and planning reuse one Codex
session; the first review starts separately and later reviews resume it. Pass
complete task context to the first call of each role, then concise updates on
follow-ups. Implementation through `tools/run_codex.sh` has its own session and
requires your independent review. Never run collaboration calls concurrently.

When the user requests both AIs clean, save a handoff if needed, reset the Codex
pointers after active calls finish, and tell the user to run `/clear` in Claude.
Clearing Claude alone does not reset Codex. Follow the full procedure in
`COLLABORATION.md`; do not claim either reset clears files or configured memory.
