#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if (($#)); then
    REQUEST=$*
else
    REQUEST=$(cat)
fi

case "${REQUEST%%$'\n'*}" in
    "PHASE: INTERPRETATION"|"PHASE: PLAN") ROLE=planning ;;
    "PHASE: REVIEW") ROLE=review ;;
    *)
        echo "ask_codex.sh: first line must be PHASE: INTERPRETATION, PHASE: PLAN, or PHASE: REVIEW" >&2
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

exec "$SCRIPT_DIR/project_python.sh" "$SCRIPT_DIR/codex_session.py" "$ROLE" <<< "$PROMPT"
