#!/usr/bin/env python3
"""Put this project's Nemo actions in one "STL fix" submenu.

    nemo_layout.py ACTION_FILE_NAME...

Nemo 6 keeps its right-click layout in `$XDG_CONFIG_HOME/nemo/actions-tree.json`
(written by `nemo-action-layout-editor`): `{"toplevel": [node, ...]}`, where a
node is `{"uuid", "type": "action"|"submenu"|"separator", "user-label",
"user-icon", "children"}` and an action's uuid is its `.nemo_action` file
name. Actions the file does not list are shown at the top level.

This merges, never rewrites: every other node keeps its place and fields.
Our actions are removed wherever they are, then listed, in the order given,
first in the submenu `SUBMENU_UUID` (created at the end of the top level if
missing); other entries a user put in that submenu stay after them. Rerunning
changes nothing. A file Nemo itself would reject (the editor's
`validate_node` checks — uuid, type, label, icon, accelerator, children —
on every node) is left untouched and the script exits 1: Nemo would ignore
the whole layout. Run with /usr/bin/python3 (standard library plus the
system GTK bindings, which only an accelerator check needs).
"""

import json
import os
import sys
import tempfile

#: Project-specific, so it cannot be mistaken for a submenu of the user's.
SUBMENU_UUID = 'stl-batch-fix-submenu'
SUBMENU_LABEL = 'STL fix'
SUBMENU_ICON = 'applications-engineering'
TYPES = ('action', 'submenu', 'separator')


class LayoutError(ValueError):
    """The existing layout file is not one Nemo would accept."""


def layout_path() -> str:
    config = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(config, 'nemo', 'actions-tree.json')


def _accelerator_ok(text: str) -> bool:
    """Nemo's own test: `Gtk.accelerator_parse` finds a key or a modifier.
    Needs the system Python's GTK bindings (install_nemo_actions.sh runs this
    script with /usr/bin/python3, as Nemo runs its editor); without them an
    accelerator cannot be checked, so the layout is refused, not guessed."""
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk
    except (ImportError, ValueError):
        raise LayoutError(f'cannot check accelerator {text!r}: no GTK bindings '
                          f'in this Python (run with /usr/bin/python3)') from None
    key, mods = Gtk.accelerator_parse(text)
    return bool(key or mods)


def _validate(nodes, where='toplevel') -> None:
    if not isinstance(nodes, list):
        raise LayoutError(f'{where} is not a list')
    for node in nodes:
        if not isinstance(node, dict):
            raise LayoutError(f'{where}: entry is not an object: {node!r}')
        uuid, kind = node.get('uuid'), node.get('type')
        if not isinstance(uuid, str) or not uuid:
            raise LayoutError(f'{where}: entry without a uuid: {node!r}')
        if kind not in TYPES:
            raise LayoutError(f'{uuid}: invalid type {kind!r}')
        label = node.get('user-label')
        if label is not None and (not isinstance(label, str) or not label):
            raise LayoutError(f'{uuid}: invalid user-label {label!r}')
        icon = node.get('user-icon')
        if icon is not None and not isinstance(icon, str):
            raise LayoutError(f'{uuid}: invalid user-icon {icon!r}')
        accelerator = node.get('accelerator')
        if accelerator not in ('', None) and (not isinstance(accelerator, str)
                                              or not _accelerator_ok(accelerator)):
            raise LayoutError(f'{uuid}: invalid accelerator {accelerator!r}')
        # Nemo checks children on every node (it ignores those of an action
        # or separator, but still rejects an invalid one).
        if 'children' in node:
            _validate(node['children'], uuid)


def _without(nodes, uuids):
    """`nodes` with every action in `uuids` removed, at every level (in place)."""
    kept = []
    for node in nodes:
        if node['type'] == 'action' and node['uuid'] in uuids:
            continue
        if node['type'] == 'submenu' and 'children' in node:
            node['children'] = _without(node['children'], uuids)
        kept.append(node)
    return kept


def merge(data, actions):
    """Return `data` (a parsed layout, or None for no file) with `actions`
    first in our submenu."""
    if data is None:
        data = {'toplevel': []}
    if not isinstance(data, dict) or 'toplevel' not in data:
        raise LayoutError('no "toplevel" list')
    _validate(data['toplevel'])
    toplevel = _without(data['toplevel'], set(actions))
    submenu = next((n for n in toplevel
                    if n['type'] == 'submenu' and n['uuid'] == SUBMENU_UUID), None)
    if submenu is None:
        submenu = {'uuid': SUBMENU_UUID, 'type': 'submenu', 'user-label': SUBMENU_LABEL,
                   'user-icon': SUBMENU_ICON, 'accelerator': None, 'children': []}
        toplevel.append(submenu)
    ours = [{'uuid': name, 'type': 'action', 'user-label': None, 'user-icon': None,
             'accelerator': None} for name in actions]
    submenu['children'] = ours + submenu.get('children', [])
    data['toplevel'] = toplevel
    return data


def install(path: str, actions) -> None:
    try:
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
    except FileNotFoundError:
        data = None
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise LayoutError(f'not valid JSON: {error}') from None
    merged = merge(data, actions)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.actions-tree.', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(merged, handle, indent=2)
            handle.write('\n')
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv=None) -> int:
    actions = sys.argv[1:] if argv is None else argv
    if not actions:
        print('usage: nemo_layout.py ACTION_FILE_NAME...', file=sys.stderr)
        return 2
    path = layout_path()
    try:
        install(path, actions)
    except LayoutError as error:
        print(f'nemo_layout.py: {path}: {error}; left unchanged. Fix or remove it '
              f'(nemo-action-layout-editor), then rerun.', file=sys.stderr)
        return 1
    print(f'updated {path}: submenu "{SUBMENU_LABEL}" holds {", ".join(actions)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
