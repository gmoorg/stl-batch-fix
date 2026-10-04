"""Tests for libs.modellog — the raw per-model log's path and line formats."""

import os
import re
import tempfile
import unittest
from pathlib import Path

from libs import modellog


class TestModelLog(unittest.TestCase):

    def test_path_is_beside_the_output_with_log_extension(self):
        self.assertEqual(modellog.path_for('/out/sub/foot.stl'), '/out/sub/foot.log')

    def test_header_names_time_run_stage_and_source(self):
        line = modellog.header('run-1', 'repair', '/in/foot.stl')
        self.assertRegex(line, r'^\n==== \d{4}-\d\d-\d\d \d\d:\d\d:\d\d  run run-1  '
                               r'repair: /in/foot\.stl ====\n$')

    def test_write_header_creates_the_folder_and_appends(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'new', 'foot.log')
            modellog.write_header(path, 'r1', 'conversion', 'a.obj')
            modellog.write_header(path, 'r2', 'repair', 'a.obj')
            text = Path(path).read_text()
        self.assertEqual(text.count('===='), 4)
        self.assertLess(text.index('run r1'), text.index('run r2'))

    def test_separators_bracket_a_step_on_one_line_each(self):
        start = modellog.separator('start', 'alpha_wrap', '1/2', None, '')
        end = modellog.separator('end', 'alpha_wrap', '1/2', 58.412, 'alpha=0.05,\noffset=0.02')
        self.assertRegex(start, r'^---- \d\d:\d\d:\d\d  start alpha_wrap \[1/2\]\n$')
        self.assertRegex(end, r'^---- \d\d:\d\d:\d\d  end   alpha_wrap \[1/2\] 58\.41s  '
                              r'alpha=0\.05, offset=0\.02\n$')


if __name__ == '__main__':
    unittest.main()
