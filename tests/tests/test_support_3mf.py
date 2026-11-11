"""Tests for support_3mf.py — automatic coverage, skips, outputs, exit codes."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import support_3mf
from libs import basecheck, bambu3mf
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

    def test_unusable_output_is_refused_before_reading(self):
        # The source is not a 3MF: had it been read first, the error would be
        # about the source, not the refusal.
        with open(self.path, 'wb') as f:
            f.write(b'not a 3mf')
        grid = os.path.join(self.dir.name, 'plate.grid.3mf')
        for label, args, existing in (
                ('ribs default', [], self.out),
                ('grid default', ['--layout', 'grid', '--max-height', '30'], grid),
                ('output is the source', ['-o', self.path], None)):
            with self.subTest(label):
                if existing:
                    with open(existing, 'wb') as f:
                        f.write(b'keep me')
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = support_3mf.main([self.path, *args], out=out)
                self.assertEqual(code, support_3mf.EXIT_ERROR)
                self.assertIn('refusing to write', err.getvalue())
                self.assertEqual(out.getvalue(), '')
                if existing:
                    with open(existing, 'rb') as f:
                        self.assertEqual(f.read(), b'keep me')
                with open(self.path, 'rb') as f:
                    self.assertEqual(f.read(), b'not a 3mf')

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


    def test_non_finite_max_height_is_an_error(self):
        # `--max-height=-inf`: argparse would read a bare `-inf` as an option.
        for value in ('nan', 'inf', '-inf', '0'):
            with self.subTest(value=value):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code, _ = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])},
                                           f'--max-height={value}')
                self.assertEqual(code, support_3mf.EXIT_ERROR)
                self.assertIn('positive finite', err.getvalue())
                self.assertFalse(os.path.exists(self.out))
        code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])},
                                  '--max-height', '2.5')
        self.assertEqual(code, support_3mf.EXIT_WRITTEN, text)

    def test_non_finite_project_line_width_falls_back(self):
        objects = {'2': dict(parts=[dict(id='1', mesh=SHALLOW)])}
        for widths, expected in ((dict(outer_wall_line_width='inf', line_width='0.4'), '0.4'),
                                 (dict(outer_wall_line_width='nan', line_width='inf'), '0.42')):
            with self.subTest(widths=widths):
                if os.path.exists(self.out):
                    os.remove(self.out)
                code, text = self.run_cli(objects, settings=dict(SETTINGS, **widths))
                self.assertEqual(code, support_3mf.EXIT_WRITTEN, text)
                self.assertIn(f'{expected} mm thick', text)

    def test_failed_analysis_skips_the_instance(self):
        # Fault injection only: a real MemoryError needs a model too big to test.
        with mock.patch.object(support_3mf.basecheck, 'check',
                               side_effect=MemoryError('probe')):
            code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])})
        self.assertEqual(code, support_3mf.EXIT_PARTIAL, text)
        self.assertIn('[SKIPPED]', text)
        self.assertIn('analysis failed: probe', text)
        self.assertFalse(os.path.exists(self.out))

    def test_failed_diagnostic_still_reports_the_written_file(self):
        # Fault injection: the post-write diagnostic's reread of the output,
        # or its second analysis, fails after the file is written.
        real_read, real_check = bambu3mf.read, basecheck.check

        def read_fails_on_output(path, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(self.out):
                raise bambu3mf.ReadError('probe')
            return real_read(path, *args, **kwargs)

        calls = []

        def second_check_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                raise MemoryError('probe')
            return real_check(*args, **kwargs)

        for label, target, name, fake in (
                ('reread', support_3mf.bambu3mf, 'read', read_fails_on_output),
                ('analysis', support_3mf.basecheck, 'check', second_check_fails)):
            with self.subTest(label):
                if os.path.exists(self.out):
                    os.remove(self.out)
                with mock.patch.object(target, name, fake):
                    code, text = self.run_cli({'2': dict(parts=[dict(id='1', mesh=SHALLOW)])})
                self.assertEqual(code, support_3mf.EXIT_WRITTEN, text)
                self.assertTrue(os.path.exists(self.out))
                self.assertIn('diagnostic not available', text)
                self.assertIn(f'{self.out} was written', text)

    def test_settings_overrides_are_accepted(self):
        args = support_3mf.parse_args(['m.3mf', '--first-layer', '0.3', '--layer-height', '0.1',
                                       '--support-gap', '0.15', '--support-angle', '45'])
        self.assertEqual((args.first_layer, args.layer_height, args.support_gap,
                          args.support_angle), (0.3, 0.1, 0.15, 45.0))

if __name__ == '__main__':
    unittest.main()
