#!/usr/bin/env python3
"""Fix selected STL/OBJ files beside their sources: `foo.stl` -> `foo_fixed.stl`.

    fix_files.py [--alpha] [--] FILE...
    fix_files.py --large [--] FILE

`--large` (the third right-click entry) is for one model too big for the
normal budget: `runconfig.large_file` runs it with one worker, no per-file
time limit and a RAM+swap allowance the rebuild may plan into when the
configured per-part target cannot fit it.

The file manager's right-click entries run this (tools/nemo/). Every option
comes from the same `batch_repair.toml` as a folder run; only `input`,
`output` and `log_file` are replaced, by a hidden staging folder inside each
source folder (`libs.besidesource`), and the unchanged pipeline runs there
through `batch_repair.run`. Results, markers (`foo_fixed.failed.stl`, …) and
logs come back beside the sources, replacing earlier results of the same
file; the staging folder is then removed.

Source folders are processed one at a time. Ctrl+C stops the run, brings
back what had finished, removes the staging folder and skips the remaining
folders.
"""

import argparse
import os
import sys
from dataclasses import replace

import batch_repair
from libs import besidesource, repairer, runconfig
from libs.runconfig import ConfigError, RunConfig

LARGE_FLAG = '--large'

def _parse(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='fix_files.py',
        description='Repair STL/OBJ files; results land beside each as NAME_fixed.stl.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(batch_repair.ALPHA_FLAG, action='store_true',
                      help='alpha-wrap reconstruction instead of winding')
    mode.add_argument(LARGE_FLAG, action='store_true',
                      help='one file, RAM+swap allowance, no time limit (runconfig.large_file)')
    parser.add_argument('files', nargs='+', metavar='FILE')
    args = parser.parse_args(argv)
    if args.large and len(args.files) != 1:
        parser.error(f'{LARGE_FLAG} takes exactly one file')
    return args


def reserved_names(config: RunConfig) -> tuple[str, ...]:
    """The staged run's own log names, relative to its output folder."""
    out = os.path.join(os.sep, 'out')
    staged = replace(config, output=out, log_file='')
    return tuple(os.path.relpath(path, out) for path in batch_repair.reserved_logs(staged))


def _fix_folder(folder, jobs, config, reconstruction, run):
    """Run one folder's jobs in a staging folder; returns (outcome, ok)."""
    ok = True
    print(f'\n=== {folder}: {len(jobs)} file(s) ===', flush=True)
    staging = besidesource.Staging(folder)
    with staging:
        for path, problem in staging.sweep_problems:
            print(f'warning: cannot remove stale staging folder {path}: {problem}')
        for job in jobs:
            staging.stage(job)
        staged = replace(config, input=staging.input, output=staging.output, log_file='')
        try:
            outcome = run(staged, reconstruction, label=batch_repair.CONFIG_PATH)
        except KeyboardInterrupt:
            outcome = batch_repair.RunOutcome(1, interrupted=True)
        print()
        for job in jobs:
            published, problems = staging.publish(job)
            shown = ', '.join(os.path.basename(p) for p in published) or 'no result'
            print(f'{job.name} -> {shown}')
            for problem in problems:
                print(f'  error: {problem}')
            ok = ok and not problems
        for problem in staging.publish_run_logs():
            print(f'error: {problem}')
            ok = False
        if outcome.unresolved:
            print(f'warning: {outcome.unresolved} worker(s) could not be confirmed '
                  f'stopped; one may still write under {staging.root}')
    if staging.cleanup_problem:
        print(f'error: {staging.cleanup_problem}')
        ok = False
    return outcome, ok


def _main(argv, run=batch_repair.run) -> int:
    args = _parse(argv)
    reconstruction = 'alpha-wrap' if args.alpha else repairer.RECONSTRUCTIONS[0]
    try:
        config = runconfig.load(batch_repair.CONFIG_PATH)
        if args.large:
            config = runconfig.large_file(config)
    except ConfigError as error:
        print(f'fix_files: error: {batch_repair.CONFIG_PATH}: {error}', file=sys.stderr)
        return 2
    if args.large:
        print(f'large mode: one worker, no time limit, per-part target '
              f'{config.reconstruct_memory_budget_gb:g} GB, RAM+swap allowance '
              f'{config.reconstruct_memory_ceiling_bytes / 1e9:.1f} GB '
              f'(slow while it swaps; Ctrl+C stops it)', flush=True)

    groups, rejected = besidesource.plan(args.files, reserved_names(config))
    for path, reason in rejected:
        print(f'skipped {path}: {reason}')
    status = 1 if rejected else 0
    for number, (folder, jobs) in enumerate(groups.items()):
        try:
            outcome, ok = _fix_folder(folder, jobs, config, reconstruction, run)
        except OSError as error:               # staging could not be set up
            print(f'error: {folder}: {error}')
            outcome, ok = batch_repair.RunOutcome(1), False
        if outcome.status or not ok:
            status = max(status, outcome.status or 1)
        if outcome.interrupted or outcome.unresolved:
            left = len(groups) - number - 1
            if left:
                print(f'Stopped: {left} folder(s) not processed.')
            return status or 1
    return status


def main(argv=None) -> int:
    """CLI boundary: interruption never reports success."""
    if argv is None:
        argv = sys.argv[1:]
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print('Interrupted.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
