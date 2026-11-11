#!/usr/bin/env bash
# fix_files.sh — repair selected STL/OBJ files beside their sources.
#
#   ./fix_files.sh [--alpha] [--] FILE...    foo.stl -> foo_fixed.stl
#   ./fix_files.sh --large [--] FILE         one big file, RAM+swap, no time limit
#
# Options come from batch_repair.toml, as for ./fix.sh. The right-click
# entries (tools/nemo/) set STL_FIX_HOLD=1 so the terminal window waits for
# Enter instead of closing over the summary.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# project_python.sh runs from the repository, so a relative FILE must be
# made absolute against the caller's folder first. Options (before `--`) pass
# through unchanged.
args=()
files_only=0
for arg in "$@"; do
    if (( !files_only )) && [[ $arg == -- ]]; then
        files_only=1
        args+=("$arg")
    elif [[ $arg == /* ]] || { (( !files_only )) && [[ $arg == -* ]]; }; then
        args+=("$arg")
    else
        args+=("$PWD/$arg")
    fi
done
set -- "${args[@]}"

if [[ ! -f $ROOT/batch_repair.toml ]]; then
    echo "fix_files.sh: no config at $ROOT/batch_repair.toml" >&2
    echo "              first time: cp batch_repair.example.toml batch_repair.toml, then edit it" >&2
    status=1
elif [[ ${STL_FIX_HOLD:-} != 1 ]]; then
    exec "$ROOT/tools/project_python.sh" fix_files.py "$@"
else
    set +e
    "$ROOT/tools/project_python.sh" fix_files.py "$@"
    status=$?
    set -e
fi

if [[ ${STL_FIX_HOLD:-} == 1 ]]; then
    echo
    echo "Finished with status $status."
    read -r -p 'Press Enter to close.' _ || true
fi
exit "$status"
