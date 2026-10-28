> **TEMPORARY HANDOFF (2026-10-27) — keel support layout; delete once the
> owner's keel test print is reported.**
> Read [docs/refactor/keel.md](docs/refactor/keel.md) first (what, why,
> numbers, open items). State: `--layout keel` committed (`23150fc`),
> Codex-reviewed PASS; grid layout output unchanged. Waiting for the owner
> to slice `girl_130.keel.3mf` (made in the old session's scratchpad —
> regenerate with `tools/project_python.sh support_3mf.py
> /mnt/sda2/assembly/print/girl_130.3mf --layout keel --max-height 30
> --pitch 5`) and test-print Arm_Left. When the report comes: did keel and
> ribs print as single lines on their feet, did the underside print well
> over the keel, did the feet hold, did it come off in one piece? Then
> decide: curved keels, the provisional 80 % band rule / 1 mm grouping /
> 3-line feet. Check sliced paths only after the CLI's 2.19 mm y shift.
> The Arm_Left break (two failures with the grid, one success without) is
> unexplained; do not claim the keel fixes it. Bambu CLI:
> `/mnt/sda1/BambuStudio_ubuntu22.04-v02.08.02.61-20260820225108.AppImage
> --appimage-extract-and-run --slice N --outputdir DIR --export-3mf X.3mf in.3mf`.
> Start the next task with `tools/reset_codex.sh`.
>
> **TEMPORARY HANDOFF (2026-10-16) — delete once the print result is in.**
> Waiting for the owner's torso test-print report. Printed with
> `support_3mf.py /mnt/sda2/torso.3mf --layout grid --width 0.2 --max-height 30 --pitch 5`
> (gap default 0.2; the owner chose width 0.2 and pitch 5 — 0.2 is half a
> 0.4 mm nozzle line, untried).
> Committed 2026-10-23 (134dac9), both Codex-reviewed PASS:
> 1. Whole-square grid mesh — `libs/gridsupport.py` (`_squares`, `_reach`,
>    unbraced runs, sideways rule now keeps `gap`), report line in
>    `support_3mf.py`, tests in `tests/tests/test_gridsupport.py`, docs
>    `docs/refactor/modules.md` grid paragraph + `TODO.md`. Why: the old grid
>    was lone 0.4 mm pins up to 29 mm tall on torso (175 of 177 pieces).
> 2. Output check before processing — `bambu3mf.check_destination`, called
>    first in `support_3mf.main`; test in `tests/tests/test_support_3mf.py`.
> Focused checks: test_gridsupport, test_support_3mf, test_bambu3mf,
> test_ribsupport, test_basecheck, test_check_3mf all OK.
> When the report comes: did the walls print (0.2 width), stand without
> falling, hold the boot/shin undersides across the 5 mm spans, mark
> surfaces near the extra walls, and come off plate and model in one piece?
> Then decide pitch/width, the "skin" (TODO.md grid item) and a commit.
> `--gap` is clearance, not mesh size (owner mixed them up once; mesh size
> is `--pitch`). Start the next task with `tools/reset_codex.sh`.

# Claude Code instructions

Treat the directory containing this file as the project root. Return to it before project commands; do not derive project paths from the shell's inherited working directory. Start with [README.md](README.md) and follow only the compact documentation needed.

Read only task-relevant sections linked from README; do not preload every
reference or recursively read the archived history in `docs/archive/` or
`../stl-batch-fix.old/` (`archive/`, `design/`, outside this repository) or
historical handoffs.

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
