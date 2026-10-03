#!/usr/bin/env python3
"""Internal: repair ONE file for `batch_repair.py`. Not for direct use.

The batch runner spawns this script once per file, so a timeout, native
crash, OOM kill or stray Blender descendant ends only this process. Every
value it needs arrives on the command line from the parent; it never reads
`batch_repair.toml`, so editing the config mid-run cannot change a running
batch. `batch_repair.py` itself takes no arguments — this script is where
the per-file arguments live instead.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import batch_repair                                                       # noqa: E402
from libs import childresult, steplog                                     # noqa: E402


def run_one_file(args) -> int:
    """Process `args.one_file` and write exactly one `ChildResult` to
    `args.result_file`.

    Exit code carries no meaning the parent trusts — it never gates whether
    a result is believed, only whether a result *file* is present, valid,
    and matches this job's own source path (see `libs.childresult`).  A
    child that dies before writing (OOM-killed, segfault inside CGAL,
    SIGKILL from the parent) simply never produces the file, which is
    itself the signal the parent's crash/reconciliation path acts on.

    `args.managed_child` is the explicit marker that this run lives inside a
    `proctree`-owned process group: `batch_repair._spawn_child` always sets
    it. Present -> `nested_process_group=True` reaches `processor.process`,
    so a nested `step_blender_repair` Blender does not get its own session
    (staying part of the enclosing worker's own group, which
    `terminate_and_confirm` can then still reach). Absent (a direct
    diagnostic or test invocation) -> `False`, the safe default.
    """
    step_logger = (steplog.open_step_log(args.log_file) if args.log_file
                   else steplog.null_logger)
    result = batch_repair._process_one_file(
        args.one_file, args.destination, args.max_faces, step_logger,
        nested_process_group=args.managed_child, skip_clean=args.skip_clean)
    childresult.write(args.result_file, result)
    return 0


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a non-negative integer') from None
    if value < 0:
        raise argparse.ArgumentTypeError('must be a non-negative integer')
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--one-file', required=True, metavar='SRC')
    parser.add_argument('--destination', required=True, metavar='DST')
    parser.add_argument('--result-file', required=True, metavar='PATH')
    parser.add_argument('--max-faces', required=True, type=_non_negative_int, metavar='N')
    parser.add_argument('--managed-child', action='store_true')
    parser.add_argument('--log-file', metavar='PATH')
    parser.add_argument('--skip-clean', action='store_true')
    return run_one_file(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
