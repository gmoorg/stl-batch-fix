"""Tests for check_3mf.py — settings precedence, statuses and exit codes."""

import io
import os
import tempfile
import unittest

import numpy as np

import check_3mf
from libs.bambu3mf import NEGATIVE
from tests.tests.print_fixtures import slab, write_3mf

SETTINGS = {'initial_layer_print_height': '0.2', 'layer_height': '0.2',
            'support_top_z_distance': '0.24', 'support_threshold_angle': '40',
            'enable_support': '1', 'support_on_build_plate_only': '1'}


def recess(x, y):
    inside = (x > 2.5) & (x < 7.5) & (y > 2.5) & (y < 7.5)
    return np.where(inside, 0.3, 0.0)


FLAT = slab(n=(20, 20))
RECESSED = slab(recess, n=(80, 80))


class Cli(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'plate.3mf')

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, objects, *args, settings=SETTINGS, plates=None, items=None):
        if items is None:
            items = [(oid, np.eye(4), True) for oid in objects]
        write_3mf(self.path, objects, items, settings=settings, plates=plates)
        out = io.StringIO()
        code = check_3mf.main([self.path, *args], out=out)
        return code, out.getvalue()

    def test_flat_part_is_ok(self):
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=FLAT)])})
        self.assertEqual(code, check_3mf.EXIT_OK, text)
        self.assertIn('[OK]', text)
        self.assertIn('Scope:', text)

    def test_recess_is_a_risk(self):
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=RECESSED)])})
        self.assertEqual(code, check_3mf.EXIT_RISK, text)
        self.assertIn('B too low for support', text)

    def test_negative_volume_makes_a_clean_result_incomplete(self):
        objects = {'2': dict(parts=[dict(id='1', mesh=FLAT),
                                    dict(id='3', mesh=FLAT, subtype=NEGATIVE)])}
        code, text = self.run_cli(objects)
        self.assertEqual(code, check_3mf.EXIT_INCOMPLETE, text)
        self.assertIn('negative volume', text)

    def test_unmatched_part_is_incomplete(self):
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=FLAT),
                                                    dict(id='3', meta_id='8', mesh=FLAT)])})
        self.assertEqual(code, check_3mf.EXIT_INCOMPLETE, text)

    def test_object_override_beats_project_and_cli_beats_both(self):
        # A support gap of 0.05 puts the 0.3 mm recess above the unsupportable
        # band (0.25 mm), so the risk disappears.
        objects = {'2': dict(parts=[dict(id='1', mesh=RECESSED)],
                             overrides={'support_top_z_distance': '0.05'})}
        code, text = self.run_cli(objects)
        self.assertEqual(code, check_3mf.EXIT_OK, text)
        self.assertIn('support_gap=0.05 [object]', text)
        code, text = self.run_cli(objects, '--support-gap', '0.24')
        self.assertEqual(code, check_3mf.EXIT_RISK, text)
        self.assertIn('support_gap=0.24 [command line]', text)

    def test_automatic_support_angle_is_marked_assumed(self):
        settings = dict(SETTINGS, support_threshold_angle='0')
        _, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=FLAT)])}, settings=settings)
        self.assertIn('assumed for automatic', text)

    def test_unparsable_setting_falls_back_and_says_so(self):
        settings = dict(SETTINGS, support_top_z_distance='lots')
        _, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=FLAT)])}, settings=settings)
        self.assertIn('support_gap=0.2 [assumed]', text)
        self.assertIn('not understood', text)

    def test_supports_from_model_note(self):
        settings = dict(SETTINGS, support_on_build_plate_only='0')
        _, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=FLAT)])}, settings=settings)
        self.assertIn('supports may also start on the model', text)

    def test_plate_selection(self):
        objects = {'2': dict(name='flat', parts=[dict(id='1', mesh=FLAT)]),
                   '4': dict(name='recessed', parts=[dict(id='1', mesh=RECESSED)])}
        plates = {1: [('2', 0)], 2: [('4', 0)]}
        code, text = self.run_cli(objects, '--plate', '1', plates=plates)
        self.assertEqual(code, check_3mf.EXIT_OK, text)
        self.assertNotIn('recessed', text)
        code, _ = self.run_cli(objects, '--plate', '7', plates=plates)
        self.assertEqual(code, check_3mf.EXIT_ERROR)

    def test_not_printable_is_skipped(self):
        objects = {'2': dict(parts=[dict(id='1', mesh=RECESSED)])}
        code, text = self.run_cli(objects, items=[('2', np.eye(4), False)])
        self.assertIn('[SKIPPED]', text)
        self.assertEqual(code, check_3mf.EXIT_OK)

    def test_invalid_cli_value_is_an_error(self):
        code, _ = self.run_cli({'2': dict(parts=[dict(id='1', mesh=FLAT)])}, '--cell', '-1')
        self.assertEqual(code, check_3mf.EXIT_ERROR)

    def test_unreadable_file_is_an_error(self):
        with open(self.path, 'w') as handle:
            handle.write('not a zip')
        self.assertEqual(check_3mf.main([self.path], out=io.StringIO()), check_3mf.EXIT_ERROR)

    def test_malformed_settings_exit_2(self):
        objects = {'2': dict(parts=[dict(id='1', mesh=FLAT)])}
        code, _ = self.run_cli(objects, settings=[])
        self.assertEqual(code, check_3mf.EXIT_ERROR)

    def test_png_map_is_written(self):
        png = os.path.join(self.dir.name, 'maps')
        _, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=RECESSED)])}, '--png', png)
        self.assertEqual(len(os.listdir(png)), 1)
        self.assertIn('map:', text)


if __name__ == '__main__':
    unittest.main()
