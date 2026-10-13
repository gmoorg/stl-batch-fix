#!/usr/bin/env python3
"""Add generated support (ribs or a grid) under a Bambu Studio 3MF's undersides.

Writes a NEW 3MF; the original is never modified.  One part is added to each
handled object.  Two layouts:

ribs (default)  thin breakaway walls under the undersides the slicer cannot
    support (check B of check_3mf.py: above the layer-1 slice, below first
    layer + support_top_z_distance), from the plate up into the underside,
    so that material is printed on ribs instead of over air.  Coverage:
      - always the unsupportable band;
      - also shallow undersides up to --max-height (default 3 mm), but only
        when the 3MF has automatic supports switched off (otherwise the
        slicer's own supports are expected to carry them — not checked here).

grid  a mesh of single-line walls in X and Y under every shallow underside
    the plate can reach below --max-height (required), tops --gap below the
    model and support_object_xy_distance away from it so nothing fuses, on a
    thin base sheet joined into ONE piece so it comes off together.  Bambu's
    own supports stay on for everything else.  Undersides with no room for
    base + wall + gap (including the check-B band) are left unsupported and
    reported.  See libs/gridsupport.py.

An instance is skipped, and says why, when its analysis cannot be trusted
(negative volumes, unknown parts, modifiers/support painting parts, per-part
or unreadable settings) or its object has several instances; for the grid
also when layer heights vary or no single-piece grid fits.

Whether the file opens in Bambu Studio, whether the support appears in the
sliced preview, whether Bambu's own supports interfere, and whether it comes
off cleanly must be checked by you: nothing here slices or prints.

Exit status: 0 written, every instance handled; 3 written but some instances
skipped, or nothing written because every candidate was skipped; 4 nothing
needed support (no file written); 2 error.

    tools/project_python.sh support_3mf.py model.3mf [-o out.3mf]
    tools/project_python.sh support_3mf.py model.3mf --layout grid --max-height 30
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import zipfile

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from libs import bambu3mf, basecheck, dependencies, ribsupport  # noqa: E402
from libs.printsettings import (instance_problems, object_number,  # noqa: E402
                                resolve_settings, settings_uncertainty, thresholds_for)

EXIT_WRITTEN, EXIT_ERROR, EXIT_PARTIAL, EXIT_NOTHING = 0, 2, 3, 4
PART_NAME = 'generated support ribs'
GRID_PART_NAME = 'generated support grid'
RIBS_MAX_HEIGHT = 3.0
GRID_GAP = 0.2
XY_DISTANCE_KEY = 'support_object_xy_distance'
XY_DISTANCE_DEFAULT = 0.35
#: Zip members Bambu writes for variable layer heights / height-range layers.
VARIABLE_LAYER_MEMBERS = ('Metadata/layer_heights_profile.txt', 'Metadata/layer_config_ranges.xml')


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('path', help='Bambu Studio 3MF file')
    parser.add_argument('-o', '--output',
                        help='new 3MF to write (default: <name>.supported.3mf, or '
                             '<name>.grid.3mf for the grid, beside the input); must not exist')
    parser.add_argument('--layout', choices=('ribs', 'grid'), default='ribs',
                        help='ribs (default) or a one-piece grid')
    parser.add_argument('--pitch', type=float, default=2.0,
                        help='distance between ribs / grid walls, mm')
    parser.add_argument('--width', type=float,
                        help='wall thickness, mm (default: the project line width, else 0.42)')
    parser.add_argument('--overlap', type=float,
                        help='ribs only: how far rib tops rise into the model, mm (default 0.05)')
    parser.add_argument('--gap', type=float,
                        help=f'grid only: clearance between wall tops and the model, mm '
                             f'(default {GRID_GAP})')
    parser.add_argument('--max-height', type=float,
                        help='ribs: highest underside ribbed when automatic supports are off, '
                             f'mm (default {RIBS_MAX_HEIGHT}); grid (required): highest '
                             'underside the grid supports, mm')
    parser.add_argument('--cell', type=float, help='analysis resolution, mm (default 0.05)')
    group = parser.add_argument_group('settings (default: read from the 3MF)')
    group.add_argument('--first-layer', type=float, help='initial layer height, mm')
    group.add_argument('--layer-height', type=float, help='layer height, mm')
    group.add_argument('--support-gap', type=float, help='support top Z distance, mm')
    group.add_argument('--support-angle', type=float, help='support threshold angle, degrees')
    return parser.parse_args(argv)


def _check_layout_args(args) -> None:
    """Fill layout defaults; reject options that belong to the other layout."""
    if args.layout == 'ribs':
        if args.gap is not None:
            raise ValueError('--gap applies to --layout grid only')
        args.overlap = 0.05 if args.overlap is None else args.overlap
        args.max_height = RIBS_MAX_HEIGHT if args.max_height is None else args.max_height
    else:
        if args.overlap is not None:
            raise ValueError('--overlap applies to --layout ribs only')
        if args.max_height is None:
            raise ValueError('--layout grid needs --max-height: the highest underside to support')
        args.gap = GRID_GAP if args.gap is None else args.gap
    if not (args.max_height > 0):
        raise ValueError(f'max-height must be positive, got {args.max_height}')


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


def _default_output(path: str, layout: str) -> str:
    base, _ = os.path.splitext(path)
    return base + ('.grid.3mf' if layout == 'grid' else '.supported.3mf')


def _areas(result: basecheck.Result) -> tuple[float, float]:
    contact = result.base.contact_area if result.base else 0.0
    return contact, sum(r.area for r in result.unsupportable)


def _log_errors(line: str) -> None:
    if line.startswith('[error]'):
        print(line, file=sys.stderr)


def _variable_layers(path: str) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    return [name for name in VARIABLE_LAYER_MEMBERS if name in names]


def main(argv=None, out=sys.stdout) -> int:
    args = parse_args(argv)
    try:
        _check_layout_args(args)
    except ValueError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR
    gridsupport = None
    if args.layout == 'grid':
        if not dependencies.check_one('manifold3d', _log_errors):
            return EXIT_ERROR
        from libs import gridsupport
    try:
        project = bambu3mf.read(args.path)
    except bambu3mf.ReadError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR
    if not project.is_bambu:
        print('error: not a Bambu Studio project', file=sys.stderr)
        return EXIT_ERROR
    output = args.output or _default_output(args.path, args.layout)
    try:
        if args.layout == 'ribs':
            rib_settings = ribsupport.RibSettings(pitch=args.pitch, width=_width(project, args),
                                                  overlap=args.overlap)
            min_area = rib_settings.min_area
        else:
            grid_settings = gridsupport.GridSettings(pitch=args.pitch, width=_width(project, args),
                                                     gap=args.gap)
            min_area = grid_settings.min_area
    except ValueError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR
    variable = _variable_layers(args.path) if args.layout == 'grid' else []

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
                                        min_region_area=min_area)
        except ValueError as error:
            print(f'error: {error}', file=sys.stderr)
            return EXIT_ERROR
        reasons = (instance_problems(instance)
                   + settings_uncertainty(project, instance, settings))
        if counts[instance.object_id] > 1:
            reasons.append(f'object has {counts[instance.object_id]} instances; one added part '
                           'cannot fit them all')
        if args.layout == 'grid':
            reasons += _grid_reasons(project, instance, variable)
        parts = [(p.vertices, p.faces) for p in instance.normal_parts]
        result = None
        if not reasons:
            result = basecheck.check(parts, thresholds, keep_grid=True)
            reasons += list(result.incomplete)
        if reasons:
            skipped += 1
            print(f'  [SKIPPED] {label}: ' + '; '.join(reasons), file=out)
            continue
        grid = result.grid

        if args.layout == 'grid':
            candidates = None if grid is None else grid.overhang & (grid.height < args.max_height)
            if candidates is None or not candidates.any():
                print(f'  [NOT NEEDED] {label}', file=out)
                continue
            xy = object_number(project, instance, XY_DISTANCE_KEY, XY_DISTANCE_DEFAULT)
            own = dataclasses.replace(
                grid_settings, xy_distance=xy.value,
                base_height=settings['first_layer'].value + settings['layer_height'].value,
                min_wall=settings['layer_height'].value)
            tris = np.concatenate([v[f] for v, f in parts])
            try:
                support = gridsupport.build(tris, candidates, grid.x0, grid.y0, grid.cell,
                                            args.max_height, own)
            except gridsupport.GridError as error:
                skipped += 1
                print(f'  [SKIPPED] {label}: {error}', file=out)
                continue
            additions.append(bambu3mf.Addition(instance, GRID_PART_NAME,
                                               support.vertices, support.faces))
            _report_grid(label, support, own, xy, result, out)
            continue

        mask = None
        if grid is not None:
            mask = grid.flagged.copy()
            if not settings['supports_enabled'].value:
                mask |= grid.overhang & (grid.height < args.max_height)
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
                                       else 'nothing needs support.'), file=out)
        return EXIT_PARTIAL if skipped else EXIT_NOTHING
    try:
        bambu3mf.add_parts(project, output, additions)
    except (bambu3mf.ReadError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR

    print(f'\nWrote {output}', file=out)
    if args.layout == 'grid':
        print('Check in Bambu Studio before printing: slice and confirm in the preview that the '
              'grid walls print as single lines and the base and joining strips survive; that '
              'Bambu adds no supports under or through the grid; and that the layer view shows '
              'a gap between wall tops and the model.  Then test removal.', file=out)
        return EXIT_PARTIAL if skipped else EXIT_WRITTEN

    # Diagnostic only: the ribs now count as plate contact in this analysis,
    # which does not show that the overhang between them prints well.
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


def _grid_reasons(project, instance, variable) -> list[str]:
    """Why the grid cannot trust its clearance settings for this instance."""
    reasons = []
    if variable:
        reasons.append('variable layer heights (' + ', '.join(variable) + '): the grid assumes '
                       'one layer height')
    if any(XY_DISTANCE_KEY in p.overrides for p in instance.parts):
        reasons.append(f'per-part override of {XY_DISTANCE_KEY}')
    try:
        object_number(project, instance, XY_DISTANCE_KEY, XY_DISTANCE_DEFAULT)
    except ValueError as error:
        reasons.append(str(error))
    return reasons


def _report_grid(label, support, settings, xy, result, out) -> None:
    s = support
    print(f'  [GRID] {label}: {s.walls} wall run(s), {s.wall_length:.0f} mm of wall, '
          f'{settings.width:g} mm thick, {settings.pitch:g} mm apart; tops '
          f'{s.top_range[0]:.2f}-{s.top_range[1]:.2f} mm', file=out)
    print(f'    base: one piece, {s.base_area:.0f} mm², {settings.base_height:g} mm thick '
          f'(was {s.base_pieces} piece(s); {s.connectors} joining strip(s), '
          f'{s.connector_area:.0f} mm²)', file=out)
    print(f'    clearance: gap {settings.gap:g} mm (CAD; wall tops sit {s.gap_median:.2f} mm '
          f'below the model at the median, {s.gap_p95:.2f} mm at the 95th percentile), '
          f'sideways {xy.value:g} mm [{xy.source}]', file=out)
    print(f'    coverage: {s.candidate_area:.0f} mm² need support; {s.near_area:.0f} mm² within '
          f'{settings.pitch / 2:g} mm of a wall, {s.missed_area:.0f} mm² farther'
          + (f' ({s.fallback_crosses} small patch(es) given their own cross)'
             if s.fallback_crosses else '')
          + f', {s.no_room_area:.0f} mm² no room for base + wall + gap', file=out)
    low = sum(r.area for r in result.unsupportable)
    if low:
        print(f'    not covered: {low:.1f} mm² too low for any support (check B; see the ribs '
              'layout)', file=out)


if __name__ == '__main__':
    sys.exit(main())
