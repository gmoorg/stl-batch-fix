#!/usr/bin/env python3
"""Add generated support (ribs, a grid or keels) under a Bambu Studio 3MF's undersides.

Writes a NEW 3MF; the original is never modified.  One part is added to each
handled object.  Three layouts:

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

keel  the grid's clearance and one-piece rules, but oriented to the part:
    under each elongated underside one wall follows its lowest line, with
    ribs across it every --pitch along it; other undersides get the grid
    lattice.  Walls stand on feet (three project line widths) instead of a
    sheet, so the plate between them stays bare.  Experimental (2026-10-27).

An instance is skipped, and says why, when its analysis cannot be trusted
(negative volumes, unknown parts, modifiers/support painting parts, per-part
or unreadable settings) or its object has several instances; for the grid
and keel layouts also when layer heights vary or no single-piece support fits.

Whether the file opens in Bambu Studio, whether the support appears in the
sliced preview, whether Bambu's own supports interfere, and whether it comes
off cleanly must be checked by you: nothing here slices or prints.

Exit status: 0 written, every instance handled; 3 written but some instances
skipped, or nothing written because every candidate was skipped; 4 nothing
needed support (no file written); 2 error — including an output that
already exists or is the source, refused before anything is read.

    tools/project_python.sh support_3mf.py model.3mf [-o out.3mf]
    tools/project_python.sh support_3mf.py model.3mf --layout grid --max-height 30
    tools/project_python.sh support_3mf.py model.3mf --layout keel --max-height 30 --pitch 5
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import sys
import zipfile

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from libs import bambu3mf, basecheck, dependencies, ribsupport  # noqa: E402
from libs.printsettings import (add_settings_arguments, instance_problems,  # noqa: E402
                                object_number, resolve_settings, settings_uncertainty,
                                thresholds_for)

EXIT_WRITTEN, EXIT_ERROR, EXIT_PARTIAL, EXIT_NOTHING = 0, 2, 3, 4
PART_NAME = 'generated support ribs'
GRID_PART_NAME = 'generated support grid'
KEEL_PART_NAME = 'generated support keel'
#: Keel feet are this many project line widths wide.
FOOT_LINES = 3
#: Keel walls are this much wider than the project line width by default, mm.
#: Bambu Studio 02.08.02.61 drops stretches of slanted walls exactly one line
#: wide: on girl_130 Arm_Left (keel at 20°) 0.42 mm walls printed 64 % of
#: their length (layers down to 30 %), 0.45 mm walls 100 %.  Axis-aligned
#: grid walls print in full at 0.42.
KEEL_WIDTH_MARGIN = 0.03
#: Layouts built by libs/gridsupport (gap, --max-height required, one piece).
GRID_LAYOUTS = ('grid', 'keel')
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
                             '<name>.grid.3mf / <name>.keel.3mf, beside the input); must not '
                             'exist')
    parser.add_argument('--layout', choices=('ribs',) + GRID_LAYOUTS, default='ribs',
                        help='ribs (default), a one-piece grid, or keels oriented to the part')
    parser.add_argument('--pitch', type=float, default=2.0,
                        help='distance between ribs / grid walls / ribs along a keel, mm')
    parser.add_argument('--width', type=float,
                        help='wall thickness, mm (default: the project line width, else 0.42; '
                             f'keel: {KEEL_WIDTH_MARGIN:g} mm more)')
    parser.add_argument('--overlap', type=float,
                        help='ribs only: how far rib tops rise into the model, mm (default 0.05)')
    parser.add_argument('--gap', type=float,
                        help=f'grid and keel: clearance between wall tops and the model, mm '
                             f'(default {GRID_GAP})')
    parser.add_argument('--max-height', type=float,
                        help='ribs: highest underside ribbed when automatic supports are off, '
                             f'mm (default {RIBS_MAX_HEIGHT}); grid and keel (required): '
                             'highest underside supported, mm')
    parser.add_argument('--cell', type=float, help='analysis resolution, mm (default 0.05)')
    add_settings_arguments(parser)
    return parser.parse_args(argv)


def _check_layout_args(args) -> None:
    """Fill layout defaults; reject options that belong to the other layout."""
    if args.layout == 'ribs':
        if args.gap is not None:
            raise ValueError('--gap applies to --layout grid and keel only')
        args.overlap = 0.05 if args.overlap is None else args.overlap
        args.max_height = RIBS_MAX_HEIGHT if args.max_height is None else args.max_height
    else:
        if args.overlap is not None:
            raise ValueError('--overlap applies to --layout ribs only')
        if args.max_height is None:
            raise ValueError(f'--layout {args.layout} needs --max-height: the highest underside '
                             'to support')
        args.gap = GRID_GAP if args.gap is None else args.gap
    _require_positive_finite('max-height', args.max_height)


def _is_positive_finite(value: float) -> bool:
    """True for a real, finite, positive number. `value > 0` alone accepts
    infinity, which is no physical dimension: `--max-height inf` would
    remove the support-height cutoff (REVIEW.md #2, 2026-11-11)."""
    return math.isfinite(value) and value > 0


def _require_positive_finite(name: str, value: float) -> None:
    if not _is_positive_finite(value):
        raise ValueError(f'{name} must be a positive finite number, got {value}')


def _width(project, args) -> float:
    if args.width is not None:
        return args.width
    return _line_width(project)


def _line_width(project) -> float:
    """The project's printed line width (outer wall, else default), else 0.42.

    A value that is not a positive finite number is skipped like an
    unreadable one, so a broken project falls through to the next key and
    then to 0.42 instead of making infinitely wide walls or feet.
    """
    for key in ('outer_wall_line_width', 'line_width'):
        try:
            value = float(project.settings[key])
        except (KeyError, ValueError):
            continue
        if _is_positive_finite(value):
            return value
    return 0.42


def _default_output(path: str, layout: str) -> str:
    base, _ = os.path.splitext(path)
    return base + {'grid': '.grid.3mf', 'keel': '.keel.3mf'}.get(layout, '.supported.3mf')


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


@dataclasses.dataclass(frozen=True)
class _RibLayout:
    """`--layout ribs`: walls from the plate up into the unsupportable band."""
    settings: ribsupport.RibSettings

    @property
    def min_area(self) -> float:
        return self.settings.min_area


@dataclasses.dataclass(frozen=True)
class _GridLayout:
    """`--layout grid` or `keel`, both built by libs/gridsupport. `foot` is
    the keel feet's width; the grid has a base sheet instead and no feet.
    `gridsupport` is the module, imported only once manifold3d is checked."""
    name: str
    settings: object
    foot: float | None
    gridsupport: object

    @property
    def min_area(self) -> float:
        return self.settings.min_area


@dataclasses.dataclass(frozen=True)
class _InstanceOutcome:
    """What one instance contributed: `skipped` counts toward the partial
    exit status; `addition` is the part to write; `before` is the ribs
    layout's pre-rib (areas, thresholds) for the post-write diagnostic."""
    skipped: bool = False
    addition: bambu3mf.Addition | None = None
    before: tuple | None = None


def _resolve_layout(project, args, gridsupport) -> _RibLayout | _GridLayout:
    """The layout's settings, built once for the whole project. The only
    constructor of either layout, so a keel always has its feet and a grid
    never does. Raises ValueError for settings that cannot be built."""
    if args.layout == 'ribs':
        return _RibLayout(ribsupport.RibSettings(pitch=args.pitch, width=_width(project, args),
                                                 overlap=args.overlap))
    width = _width(project, args)
    if args.layout == 'keel' and args.width is None:
        width += KEEL_WIDTH_MARGIN
    settings = gridsupport.GridSettings(pitch=args.pitch, width=width, gap=args.gap)
    foot = None
    if args.layout == 'keel':
        foot = FOOT_LINES * _line_width(project)
        if settings.width > foot:
            raise ValueError(f'--width {settings.width:g} is wider than the keel feet '
                             f'({FOOT_LINES} line widths = {foot:g} mm)')
    return _GridLayout(args.layout, settings, foot, gridsupport)


def main(argv=None, out=sys.stdout) -> int:
    args = parse_args(argv)
    try:
        _check_layout_args(args)
    except ValueError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR
    output = args.output or _default_output(args.path, args.layout)
    try:
        # Before reading or analysing anything: both can take minutes.
        bambu3mf.check_destination(args.path, output)
    except bambu3mf.ReadError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR
    gridsupport = None
    if args.layout in GRID_LAYOUTS:
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
    try:
        layout = _resolve_layout(project, args, gridsupport)
    except ValueError as error:
        print(f'error: {error}', file=sys.stderr)
        return EXIT_ERROR
    variable = _variable_layers(args.path) if args.layout in GRID_LAYOUTS else []

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
                                        min_region_area=layout.min_area)
        except ValueError as error:
            print(f'error: {error}', file=sys.stderr)
            return EXIT_ERROR
        outcome = _plan_instance(project, instance, label, settings, thresholds, args, layout,
                                 counts[instance.object_id], variable, out)
        skipped += outcome.skipped
        if outcome.addition is not None:
            additions.append(outcome.addition)
        if outcome.before is not None:
            before[instance.object_id] = outcome.before

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
    if args.layout == 'keel':
        print('Check in Bambu Studio before printing: slice and confirm in the preview that the '
              'keel and rib walls print as single lines on their feet and the joining strips '
              'survive; that the keels sit under the lowest lines of the undersides; and that '
              'the layer view shows a gap between wall tops and the model.  Supports you paint '
              'can reach the plate between the walls.  Then test removal.', file=out)
    elif args.layout == 'grid':
        print('Check in Bambu Studio before printing: slice and confirm in the preview that the '
              'grid walls print as single lines and the base and joining strips survive; that '
              'Bambu adds no supports under or through the grid; and that the layer view shows '
              'a gap between wall tops and the model.  Then test removal.', file=out)
    else:
        _report_written_ribs(output, before, out)
        print('Check before printing: open it in Bambu Studio, confirm the ribs show in the '
              'sliced preview, and test how they break off.', file=out)
    return EXIT_PARTIAL if skipped else EXIT_WRITTEN


def _plan_instance(project, instance, label, settings, thresholds, args, layout, count,
                   variable, out) -> _InstanceOutcome:
    """Decide and build one printable instance's support, printing its line.

    An instance whose analysis cannot be trusted, or whose analysis itself
    fails with an expected error (ValueError, or MemoryError on a huge map),
    is SKIPPED with the reason, as check_3mf.py reports it incomplete
    (REVIEW.md #1): a traceback would lose every other instance's result.
    """
    reasons = (instance_problems(instance)
               + settings_uncertainty(project, instance, settings))
    if count > 1:
        reasons.append(f'object has {count} instances; one added part cannot fit them all')
    if isinstance(layout, _GridLayout):
        reasons += _grid_reasons(project, instance, variable)
    parts = [(p.vertices, p.faces) for p in instance.normal_parts]
    result = None
    if not reasons:
        try:
            result = basecheck.check(parts, thresholds, keep_grid=True)
        except (ValueError, MemoryError) as error:
            reasons.append(f'analysis failed: {error}')
        else:
            reasons += list(result.incomplete)
    if reasons:
        print(f'  [SKIPPED] {label}: ' + '; '.join(reasons), file=out)
        return _InstanceOutcome(skipped=True)
    if isinstance(layout, _GridLayout):
        return _plan_grid(project, instance, label, settings, parts, result, args, layout, out)
    return _plan_ribs(instance, label, settings, thresholds, result, args, layout, out)


def _plan_grid(project, instance, label, settings, parts, result, args, layout,
               out) -> _InstanceOutcome:
    grid = result.grid
    candidates = None if grid is None else grid.overhang & (grid.height < args.max_height)
    if candidates is None or not candidates.any():
        print(f'  [NOT NEEDED] {label}', file=out)
        return _InstanceOutcome()
    xy = object_number(project, instance, XY_DISTANCE_KEY, XY_DISTANCE_DEFAULT)
    own = dataclasses.replace(
        layout.settings, xy_distance=xy.value,
        base_height=settings['first_layer'].value + settings['layer_height'].value,
        min_wall=settings['layer_height'].value)
    tris = np.concatenate([v[f] for v, f in parts])
    gridsupport = layout.gridsupport
    try:
        if layout.name == 'keel':
            support = gridsupport.build_keel(tris, candidates, grid.height, grid.x0, grid.y0,
                                             grid.cell, args.max_height, own, layout.foot)
        else:
            support = gridsupport.build(tris, candidates, grid.x0, grid.y0, grid.cell,
                                        args.max_height, own)
    except gridsupport.GridError as error:
        print(f'  [SKIPPED] {label}: {error}', file=out)
        return _InstanceOutcome(skipped=True)
    name = KEEL_PART_NAME if layout.name == 'keel' else GRID_PART_NAME
    if layout.name == 'keel':
        _report_keel(label, support, own, xy, result, out)
    else:
        _report_grid(label, support, own, xy, result, out)
    return _InstanceOutcome(addition=bambu3mf.Addition(instance, name, support.vertices,
                                                       support.faces))


def _plan_ribs(instance, label, settings, thresholds, result, args, layout,
               out) -> _InstanceOutcome:
    grid = result.grid
    mask = None
    if grid is not None:
        mask = grid.flagged.copy()
        if not settings['supports_enabled'].value:
            mask |= grid.overhang & (grid.height < args.max_height)
    if mask is None or not mask.any():
        print(f'  [NOT NEEDED] {label}', file=out)
        return _InstanceOutcome()
    ribs = ribsupport.ribs(grid.height, mask, grid.x0, grid.y0, grid.cell, layout.settings)
    if ribs.empty:
        print(f'  [NOT NEEDED] {label}: flagged areas too small or too stepped for ribs',
              file=out)
        return _InstanceOutcome()
    print(f'  [RIBS] {label}: {ribs.walls} wall(s) in {ribs.regions} region(s), '
          f'{layout.settings.width:g} mm thick, {layout.settings.pitch:g} mm apart', file=out)
    return _InstanceOutcome(addition=bambu3mf.Addition(instance, PART_NAME, ribs.vertices,
                                                       ribs.faces),
                            before=(_areas(result), thresholds))


def _report_written_ribs(output, before, out) -> None:
    """Diagnostic only: re-analyse the written file with the ribs in place.

    The ribs now count as plate contact in this analysis, which does not
    show that the overhang between them prints well. The file is already
    written when this runs, so an expected failure here (unreadable output,
    analysis error, memory) is reported with the output's path and does not
    change the exit status (REVIEW.md #1).
    """
    try:
        written = bambu3mf.read(output)
        lines = []
        for instance in written.instances:
            if instance.object_id not in before:
                continue
            (contact, gap), thresholds = before[instance.object_id]
            after = basecheck.check([(p.vertices, p.faces) for p in instance.normal_parts],
                                    thresholds)
            new_contact, new_gap = _areas(after)
            lines.append(f'  {instance.name}: plate contact {contact:.1f} -> '
                         f'{new_contact:.1f} mm², too low for support {gap:.1f} -> '
                         f'{new_gap:.1f} mm²')
    except (bambu3mf.ReadError, OSError, ValueError, MemoryError) as error:
        print(f'  diagnostic not available ({type(error).__name__}: {error}); {output} was '
              'written', file=out)
        return
    for line in lines:
        print(line, file=out)


def _grid_reasons(project, instance, variable) -> list[str]:
    """Why the grid or keels cannot trust their clearance settings for this instance."""
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
    if s.unbraced_runs:
        print(f'    unbraced: {s.unbraced_runs} wall run(s), {s.unbraced_length:.0f} mm, meet no '
              'crossing wall and may need support themselves', file=out)
    print(f'    base: one piece, {s.base_area:.0f} mm², {settings.base_height:g} mm thick '
          f'(was {s.base_pieces} piece(s); {s.connectors} joining strip(s), '
          f'{s.connector_area:.0f} mm²)', file=out)
    measured = ('no model directly above any wall top' if math.isnan(s.gap_median) else
                f'wall tops sit {s.gap_median:.2f} mm below the model at the median, '
                f'{s.gap_p95:.2f} mm at the 95th percentile')
    print(f'    clearance: gap {settings.gap:g} mm (CAD; {measured}), '
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


def _report_keel(label, support, settings, xy, result, out) -> None:
    s = support
    keels = [p for p in s.patches if p.keel]
    lattice = [p for p in s.patches if not p.keel]
    print(f'  [KEEL] {label}: {len(keels)} keel(s), {s.walls} wall run(s), '
          f'{s.wall_length:.0f} mm of wall, {settings.width:g} mm thick on {s.foot:g} mm feet; '
          f'ribs {settings.pitch:g} mm apart; tops {s.top_range[0]:.2f}-{s.top_range[1]:.2f} mm',
          file=out)
    for p in keels:
        print(f'    keel at ({p.centre[0]:.1f}, {p.centre[1]:.1f}): {p.direction:.0f}°, '
              f'{p.ribs} rib(s), {p.area:.0f} mm²', file=out)
    for p in lattice:
        print(f'    lattice at ({p.centre[0]:.1f}, {p.centre[1]:.1f}), {p.area:.1f} mm²: '
              f'{p.reason}', file=out)
    if s.unbraced_runs:
        print(f'    unbraced: {s.unbraced_runs} wall run(s), {s.unbraced_length:.0f} mm, meet no '
              'crossing wall and may need support themselves', file=out)
    print(f'    base: one piece, {s.base_area:.0f} mm² of feet and strips, '
          f'{settings.base_height:g} mm thick (was {s.base_pieces} piece(s); '
          f'{s.connectors} joining strip(s), {s.connector_area:.0f} mm²)', file=out)
    measured = ('no model directly above any wall top' if math.isnan(s.gap_median) else
                f'wall tops sit {s.gap_median:.2f} mm below the model at the median, '
                f'{s.gap_p95:.2f} mm at the 95th percentile')
    print(f'    clearance: gap {settings.gap:g} mm (CAD; {measured}), '
          f'sideways {xy.value:g} mm [{xy.source}]', file=out)
    print(f'    coverage: {s.candidate_area:.0f} mm² need support; {s.near_area:.0f} mm² within '
          f'{settings.pitch / 2:g} mm of a wall, {s.missed_area:.0f} mm² farther'
          + (f' ({s.left_out_area:.1f} mm² of it specks under {settings.min_area:g} mm² given '
             'no wall)' if s.left_out_area >= 0.05 else '')
          + f', {s.no_room_area:.0f} mm² no room for base + wall + gap', file=out)
    low = sum(r.area for r in result.unsupportable)
    if low:
        print(f'    not covered: {low:.1f} mm² too low for any support (check B; see the ribs '
              'layout)', file=out)


if __name__ == '__main__':
    sys.exit(main())
