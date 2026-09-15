"""Tests for libs.indicators — filesystem detection only, no meshes.

Every file here is an empty touch: the module tests for existence and never
opens anything, so content is irrelevant and the tests stay fast.
"""

import os
import shutil
import tempfile
import unittest

from libs.indicators import (
    EXPORT_DIRNAME, Finding, Indicator, check, export_path,
)


class IndicatorCase(unittest.TestCase):
    """A source tree and an output tree, as the real run has."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='indicators-')
        self.src_dir = os.path.join(self.root, 'Fixing')
        self.out_dir = os.path.join(self.root, 'Fixed')
        os.makedirs(os.path.join(self.src_dir, 'Leia'))
        os.makedirs(os.path.join(self.out_dir, 'Leia'))
        self.source = os.path.join(self.src_dir, 'Leia', 'Head.stl')
        self.output = os.path.join(self.out_dir, 'Leia', 'Head.stl')
        self._touch(self.source)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').close()
        return path

    def _marker(self, suffix):
        base, _ = os.path.splitext(self.output)
        return self._touch(base + suffix)

    def _check(self):
        return check(self.source, self.src_dir, self.output)


class TestNothingFound(IndicatorCase):

    def test_clean_source_is_process(self):
        found = self._check()
        self.assertIs(found.indicator, Indicator.PROCESS)
        self.assertIsNone(found.path)
        self.assertFalse(found.found)
        self.assertEqual(found.source, self.source)


class TestOutputMarkers(IndicatorCase):

    def test_already_fixed(self):
        self._touch(self.output)
        found = self._check()
        self.assertIs(found.indicator, Indicator.ALREADY_FIXED)
        self.assertEqual(found.path, self.output)
        self.assertTrue(found.found)

    def test_each_marker_is_recognised(self):
        for suffix, expected in (('.broken.stl', Indicator.BROKEN),
                                 ('.failed.stl', Indicator.FAILED),
                                 ('.unrepaired.stl', Indicator.UNREPAIRED),
                                 ('.open.stl', Indicator.OPEN_EDGES),
                                 ('.timeout.stl', Indicator.TIMED_OUT)):
            with self.subTest(suffix=suffix):
                self.setUp()
                marker = self._marker(suffix)
                found = self._check()
                self.assertIs(found.indicator, expected)
                self.assertEqual(found.path, marker)
                self.tearDown()

    def test_a_marker_outranks_the_finished_output(self):
        """Both can exist; the marker is the more specific statement."""
        self._touch(self.output)
        marker = self._marker('.failed.stl')
        found = self._check()
        self.assertIs(found.indicator, Indicator.FAILED)
        self.assertEqual(found.path, marker)

    def test_first_match_wins_among_markers(self):
        """Cosmetic by design — any marker means the file was dealt with.

        Checking stops at the first hit: once the answer is known, the
        remaining existence tests cannot change it.
        """
        first = self._marker('.broken.stl')
        self._marker('.timeout.stl')
        found = self._check()
        self.assertIs(found.indicator, Indicator.BROKEN)
        self.assertEqual(found.path, first)

    def test_original_stl_is_not_an_indicator(self):
        """It sits beside a SUCCESSFUL output as bbox-drift evidence.

        Treating it as an indicator would skip files that actually worked.
        """
        self._marker('.original.stl')
        found = self._check()
        self.assertIs(found.indicator, Indicator.PROCESS)


class TestExport(IndicatorCase):

    def test_export_is_found_and_its_path_returned(self):
        export = self._touch(export_path(self.source, self.src_dir))
        found = self._check()
        self.assertIs(found.indicator, Indicator.EXPORT_READY)
        self.assertEqual(found.path, export)

    def test_export_lives_in_the_source_tree(self):
        path = export_path(self.source, self.src_dir)
        self.assertTrue(path.startswith(self.src_dir),
                        "the export must be inside the source tree")
        self.assertIn(EXPORT_DIRNAME, path)

    def test_export_keeps_the_relative_path(self):
        path = export_path(self.source, self.src_dir)
        self.assertEqual(
            path, os.path.join(self.src_dir, EXPORT_DIRNAME, 'Leia', 'Head.stl'))

    def test_obj_export_is_named_stl(self):
        obj = os.path.join(self.src_dir, 'Leia', 'Body.obj')
        self.assertTrue(export_path(obj, self.src_dir).endswith('.stl'))

    def test_output_markers_outrank_the_export(self):
        """Converting a file we are not going to process is wasted work."""
        self._touch(export_path(self.source, self.src_dir))
        marker = self._marker('.failed.stl')
        found = self._check()
        self.assertIs(found.indicator, Indicator.FAILED)
        self.assertEqual(found.path, marker)

    def test_finished_output_outranks_the_export(self):
        self._touch(export_path(self.source, self.src_dir))
        self._touch(self.output)
        found = self._check()
        self.assertIs(found.indicator, Indicator.ALREADY_FIXED)


class TestCopyAsIs(IndicatorCase):
    """Non-mesh companions: images, READMEs, archives beside a model."""

    COPY_EXTS = frozenset({'.png', '.jpg', '.txt', '.zip'})

    def setUp(self):
        super().setUp()
        self.image = os.path.join(self.src_dir, 'Leia', 'render.png')
        self.image_out = os.path.join(self.out_dir, 'Leia', 'render.png')
        self._touch(self.image)

    def _check_image(self):
        return check(self.image, self.src_dir, self.image_out,
                     copy_extensions=self.COPY_EXTS)

    def test_a_companion_not_yet_copied(self):
        found = self._check_image()
        self.assertIs(found.indicator, Indicator.COPY_AS_IS)
        self.assertEqual(found.path, self.image_out)

    def test_a_companion_already_copied(self):
        self._touch(self.image_out)
        found = self._check_image()
        self.assertIs(found.indicator, Indicator.ALREADY_COPIED)
        self.assertEqual(found.path, self.image_out)

    def test_already_copied_is_not_already_fixed(self):
        """Same existence test, different claim — collapsing them would make
        any count of repaired files wrong."""
        self._touch(self.image_out)
        self.assertIsNot(self._check_image().indicator, Indicator.ALREADY_FIXED)

    def test_matching_is_case_insensitive(self):
        upper = os.path.join(self.src_dir, 'Leia', 'PHOTO.JPG')
        self._touch(upper)
        found = check(upper, self.src_dir,
                      os.path.join(self.out_dir, 'Leia', 'PHOTO.JPG'),
                      copy_extensions=self.COPY_EXTS)
        self.assertIs(found.indicator, Indicator.COPY_AS_IS)

    def test_a_mesh_is_unaffected_by_the_parameter(self):
        found = check(self.source, self.src_dir, self.output,
                      copy_extensions=self.COPY_EXTS)
        self.assertIs(found.indicator, Indicator.PROCESS)

    def test_omitting_the_parameter_keeps_old_behaviour(self):
        """A .png with no copy_extensions is treated as any other path."""
        found = check(self.image, self.src_dir, self.image_out)
        self.assertIs(found.indicator, Indicator.PROCESS)

    def test_a_companion_short_circuits_the_marker_checks(self):
        """None of the mesh markers can exist for a .png, so testing for them
        is meaningless work — measured, not assumed."""
        import libs.indicators as ind
        real_exists, calls = os.path.exists, []

        def counting(p):
            calls.append(p)
            return real_exists(p)

        ind.os.path.exists = counting
        try:
            self._check_image()
        finally:
            ind.os.path.exists = real_exists
        self.assertEqual(len(calls), 1,
                         f"expected one stat for the destination, got {calls}")


class TestFinding(unittest.TestCase):

    def test_finding_is_immutable(self):
        found = Finding('/a/b.stl', Indicator.PROCESS)
        with self.assertRaises(Exception):
            found.indicator = Indicator.BROKEN


if __name__ == '__main__':
    unittest.main(verbosity=2)
