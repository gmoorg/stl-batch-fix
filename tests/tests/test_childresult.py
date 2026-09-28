"""Bounded, strictly-validated result-file protocol between --one-file and the parent."""

import json
import tempfile
import unittest
from pathlib import Path

from libs import childresult
from libs.childresult import ChildResult
from libs.indicators import Indicator


class TestChildResult(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'result.json')

    def test_round_trip(self):
        result = ChildResult(path='/a/b.stl', category='published',
                             indicator='PROCESS', stage='process', reason='clean',
                             written_path='/out/b.stl')
        childresult.write(self.path, result)
        got = childresult.read_and_validate(self.path, '/a/b.stl')
        self.assertEqual(got, result)

    def test_clean_property(self):
        clean = ChildResult('/a', 'published', 'PROCESS', 'process', 'ok')
        self.assertTrue(clean.clean)
        not_clean = ChildResult('/a', 'published', 'FAILED', 'process', 'bad')
        self.assertFalse(not_clean.clean)
        no_indicator = ChildResult('/a', 'intake_failure', None, 'intake', 'bad')
        self.assertFalse(no_indicator.clean)

    def test_missing_file_is_none(self):
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_empty_file_is_none(self):
        Path(self.path).write_bytes(b'')
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_oversized_file_is_none(self):
        Path(self.path).write_bytes(b'{"path": "' + b'x' * childresult.MAX_RESULT_BYTES + b'"}')
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_malformed_json_is_none(self):
        Path(self.path).write_text('{not json')
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_not_an_object_is_none(self):
        Path(self.path).write_text('[1, 2, 3]')
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_missing_required_key_is_none(self):
        Path(self.path).write_text(json.dumps({'path': '/a/b.stl', 'category': 'published'}))
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_wrong_path_is_none(self):
        result = ChildResult('/a/OTHER.stl', 'published', 'PROCESS', 'process', 'ok')
        childresult.write(self.path, result)
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_unknown_category_is_none(self):
        Path(self.path).write_text(json.dumps({
            'path': '/a/b.stl', 'category': 'not_a_real_category', 'indicator': None,
            'stage': 'x', 'reason': 'x', 'written_path': None}))
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_unknown_indicator_is_none(self):
        Path(self.path).write_text(json.dumps({
            'path': '/a/b.stl', 'category': 'published', 'indicator': 'NOT_REAL',
            'stage': 'x', 'reason': 'x', 'written_path': None}))
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_wrong_field_type_is_none(self):
        Path(self.path).write_text(json.dumps({
            'path': '/a/b.stl', 'category': 123, 'indicator': None,
            'stage': 'x', 'reason': 'x', 'written_path': None}))
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_every_known_indicator_accepted(self):
        for indicator in Indicator:
            result = ChildResult('/a/b.stl', 'published', indicator.name, 'process', 'ok')
            childresult.write(self.path, result)
            self.assertIsNotNone(childresult.read_and_validate(self.path, '/a/b.stl'))

    def test_steps_round_trip(self):
        result = ChildResult('/a/b.stl', 'published', 'PROCESS', 'process', 'ok',
                             steps=('PREP: welded', 'PART: alpha=0.12, offset=0.03'))
        childresult.write(self.path, result)
        got = childresult.read_and_validate(self.path, '/a/b.stl')
        self.assertEqual(got.steps, ('PREP: welded', 'PART: alpha=0.12, offset=0.03'))

    def test_missing_steps_defaults_to_empty(self):
        Path(self.path).write_text(json.dumps({
            'path': '/a/b.stl', 'category': 'published', 'indicator': 'PROCESS',
            'stage': 'process', 'reason': 'ok', 'written_path': None}))
        got = childresult.read_and_validate(self.path, '/a/b.stl')
        self.assertEqual(got.steps, ())

    def test_non_string_step_is_none(self):
        Path(self.path).write_text(json.dumps({
            'path': '/a/b.stl', 'category': 'published', 'indicator': 'PROCESS',
            'stage': 'process', 'reason': 'ok', 'written_path': None, 'steps': [123]}))
        self.assertIsNone(childresult.read_and_validate(self.path, '/a/b.stl'))


if __name__ == '__main__':
    unittest.main()
