#!/usr/bin/env python3
"""Add thin breakaway ribs under near-plate overhangs in a Bambu Studio 3MF.

Writes a NEW 3MF; the original is never modified.  For each instance it
finds the undersides the slicer cannot support (check B of check_3mf.py:
above the layer-1 slice, below first layer + support_top_z_distance) and adds
one part to the object holding single-line walls ("ribs") from the plate up
to the underside, so that material is printed on ribs instead of over air.

Coverage is decided automatically:
  - always the unsupportable band;
  - also shallow undersides up to --max-height, but only when the 3MF has
    automatic supports switched off (otherwise the slicer's own supports are
    expected to carry them — not checked here).

An instance is skipped, and says why, when its analysis cannot be trusted
(negative volumes, unknown parts, modifiers/support painting parts, per-part
or unreadable settings) or its object has several instances.

Whether the file opens in Bambu Studio, whether the ribs appear in the
sliced preview and whether they snap off cleanly must be checked by you:
nothing here slices or prints.

Exit status: 0 written, every instance handled; 3 written but some instances
skipped, or nothing written because every candidate was skipped; 4 nothing
needed ribs (no file written); 2 error.

    tools/project_python.sh support_3mf.py model.3mf [-o out.3mf]
"""

from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from libs import bambu3mf, basecheck, ribsupport  # noqa: E402
from libs.printsettings import (instance_problems, resolve_settings,  # noqa: E402
                                settings_uncertainty, thresholds_for)

EXIT_WRITTEN, EXIT_ERROR, EXIT_PARTIAL, EXIT_NOTHING = 0, 2, 3, 4
PART_NAME = 'generated support ribs'


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('path', help='Bambu Studio 3MF file')
    parser.add_argument('-o', '--output',
                        help='new 3MF to write (default: <name>.supported.3mf beside the input); '
                             'must not exist')
    parser.add_argument('--pitch', type=float, default=2.0, help='distance between ribs, mm')
    parser.add_argument('--width', type=float,
                        help='rib thickness, mm (default: the project line width, else 0.42)')
    parser.add_argument('--overlap', type=float, default=0.05,
                        help='how far rib tops rise into the model, mm')
    parser.add_argument('--max-height', type=float, default=3.0,
                        help='highest underside ribbed when automatic supports are off, mm')
    parser.add_argument('--cell', type=float, help='analysis resolution, mm (default 0.05)')
    group = parser.add_argument_group('settings (default: read from the 3MF)')
    group.add_argument('--first-layer', type=float, help='initial layer height, mm')
    group.add_argument('--layer-height', type=float, help='layer height, mm')
    group.add_argument('--support-gap', type=float, help='support top Z distance, mm')
    group.add_argument('--support-angle', type=float, help='support threshold angle, degrees')
    return parser.parse_args(argv)


def _width(project, args) -> float:
    if args.width is not None:
        return args.width
    for key in ('outer_wall_line_width', 'line_width'):
        try:
            value = float(project.settings[key])
        except (KeyError, ValueError):
            continue
        if value > 0:
            return value
    return 0.42


def _default_output(path: str) -> str:
    base, _ = os.path.splitext(path)
    return base + '.supported.3mf'


def _areas(result: basecheck.Result) -> tuple[float, float]:
    contact = result.base.contact_area if result.base else 0.0
    return contact, sum(r.area for r in result.unsupportable)


def main(argv=None, out=sys.stdout) -> int:
    args = parse_args(argv)
    try:
        project = bambu3mf.read(args.path)
    except bambu3mf.ReadError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR
    if not project.is_bambu:
        print('error: not a Bambu Studio project', file=sys.stderr)
        return EXIT_ERROR
    output = args.output or _default_output(args.path)
    try:
        rib_settings = ribsupport.RibSettings(pitch=args.pitch, width=_width(project, args),
                                              overlap=args.overlap)
        if not (args.max_height > 0):
            raise ValueError(f'max-height must be positive, got {args.max_height}')
    except ValueError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR

    counts = {}
    for instance in project.instances:
        counts[instance.object_id] = counts.get(instance.object_id, 0) + 1

    print(args.path, file=out)
    additions, before, skipped = [], {}, 0
    for instance in project.instances:
        label = f'{instance.name} (object {instance.object_id})'
        if not instance.printable:
            print(f'  [SKIPPED] {label}: marked not printable', file=out)
            continue
        try:
            settings, _ = resolve_settings(project, instance, args)
            thresholds = thresholds_for(settings, args,
                                        foot_height=max(args.max_height, 3.0),
                                        min_region_area=rib_settings.min_area)
        except ValueError as error:
            print(f'error: {error}', file=sys.stderr)
            return EXIT_ERROR
        reasons = (instance_problems(instance)
                   + settings_uncertainty(project, instance, settings))
        if counts[instance.object_id] > 1:
            reasons.append(f'object has {counts[instance.object_id]} instances; one added part '
                           'cannot fit them all')
        parts = [(p.vertices, p.faces) for p in instance.normal_parts]
        result = None
        if not reasons:
            result = basecheck.check(parts, thresholds, keep_grid=True)
            reasons += list(result.incomplete)
        mask = None
        if result is not None and result.grid is not None:
            grid = result.grid
            mask = grid.flagged.copy()
            if not settings['supports_enabled'].value:
                mask |= grid.overhang & (grid.height < args.max_height)
        if reasons:
            skipped += 1
            print(f'  [SKIPPED] {label}: ' + '; '.join(reasons), file=out)
            continue
        if mask is None or not mask.any():
            print(f'  [NOT NEEDED] {label}', file=out)
            continue
        ribs = ribsupport.ribs(grid.height, mask, grid.x0, grid.y0, grid.cell, rib_settings)
        if ribs.empty:
            print(f'  [NOT NEEDED] {label}: flagged areas too small or too stepped for ribs',
                  file=out)
            continue
        additions.append(bambu3mf.Addition(instance, PART_NAME, ribs.vertices, ribs.faces))
        before[instance.object_id] = (_areas(result), thresholds)
        print(f'  [RIBS] {label}: {ribs.walls} wall(s) in {ribs.regions} region(s), '
              f'{rib_settings.width:g} mm thick, {rib_settings.pitch:g} mm apart', file=out)

    if not additions:
        print('\nNo file written: ' + ('every candidate was skipped.' if skipped
                                       else 'nothing needs ribs.'), file=out)
        return EXIT_PARTIAL if skipped else EXIT_NOTHING
    try:
        bambu3mf.add_parts(project, output, additions)
    except (bambu3mf.ReadError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR

    # Diagnostic only: the ribs now count as plate contact in this analysis,
    # which does not show that the overhang between them prints well.
    print(f'\nWrote {output}', file=out)
    written = bambu3mf.read(output)
    for instance in written.instances:
        if instance.object_id not in before:
            continue
        (contact, gap), thresholds = before[instance.object_id]
        after = basecheck.check([(p.vertices, p.faces) for p in instance.normal_parts], thresholds)
        new_contact, new_gap = _areas(after)
        print(f'  {instance.name}: plate contact {contact:.1f} -> {new_contact:.1f} mm², '
              f'too low for support {gap:.1f} -> {new_gap:.1f} mm²', file=out)
    print('Check before printing: open it in Bambu Studio, confirm the ribs show in the sliced '
          'preview, and test how they break off.', file=out)
    return EXIT_PARTIAL if skipped else EXIT_WRITTEN


if __name__ == '__main__':
    sys.exit(main())
