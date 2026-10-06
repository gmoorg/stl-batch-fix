# Claude Code instructions

> **Temporary handoff (2026-10-05).** Read this, then delete this note when
> the next task's first commit lands.
>
> - **State:** branch `error-fixes`, pushed, tree clean. Today: `05968d5`
>   float64 `Geometry.verts` (`Geometry` raises `TypeError` on any other
>   dtype; `load` welds on float32 bits then converts once; `read_ply`
>   keeps PyMeshLab doubles; `write` rounds once and refuses values beyond
>   float32 range). Job memory re-measured, constants kept. Then TODO and
>   doc notes only. Full suite: 954 tests, ~14 min.
> - **Next:** [TODO](docs/refactor/TODO.md) priority 1, **record the crash
>   signal**. New task: run `tools/reset_codex.sh`, then INTERPRETATION with
>   full context. Background: [oom-kill-silent.md](docs/errors/oom-kill-silent.md),
>   "Two separate problems", item 1 only (item 2, memory, is out of scope:
>   Azula). Code: `batch_repair.py`, `_Runner` wait/reap block (~line 895):
>   a child with no valid result becomes `marker_cause` `'crashed'` or
>   `'timed_out'` and goes to `_reconcile` (~line 634), whose reasons say
>   only "child crashed". The exit status is never read: after
>   `terminate_and_confirm`, `proc.returncode` < 0 is the signal. Keep:
>   a validated child result is trusted whatever the exit code; say "out of
>   memory" only with kernel/cgroup evidence (SIGKILL also comes from our
>   own timeout and cancellation kills, so tell those apart).
> - **After that:** priority 2, test the `skip_clean` gate on real models
>   together with the NM-only fast path (owner has `skip_clean = true` in
>   the local `batch_repair.toml`; Amidara `base.stl` not yet located).
> - **Owner decisions this session:** Blender and the PLY dialects were out
>   of scope for the float64 task; the Blender double-PLY round trip is
>   noted as unverified (modules.md, `step_blender_repair`). Ideas recorded,
>   not requirements: chunked STL writes; alpha-wrap → decimate → MeshFix
>   when the NM count is high.

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
