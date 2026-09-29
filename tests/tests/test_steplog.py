"""Tests for libs.steplog: escaping, the `part` field, and format migration.

A grep across `tests/tests/*.py` for existing test-defined 5-arg `StepLogger`
stubs/callbacks (matching the pre-`part` shape) found none — every place a
`step_logger=` is exercised in the existing suite either uses the module
default (`steplog.null_logger`) or drives `execstep.run_sequence` without
overriding `step_logger` at all (e.g. `test_repairer.py`'s
`TestBlenderBeforePymeshfix`), so no existing test file needed updating for
the new required `part` parameter.
"""

import unittest

from libs import steplog
from libs.steplog import _escape, logged_step, null_logger, open_step_log, timed_info


def _decode(escaped: str) -> str:
    """Test-only decoder: a single-pass, left-to-right scanner (not staged
    blind replacements, which would misdecode a literal backslash
    immediately followed by a literal t/n/r)."""
    out = []
    i = 0
    n = len(escaped)
    while i < n:
        ch = escaped[i]
        if ch == '\\' and i + 1 < n:
            nxt = escaped[i + 1]
            if nxt == '\\':
                out.append('\\')
                i += 2
                continue
            if nxt == 't':
                out.append('\t')
                i += 2
                continue
            if nxt == 'n':
                out.append('\n')
                i += 2
                continue
            if nxt == 'r':
                out.append('\r')
                i += 2
                continue
        out.append(ch)
        i += 1
    return ''.join(out)


class TestEscapeRoundTrip(unittest.TestCase):
    def test_round_trip_arbitrary_strings(self):
        cases = [
            '',
            'plain text',
            'a\tb.stl',
            'a\nb',
            'a\rb',
            'a\\b',
            'a\\tb',              # literal backslash immediately followed by literal t
            'a\\nb',
            '\\',
            '\t\n\r\\',
            'mix\\ed\twith\nall\rkinds\\of\tstuff',
        ]
        for case in cases:
            with self.subTest(case=repr(case)):
                self.assertEqual(_decode(_escape(case)), case)

    def test_tab_and_space_remain_distinguishable(self):
        self.assertNotEqual(_escape("a\tb.stl"), _escape("a b.stl"))


class TestSevenFieldFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = self._make_tmp()

    def _make_tmp(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.log')
        import os
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def test_new_row_has_seven_fields_with_part(self):
        logger = open_step_log(self.tmp)
        logger('src.stl', 'start', 'decimate', '1/2', None, '100 faces')
        with open(self.tmp) as f:
            line = f.readline().rstrip('\n')
        fields = line.split('\t')
        self.assertEqual(len(fields), 7)
        self.assertEqual(fields[1], 'src.stl')
        self.assertEqual(fields[2], 'start')
        self.assertEqual(fields[3], 'decimate')
        self.assertEqual(fields[4], '1/2')
        self.assertEqual(fields[5], '')
        self.assertEqual(fields[6], '100 faces')

    def test_logged_step_defaults_part_to_dash(self):
        logger = open_step_log(self.tmp)
        with logged_step(logger, 'src.stl', 'scan_volume_in', 'x') as end:
            end('done')
        with open(self.tmp) as f:
            lines = [l.rstrip('\n').split('\t') for l in f]
        self.assertEqual(lines[0][4], '-')
        self.assertEqual(lines[1][4], '-')

    def test_timed_info_defaults_part_to_dash(self):
        logger = open_step_log(self.tmp)
        with timed_info(logger, 'src.stl', 'scan_diagonal') as report:
            report('42.0')
        with open(self.tmp) as f:
            line = f.readline().rstrip('\n').split('\t')
        self.assertEqual(line[4], '-')
        self.assertEqual(line[2], 'info')


class TestMixedFormatFile(unittest.TestCase):
    def test_old_six_field_then_new_seven_field(self):
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix='.log')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, 'a') as f:
            f.write('10:00:00\told_source.stl\tstart\tdecimate\t\t100 faces\n')
        logger = open_step_log(path)
        logger('new_source.stl', 'start', 'decimate', '1/1', None, '200 faces')
        with open(path) as f:
            lines = [l.rstrip('\n') for l in f]
        old_fields = lines[0].split('\t')
        new_fields = lines[1].split('\t')
        self.assertEqual(len(old_fields), 6)
        self.assertEqual(len(new_fields), 7)


class TestInterleavedMultiFileMultiPart(unittest.TestCase):
    def test_interleaved_events_keep_source_and_part_distinct(self):
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix='.log')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        logger = open_step_log(path)

        # Two files' worth of start/scan/end events, driven through one
        # shared logger in an interleaved order — matching real concurrent
        # children.
        logger('/a/file1.stl', 'start', 'alpha_wrap', '1/2', None, '10 faces in')
        logger('/b/file2.stl', 'start', 'alpha_wrap', '1/1', None, '20 faces in')
        logger('/a/file1.stl', 'end', 'alpha_wrap', '1/2', 0.5, '30 faces out')
        logger('/a/file1.stl', 'start', 'alpha_wrap', '2/2', None, '15 faces in')
        logger('/b/file2.stl', 'end', 'alpha_wrap', '1/1', 0.2, '40 faces out')
        logger('/a/file1.stl', 'end', 'alpha_wrap', '2/2', 0.3, '25 faces out')

        with open(path) as f:
            rows = [l.rstrip('\n').split('\t') for l in f]

        # source is field[1], part is field[4].
        self.assertEqual(rows[0][1], '/a/file1.stl')
        self.assertEqual(rows[0][4], '1/2')
        self.assertEqual(rows[1][1], '/b/file2.stl')
        self.assertEqual(rows[1][4], '1/1')
        self.assertEqual(rows[2][1], '/a/file1.stl')
        self.assertEqual(rows[2][4], '1/2')
        self.assertEqual(rows[3][1], '/a/file1.stl')
        self.assertEqual(rows[3][4], '2/2')
        self.assertEqual(rows[4][1], '/b/file2.stl')
        self.assertEqual(rows[4][4], '1/1')
        self.assertEqual(rows[5][1], '/a/file1.stl')
        self.assertEqual(rows[5][4], '2/2')


class TestNullLogger(unittest.TestCase):
    def test_accepts_part_argument(self):
        # Must not raise — the callback type now requires `part`.
        null_logger('src', 'start', 'step', '1/1', None, 'detail')


if __name__ == '__main__':
    unittest.main()
