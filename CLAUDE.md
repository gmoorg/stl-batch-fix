# Claude Code instructions

> **Temporary handoff (2026-10-07).** Read this, then delete this note when
> the next task's first commit lands.
>
> - **State:** branch `error-fixes`, pushed. Done and Codex-reviewed:
>   `6f75e63` int64 weld keys; `375ca02` `_block_points` (grid filled in
>   place) + `_take` (piece lists emptied as joined, input/tree/keys dropped
>   before the weld). Outputs byte-identical. Figures in
>   reconstruction.md "Memory floor" (last two subsections) and TODO.
> - **Sphere r 132 5³ whole run:** 5.31 → 4.07 → 3.50–3.85 GB (8 runs,
>   median 3.63). Old code (`6f75e63`) is steady (4.066–4.077, 5 runs);
>   the new spread lies entirely in the weld (every run 3.26–3.32 before
>   it). Cause NOT measured; the allocator guess in the doc is untested.
>   join_complication 2³ peaks in the block field (2.94), untouched.
> - **Probed phases now (new):** field 2.22, marching cubes 3.27, join
>   2.54, weld unique 2.58, weld reindex **3.51** (peak), final scan 2.92.
> - **Open decision for the owner (none started):** (a) find the weld's
>   run-to-run spread first, or (b) reindex in place/in chunks (`inv[Fo]`
>   builds a new ~0.7 GB face array while `Fo` lives) — expected to bring
>   the peak to the marching-cubes level (~3.3), not measured. Then the
>   TODO's "streaming each block's output".
> - **Measuring:** fresh process per run; check `/proc/vmstat`
>   `pswpin`/`pswpout` before/after (238 GiB swap is installed; any swap
>   I/O makes VmHWM inconclusive); compare outputs old vs new by dtype,
>   shape and bytes (old = `git worktree` of the previous commit). The
>   plain-run and 2 ms RSS-sampler scripts were scratch files; rebuild them
>   from `tools/experiments/winding_phases.py` (`--plain`, `status()`). Attach
>   a sampler to the python PID (`$!` of the python process), not
>   `pgrep -f`, which matched the wrapping shell once.
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
