"""tools/nemo_layout.py: the "STL fix" submenu merged into Nemo's layout file."""

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import nemo_layout

OURS = ['stl-fix.nemo_action', 'stl-alpha-fix.nemo_action', 'stl-fix-large.nemo_action']


def action(uuid, label=None):
    return {'uuid': uuid, 'type': 'action', 'user-label': label, 'user-icon': None}


class TestNemoLayout(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(temp.name, 'nemo', 'actions-tree.json')

    def install(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return nemo_layout.main(OURS)

    def submenu(self, data):
        found = [n for n in data['toplevel'] if n['uuid'] == nemo_layout.SUBMENU_UUID]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_no_file_creates_the_submenu_in_order(self):
        self.assertEqual(self.install(), 0)
        data = json.loads(self.path.read_text())
        menu = self.submenu(data)
        self.assertEqual((menu['type'], menu['user-label']), ('submenu', 'STL fix'))
        self.assertEqual([n['uuid'] for n in menu['children']], OURS)

    def test_other_entries_are_kept_and_ours_moved_into_the_submenu(self):
        mine = {'uuid': 'Tools', 'type': 'submenu', 'user-label': 'Tools', 'user-icon': None,
                'accelerator': '<Control>t',
                'children': [action('zip.nemo_action', 'Zip'), action('stl-fix.nemo_action')]}
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'toplevel': [
            action('stl-alpha-fix.nemo_action'), {'uuid': 'separator', 'type': 'separator'},
            mine]}))
        with mock.patch.object(nemo_layout, '_accelerator_ok', return_value=True):
            self.assertEqual(self.install(), 0)
        data = json.loads(self.path.read_text())
        self.assertEqual([n['uuid'] for n in data['toplevel']],
                         ['separator', 'Tools', nemo_layout.SUBMENU_UUID])
        tools = data['toplevel'][1]
        self.assertEqual(tools['children'], [action('zip.nemo_action', 'Zip')])
        self.assertEqual(tools['accelerator'], '<Control>t')
        self.assertEqual([n['uuid'] for n in self.submenu(data)['children']], OURS)

    def test_rerun_changes_nothing_and_keeps_user_additions_to_our_submenu(self):
        self.install()
        data = json.loads(self.path.read_text())
        self.submenu(data)['children'].append(action('extra.nemo_action'))
        self.submenu(data)['user-label'] = 'Meshes'
        self.path.write_text(json.dumps(data))
        before = copy.deepcopy(data)
        self.assertEqual(self.install(), 0)
        after = json.loads(self.path.read_text())
        self.assertEqual(after, before)

    def test_a_layout_nemo_would_reject_is_left_untouched(self):
        self.path.parent.mkdir(parents=True)
        for text in ('{not json', '[]', '{"toplevel": {}}',
                     '{"toplevel": [{"type": "action"}]}',
                     '{"toplevel": [{"uuid": "a", "type": "menu"}]}',
                     '{"toplevel": [{"uuid": "a", "type": "action", "user-label": ""}]}',
                     '{"toplevel": [{"uuid": "s", "type": "submenu", "children": [{"uuid": 3, "type": "action"}]}]}'):
            with self.subTest(text=text):
                self.path.write_text(text)
                self.assertEqual(self.install(), 1)
                self.assertEqual(self.path.read_text(), text)
                self.assertEqual(sorted(os.listdir(self.path.parent)), ['actions-tree.json'])

    def test_accelerators_and_children_on_any_node_are_checked_like_nemo(self):
        self.path.parent.mkdir(parents=True)
        checked = []

        def gtk(text):
            checked.append(text)
            return text == '<Control>t'

        bad = ['{"toplevel": [{"uuid": "a", "type": "action", "accelerator": "junk"}]}',
               '{"toplevel": [{"uuid": "a", "type": "action", "accelerator": 5}]}',
               '{"toplevel": [{"uuid": "a", "type": "action", "children": [{"type": "action"}]}]}',
               '{"toplevel": [{"uuid": "s", "type": "separator", "children": "x"}]}']
        with mock.patch.object(nemo_layout, '_accelerator_ok', side_effect=gtk):
            for text in bad:
                with self.subTest(text=text):
                    self.path.write_text(text)
                    self.assertEqual(self.install(), 1)
                    self.assertEqual(self.path.read_text(), text)
            self.path.write_text('{"toplevel": [{"uuid": "a", "type": "action", '
                                 '"accelerator": "<Control>t"}, '
                                 '{"uuid": "b", "type": "action", "accelerator": ""}]}')
            self.assertEqual(self.install(), 0)
        self.assertIn('<Control>t', checked)
        self.assertNotIn('', checked)

    def test_an_accelerator_without_gtk_bindings_is_refused(self):
        self.path.parent.mkdir(parents=True)
        text = '{"toplevel": [{"uuid": "a", "type": "action", "accelerator": "<Control>t"}]}'
        self.path.write_text(text)
        with mock.patch.dict('sys.modules', {'gi': None}):
            self.assertEqual(self.install(), 1)
        self.assertEqual(self.path.read_text(), text)


if __name__ == '__main__':
    unittest.main()
