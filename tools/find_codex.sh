#!/usr/bin/env bash
set -euo pipefail

if [[ -n ${CODEX_BIN:-} ]]; then
    if [[ -x $CODEX_BIN ]]; then
        printf '%s\n' "$CODEX_BIN"
        exit 0
    fi
    echo "find_codex.sh: CODEX_BIN is not executable: $CODEX_BIN" >&2
    exit 1
fi

if CODEX_PATH=$(command -v codex 2>/dev/null); then
    printf '%s\n' "$CODEX_PATH"
    exit 0
fi

shopt -s nullglob
CANDIDATES=(
    "$HOME"/.vscode/extensions/openai.chatgpt-*/bin/linux-*/codex
    "$HOME"/.vscode-server/extensions/openai.chatgpt-*/bin/linux-*/codex
    "$HOME"/.local/bin/codex
)

for ((INDEX=${#CANDIDATES[@]} - 1; INDEX >= 0; INDEX--)); do
    if [[ -x ${CANDIDATES[INDEX]} ]]; then
        printf '%s\n' "${CANDIDATES[INDEX]}"
        exit 0
    fi
done

echo "find_codex.sh: Codex CLI was not found; set CODEX_BIN to its executable path" >&2
exit 1
