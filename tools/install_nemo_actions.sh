#!/usr/bin/env bash
# install_nemo_actions.sh — add an "STL fix" submenu to Nemo's right-click
# menu for .stl/.obj files (Cinnamon's file manager), holding "STL fix",
# "STL alpha fix" and "STL fix large (RAM+swap)".
#
# Copies tools/nemo/*.nemo_action to ~/.local/share/nemo/actions/ with this
# checkout's path filled in, then puts them in the submenu through Nemo's
# layout file (tools/nemo_layout.py; other entries there are kept). Safe to
# rerun; rerun after moving the checkout. To uninstall, remove the three
# files there and the "STL fix" submenu in nemo-action-layout-editor.
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

# Menu order: the everyday entry first. The system Python, as Nemo's own
# layout editor uses: its GTK bindings check accelerators the same way.
/usr/bin/python3 -I "$ROOT/tools/nemo_layout.py" \
    stl-fix.nemo_action stl-alpha-fix.nemo_action stl-fix-large.nemo_action
echo "Close every Nemo window (or run: nemo -q) for the menu to change."
