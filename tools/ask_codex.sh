#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
CODEX_EXEC=$("$SCRIPT_DIR/find_codex.sh")

if (($#)); then
    REQUEST=$*
else
    REQUEST=$(cat)
fi

case "$REQUEST" in
    *"PHASE: INTERPRETATION"*|*"PHASE: PLAN"*|*"PHASE: REVIEW"*) ;;
    *)
        echo "ask_codex.sh: request must include PHASE: INTERPRETATION, PLAN, or REVIEW" >&2
        exit 2
        ;;
esac

PROMPT=$(cat <<EOF
Follow COLLABORATION.md as Claude Code's independent peer. This is read-only: do not edit files. The request must identify PHASE: INTERPRETATION, PLAN, or REVIEW.

For INTERPRETATION, independently read the original prompt and relevant repository evidence before judging Claude's understanding. Return AGREE or DISAGREE, missing requirements, assumptions, ambiguities, evidence, and whether user clarification is materially required.

For PLAN, challenge correctness, architecture, edge cases, security, regressions, unnecessary complexity, tests, and alignment with the agreed interpretation. Return AGREE or DISAGREE with evidence and any consequential unresolved decision.

For REVIEW, inspect the actual working-tree diff and relevant tests. Return PASS or concrete findings ordered by severity with file/line evidence. Do not accept an implementation summary as proof.

$REQUEST
EOF
)

exec "$CODEX_EXEC" exec --cd "$PROJECT_DIR" --sandbox read-only --ephemeral --color never "$PROMPT"
