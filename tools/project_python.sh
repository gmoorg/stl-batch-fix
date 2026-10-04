#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON_BIN=$(cd -- "$PROJECT_DIR/.." && pwd)/.venv/bin/python

if [[ ! -x $PYTHON_BIN ]]; then
    echo "project_python.sh: project interpreter not found: $PYTHON_BIN" >&2
    exit 1
fi

cd "$PROJECT_DIR"
exec "$PYTHON_BIN" "$@"
