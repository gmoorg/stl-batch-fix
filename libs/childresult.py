"""The JSON result a `--one-file` child writes, and the parent's bounded read of it."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from . import mesh_io
from .indicators import Indicator

#: A handful of string fields. Anything larger than this is itself a signal
#: something is wrong — refuse rather than parse an unbounded file.
MAX_RESULT_BYTES = 64 * 1024

_KNOWN_CATEGORIES = frozenset({
    'published', 'intake_failure', 'load_failure', 'process_failure', 'write_failure',
})


@dataclass(frozen=True)
class ChildResult:
    """One file's outcome, as the `--one-file` child reports it.

    `clean` is deliberately absent here — the parent computes it from
    `category`/`indicator` rather than trusting an independently supplied
    field, which removes the possibility of a contradictory result by
    construction rather than by validating it after the fact.
    """

    path: str
    category: str
    indicator: str | None
    stage: str
    reason: str
    written_path: str | None = None
    #: One line per repair step (name + detail, e.g. which alpha-wrap
    #: parameters ran and the resulting face count) — a summary for the
    #: parent's progress log, not the full `repairer.StepResult` objects
    #: (no scan/volume data serialized here; those stay in-process only).
    steps: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.category == 'published' and self.indicator == Indicator.PROCESS.name


def write(path: str, result: ChildResult) -> None:
    """Write `result` to `path` atomically — the child's side of the protocol."""
    with mesh_io.staged_write(path) as staged:
        with open(staged, 'w') as f:
            json.dump(asdict(result), f)


def read_and_validate(path: str, expected_source: str) -> ChildResult | None:
    """The parent's side: a bounded, strictly validated read.

    Returns `None` for anything that is not unambiguously a valid result —
    missing file, empty file, oversized file, malformed JSON, a wrong or
    missing field, an unknown category, or an indicator name this project
    does not recognise.  `None` is the single signal that routes the caller
    into crash/timeout reconciliation; there is no partial trust.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == 0 or size > MAX_RESULT_BYTES:
        return None
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    required = ('path', 'category', 'indicator', 'stage', 'reason', 'written_path')
    if any(key not in raw for key in required):
        return None
    if not isinstance(raw['path'], str) or raw['path'] != expected_source:
        return None
    if not isinstance(raw['category'], str) or raw['category'] not in _KNOWN_CATEGORIES:
        return None
    indicator = raw['indicator']
    if indicator is not None:
        if not isinstance(indicator, str) or indicator not in Indicator.__members__:
            return None
    if not isinstance(raw['stage'], str) or not isinstance(raw['reason'], str):
        return None
    written_path = raw['written_path']
    if written_path is not None and not isinstance(written_path, str):
        return None
    steps = raw.get('steps', [])
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        return None

    return ChildResult(path=raw['path'], category=raw['category'], indicator=indicator,
                       stage=raw['stage'], reason=raw['reason'], written_path=written_path,
                       steps=tuple(steps))
