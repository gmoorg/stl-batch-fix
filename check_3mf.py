#!/usr/bin/env python3
"""Predict base-layer print risks in a Bambu Studio 3MF before printing.

Read-only: the 3MF is never modified.  For every instance on every plate it
reports (see libs/basecheck.py for the method):

  A  uneven/rippled base — how much of the near-plate footprint reaches layer 1
  B  undersides too low for support yet above layer 1 (printed over air)
  C  shallow near-horizontal surfaces around the foot (banding; informational)

Separate from the repair pipeline (batch_repair.py), so unlike it this takes
command-line arguments.  Exit status: 0 no risk detected within the checks'
scope, 1 a risk was found, 2 the file could not be read, 3 no risk found but
some analysis was incomplete.

    tools/project_python.sh check_3mf.py model.3mf [--plate N] [--png DIR]
"""

from __future__ import annotations

import argparse
import math
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from libs import bambu3mf, basecheck  # noqa: E402
from libs.printsettings import (instance_problems, resolve_settings,  # noqa: E402
                                thresholds_for)

EXIT_OK, EXIT_RISK, EXIT_ERROR, EXIT_INCOMPLETE = 0, 1, 2, 3

SCOPE = ('Scope: predictions from geometry, not a slice. Thresholds are provisional '
         '(not yet checked against sliced output). Check B measures clearance to the '
         'plate only; undersides above other model geometry are not evaluated.')


def _region_line(kind, region: basecheck.Region) -> str:
    text = (f'      {kind}: {region.area:.1f} mm², z {region.z_min:.2f}-{region.z_max:.2f} mm, '
            f'at X {region.centroid[0]:.1f} Y {region.centroid[1]:.1f} '
            f'(+{region.offset[0]:.1f}, +{region.offset[1]:.1f} from the object\'s corner)')
    if region.reach is not None:
        text += f', up to {region.reach:.2f} mm from layer-1 contact'
    if region.facing:
        text += f', facing {region.facing}'
    return text


def report(instance, settings, notes, problems, result, thresholds, out) -> str:
    """Print one instance's findings; return its status."""
    status = 'risk' if result.risks else 'incomplete' if (problems or result.incomplete) else 'ok'
    print(f'  [{status.upper()}] {instance.name} (object {instance.object_id}, '
          f'instance {instance.instance_id})', file=out)
    print('    settings: ' + ', '.join(
        f'{k}={v.value} [{v.source}]' for k, v in settings.items()), file=out)
    for line in problems + list(result.incomplete):
        print(f'    INCOMPLETE: {line}', file=out)
    for line in notes + list(result.notes):
        print(f'    note: {line}', file=out)
    base = result.base
    if base is not None:
        print(f'    A base: footprint {base.footprint_area:.1f} mm², layer-1 contact '
              f'{base.contact_area:.1f} mm² ({base.contact_fraction:.0%}) in '
              f'{base.contact_islands} island(s), largest {base.largest_island:.1f} mm²; '
              f'roughness {base.roughness:.3f} mm, ripple RMS '
              + ('n/a (footprint too small)' if base.ripple_rms is None
                 else f'{base.ripple_rms:.3f} mm'), file=out)
        if base.sink > 0:
            print(f'      optional: lowering the object {base.sink:.2f} mm into the plate would '
                  f'put {thresholds.sink_quantile:.0%} of the footprint in layer 1 '
                  f'(cuts {base.sink:.2f} mm off the model)', file=out)
    for line in result.risks:
        print(f'    RISK: {line}', file=out)
    for region in result.unsupportable:
        print(_region_line('B too low for support', region), file=out)
    for region in result.unsupported_overhangs:
        print(_region_line('B unsupported overhang', region), file=out)
    for region in result.shallow[:10]:
        print(_region_line('C shallow surface', region), file=out)
    if len(result.shallow) > 10:
        print(f'      C ... and {len(result.shallow) - 10} smaller shallow region(s)', file=out)
    return status


def save_png(directory, plate, instance, result) -> str | None:
    if result.grid is None:
        return None
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    grid = result.grid
    os.makedirs(directory, exist_ok=True)
    name = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in instance.name)
    path = os.path.join(directory, f'plate{plate}_{name}_{instance.object_id}_{instance.instance_id}.png')
    ny, nx = grid.height.shape
    extent = (grid.x0, grid.x0 + nx * grid.cell, grid.y0, grid.y0 + ny * grid.cell)
    # Long thin parts would make an unreadable strip; stretch them instead.
    ratio = ny / max(nx, 1)
    aspect = 'equal' if 0.25 <= ratio <= 4 else 'auto'
    fig, ax = plt.subplots(figsize=(8, min(max(8 * ratio, 4), 10) + 1))
    image = ax.imshow(grid.height, origin='lower', extent=extent, cmap='viridis', aspect=aspect)
    fig.colorbar(image, ax=ax, label='underside height above plate (mm)')
    overlay = np.zeros((ny, nx, 4))
    overlay[grid.flagged] = (1, 0, 0, 0.8)
    ax.imshow(overlay, origin='lower', extent=extent, aspect=aspect)
    ax.set_title(f'{instance.name}: red = too low for support, above layer 1')
    ax.set_xlabel('X (mm)')
    ax.set_ylabel('Y (mm)')
    fig.savefig(path, dpi=120, bbox_inches='tight')
    plt.close(fig)
    return path


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('path', help='Bambu Studio (or plain) 3MF file')
    parser.add_argument('--plate', type=int, help='only this plate number')
    parser.add_argument('--png', metavar='DIR', help='write an underside map per instance')
    group = parser.add_argument_group('settings (default: read from the 3MF)')
    group.add_argument('--first-layer', type=float, help='initial layer height, mm')
    group.add_argument('--layer-height', type=float, help='layer height, mm')
    group.add_argument('--support-gap', type=float, help='support top Z distance, mm')
    group.add_argument('--support-angle', type=float, help='support threshold angle, degrees')
    group = parser.add_argument_group('analysis thresholds (provisional defaults)')
    defaults = basecheck.Thresholds()
    group.add_argument('--cell', type=float, help=f'map resolution, mm (default {defaults.cell})')
    group.add_argument('--foot-height', type=float,
                       help=f'band examined for C, mm (default {defaults.foot_height})')
    group.add_argument('--shallow-angle', type=float,
                       help=f'C: max angle from horizontal, degrees (default {defaults.shallow_angle})')
    group.add_argument('--min-region-area', type=float,
                       help=f'smallest reported region, mm² (default {defaults.min_region_area})')
    group.add_argument('--min-contact-fraction', type=float,
                       help=f'A: risk below this (default {defaults.min_contact_fraction})')
    group.add_argument('--max-extra-islands', type=int,
                       help='A: ripple risk when layer-1 contact has more islands than this '
                            f'beyond one per footprint region (default {defaults.max_extra_islands})')
    group.add_argument('--sink-quantile', type=float,
                       help=f'A: fraction the sink suggestion targets (default {defaults.sink_quantile})')
    return parser.parse_args(argv)


def main(argv=None, out=sys.stdout) -> int:
    args = parse_args(argv)
    try:
        project = bambu3mf.read(args.path)
    except bambu3mf.ReadError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR

    print(f'{args.path}', file=out)
    print(SCOPE, file=out)
    if not project.is_bambu:
        print('note: not a Bambu Studio project — no plates, part types or settings; '
              'every part treated as printable and defaults used', file=out)
    instances = [i for i in project.instances if args.plate is None or i.plate == args.plate]
    if not instances:
        print(f'error: no instances{"" if args.plate is None else f" on plate {args.plate}"}',
              file=sys.stderr)
        return EXIT_ERROR

    statuses = []
    for plate in sorted({i.plate for i in instances}):
        print(f'\nPlate {plate}' + (' (not on any plate)' if plate == 0 else ''), file=out)
        for instance in (i for i in instances if i.plate == plate):
            if not instance.printable:
                print(f'  [SKIPPED] {instance.name}: marked not printable', file=out)
                continue
            try:
                settings, notes = resolve_settings(project, instance, args)
                thresholds = thresholds_for(settings, args)
            except ValueError as error:
                print(f'error: {error}', file=sys.stderr)
                return EXIT_ERROR
            problems = instance_problems(instance)
            if not settings['support_on_build_plate_only'].value:
                notes.append('supports may also start on the model; their clearance is not checked')
            parts = [(p.vertices, p.faces) for p in instance.normal_parts]
            try:
                result = basecheck.check(parts, thresholds, keep_grid=bool(args.png))
            except (ValueError, MemoryError) as error:
                problems.append(f'analysis failed: {error}')
                result = basecheck.Result(None, (), (), (), (), (), (), thresholds.cell, math.nan)
            statuses.append(report(instance, settings, notes, problems, result, thresholds, out))
            if args.png:
                path = save_png(args.png, plate, instance, result)
                if path:
                    print(f'    map: {path}', file=out)

    risky = statuses.count('risk')
    incomplete = statuses.count('incomplete')
    print(f'\nSummary: {len(statuses)} instance(s), {risky} at risk, {incomplete} incomplete, '
          f'{statuses.count("ok")} with no risk detected', file=out)
    if risky:
        return EXIT_RISK
    if incomplete:
        return EXIT_INCOMPLETE
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
