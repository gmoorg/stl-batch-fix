# Claude Code + Codex workflow

For every non-trivial task, Claude Code is the lead orchestrator and Codex is an independent peer validator and reviewer.

A task is non-trivial when it changes behavior, algorithms, architecture, interfaces, tests, configuration, multiple files, or project decisions. A spelling-only or similarly mechanical edit may skip peer validation.

## Roles and independence

- **Claude:** owns the user conversation, workflow state, reconciliation, and final report.
- **Codex:** independently checks intent and plans against the original prompt, repository, tests, and compact docs.
- **Implementer:** exactly one agent edits for a task.
- **Reviewer:** the other agent reviews the completed changes without editing them.

Codex must not merely endorse Claude's framing. It should locate contrary repository evidence and identify omitted requirements. The reviewer must judge the actual diff, not the implementer's summary.

## Phase 1: validate intent before planning

1. Claude reads the original user prompt and only enough repository context to state its understanding.
2. Before writing an implementation plan, Claude sends Codex the original prompt verbatim, its interpretation, proposed acceptance conditions, and relevant paths or facts.
3. Codex independently checks the prompt and repository, then returns `AGREE` or `DISAGREE`, missing requirements, assumptions, ambiguities, evidence, and whether user clarification is materially required.
4. Claude reconciles factual differences from repository evidence. If intent remains materially ambiguous or disputed, Claude asks the user. If they agree, Claude proceeds automatically.

## Phase 2: challenge the plan

1. Claude produces a concrete plan aligned with the agreed interpretation: behavior, files, ordering, failure handling, tests, and stop condition.
2. Claude sends Codex the agreed interpretation and plan under a separate `PLAN` request.
3. Codex independently challenges correctness, architecture, edge cases, security, regressions, unnecessary complexity, test quality, and alignment with the agreed intent.
4. Claude reconciles findings using repository evidence. One focused rebuttal round is normally enough; stop circular discussion.
5. If they agree, implementation starts automatically. Ask the user only when a consequential disagreement or preference remains.

## Phase 3: one implementer, one reviewer

1. Claude assigns exactly one implementer. By default Claude implements and Codex reviews. Claude may assign Codex through `tools/run_codex.sh`; if so, Claude reviews and must not also edit the solution.
2. The implementer completes the agreed plan and runs focused checks with `../.venv/bin/python`.
3. The reviewer receives the original prompt, agreed interpretation, agreed plan, changed files, and test results. The reviewer inspects the working-tree diff independently for correctness, completeness, regressions, security, edge cases, tests, and plan compliance.
4. The reviewer returns `PASS` or concrete findings with file/line evidence. The implementer fixes valid findings; the reviewer checks behavior-changing fixes again.
5. When both agents agree and relevant checks pass, Claude reports completion automatically.

## Escalate only when

- intent remains materially ambiguous;
- repository evidence cannot resolve a consequential agent disagreement;
- a consequential choice depends on user preference;
- an action is destructive, irreversible, externally visible, or otherwise requires explicit approval.

Agreement does not override an approval required by the execution environment. Routine agreement does not require user approval.

## Codex calls

Claude uses `tools/ask_codex.sh` for read-only validation and review. Start each request with one phase label:

```text
PHASE: INTERPRETATION
ORIGINAL USER PROMPT:
...
CLAUDE'S UNDERSTANDING:
...
PROPOSED ACCEPTANCE CONDITIONS:
...
```

```text
PHASE: PLAN
AGREED INTERPRETATION:
...
PROPOSED PLAN:
...
```

```text
PHASE: REVIEW
ORIGINAL USER PROMPT:
...
AGREED INTERPRETATION AND PLAN:
...
IMPLEMENTATION SUMMARY AND CHECKS:
...
Inspect the working-tree diff.
```

If Codex is the implementer, Claude invokes `tools/run_codex.sh` once the plan is agreed, then performs the review itself. Claude prevents endless discussion and finishes with the implementation summary, checks, peer-review result, and unresolved risks.
