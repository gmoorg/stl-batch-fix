#!/usr/bin/env bash
# fix.sh — run a batch repair with the current batch_repair.toml.
#
# batch_repair.py takes no arguments: every option lives in batch_repair.toml
# beside it. On first use, copy the documented example and edit it:
#   cp batch_repair.example.toml batch_repair.toml
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ $# -gt 0 ]]; then
    echo "fix.sh: takes no arguments; edit $ROOT/batch_repair.toml instead" >&2
    exit 2
fi

if [[ ! -f $ROOT/batch_repair.toml ]]; then
    echo "fix.sh: no config at $ROOT/batch_repair.toml" >&2
    echo "        first time: cp batch_repair.example.toml batch_repair.toml, then edit it" >&2
    exit 1
fi

exec "$ROOT/tools/project_python.sh" batch_repair.py
