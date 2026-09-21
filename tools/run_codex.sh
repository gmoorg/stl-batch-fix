#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
CODEX_EXEC=$("$SCRIPT_DIR/find_codex.sh")
PYTHON_BIN=$(cd -- "$PROJECT_DIR/.." && pwd)/.venv/bin/python

if [[ ! -x $PYTHON_BIN ]]; then
    echo "run_codex.sh: project interpreter not found: $PYTHON_BIN" >&2
    exit 1
fi

if (($#)); then
    REQUEST=$*
else
    REQUEST=$(cat)
fi

PROMPT=$(cat <<EOF
Follow COLLABORATION.md as the sole implementer for this agreed task. The interpretation and plan have already passed independent validation. Implement exactly that plan, run focused checks with $PYTHON_BIN, and report changed files, checks, and remaining risks. Do not expand scope. Claude Code will independently review your diff.

$REQUEST
EOF
)

exec "$CODEX_EXEC" exec --cd "$PROJECT_DIR" --sandbox workspace-write --approve-for-me --ephemeral --color never "$PROMPT"
