"""Load and validate the batch runner's TOML configuration.

`batch_repair.py` takes no command-line arguments: every run option comes
from `batch_repair.toml` beside the script (documented copy:
`batch_repair.example.toml`). This module turns that file into one frozen
`RunConfig`, or raises `ConfigError` naming the offending key — before the
runner creates any output or log file.

TOML has no null, so "automatic" values use documented sentinels: `workers =
0`, `memory_budget_bytes = 0` and `log_file = ""`. `resolve` replaces them
with concrete values; nothing downstream sees a sentinel.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import MISSING, dataclass, fields, replace


class ConfigError(Exception):
    """The configuration file is missing, unreadable, or has a bad value."""


@dataclass(frozen=True)
class RunConfig:
    """Every batch run option. Fields without a default are required keys.

    input / output         source and destination folders; relative paths
                           resolve against the config file's own folder
    max_faces              face budget for the INITIAL whole-mesh decimation
                           only; 0 disables that pass
    log_file               step log; "" means <output>/batch.log
    skip_clean             opt-in `is_already_clean` gate (repairer.repair)
    workers                parallel child processes; 0 means min(4, cpu count)
    per_file_timeout       seconds one file may run before it is killed
    reap_deadline          seconds to wait for a killed child to be confirmed gone
    memory_budget_fraction share of currently available RAM to admit work
                           against, used when memory_budget_bytes is 0
    memory_budget_bytes    explicit budget in bytes; 0 derives it from the fraction
    """

    input: str
    output: str
    max_faces: int
    log_file: str = ''
    skip_clean: bool = False
    workers: int = 0
    per_file_timeout: float = 3600.0
    reap_deadline: float = 10.0
    memory_budget_fraction: float = 0.7
    memory_budget_bytes: int = 0


#: Keys that hold paths, resolved against the config file's folder.
_PATH_KEYS = ('input', 'output', 'log_file')


def required_keys() -> tuple[str, ...]:
    """Field names with no default — the keys a config file must set."""
    return tuple(f.name for f in fields(RunConfig)
                 if f.default is MISSING and f.default_factory is MISSING)


def load(path: str) -> RunConfig:
    """Read and validate `path`; raise `ConfigError` on any problem.

    Unknown keys are errors, not ignored, so a misspelled key cannot silently
    leave its option at the default.
    """
    try:
        with open(path, 'rb') as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f'config file not found: {path}') from None
    except IsADirectoryError:
        raise ConfigError(f'config path is a directory, not a file: {path}') from None
    except OSError as error:
        raise ConfigError(f'cannot read config file {path}: {error.strerror or error}') from None
    except UnicodeDecodeError as error:
        raise ConfigError(f'config file {path} is not valid UTF-8: {error}') from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f'config file {path} is not valid TOML: {error}') from None
    return from_mapping(data, os.path.dirname(os.path.abspath(path)))


def from_mapping(data: dict, base_dir: str) -> RunConfig:
    """Validate parsed TOML `data`; relative paths resolve against `base_dir`."""
    known = {f.name: f for f in fields(RunConfig)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f'unknown key(s): {", ".join(unknown)}; '
                          f'allowed: {", ".join(known)}')
    missing = [key for key in required_keys() if key not in data]
    if missing:
        raise ConfigError(f'missing required key(s): {", ".join(missing)}')

    values = {}
    for key, value in data.items():
        values[key] = _check(key, value, known[key].type)
    for key in _PATH_KEYS:
        if values.get(key):
            values[key] = os.path.normpath(os.path.join(base_dir, values[key]))
    return RunConfig(**values)


def _check(key: str, value, type_name: str):
    """Type and range check for one key; returns the value to store."""
    # `bool` is a subclass of `int` in Python, so it is excluded explicitly:
    # `workers = true` must be an error, not one worker.
    if type_name == 'str':
        if not isinstance(value, str):
            raise ConfigError(f'{key} must be a string, got {_kind(value)}')
        if key in ('input', 'output') and not value.strip():
            raise ConfigError(f'{key} must not be empty')
        # TOML can encode "\u0000"; the OS rejects it in a path, and that
        # must surface here rather than as a traceback mid-run.
        if '\0' in value:
            raise ConfigError(f'{key} must not contain a NUL character')
        return value
    if type_name == 'bool':
        if not isinstance(value, bool):
            raise ConfigError(f'{key} must be true or false, got {_kind(value)}')
        return value
    if type_name == 'int':
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f'{key} must be an integer, got {_kind(value)}')
        if value < 0:
            raise ConfigError(f'{key} must not be negative, got {value}')
        return value
    if type_name == 'float':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f'{key} must be a number, got {_kind(value)}')
        try:
            value = float(value)
        except OverflowError:
            raise ConfigError(f'{key} is too large, got {value}') from None
        if not math.isfinite(value) or value <= 0:
            raise ConfigError(f'{key} must be a finite positive number, got {value}')
        if key == 'memory_budget_fraction' and value > 1:
            raise ConfigError(f'{key} must be at most 1, got {value}')
        return value
    raise AssertionError(f'RunConfig field {key} has unhandled type {type_name}')


def _kind(value) -> str:
    return f'{type(value).__name__} {value!r}'


def resolve(config: RunConfig) -> RunConfig:
    """Replace the automatic sentinels with concrete values.

    Reads the machine (CPU count, available memory), so it is separate from
    `load` and runs once in the parent. Raises `ConfigError` when the memory
    budget cannot be derived or comes out as zero.
    """
    workers = config.workers or min(4, os.cpu_count() or 1)
    log_file = config.log_file or os.path.join(config.output, 'batch.log')
    budget = config.memory_budget_bytes
    if budget == 0:
        try:
            available = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_AVPHYS_PAGES')
        except (ValueError, OSError, AttributeError):
            raise ConfigError('cannot determine available memory on this platform; '
                              'set memory_budget_bytes explicitly') from None
        budget = int(available * config.memory_budget_fraction)
        if budget <= 0:
            raise ConfigError('derived memory budget is zero; '
                              'set memory_budget_bytes explicitly')
    return replace(config, workers=workers, log_file=log_file,
                   memory_budget_bytes=budget)
