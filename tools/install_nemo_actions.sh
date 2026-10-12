#!/usr/bin/env bash
# install_nemo_actions.sh — add "STL fix" and "STL alpha fix" to Nemo's
# right-click menu for .stl/.obj files (Cinnamon's file manager).
#
# Copies tools/nemo/*.nemo_action to ~/.local/share/nemo/actions/ with this
# checkout's path filled in. Safe to rerun; rerun after moving the checkout.
# Remove the two files there to uninstall.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DEST=${XDG_DATA_HOME:-$HOME/.local/share}/nemo/actions

# The path goes inside "..." in Exec= and through sed's s|...|...|.
if [[ $ROOT == *[\"\\\$\`%\|\&]* || $ROOT == *$'\n'* ]]; then
    echo "install_nemo_actions.sh: cannot embed this path in a Nemo action: $ROOT" >&2
    exit 1
fi

mkdir -p "$DEST"
for template in "$ROOT"/tools/nemo/*.nemo_action; do
    target=$DEST/$(basename "$template")
    sed "s|@ROOT@|$ROOT|g" "$template" > "$target.tmp"
    mv "$target.tmp" "$target"
    echo "installed $target"
done
