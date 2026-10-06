# Claude Code instructions

> **Temporary handoff (2026-10-06).** Read this, then delete this note when
> the next task's first commit lands.
>
> - **State:** branch `error-fixes`, pushed, tree clean. Today: `4f6e3b8`
>   children die with a SIGKILLed runner (`--parent-pid`,
>   `proctree.exit_with_parent`, PR_SET_PDEATHSIG; Codex review PASS);
>   `966bfd5` Blender deficiencies documented (`libs/blender.py`
>   docstring, `blender` row in modules.md); then the TODO task below.
>   Full suite: 980 tests, ~13 min.
> - **Next (owner's choice):** [TODO](docs/refactor/TODO.md), "Intake
>   conversion without Blender": convert OBJ and ASCII STL to binary STL in
>   our own code. New task: run `tools/reset_codex.sh`, then
>   INTERPRETATION with full context. The TODO section holds the evidence
>   (Blender probes) and every decision so far. Decided: keep source
>   coordinates (no axis change); stale exports accepted; triangles and
>   quads only, a quad split on the diagonal that keeps both triangles
>   inside; an OBJ face with 5+ vertices gets a `FAILED` marker (full
>   source copy) — a new marker path, since an intake conversion failure
>   is only a diagnostic today.
> - **Open, ask one at a time:** (1) an ASCII STL facet without exactly
>   three vertices: also `FAILED`? (asked, not answered); (2) keep writing
>   exports to `<input>/<export dir>` or load OBJ/ASCII directly in
>   `mesh_io`.
> - **Code to read:** `mesh_io.kind` / `probe` / `Mesh.needs_conversion`
>   (OBJ and ASCII STL get no triangle count until converted);
>   `converter.prepare` (`pending` list, `convert_one`, `_conversion_failure`);
>   `batch_repair._convert_logged` and the intake block with
>   `intake_runner`; `indicators.export_path` / `check` (`EXPORT_READY` by
>   existence); `batch_repair._preflight` (invalid intake meshes become
>   `rejected`, a diagnostic only, no marker).
> - **Noted, not tasks:** with `-W always::ResourceWarning`, steplog's
>   never-closed handle (owner: harmless) and unclosed readers in
>   `test_mesh_io.py` (lines ~308, 315, 321, 864).

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
