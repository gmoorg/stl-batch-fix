"""Fixing selected files beside their sources (`libs.besidesource`,
`fix_files.py`): planned names, rejected selections, publication, log
appending and the staging folder's life. Fast: no real repair runs here
(test_fix_files_smoke.py runs the real pipeline)."""

import fcntl
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest import mock

import batch_repair
import fix_files
from libs import besidesource
from libs.besidesource import Staging

PROJECT = Path(__file__).resolve().parent.parent.parent


class _Folder(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / 'models'
        self.folder.mkdir()

    def file(self, name, data=b'mesh', folder=None):
        path = (folder or self.folder) / name
        path.write_bytes(data)
        return str(path)

    def staging_left(self, folder=None):
        return [p.name for p in (folder or self.folder).iterdir()
                if p.name.startswith(besidesource.STAGING_PREFIX)]


class TestPlan(_Folder):
    def test_names_beside_the_source(self):
        groups, rejected = besidesource.plan([self.file('foot one.stl'), self.file('leg.obj'),
                                              self.file('ARM.STL')])
        self.assertEqual(rejected, [])
        jobs = groups[str(self.folder)]
        finals = {job.name: job.finals() for job in jobs}
        self.assertEqual(os.path.basename(finals['foot one.stl'][0]), 'foot one_fixed.stl')
        self.assertIn(str(self.folder / 'foot one_fixed.failed.stl'), finals['foot one.stl'])
        self.assertEqual(os.path.basename(finals['leg.obj'][0]), 'leg_fixed.stl')
        self.assertEqual(os.path.basename(finals['ARM.STL'][0]), 'ARM_fixed.stl')
        self.assertEqual(jobs[1].products[0][0], 'leg.stl')        # OBJ comes out as STL
        self.assertEqual(jobs[0].log, ('foot one.log', str(self.folder / 'foot one_fixed.log')))

    def test_selecting_a_result_of_another_selection_is_rejected(self):
        foo, fixed = self.file('foo.stl'), self.file('foo_fixed.stl')
        groups, rejected = besidesource.plan([foo, fixed])
        self.assertEqual([path for path, _ in rejected], [foo])
        self.assertEqual([job.source for job in groups[str(self.folder)]], [fixed])

    def test_same_stem_rejects_every_participant(self):
        stl, obj = self.file('foo.stl'), self.file('foo.obj')
        groups, rejected = besidesource.plan([stl, obj])
        self.assertEqual(groups, {})
        self.assertEqual(sorted(path for path, _ in rejected), sorted([stl, obj]))

    def test_model_log_name_clash_rejects_both(self):
        # `batch.stl` logs to batch.model.log (batch.log is the run's), the
        # very name `batch.model.stl` logs to.
        one, two = self.file('batch.stl'), self.file('batch.model.stl')
        groups, rejected = besidesource.plan([one, two])
        self.assertEqual(groups, {})
        self.assertEqual(len(rejected), 2)

    def test_result_path_that_is_a_folder_is_rejected(self):
        foo = self.file('foo.stl')
        (self.folder / 'foo_fixed.stl').mkdir()
        groups, rejected = besidesource.plan([foo])
        self.assertEqual((groups, [path for path, _ in rejected]), ({}, [foo]))

    def test_non_meshes_and_missing_files_are_rejected(self):
        text, missing = self.file('notes.txt'), str(self.folder / 'gone.stl')
        groups, rejected = besidesource.plan([text, missing, str(self.folder)])
        self.assertEqual(groups, {})
        self.assertEqual(len(rejected), 3)

    def test_folder_aliases_plan_one_job_per_file(self):
        foo = self.file('foo.stl')
        alias = self.root / 'alias'
        alias.symlink_to(self.folder, target_is_directory=True)
        groups, rejected = besidesource.plan([foo, str(alias / 'foo.stl')])
        self.assertEqual(rejected, [])
        self.assertEqual([len(jobs) for jobs in groups.values()], [1])

    def test_symlinked_file_keeps_its_selected_name_and_place(self):
        target = self.root / 'elsewhere'
        target.mkdir()
        (self.folder / 'link.stl').symlink_to(self.file('original.stl', folder=target))
        groups, _ = besidesource.plan([str(self.folder / 'link.stl')])
        self.assertEqual(groups[str(self.folder)][0].finals()[0],
                         str(self.folder / 'link_fixed.stl'))

    def test_several_folders_are_separate_groups(self):
        other = self.root / 'other'
        other.mkdir()
        groups, _ = besidesource.plan([self.file('a.stl'), self.file('b.stl', folder=other)])
        self.assertEqual(sorted(groups), sorted([str(self.folder), str(other)]))


class TestStaging(_Folder):
    def test_removed_after_a_normal_run(self):
        with Staging(str(self.folder)) as staging:
            self.assertTrue(os.path.isdir(staging.input))
            Path(staging.output, 'x.stl').write_bytes(b'x')
        self.assertEqual(self.staging_left(), [])

    def test_removed_when_the_run_raises(self):
        with self.assertRaises(KeyboardInterrupt):
            with Staging(str(self.folder)):
                raise KeyboardInterrupt
        self.assertEqual(self.staging_left(), [])

    def stale(self, name, sentinel=besidesource.SENTINEL_TEXT):
        path = self.folder / name
        (path / 'out').mkdir(parents=True)
        if sentinel is not None:
            (path / besidesource.SENTINEL).write_text(sentinel)
        return path

    def test_sweep_removes_only_unlocked_folders_of_ours(self):
        dead = self.stale('.stl-fix-dead')
        live = self.stale('.stl-fix-live')
        init = self.stale('.stl-fix-init-abc')
        foreign = self.stale('.stl-fix-foreign', sentinel=None)
        wrong = self.stale('.stl-fix-wrong', sentinel='something else\n')
        (self.folder / '.stl-fix-link').symlink_to(dead, target_is_directory=True)
        fd = os.open(live, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)        # a live run holds this one
        self.assertEqual(besidesource.sweep_stale(str(self.folder)), [])
        self.assertFalse(dead.exists())
        for kept in (live, init, foreign, wrong):
            self.assertTrue(kept.exists(), kept.name)
        self.assertTrue((self.folder / '.stl-fix-link').is_symlink())

    def test_a_live_staging_folder_survives_another_runs_sweep(self):
        with Staging(str(self.folder)) as staging:
            besidesource.sweep_stale(str(self.folder))
            self.assertTrue(os.path.isdir(staging.root))


class TestPublish(_Folder):
    def job(self, name='foo.stl'):
        groups, _ = besidesource.plan([self.file(name)])
        return groups[str(self.folder)][0]

    def test_new_result_replaces_old_and_removes_stale_marker(self):
        job = self.job()
        self.file('foo_fixed.stl', b'old')
        self.file('foo_fixed.failed.stl', b'old failure')
        with Staging(str(self.folder)) as staging:
            Path(staging.output, 'foo.stl').write_bytes(b'new')
            published, problems = staging.publish(job)
        self.assertEqual((published, problems), ([str(self.folder / 'foo_fixed.stl')], []))
        self.assertEqual((self.folder / 'foo_fixed.stl').read_bytes(), b'new')
        self.assertFalse((self.folder / 'foo_fixed.failed.stl').exists())

    def test_marker_lands_beside_and_replaces_old_result(self):
        job = self.job()
        self.file('foo_fixed.stl', b'old')
        with Staging(str(self.folder)) as staging:
            Path(staging.output, 'foo.failed.stl').write_bytes(b'source copy')
            staging.publish(job)
        self.assertEqual((self.folder / 'foo_fixed.failed.stl').read_bytes(), b'source copy')
        self.assertFalse((self.folder / 'foo_fixed.stl').exists())

    def test_no_new_result_keeps_old_files(self):
        job = self.job()
        self.file('foo_fixed.stl', b'old')
        with Staging(str(self.folder)) as staging:
            self.assertEqual(staging.publish(job), ([], []))
        self.assertEqual((self.folder / 'foo_fixed.stl').read_bytes(), b'old')

    def test_failed_move_keeps_old_results(self):
        job = self.job()
        self.file('foo_fixed.failed.stl', b'old failure')
        blocker = self.folder / 'foo_fixed.stl'        # a non-empty folder: rename fails
        blocker.mkdir()
        (blocker / 'x').write_bytes(b'x')
        with Staging(str(self.folder)) as staging:
            Path(staging.output, 'foo.stl').write_bytes(b'new')
            published, problems = staging.publish(job)
        self.assertEqual(published, [])
        self.assertEqual(len(problems), 1)
        self.assertEqual((self.folder / 'foo_fixed.failed.stl').read_bytes(), b'old failure')
        self.assertEqual(self.staging_left(), [])

    def test_logs_are_appended(self):
        job = self.job()
        self.file('foo_fixed.log', b'run 1\n')
        self.file('batch.log', b'batch 1\n')
        with Staging(str(self.folder)) as staging:
            Path(staging.output, 'foo.log').write_bytes(b'run 2\n')
            Path(staging.output, 'batch.log').write_bytes(b'batch 2\n')
            Path(staging.output, 'progress.log').write_bytes(b'{}\n')
            staging.publish(job)
            self.assertEqual(staging.publish_run_logs(), [])
        self.assertEqual((self.folder / 'foo_fixed.log').read_bytes(), b'run 1\nrun 2\n')
        self.assertEqual((self.folder / 'batch.log').read_bytes(), b'batch 1\nbatch 2\n')
        self.assertEqual((self.folder / 'progress.log').read_bytes(), b'{}\n')

    def test_log_append_never_follows_a_symlink(self):
        source = self.file('mesh.stl', b'precious')
        (self.folder / 'batch.log').symlink_to(source)
        log = self.file('new.log', b'appended')
        self.assertIsNotNone(besidesource.append_log(log, str(self.folder / 'batch.log')))
        self.assertEqual(Path(source).read_bytes(), b'precious')


class TestFixFiles(_Folder):
    """The front end's loop, with the pipeline replaced by `run`."""

    def setUp(self):
        super().setUp()
        config = self.root / 'batch_repair.toml'
        config.write_text('input = "in"\noutput = "out"\nmax_faces = 0\n')
        for patcher in (mock.patch.object(batch_repair, 'CONFIG_PATH', str(config)),
                        mock.patch('sys.stdout', new_callable=io.StringIO)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []

    def run_returning(self, outcome):
        def run(config, reconstruction, *, label):
            self.calls.append((config, reconstruction))
            Path(config.output, 'a.stl').write_bytes(b'fixed')
            return outcome
        return run

    def test_reconstruction_follows_the_flag(self):
        path = self.file('a.stl')
        run = self.run_returning(batch_repair.RunOutcome(0))
        self.assertEqual(fix_files._main(['--alpha', '--', path], run=run), 0)
        self.assertEqual(fix_files._main([path], run=run), 0)
        self.assertEqual([r for _, r in self.calls], ['alpha-wrap', 'winding'])
        staged = self.calls[0][0]
        self.assertEqual((staged.log_file, staged.max_faces), ('', 0))
        self.assertTrue((self.folder / 'a_fixed.stl').exists())
        self.assertEqual(self.staging_left(), [])

    def test_large_runs_one_file_with_the_large_file_config(self):
        path = self.file('a.stl')
        run = self.run_returning(batch_repair.RunOutcome(0))
        large = lambda config: replace(config, workers=1, per_file_timeout=None,
                                       memory_budget_bytes=123,
                                       reconstruct_memory_ceiling_bytes=123)
        with mock.patch.object(fix_files.runconfig, 'large_file', side_effect=large):
            self.assertEqual(fix_files._main(['--large', '--', path], run=run), 0)
        config, reconstruction = self.calls[0]
        self.assertEqual(reconstruction, 'winding')
        self.assertEqual((config.workers, config.per_file_timeout,
                          config.reconstruct_memory_ceiling_bytes), (1, None, 123))
        self.assertTrue((self.folder / 'a_fixed.stl').exists())

    def test_large_rejects_alpha_and_several_files(self):
        a, b = self.file('a.stl'), self.file('b.stl')
        run = self.run_returning(batch_repair.RunOutcome(0))
        for argv in (['--large', '--alpha', '--', a], ['--large', '--', a, b]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as ctx:
                fix_files._main(argv, run=run)
            self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(self.calls, [])

    def test_large_mode_memory_problem_stops_before_running(self):
        path = self.file('a.stl')
        run = self.run_returning(batch_repair.RunOutcome(0))
        with mock.patch.object(fix_files.runconfig, 'large_file',
                               side_effect=fix_files.ConfigError('large mode: no meminfo')), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(fix_files._main(['--large', path], run=run), 2)
        self.assertEqual(self.calls, [])

    def test_leading_hyphen_file_name(self):
        self.file('-a.stl')
        cwd = os.getcwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(fix_files._main(['--', '-a.stl'], run=self.run_returning(
            batch_repair.RunOutcome(0))), 0)
        self.assertEqual(len(self.calls), 1)

    def test_interruption_stops_later_folders(self):
        other = self.root / 'other'
        other.mkdir()
        paths = [self.file('a.stl'), self.file('a.stl', folder=other)]
        run = self.run_returning(batch_repair.RunOutcome(1, interrupted=True))
        self.assertEqual(fix_files._main(paths, run=run), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.folder / 'a_fixed.stl').exists())    # finished work kept
        self.assertEqual(self.staging_left(), [])

    def test_rejection_makes_the_run_fail(self):
        path, text = self.file('a.stl'), self.file('a.txt')
        run = self.run_returning(batch_repair.RunOutcome(0))
        self.assertEqual(fix_files._main([path, text], run=run), 1)
        self.assertEqual(len(self.calls), 1)


@unittest.skipUnless(os.path.exists(batch_repair.CONFIG_PATH),
                     'fix_files.sh needs a batch_repair.toml beside the scripts')
class TestLauncher(_Folder):
    """fix_files.sh itself: the repository's launcher, with a selection it
    rejects before any run, so no pipeline work is done."""

    def test_relative_names_resolve_against_the_callers_folder(self):
        self.file('notes.txt')
        result = subprocess.run(
            [str(PROJECT / 'fix_files.sh'), '--', 'notes.txt', '-x.txt'],
            cwd=self.folder, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f'skipped {self.folder}/notes.txt: not an STL or OBJ file',
                      result.stdout)
        self.assertIn(f'skipped {self.folder}/-x.txt: not a file', result.stdout)


if __name__ == '__main__':
    unittest.main()
