"""Fix selected files beside their sources (`fix_files.py`).

The batch pipeline works on folders: an input tree in, a separate output tree
out, which must not overlap. To fix individual files where they are, each
source folder gets a hidden staging folder INSIDE it, so every move back is a
rename on the same filesystem:

    <folder>/.stl-fix-<random>/
        .stl-fix-staging   sentinel: proves this folder is ours
        in/                one symlink per selected file, same name
        in.decimated/      the pipeline's decimation cache (sibling of in/)
        out/               the pipeline's output tree

The unchanged pipeline runs on in/ -> out/; then each selected `foo.stl`
(or `foo.obj`) gets its results back beside it:

    out/foo.stl            -> foo_fixed.stl
    out/foo<marker>        -> foo_fixed<marker>   (e.g. foo_fixed.failed.stl)
    out/foo.log            -> appended to foo_fixed.log
    out/batch.log, out/progress.log -> appended to <folder>/batch.log, progress.log

Every path is planned per selected file BEFORE the run (`plan`): nothing is
inferred from what out/ happens to hold, and a selection whose paths collide
is rejected as a whole, in every participant, rather than first-come-wins.

The staging folder is removed when the run ends, however it ends. A run
killed hard (SIGKILL, power loss) cannot clean up, so the next run in that
folder sweeps it (`sweep_stale`). Liveness is an flock on the staging
folder's own directory inode, held by the owning run for its whole life;
the folder is locked before it ever carries a sweepable name, because it is
created as `.stl-fix-init-*` (never swept) and renamed once locked.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass

from . import converter, indicators, modellog

FIXED = '_fixed'
STAGING_PREFIX = '.stl-fix-'
INIT_PREFIX = '.stl-fix-init-'
SENTINEL = '.stl-fix-staging'
SENTINEL_TEXT = 'stl-batch-fix staging folder; removed when no run holds its lock\n'
#: Run logs copied back to the source folder, by name in out/.
RUN_LOGS = ('batch.log', 'progress.log')

#: A stand-in output root for planning names before any staging exists;
#: only paths relative to it are kept.
_PLAN_OUT = os.path.join(os.sep, 'out')


@dataclass(frozen=True)
class Job:
    """One selected file and every path its results may take.

    `products` pairs each pipeline output (relative to out/) with its final
    place beside the source: the STL first, then one per marker suffix.
    """
    source: str                               # absolute, lexical, as selected
    folder: str                               # its folder; results land here
    products: tuple[tuple[str, str], ...]     # (out-relative name, final path)
    log: tuple[str, str]                      # (out-relative model log, final log)

    @property
    def name(self) -> str:
        return os.path.basename(self.source)

    def finals(self) -> tuple[str, ...]:
        return tuple(final for _, final in self.products)


def _job(source: str, folder: str, reserved: tuple[str, ...]) -> Job:
    name = os.path.basename(source)
    out_stl = converter.output_for(name, '', '')   # foo.obj -> foo.stl
    out_base = os.path.splitext(out_stl)[0]
    final_base = os.path.join(folder, os.path.splitext(name)[0] + FIXED)
    products = [(out_stl, final_base + '.stl')]
    products += [(out_base + suffix, final_base + suffix)
                 for suffix in indicators.marker_suffixes()]
    log = modellog.path_for(os.path.join(_PLAN_OUT, out_stl),
                            tuple(os.path.join(_PLAN_OUT, r) for r in reserved))
    return Job(source, folder, tuple(products),
               (os.path.relpath(log, _PLAN_OUT), final_base + '.log'))


def plan(paths, reserved: tuple[str, ...] = RUN_LOGS
         ) -> tuple[dict[str, list[Job]], list[tuple[str, str]]]:
    """Group the selected `paths` by folder and plan every result path.

    `reserved` are the run's own log names in out/ (a model named `batch`
    gets `batch.model.log`, as `modellog.path_for` decides). Returns
    `(groups, rejected)`: groups map a folder (lexical, first spelling seen)
    to its jobs in selection order; rejected pairs a path with the reason.
    A file selected twice, by any spelling, is planned once.
    """
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    folders: dict[str, str] = {}               # real folder -> lexical folder
    candidates: dict[str, list[Job]] = {}
    for path in paths:
        source = os.path.abspath(path)
        if not os.path.isfile(source):
            rejected.append((path, 'not a file'))
            continue
        if os.path.splitext(source)[1].lower() not in converter.MESH_EXTENSIONS:
            rejected.append((path, 'not an STL or OBJ file'))
            continue
        real = os.path.realpath(source)
        if real in seen:
            continue
        seen.add(real)
        folder = folders.setdefault(os.path.realpath(os.path.dirname(source)),
                                    os.path.dirname(source))
        candidates.setdefault(folder, []).append(_job(source, folder, reserved))

    groups: dict[str, list[Job]] = {}
    for folder, jobs in candidates.items():
        claims: dict[str, list[Job]] = {}
        for job in jobs:
            outs = [out for out, _ in job.products] + [job.log[0]]
            finals = [os.path.realpath(p) for p in job.finals() + (job.log[1],)]
            for key in [('out', o) for o in outs] + [('final', f) for f in finals]:
                claims.setdefault(key, []).append(job)
        clashing = {id(job) for owners in claims.values() if len(owners) > 1
                    for job in owners}
        kept = []
        for job in jobs:
            reason = None
            if id(job) in clashing:
                reason = 'its results would share a file name with another selected file'
            else:
                for final in job.finals() + (job.log[1],):
                    if os.path.realpath(final) in seen:
                        reason = f'its result {final} would replace a selected file'
                        break
                    if os.path.isdir(final):
                        reason = f'its result {final} exists as a folder'
                        break
            if reason is None:
                kept.append(job)
            else:
                rejected.append((job.source, reason))
        if kept:
            groups[folder] = kept
    return groups, rejected


def _owned_and_unlocked(path: str, fd: int) -> bool:
    """True when `fd` (opened on `path`) is a staging folder of ours whose
    run is gone: lock acquired, sentinel exact, path still that inode."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False                           # a live run holds it
    try:
        sentinel = os.open(SENTINEL, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError:
        return False
    try:
        if os.read(sentinel, len(SENTINEL_TEXT) + 1).decode('utf-8', 'replace') != SENTINEL_TEXT:
            return False
    finally:
        os.close(sentinel)
    held, now = os.fstat(fd), os.lstat(path)
    return (held.st_dev, held.st_ino) == (now.st_dev, now.st_ino)


def sweep_stale(folder: str) -> list[tuple[str, str]]:
    """Remove staging folders in `folder` left by runs that died hard.

    Only real directories named `.stl-fix-*` (never `.stl-fix-init-*`)
    holding the exact sentinel, whose lock no live run holds, are removed,
    under that lock. Returns `(path, problem)` for any that could not be.
    """
    problems = []
    try:
        entries = list(os.scandir(folder))
    except OSError as exc:
        return [(folder, f'cannot list: {exc}')]
    for entry in entries:
        if not entry.name.startswith(STAGING_PREFIX) or entry.name.startswith(INIT_PREFIX):
            continue
        try:
            fd = os.open(entry.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            continue                           # not a real directory of ours
        try:
            if _owned_and_unlocked(entry.path, fd):
                shutil.rmtree(entry.path)
        except OSError as exc:
            problems.append((entry.path, str(exc)))
        finally:
            os.close(fd)
    return problems


class Staging:
    """The locked staging folder for one run in `folder`; a context manager
    that removes it on exit, however the run ended.

    Use: `with Staging(folder) as staging: staging.stage(job) ...`, then
    `config.input = staging.input`, `config.output = staging.output`.
    """

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.root: str | None = None
        self._fd: int | None = None
        #: (path, problem) for stale folders the opening sweep could not remove.
        self.sweep_problems: list[tuple[str, str]] = []
        #: Why the staging folder could not be removed on exit, if it could not.
        self.cleanup_problem: str | None = None

    @property
    def input(self) -> str:
        return os.path.join(self.root, 'in')

    @property
    def output(self) -> str:
        return os.path.join(self.root, 'out')

    def __enter__(self) -> 'Staging':
        self.sweep_problems = sweep_stale(self.folder)
        init = tempfile.mkdtemp(prefix=INIT_PREFIX, dir=self.folder)
        try:
            self._fd = os.open(init, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            with open(os.path.join(init, SENTINEL), 'x', encoding='utf-8') as handle:
                handle.write(SENTINEL_TEXT)
            while True:
                root = os.path.join(self.folder, STAGING_PREFIX + secrets.token_hex(6))
                if not os.path.lexists(root):
                    break
            os.rename(init, root)              # the lock follows the inode
            self.root = root
            os.mkdir(self.input)
            os.mkdir(self.output)
        except BaseException:
            shutil.rmtree(self.root or init, ignore_errors=True)
            self._release()
            raise
        return self

    def stage(self, job: Job) -> None:
        """Link `job.source` into in/ under its own name."""
        os.symlink(job.source, os.path.join(self.input, job.name))

    def publish(self, job: Job) -> tuple[list[str], list[str]]:
        """Move `job`'s new results beside its source; append its model log.

        Each new file replaces its final path first. Only when every new
        file arrived are the job's other, now stale, results removed (an old
        `foo_fixed.failed.stl` beside a new `foo_fixed.stl`); a job with no
        new result, or a failed move, keeps every old file. Returns
        `(published finals, problems)`.
        """
        published, problems = [], []
        produced = [(out, final) for out, final in job.products
                    if os.path.lexists(os.path.join(self.output, out))]
        for out, final in produced:
            try:
                os.replace(os.path.join(self.output, out), final)
                published.append(final)
            except OSError as exc:
                problems.append(f'cannot move {out} to {final}: {exc}')
        if published and len(published) == len(produced):
            for final in job.finals():
                if final not in published and os.path.lexists(final):
                    try:
                        os.remove(final)
                    except OSError as exc:
                        problems.append(f'cannot remove stale {final}: {exc}')
        problem = append_log(os.path.join(self.output, job.log[0]), job.log[1])
        if problem:
            problems.append(problem)
        return published, problems

    def publish_run_logs(self) -> list[str]:
        """Append out/batch.log and out/progress.log to the source folder's."""
        return [problem for name in RUN_LOGS
                if (problem := append_log(os.path.join(self.output, name),
                                          os.path.join(self.folder, name)))]

    def __exit__(self, *exc) -> None:
        """Remove the staging folder. A failure is kept in
        `cleanup_problem` rather than raised, so it never hides the
        exception that ended the run; the next run's sweep retries it."""
        try:
            if self.root is not None:
                shutil.rmtree(self.root)
        except OSError as error:
            self.cleanup_problem = f'cannot remove {self.root}: {error}'
        finally:
            self._release()

    def _release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)                 # closing drops the flock
            self._fd = None


def append_log(source: str, destination: str) -> str | None:
    """Append `source` to `destination`, creating it; a problem or None.

    A missing `source` is no problem (nothing was logged). `destination`
    must be a regular file or absent: never followed through a symlink, so
    appending can never write into another file, a selected mesh included
    (O_NONBLOCK only keeps a FIFO there from hanging the open).
    """
    try:
        reader = open(source, 'rb')
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f'cannot read {source}: {exc}'
    with reader:
        try:
            fd = os.open(destination,
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_NONBLOCK, 0o644)
        except OSError as exc:
            return f'cannot append to {destination}: {exc}'
        with os.fdopen(fd, 'ab') as writer:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return f'cannot append to {destination}: not a regular file'
            try:
                shutil.copyfileobj(reader, writer)
            except OSError as exc:
                return f'cannot append to {destination}: {exc}'
    return None
