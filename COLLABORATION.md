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
2. The implementer completes the agreed plan and runs focused checks through `tools/project_python.sh`, which resolves the project environment independently of the caller's working directory.
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

Claude uses `tools/ask_codex.sh` for read-only validation and review. The first line must be exactly one phase label (later quoted phase labels do not affect routing):

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

## Session continuity

There is one active collaboration task per checkout. Run `tools/reset_codex.sh`
before beginning a new task, including the first task after adopting this workflow.
Do not reset between phases or while addressing review findings.

- `INTERPRETATION` and `PLAN` share a persistent planning session. Follow-up calls
  resume its exact ID, so previous decisions and objections remain available.
- The first `REVIEW` starts a separate session with fresh conversation context.
  Supply the original prompt, agreed interpretation and plan, changed files, and
  checks because the reviewer does not inherit planning history. Further `REVIEW`
  calls resume that reviewer to check fixes; tell it what changed since last time.
- `tools/run_codex.sh` uses a separate persistent implementation session. Include
  the agreed plan in its first request. Claude independently reviews its changes;
  Codex must not review its own implementation through the review wrapper.

The launcher stores explicit IDs in the ignored `.codex-collaboration/` directory
and resumes them with `codex exec resume`. It never selects `--last`. Planning and
review stay read-only; implementation retains automatic approval review. Calls
are serialized by a checkout lock; a concurrent call or reset fails with an
actionable message. Wait for the active call to finish before retrying.

Agent responses remain plain text on stdout; session notices and errors go to
stderr. A failed call returns a nonzero status and retains any captured session
ID for a deliberate retry. Do not interpret partial output as approval. An invalid
or unavailable saved session must be investigated or explicitly reset, never
silently replaced. Sessions are now persisted by Codex; clearing pointers does
not delete their saved transcripts. Continuity reduces repeated setup but long
histories still consume context.

## Starting both AIs clean

1. If continuing unfinished work, save a short handoff in the repository with the
   original task, accepted decisions, changed files, checks, and next steps.
2. After active calls finish, run `tools/reset_codex.sh` from the repository root
   (or invoke it by absolute path from elsewhere). This clears all three Codex
   session pointers without changing project files or deleting past transcripts.
3. Run `/clear` in the interactive Claude Code session. The reset script cannot
   clear Claude's conversation. If also using a separate Codex app chat, start a
   new task/chat there; the CLI sessions do not share that conversation.
4. Ask Claude to read `CLAUDE.md`, `COLLABORATION.md`, and the handoff if present.
   Its next Codex call starts fresh and must include the task context.

Clearing Claude alone does not clear the saved Codex session pointers. Repository
instructions, files, and any separately configured agent memory remain available
after a conversation reset.
