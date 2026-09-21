# Codex instructions

Start with [README.md](README.md) and follow only the compact documentation needed.

Claude Code is the lead orchestrator for every non-trivial task under [COLLABORATION.md](COLLABORATION.md). When invoked through `tools/ask_codex.sh`, remain read-only and independently validate the labeled phase:

- `INTERPRETATION`: check the original prompt and repository before judging Claude's understanding.
- `PLAN`: challenge correctness, architecture, edge cases, security, regressions, complexity, tests, and intent alignment.
- `REVIEW`: inspect the actual working-tree diff and return `PASS` or evidence-backed findings.

Do not edit as validator/reviewer. If explicitly invoked through `tools/run_codex.sh`, act as the sole implementer for the agreed plan; Claude then reviews. Run project Python commands through `tools/project_python.sh`. Do not use legacy `stl_batch_fix.py` as repair-design authority while the refactor is unfinished.

Session history is scoped to one task and role: interpretation/planning share a
session, review starts separately and resumes for fixes, and implementation has
its own session. Recheck current files when evaluating changes; conversation
history is not proof of the current diff. See `COLLABORATION.md` for the reset
procedure. `tools/reset_codex.sh` clears all role pointers; Claude's `/clear` and
any separate Codex app chat must be handled separately.
