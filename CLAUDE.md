# Claude Code instructions

> **Temporary handoff (2026-10-06, owner rebooting).** Read this, then
> delete this note when the next task's first commit lands.
>
> - **State:** branch `error-fixes`, tree clean, local commits not pushed.
>   `9ea6613`: whole winding run re-measured after the scan fix; large
>   outputs now peak in `_weld`'s row-wise `np.unique(edges, axis=0)`
>   (sphere r 132 5³: 5.30 GB whole run, 3.37 → 5.31 GB in that call,
>   23 s); probe script fixed (it held the weld inputs alive); the
>   `skip_clean` gate test closed (default stays off).
> - **Task in progress (owner approved):** one int64 key per vertex in the
>   weld, single path, no row-wise fallback. Codex INTERPRETATION done
>   (planning session saved under `.codex-collaboration/`; do NOT reset —
>   continue with PLAN). Agreed:
>   - `_edge_ids` returns 1-D int64 `((axis·NX + x)·NY + y)·NZ + z` with
>     the GLOBAL grid shape (x, y, z = edge's global lower corner). This
>     order equals today's lexicographic `(axis, x, y, z)` rows, so the
>     output must be **array-identical** to the old weld (vertices, faces,
>     order, the same first copy kept per edge) — not just equivalent.
>     Corner-major `corner·3 + axis` was rejected: it reorders vertices,
>     and decimation depends on order.
>   - `_weld`: 1-D `np.unique(keys, return_index, return_inverse)`;
>     pieces joined with `np.concatenate`, not `vstack`.
>   - Guard: largest key is `3·prod(shape) − 1`; check with Python ints
>     before any key arithmetic / big allocation, raise (never collide).
>     `_grid`'s existing bound (dims < 2³¹−1, product < 2⁶²) does not
>     imply it. Keep the per-block 32-bit packed-corner check and the
>     marching-cubes edge-map validation (different constraints). Test
>     the boundary on both sides.
>   - Also: tests in `tests/tests/test_winding.py` `TestWeld` (4-column
>     edges / `_edge_ids` rows today; `with_nm_pair` wraps `_weld`), the
>     probed copy `tools/experiments/winding_phases.py` + `SOURCE_SHA`,
>     docs (reconstruction.md algorithm step 7 and "Memory floor", TODO
>     Reconstruction item, modules.md if it mentions edge ids).
>   - Measure before/after: plain whole-run peak + time on sphere r 132 5³
>     and join_complication 2³ (`/mnt/sda2/join_complication.stl`);
>     compare old vs new weld output array-for-array. Peak reduction must
>     be measured; time gain is an observation only.
> - **Next steps:** PLAN to Codex → implement (Claude) → focused tests →
>   measurements → Codex REVIEW → commit with docs.

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
