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



class TestPrepareHandoff(unittest.TestCase):
    """A prepare child either hands off (category PREPARED, the expected
    path, a positive integer estimate) or ends the job with an ordinary
    terminal result; anything in between is rejected."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'result.json')

    def handoff(self, **changes):
        fields = dict(path='/a/b.stl', category=childresult.PREPARED, indicator=None,
                      stage='prepare', reason='ok', written_path=None, mode='prepare',
                      prepared_path='/cache/b.stl.9.stl', estimate_bytes=123)
        fields.update(changes)
        return fields

    def read(self, raw, mode='prepare', expected='/cache/b.stl.9.stl'):
        Path(self.path).write_text(json.dumps(raw))
        return childresult.read_and_validate(self.path, '/a/b.stl', mode=mode,
                                             expected_prepared=expected)

    def test_a_valid_handoff_round_trips(self):
        got = self.read(self.handoff())
        self.assertTrue(got.is_handoff)
        self.assertEqual((got.prepared_path, got.estimate_bytes), ('/cache/b.stl.9.stl', 123))

    def test_a_handoff_must_match_the_expected_path(self):
        self.assertIsNone(self.read(self.handoff(prepared_path='/elsewhere.stl')))
        self.assertIsNone(self.read(self.handoff(prepared_path=None)))

    def test_a_handoff_needs_a_positive_integer_estimate(self):
        for bad in (0, -1, 1.5, True, '123', None):
            with self.subTest(estimate=bad):
                self.assertIsNone(self.read(self.handoff(estimate_bytes=bad)))

    def test_a_handoff_cannot_also_publish(self):
        self.assertIsNone(self.read(self.handoff(indicator='PROCESS')))
        self.assertIsNone(self.read(self.handoff(written_path='/out/b.stl')))

    def test_the_mode_must_match_the_launch(self):
        self.assertIsNone(self.read(self.handoff(), mode='repair'))
        terminal = dict(path='/a/b.stl', category='published', indicator='PROCESS',
                        stage='process', reason='ok', written_path='/out/b.stl')
        self.assertIsNone(self.read(terminal, mode='prepare'))       # no mode field = repair
        self.assertIsNotNone(self.read({**terminal, 'mode': 'prepare'}, mode='prepare'))

    def test_repair_mode_never_accepts_prepared(self):
        self.assertIsNone(self.read(self.handoff(mode='repair'), mode='repair'))

    def test_a_terminal_result_carries_no_handoff(self):
        terminal = dict(path='/a/b.stl', category='published', indicator='UNDECIMATED',
                        stage='process', reason='x', written_path='/out/b.undecimated.stl',
                        mode='prepare')
        self.assertIsNotNone(self.read(terminal))
        self.assertIsNone(self.read({**terminal, 'estimate_bytes': 5}))
        self.assertIsNone(self.read({**terminal, 'prepared_path': '/cache/b.stl.9.stl'}))


if __name__ == '__main__':
    unittest.main()
