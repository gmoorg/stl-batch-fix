"""Config types every uniform pipeline step can read.

See docs/refactor/modules.md's `pipeconfig` entry for the step contract
this feeds. This module holds only plain config data — no step lists and
no tool imports, so tool modules (which already import `pipeconfig`, e.g.
`alphawrap`) never form an import cycle with it. The actual pipeline
composition — which steps run, in what order, under which condition — lives
in `repairer.py` (whole-mesh/split/part stages) and `processor.py` (initial
decimation, repair, judge), where it is visible as ordinary code, not as
booleans here.

The boolean ENABLE_SPLIT_SHELLS/ENABLE_SPLIT_SEAMS/ENABLE_ALPHA_WRAP flags
this module used to hold were removed 2026-09-2x as part of the uniform-step
refactor (docs/refactor/TODO.md's "Uniform-step refactor" section): a step
now runs because it is present in the sequence that composes it, and does
not run because it is absent — no separate switch anywhere. This continues
the same reasoning as the ENABLE_WELD/CLEAN_FILTERS/etc. removal on
2026-09-23 — user: "since we control what need to be executed in the
repairer, we do not need the Boolean flags what enable/disable steps."
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StepConfig:
    """The per-call context a uniform step may read.

    Every mesh step has the signature `(mesh, config: StepConfig | None =
    None) -> (ok, mesh, detail)`. Most steps ignore `config` entirely;
    decimation reads `faceCount`, alpha wrap reads `whole_model_diag`. Kept
    to exactly these two fields "for now" (docs/refactor/TODO.md) — add a
    field here only when a step actually needs it, not speculatively.

    faceCount         target face count for a decimation step; 0 means "no
                      target for this call" (matches `decimator.decimate`'s
                      own zero-disables convention).
    whole_model_diag  the whole mesh's bounding-box diagonal, computed once
                      per `repair()` call after initial decimation and
                      before splitting, then carried unchanged into every
                      part's own `StepConfig` — see `repairer._repair_sequence`.
    nested_process_group  read by `blender.step_blender_repair` to decide
                      whether the Blender invocation it launches should get
                      its own process session. `False` (the default) means
                      "not nested — safe, this Blender invocation should own
                      its own process group", matching `blender.Runner`'s own
                      safe-by-default. `True` means this step is known to run
                      nested inside another, enclosing `proctree`-managed
                      process group (e.g. a batch worker), so the Blender it
                      launches must NOT get its own session — otherwise the
                      enclosing worker's own group-kill would no longer reach
                      it. Threaded in from `repairer.repair`/`processor.process`,
                      ultimately from `batch_repair.py`'s explicit
                      `--managed-child` marker.
    """

    faceCount: int = 0
    whole_model_diag: float | None = None
    nested_process_group: bool = False
