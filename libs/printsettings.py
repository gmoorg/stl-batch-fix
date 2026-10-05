"""Effective print settings for one 3MF instance, with where each came from.

Shared by check_3mf.py (reporting) and support_3mf.py (rib generation).
Only the handful of settings the base-layer checks use are resolved.
"""

from __future__ import annotations

import math

from . import bambu3mf, basecheck

#: Bambu treats a support threshold angle of 0 as "automatic"; this is the
#: angle assumed for it.
AUTO_SUPPORT_ANGLE = 30.0

#: Settings resolve_settings reads; a per-part override of any of them is not
#: applied, which makes the analysis less certain.
USED_KEYS = frozenset({'initial_layer_print_height', 'layer_height', 'support_top_z_distance',
                       'support_threshold_angle', 'enable_support', 'support_type'})


class Value:
    """A threshold value and where it came from."""

    def __init__(self, value, source):
        self.value, self.source = value, source

    def __repr__(self):
        return f'{self.value} ({self.source})'


def _number(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return value


def resolve_settings(project: bambu3mf.Project, instance: bambu3mf.Instance,
                     cli) -> tuple[dict[str, Value], list[str]]:
    """The effective support/layer settings for one instance, with sources.

    Precedence: command line > object override > project settings > default.
    `cli` is any object with first_layer, layer_height, support_gap and
    support_angle attributes (None = not given).  A value that cannot be
    parsed falls back to the default and is marked 'assumed'.
    """
    notes = []
    layered = [('project', project.settings), ('object', instance.overrides)]

    def pick(key, default, parse):
        value, source = default, 'default'
        for name, table in layered:
            if key in table:
                try:
                    value, source = parse(table[key]), name
                except ValueError:
                    value, source = default, 'assumed'
                    notes.append(f'{key}={table[key]!r} ({name}) not understood; using {default}')
        return Value(value, source)

    def flag(text):
        if text not in ('0', '1'):
            raise ValueError(text)
        return text == '1'

    first = pick('initial_layer_print_height', 0.2, _number)
    layer = pick('layer_height', 0.2, _number)
    gap = pick('support_top_z_distance', 0.2, _number)
    angle = pick('support_threshold_angle', AUTO_SUPPORT_ANGLE, _number)
    if angle.source in ('project', 'object') and angle.value == 0:
        angle = Value(AUTO_SUPPORT_ANGLE, 'assumed for automatic (0)')
    enabled = pick('enable_support', True, flag)
    support_type = pick('support_type', 'normal(auto)', str)
    supports = Value(bool(enabled.value) and 'manual' not in str(support_type.value),
                     enabled.source)
    plate_only = pick('support_on_build_plate_only', False, flag)

    def cli_override(value, current):
        return current if value is None else Value(value, 'command line')

    first = cli_override(getattr(cli, 'first_layer', None), first)
    layer = cli_override(getattr(cli, 'layer_height', None), layer)
    gap = cli_override(getattr(cli, 'support_gap', None), gap)
    angle = cli_override(getattr(cli, 'support_angle', None), angle)

    if any(p.subtype not in (bambu3mf.NORMAL, bambu3mf.NEGATIVE) for p in instance.parts):
        notes.append('modifier or support enforcer/blocker parts present: effective '
                     'settings may differ inside them')
    part_keys = {k for p in instance.parts for k in p.overrides} & USED_KEYS
    if part_keys:
        notes.append('per-part overrides of ' + ', '.join(sorted(part_keys)) + ' are not applied')
    return ({'first_layer': first, 'layer_height': layer, 'support_gap': gap,
             'support_angle': angle, 'supports_enabled': supports,
             'support_on_build_plate_only': plate_only}, notes)


def thresholds_for(settings: dict[str, Value], cli, **fixed) -> basecheck.Thresholds:
    """basecheck thresholds from resolved settings plus any CLI-given extras.

    `fixed` entries are set unconditionally (a caller's own requirements);
    CLI attributes that are None are left at their defaults.
    """
    extra = {}
    for name in ('cell', 'foot_height', 'shallow_angle', 'min_region_area',
                 'min_contact_fraction', 'max_extra_islands', 'sink_quantile'):
        value = getattr(cli, name, None)
        if value is not None:
            extra[name] = value
    extra.update(fixed)
    return basecheck.Thresholds(first_layer=settings['first_layer'].value,
                                layer_height=settings['layer_height'].value,
                                support_gap=settings['support_gap'].value,
                                support_angle=settings['support_angle'].value,
                                supports_enabled=settings['supports_enabled'].value,
                                **extra)


def instance_problems(instance: bambu3mf.Instance) -> list[str]:
    """Reasons the analysed geometry may not be what prints."""
    problems = []
    if any(p.subtype == bambu3mf.NEGATIVE for p in instance.parts):
        problems.append('negative volume present: it is not subtracted, so the analysed '
                        'base may include material that will not print')
    unmatched = [p.name for p in instance.parts if p.subtype is None]
    if unmatched:
        problems.append('parts with no matching metadata (subtype unknown, not analysed): '
                        + ', '.join(unmatched))
    return problems


#: support_type values Bambu Studio writes.  Anything else may mean either
#: automatic or manual support, so a generator cannot rely on it.
KNOWN_SUPPORT_TYPES = frozenset({'normal(auto)', 'tree(auto)', 'normal(manual)',
                                 'tree(manual)'})


def settings_uncertainty(project: bambu3mf.Project, instance: bambu3mf.Instance,
                         settings: dict[str, Value]) -> list[str]:
    """Reasons the resolved settings may not be the ones that apply.

    The checker only notes these; a generator that builds geometry from the
    settings must refuse instead.
    """
    reasons = []
    support_type = instance.overrides.get('support_type', project.settings.get('support_type'))
    if support_type is not None and support_type not in KNOWN_SUPPORT_TYPES:
        reasons.append(f'support_type {support_type!r} not recognised')
    if any(p.subtype not in (bambu3mf.NORMAL, bambu3mf.NEGATIVE, None) for p in instance.parts):
        reasons.append('modifier or support enforcer/blocker parts change settings locally')
    part_keys = {k for p in instance.parts for k in p.overrides} & USED_KEYS
    if part_keys:
        reasons.append('per-part overrides of ' + ', '.join(sorted(part_keys)))
    unparsed = [k for k, v in settings.items() if v.source == 'assumed']
    if unparsed:
        reasons.append('settings not understood: ' + ', '.join(unparsed))
    return reasons
