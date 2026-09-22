#!/usr/bin/env python3
"""Milestone 1 of the runner described in docs/refactor/orchestration.md.

Serial intake followed by serial mesh processing and a printed run summary.
Fulfills open-issues.md's startup hard-requirement-check item. Process
isolation, resource admission (cgroup/memory), JSON FileResult format, TUI
display, and run.sh integration are deferred to future work.
"""

import argparse
from collections import Counter
from dataclasses import fields
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from libs import alphawrap, blender, converter, decimator, meshfix, meshlab  # noqa: E402
from libs import mesh_io, processor                                       # noqa: E402
from libs.indicators import Indicator                                    # noqa: E402
from libs.mesh_io import Mesh                                            # noqa: E402


DEPENDENCIES = (
    ('CGAL', alphawrap),
    ('fast_simplification', decimator),
    ('Blender', blender),
    ('PyMeshFix', meshfix),
    ('PyMeshLab', meshlab),
)


def _run(args):
    emitted: list[Mesh] = []

    def collect(mesh: Mesh) -> None:
        emitted.append(mesh)

    try:
        summary = converter.prepare(
            args.input, args.output, collect,
            copy_extensions={'.png', '.jpg', '.txt'},
            convert=blender.convert, workers=1,
        )
    except Exception as error:
        print(f'Run incomplete: intake raised {type(error).__name__}: {error}',
              file=sys.stderr)
        return 1

    terminal = Counter()
    published = Counter()
    diagnostics = []
    for mesh in emitted:
        stage = 'intake'
        category = 'intake_failure'
        reason = mesh.problem or 'invalid intake mesh'
        try:
            if mesh.is_valid:
                stage = 'load'
                category = 'load_failure'
                loaded = mesh_io.load(mesh)
                reason = loaded.problem or 'invalid loaded mesh'
                if loaded.is_valid:
                    stage = 'process'
                    category = 'process_failure'
                    outcome = processor.process(loaded, args.max_faces)
                    stage = 'write'
                    category = 'write_failure'
                    path = processor.write(outcome, mesh.path, mesh.destination)
                    if path is None:
                        reason = ('processor.write returned None: no publication '
                                  f'for {outcome.indicator.name}')
                    else:
                        category = 'published'
                        published[outcome.indicator] += 1
                        stage = 'process'
                        reason = outcome.reason
        except Exception as error:
            reason = f'{type(error).__name__}: {error}'
        terminal[category] += 1
        if category != 'published' or outcome.indicator is not Indicator.PROCESS:
            diagnostics.append((mesh.path, stage, reason))

    total = sum(terminal.values())
    assert total == len(emitted)
    print('Intake: ' + ', '.join(
        f'{field.name}={getattr(summary, field.name)}' for field in fields(summary)))
    print('Terminal: ' + ', '.join(
        f'{name}={terminal[name]}' for name in (
            'intake_failure', 'load_failure', 'process_failure', 'write_failure',
            'published')) + f', total={total}, jobs={len(emitted)}')
    print('Published: ' + (', '.join(
        f'{indicator.name}={published[indicator]}'
        for indicator in Indicator if published[indicator]) or 'none'))
    for path, stage, reason in diagnostics:
        print(f'Diagnostic: path={path!r}, stage={stage}, reason={reason}')
    print('Run complete.')
    return int(bool(diagnostics) or summary.copy_failed > 0)


def _main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, metavar='DIR')
    parser.add_argument('--output', required=True, metavar='DIR')
    parser.add_argument('--max-faces', required=True, metavar='N',
                        help='non-negative integer face budget; 0 disables decimation')
    args = parser.parse_args(argv)
    # Validate paths before the budget, preserving the documented error order.
    if not os.path.isdir(args.input):
        parser.error('--input must exist and be a directory')
    if os.path.isfile(args.output):
        parser.error('--output must be a directory, not an existing file')
    try:
        source = Path(args.input).resolve()
        destination = Path(args.output).resolve()
    except (OSError, RuntimeError) as error:
        parser.error(f'cannot resolve input/output paths: {error}')
    if (source == destination or source in destination.parents
            or destination in source.parents):
        parser.error('--input and --output must not overlap in either direction')
    try:
        args.max_faces = int(args.max_faces)
        if args.max_faces < 0:
            raise ValueError
    except ValueError:
        parser.error('--max-faces must be a non-negative integer')

    missing = []
    for name, module in DEPENDENCIES:
        try:
            available = module.is_available()
        except Exception as error:
            missing.append(f'{name} (check raised {type(error).__name__}: {error})')
        else:
            if not available:
                missing.append(name)
    if missing:
        parser.error('missing required dependencies: ' + ', '.join(missing))
    return _run(args)


def main(argv=None):
    """CLI boundary: interruption never reports a completed run."""
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print('Run interrupted/incomplete.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
