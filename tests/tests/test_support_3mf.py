"""Tests for support_3mf.py — automatic coverage, skips, outputs, exit codes."""

import io
import os
import tempfile
import unittest

import numpy as np

import support_3mf
from libs import bambu3mf
from tests.tests.print_fixtures import slab, translate, write_3mf

SETTINGS = {'initial_layer_print_height': '0.2', 'layer_height': '0.2',
            'support_top_z_distance': '0.24', 'support_threshold_angle': '40',
            'enable_support': '1', 'support_on_build_plate_only': '1',
            'outer_wall_line_width': '0.42'}


def recess(depth):
    def bottom(x, y):
        inside = (x > 2.5) & (x < 7.5) & (y > 2.5) & (y < 7.5)
        return np.where(inside, depth, 0.0)
    return bottom


SHALLOW = slab(recess(0.3), n=(80, 80))       # in the unsupportable band
HIGHER = slab(recess(1.0), n=(80, 80))        # supportable by the slicer
DEEP = slab(recess(4.0), n=(80, 80), top=6.0)
FLAT = slab(n=(20, 20))


class Cli(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'plate.3mf')
        self.out = os.path.join(self.dir.name, 'plate.supported.3mf')

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, objects, *args, settings=SETTINGS, items=None):
        if items is None:
            items = [(oid, translate(x=50.0, y=40.0, rotate_z_deg=20), True) for oid in objects]
        write_3mf(self.path, objects, items, settings=settings)
        out = io.StringIO()
        code = support_3mf.main([self.path, *args], out=out)
        return code, out.getvalue()

    def added_parts(self):
        project = bambu3mf.read(self.out)
        return [p for i in project.instances for p in i.parts if p.name == support_3mf.PART_NAME]

    def test_unsupportable_recess_gets_ribs(self):
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])})
        self.assertEqual(code, support_3mf.EXIT_WRITTEN, text)
        self.assertIn('[RIBS]', text)
        (part,) = self.added_parts()
        self.assertAlmostEqual(part.vertices[:, 2].min(), 0.0, places=6)
        self.assertAlmostEqual(part.vertices[:, 2].max(), 0.35, places=6)
        self.assertIn('too low for support', text)

    def test_flat_model_needs_nothing_and_writes_nothing(self):
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=FLAT)])})
        self.assertEqual(code, support_3mf.EXIT_NOTHING, text)
        self.assertFalse(os.path.exists(self.out))

    def test_supportable_recess_is_left_to_the_slicer(self):
        code, _ = self.run_cli({'2': dict(parts=[dict(id='1', mesh=HIGHER)])})
        self.assertEqual(code, support_3mf.EXIT_NOTHING)

    def test_supports_off_ribs_higher_undersides_up_to_max_height(self):
        off = dict(SETTINGS, enable_support='0')
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=HIGHER)])}, settings=off)
        self.assertEqual(code, support_3mf.EXIT_WRITTEN, text)
        self.assertAlmostEqual(self.added_parts()[0].vertices[:, 2].max(), 1.05, places=6)

    def test_max_height_extends_the_analysis(self):
        off = dict(SETTINGS, enable_support='0')
        code, _ = self.run_cli({'2': dict(parts=[dict(id='1', mesh=DEEP)])}, settings=off)
        self.assertEqual(code, support_3mf.EXIT_NOTHING)
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=DEEP)])},
                                  '--max-height', '5', settings=off)
        self.assertEqual(code, support_3mf.EXIT_WRITTEN, text)

    def test_untrustworthy_instances_are_skipped(self):
        cases = {
            'negative': {'2': dict(parts=[dict(id='1', mesh=SHALLOW),
                                          dict(id='3', mesh=FLAT, subtype='negative_part')])},
            'modifier': {'2': dict(parts=[dict(id='1', mesh=SHALLOW),
                                          dict(id='3', mesh=FLAT, subtype='modifier_part')])},
            'unmatched': {'2': dict(parts=[dict(id='1', mesh=SHALLOW),
                                           dict(id='3', meta_id='9', mesh=FLAT)])},
            'part override': {'2': dict(parts=[dict(id='1', mesh=SHALLOW,
                                                    overrides={'layer_height': '0.1'})])},
        }
        for label, objects in cases.items():
            with self.subTest(label):
                code, text = self.run_cli(objects)
                self.assertEqual(code, support_3mf.EXIT_PARTIAL, text)
                self.assertIn('[SKIPPED]', text)
                self.assertFalse(os.path.exists(self.out))

    def test_unreadable_setting_is_skipped(self):
        bad = dict(SETTINGS, support_top_z_distance='lots')
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])}, settings=bad)
        self.assertEqual(code, support_3mf.EXIT_PARTIAL, text)
        self.assertIn('not understood', text)

    def test_unknown_support_type_is_skipped(self):
        odd = dict(SETTINGS, support_type='unrecognised(manual-ish)')
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])}, settings=odd)
        self.assertEqual(code, support_3mf.EXIT_PARTIAL, text)
        self.assertIn('not recognised', text)

    def test_multi_instance_skipped_others_still_written(self):
        objects = {'2': dict(name='twice', parts=[dict(id='1', mesh=SHALLOW)]),
                   '4': dict(name='once', parts=[dict(id='1', mesh=SHALLOW)])}
        items = [('2', np.eye(4), True), ('2', translate(x=30), True),
                 ('4', translate(x=60), True)]
        code, text = self.run_cli(objects, items=items)
        self.assertEqual(code, support_3mf.EXIT_PARTIAL, text)
        self.assertIn('instances', text)
        self.assertEqual(len(self.added_parts()), 1)

    def test_existing_output_is_not_overwritten(self):
        with open(self.out, 'w') as handle:
            handle.write('keep')
        code, _ = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])})
        self.assertEqual(code, support_3mf.EXIT_ERROR)
        with open(self.out) as handle:
            self.assertEqual(handle.read(), 'keep')

    def test_invalid_rib_settings_are_errors(self):
        for args in (('--width', '2', '--pitch', '2'), ('--pitch', '-1'), ('--max-height', '0'),
                     ('--overlap', 'nan')):
            with self.subTest(args=args):
                code, _ = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])}, *args)
                self.assertEqual(code, support_3mf.EXIT_ERROR)
                self.assertFalse(os.path.exists(self.out))


if __name__ == '__main__':
    unittest.main()
