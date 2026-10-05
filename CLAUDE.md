# Claude Code instructions

> **Temporary handoff (2026-10-05).** Read this, then delete this note when
> the next task's first commit lands.
>
> - **State:** branch `error-fixes`, pushed at `5ca221a`, tree clean.
>   Today: `e590ec4` PLY decimation cache at `<input>.decimated/` (cache key
>   is the source's lexical path; NaN/inf triangles dropped at load);
>   `5ca221a` `mesh_io.load` reads 1024-triangle chunks and also drops
>   coincident-corner triangles (`Mesh.load_drops`, one step note in the
>   pass that loaded the STL). Full suite was 950 tests.
> - **Data:** the old `/mnt/sda2/STL/Fixing/stl-decimated/` and two stray
>   experiment PLYs were deleted by the owner. The next batch run
>   re-decimates every model over target into `/mnt/sda2/STL/Fixing.decimated/`
>   (Bat Girl ~73 s). The owner's local `batch_repair.toml` (ignored by git)
>   still says the cache is `<input>/stl-decimated/`; only the comment is
>   stale.
> - **Next:** [TODO](docs/refactor/TODO.md) priority 1, float64
>   `Geometry.verts`, as its own plan (weld stays on float32 bits; convert
>   the welded table; then re-fit `jobmemory.prepare_bytes`). Then priority 2.
>   New task: run `tools/reset_codex.sh`, then INTERPRETATION with full context.
> - **Owner decisions this session:** loader correctness is tested with
>   known-property fixtures, not an old-loader oracle (vertex order is free);
>   load drop counts appear only in the step note, a cached PLY is "a new
>   input"; remove unneeded allocations even when not at the peak; discuss
>   open decisions one point at a time.

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
