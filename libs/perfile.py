"""Repair or prepare ONE file: the per-file work `batch_repair_child.py` runs.

Two public operations, one per pass (docs/refactor/orchestration.md):
`prepare_one_file` (initial decimation, PLY cache, memory estimate) and
`process_one_file` (repair and publish). Each returns exactly one
`ChildResult` and never raises for a per-file failure.

Also the prepare cache's layout (`decimated_root`, `decimated_path`,
`expected_prepared_path`), which the parent uses to name each job's cache
and to check the cache root at startup.

A library module, not the batch runner, so the child no longer imports the
runner's own entry point, pool and progress code (REVIEW.md #7,
2026-11-11). Subprocess creation, cancellation, reconciliation and the run
configuration stay in `batch_repair.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import childresult, decimator, jobmemory, mesh_io, pipeconfig, processor, repairer
from . import splitter, steplog
from .childresult import ChildResult
from .mesh_io import Mesh



def process_one_file(source_path: str, destination: str, max_faces: int,
                     step_logger: steplog.StepLogger = steplog.null_logger,
                     *,
                     skip_clean: bool = False,
                     reconstruct_budget_bytes: int = pipeconfig.StepConfig.reconstruct_memory_budget_bytes,
                     reconstruct_ceiling_bytes: int | None = None,
                     min_shell_faces: int = splitter.MIN_SHELL_FACES,
                     load_path: str | None = None,
                     reconstruction: str = repairer.RECONSTRUCTIONS[0]
                     ) -> ChildResult:
    """The exact per-file body the old serial loop ran, now for one file only.

    `load_path` is the mesh to repair when it is not `source_path` itself:
    the repair pass loads what the prepare pass saved. Markers still copy
    `source_path`, the job's own source.

    `skip_clean` comes from the opt-in `--skip-clean` flag; see
    `repairer.repair`.
    """
    stage = 'intake'
    category = 'intake_failure'
    reason = 'invalid intake mesh'
    indicator = None
    written_path = None
    steps: tuple[str, ...] = ()
    # Absolute, not basename: `converter._walk`/`os.walk(root)` does not
    # normalize `root`, and a direct child invocation may pass a relative
    # path — a basename alone would make two same-named files in different
    # subfolders indistinguishable in a shared step log.
    source_name = os.path.abspath(source_path)
    from_cache = load_path is not None and load_path.lower().endswith('.ply')
    mesh = mesh_io.probe(source_path if from_cache else (load_path or source_path),
                         destination)
    if not mesh.is_valid:
        reason = mesh.problem or reason
    else:
        try:
            stage = 'load'
            category = 'load_failure'
            if from_cache:
                # The prepare pass's PLY cache, attached to the source's own
                # identity (path, kind); markers still copy the source. A
                # malformed cache raises ValueError: a load_failure below.
                loaded = mesh_io.read_ply(load_path, mesh)
            else:
                loaded = mesh_io.load(mesh)
                # A job with a prepare handoff (`load_path` set) had its note
                # from the prepare pass, which loaded this same STL; a direct
                # repair of an STL reports its own.
                if loaded.is_valid and load_path is None:
                    steps = tuple(_dropped_note(loaded))
            reason = loaded.problem or 'invalid loaded mesh'
            if loaded.is_valid:
                stage = 'process'
                category = 'process_failure'
                outcome = processor.process(loaded, max_faces,
                                           step_logger=step_logger, source_name=source_name,
                                           skip_clean=skip_clean,
                                           reconstruct_budget_bytes=reconstruct_budget_bytes,
                                           reconstruct_ceiling_bytes=reconstruct_ceiling_bytes,
                                           min_shell_faces=min_shell_faces,
                                           reconstruction=reconstruction)
                if outcome.repair is not None:
                    steps += tuple(f'{s.step.name}: {s.detail}' for s in outcome.repair.steps)
                stage = 'write'
                category = 'write_failure'
                path = processor.write(outcome, source_path, destination)
                if path is None:
                    reason = ('processor.write returned None: no publication '
                              f'for {outcome.indicator.name}')
                else:
                    category = 'published'
                    indicator = outcome.indicator.name
                    written_path = path
                    stage = 'process'
                    reason = outcome.reason
        except Exception as exc:                      # noqa: BLE001 — reported, not raised
            reason = f'{type(exc).__name__}: {exc}'
    return ChildResult(path=source_path, category=category, indicator=indicator,
                       stage=stage, reason=reason, written_path=written_path, steps=steps)


def decimated_root(input_root: str) -> str:
    """The cache folder for decimated sources: a sibling of the input folder,
    `<input>.decimated` (owner, 2026-10-05), so the cache is never inside the
    tree the batch scans."""
    return str(Path(input_root).resolve()) + '.decimated'


def decimated_path(input_root: str, source: str, max_faces: int) -> str:
    """Where the prepare pass keeps the decimated mesh of job `source`: a PLY
    written by `mesh_io.write_ply`, so the decimator's vertex table is handed
    to the repair pass as is, never re-welded from STL triangles, and with
    the decimator's float64 coordinates unrounded.

    Keyed by the job's own source path relative to the input tree — the
    file the job loads, so a converted model is keyed by its
    `stl-exported/` copy and never shares a cache with a native STL bound
    for the same output (`<input>.decimated/stl-exported/a.stl.N.T.ply` vs
    `<input>.decimated/a.stl.N.T.ply`). A new `max_faces` gets its own file,
    and so do new decimator settings: `T` is `decimator.settings_tag()`.
    Sources are assumed unmodified: a changed source or PyMeshLab version is
    not detected. Caches from before (`<input>/stl-decimated/*.stl`) are not read.

    The one clash left needs a directory named like a cache file
    (`a.stl.900000.T.ply/`); writing that cache then fails, and the job
    fails with the reason rather than reading the wrong geometry.

    The relative path is lexical, as in `converter.output_for`: a source
    that is a symlink keeps its own name in the input tree. Resolving it
    would key `/in/link.stl -> /outside/m.stl` as `../outside/m.stl` and
    put the cache beside the target, outside the cache root and its
    overlap checks (Codex review, 2026-10-05). A source outside the input
    tree raises ValueError rather than escaping.
    """
    rel = os.path.relpath(os.path.abspath(source), os.path.abspath(input_root))
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        raise ValueError(f'{source} is not inside the input folder {input_root}')
    return os.path.join(decimated_root(input_root),
                        f'{rel}.{int(max_faces)}.{decimator.settings_tag()}.ply')


def expected_prepared_path(mesh: Mesh, cache_path: str, max_faces: int) -> str:
    """The mesh the repair pass will load: the cache when the initial
    decimation has work to do (the decimator's own guard), else the source."""
    needs = max_faces > 0 and (mesh.triangles or 0) > max_faces
    return cache_path if needs else mesh.path


def _load_cache(cache_path: str, source: Mesh) -> Mesh | None:
    """A usable cached decimation attached to the probed `source` (path,
    destination and kind stay the source's), or None to rebuild it."""
    if not os.path.isfile(cache_path):
        return None
    try:
        loaded = mesh_io.read_ply(cache_path, source)
    except (OSError, ValueError):                     # rebuild instead
        return None
    return loaded if loaded.triangles > 0 else None


def _dropped_note(loaded: Mesh) -> list[str]:
    """A step line naming what `mesh_io.load` dropped, per reason, else
    nothing. Informational: the scanner finds the holes on its own."""
    drops = loaded.load_drops
    if drops is None:
        return []
    reasons = [text for n, text in (
        (drops.nonfinite, f'{drops.nonfinite} triangles with NaN/inf coordinates'),
        (drops.degenerate, f'{drops.degenerate} triangles with coincident corners')) if n]
    return [f'load: dropped {", ".join(reasons)}'] if reasons else []


def prepare_one_file(source_path: str, destination: str, max_faces: int,
                     cache_path: str,
                     step_logger: steplog.StepLogger = steplog.null_logger,
                     *,
                     reconstruct_budget_bytes: int = pipeconfig.StepConfig.reconstruct_memory_budget_bytes,
                     reconstruct_ceiling_bytes: int | None = None,
                     min_shell_faces: int = splitter.MIN_SHELL_FACES,
                     reconstruction: str = repairer.RECONSTRUCTIONS[0]) -> ChildResult:
    """The prepare pass for one file: initial decimation, cache, estimate.

    Either hands the job to the repair pass (`childresult.PREPARED`, with the
    mesh to load and its `jobmemory.repair_bytes` estimate) or ends it with
    an ordinary terminal result: an unreadable file, or UNDECIMATED published
    exactly as `processor.process` would publish it.

    A source over `max_faces` is loaded with `mesh_io.load` (which drops
    triangles with NaN/inf coordinates), decimated from arrays and saved as
    a PLY cache. Our loader, not PyMeshLab's STL reader: that reader holds
    ~4.5x the mesh's own memory (docs/archive/errors/decimation-memory-path.md). The
    cache is read back before estimating, through the same path a cache hit
    takes, so the estimate is computed on exactly the mesh the repair pass
    will load.
    """
    stage, category, reason = 'intake', 'intake_failure', 'invalid intake mesh'
    source_name = os.path.abspath(source_path)
    steps: list[str] = []

    def terminal(**fields) -> ChildResult:
        return ChildResult(path=source_path, mode='prepare', steps=tuple(steps),
                           **{'category': category, 'indicator': None, 'stage': stage,
                              'reason': reason, 'written_path': None, **fields})

    probed = mesh_io.probe(source_path, destination)
    if not probed.is_valid:
        reason = probed.problem or reason
        return terminal()
    try:
        prepared_path = expected_prepared_path(probed, cache_path, max_faces)
        mesh = None
        if prepared_path == cache_path:
            mesh = _load_cache(cache_path, probed)
            if mesh is not None:
                steps.append(f'decimate: cached, {mesh.triangles} faces ({cache_path})')
                step_logger(source_name, 'info', 'decimate', '-', 0.0,
                            f'cached, {mesh.triangles} faces out ({cache_path})')
        if mesh is None:
            stage, category = 'load', 'load_failure'
            loaded = mesh_io.load(probed)
            if not loaded.is_valid:
                reason = loaded.problem or 'invalid loaded mesh'
                return terminal()
            steps.extend(_dropped_note(loaded))
            if prepared_path == source_path:
                mesh = loaded
            else:
                stage, category = 'process', 'process_failure'
                failed, decimated = processor.decimate_initial(
                    loaded, max_faces, step_logger, source_name)
                del loaded
                steps.append(f'decimate: {decimated.rung.value}, {decimated.faces_out} faces out')
                if failed is not None:
                    stage, category = 'write', 'write_failure'
                    written = processor.write(failed, source_path, destination)
                    if written is None:
                        reason = 'processor.write returned None: no publication for UNDECIMATED'
                        return terminal()
                    category, stage, reason = 'published', 'process', failed.reason
                    return terminal(indicator=failed.indicator.name, written_path=written)
                stage, category = 'write', 'write_failure'
                if os.path.isdir(cache_path):
                    reason = (f'decimated cache {cache_path} is a directory; '
                              f'rename that directory to repair this file')
                    return terminal()
                with mesh_io.staged_write(cache_path) as staged:
                    mesh_io.write_ply(decimated.mesh, staged)
                mesh = _load_cache(cache_path, probed)
                if mesh is None:
                    reason = f'decimated cache {cache_path} could not be read back'
                    return terminal()
        stage, category = 'process', 'process_failure'
        estimate = jobmemory.repair_bytes(mesh, min_shell_faces, reconstruct_budget_bytes,
                                          reconstruction, reconstruct_ceiling_bytes)
    except Exception as exc:                          # noqa: BLE001 — reported, not raised
        reason = f'{type(exc).__name__}: {exc}'
        return terminal()
    return ChildResult(path=source_path, category=childresult.PREPARED, indicator=None,
                       stage='prepare', mode='prepare', written_path=None,
                       reason=f'{mesh.triangles} faces, repair estimate {estimate / 1e9:.2f} GB',
                       steps=tuple(steps), prepared_path=prepared_path,
                       estimate_bytes=int(estimate))
