"""Tests for libs.bambu3mf — reading placement, parts, plates and settings.

Archives are written to a temporary directory by print_fixtures.write_3mf in
the same layout Bambu Studio uses (component wrappers in 3D/3dmodel.model,
meshes in 3D/Objects/, Metadata/*.config).
"""

import os
import tempfile
import unittest

import numpy as np

from libs import bambu3mf
from libs.bambu3mf import NEGATIVE, NORMAL, ReadError
from tests.tests.print_fixtures import slab, translate, write_3mf

BOX = slab(size=(2.0, 4.0), n=(1, 1), top=1.0)


class Archive(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'test.3mf')

    def tearDown(self):
        self.dir.cleanup()

    def write(self, objects, items, **kwargs):
        write_3mf(self.path, objects, items, **kwargs)
        return bambu3mf.read(self.path)


class TestPlacement(Archive):

    def test_component_then_item_transform(self):
        component = translate(x=1.0)
        item = translate(x=100.0, y=50.0, z=0.0, scale=2.0, rotate_z_deg=90)
        project = self.write({'2': dict(parts=[dict(id='1', mesh=BOX, transform=component)])},
                             [('2', item, True)])
        v = project.instances[0].parts[0].vertices
        # Box x 0..2 -> +1 -> 1..3; scale 2 -> 2..6; rotate 90 about z:
        # (x, y) -> (-y, x) for row vectors with this matrix.
        expected = (BOX[0] + [1.0, 0, 0]) * 2.0
        expected = np.stack([-expected[:, 1], expected[:, 0], expected[:, 2]], axis=1) + [100, 50, 0]
        np.testing.assert_allclose(v, expected, atol=1e-9)

    def test_part_matrix_metadata_is_not_applied_again(self):
        project = self.write({'2': dict(parts=[dict(id='1', mesh=BOX, transform=translate(x=5))])},
                             [('2', np.eye(4), True)],
                             part_matrix='1 0 0 5 0 1 0 0 0 0 1 0 0 0 0 1')
        self.assertAlmostEqual(project.instances[0].parts[0].vertices[:, 0].min(), 5.0)

    def test_inch_unit_is_converted_to_mm(self):
        project = self.write({'2': dict(parts=[dict(id='1', mesh=BOX)])},
                             [('2', translate(x=1.0), True)], unit='inch')
        v = project.instances[0].parts[0].vertices
        self.assertAlmostEqual(v[:, 0].min(), 25.4)
        self.assertAlmostEqual(v[:, 0].max(), 25.4 * 3)


class TestPartsAndPlates(Archive):

    def test_subtypes_are_matched_by_identity_not_order(self):
        objects = {'2': dict(parts=[dict(id='7', mesh=BOX, subtype=NEGATIVE),
                                    dict(id='3', mesh=BOX, subtype=NORMAL)])}
        project = self.write(objects, [('2', np.eye(4), True)])
        subtypes = [p.subtype for p in project.instances[0].parts]
        self.assertEqual(subtypes, [NEGATIVE, NORMAL])
        self.assertEqual(len(project.instances[0].normal_parts), 1)

    def test_unmatched_part_has_unknown_subtype(self):
        objects = {'2': dict(parts=[dict(id='1', meta_id='99', mesh=BOX)])}
        project = self.write(objects, [('2', np.eye(4), True)])
        self.assertIsNone(project.instances[0].parts[0].subtype)
        self.assertEqual(project.instances[0].normal_parts, ())

    def test_instances_and_plates(self):
        objects = {'2': dict(name='a', parts=[dict(id='1', mesh=BOX)]),
                   '4': dict(name='b', parts=[dict(id='1', mesh=BOX)])}
        items = [('2', np.eye(4), True), ('4', np.eye(4), False), ('2', translate(x=10), True)]
        project = self.write(objects, items, plates={1: [('2', 0)], 2: [('4', 0), ('2', 1)]})
        found = [(i.name, i.instance_id, i.plate, i.printable) for i in project.instances]
        self.assertEqual(found, [('a', 0, 1, True), ('b', 0, 2, False), ('a', 1, 2, True)])

    def test_settings_and_overrides(self):
        objects = {'2': dict(parts=[dict(id='1', mesh=BOX, overrides={'layer_height': '0.1'})],
                             overrides={'support_top_z_distance': '0.3'})}
        project = self.write(objects, [('2', np.eye(4), True)],
                             settings={'support_top_z_distance': '0.24', 'layer_height': ['0.2']})
        self.assertEqual(project.settings['support_top_z_distance'], '0.24')
        self.assertEqual(project.settings['layer_height'], '0.2')
        instance = project.instances[0]
        self.assertEqual(instance.overrides, {'support_top_z_distance': '0.3'})
        self.assertEqual(instance.parts[0].overrides, {'layer_height': '0.1'})

    def test_plain_3mf_is_one_plate_of_normal_parts(self):
        project = self.write({'1': dict(parts=[dict(id='1', mesh=BOX)])},
                             [('1', translate(z=0.0), True)], bambu=False)
        self.assertFalse(project.is_bambu)
        instance = project.instances[0]
        self.assertEqual((instance.plate, instance.parts[0].subtype), (1, NORMAL))
        self.assertEqual(project.settings, {})


class TestErrors(Archive):

    def test_not_a_zip(self):
        with open(self.path, 'w') as handle:
            handle.write('nope')
        with self.assertRaises(ReadError):
            bambu3mf.read(self.path)

    def test_missing_object(self):
        with self.assertRaises(ReadError):
            self.write({'2': dict(parts=[dict(id='1', mesh=BOX)])}, [('9', np.eye(4), True)])

    def test_triangle_index_out_of_range(self):
        v, f = BOX
        with self.assertRaises(ReadError):
            self.write({'2': dict(parts=[dict(id='1', mesh=(v, f + 100))])},
                       [('2', np.eye(4), True)])

    def test_singular_transform(self):
        with self.assertRaises(ReadError):
            self.write({'2': dict(parts=[dict(id='1', mesh=BOX)])},
                       [('2', np.zeros((4, 4)), True)])

    def test_bad_transform_text(self):
        for text in ('1 2 3', '1 0 0 0 1 0 0 0 1 0 0 nan', 'a b c d e f g h i j k l'):
            with self.subTest(text=text):
                with self.assertRaises(ReadError):
                    bambu3mf.parse_transform(text)

    def test_malformed_metadata(self):
        import zipfile
        objects = {'2': dict(parts=[dict(id='1', mesh=BOX)])}
        cases = {'Metadata/project_settings.config': ('[]', 'null'),
                 'Metadata/model_settings.config': (
                     '<config><plate><metadata key="plater_id" value="x"/></plate></config>',
                     '<config><plate><model_instance><metadata key="object_id" value="2"/>'
                     '<metadata key="instance_id" value="?"/></model_instance></plate></config>')}
        for member, bodies in cases.items():
            for body in bodies:
                with self.subTest(member=member, body=body):
                    write_3mf(self.path, objects, [('2', np.eye(4), True)])
                    with zipfile.ZipFile(self.path) as source:
                        contents = {n: source.read(n) for n in source.namelist()}
                    contents[member] = body
                    with zipfile.ZipFile(self.path, 'w') as target:
                        for name, data in contents.items():
                            target.writestr(name, data)
                    with self.assertRaises(ReadError):
                        bambu3mf.read(self.path)

    def test_component_cycle(self):
        import zipfile
        model = ('<?xml version="1.0"?><model unit="millimeter" '
                 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                 '<resources><object id="1"><components><component objectid="2"/></components></object>'
                 '<object id="2"><components><component objectid="1"/></components></object>'
                 '</resources><build><item objectid="1"/></build></model>')
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('3D/3dmodel.model', model)
        with self.assertRaises(ReadError) as caught:
            bambu3mf.read(self.path)
        self.assertIn('cycle', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
