"""Every pipeline step's on/off switch, in one place.

`meshfix` and `repairer` read these as `pipeconfig.ENABLE_X` at the point of
use, not by importing the name — a name import binds the value at import
time, so a later `pipeconfig.ENABLE_X = False` would silently stop taking
effect. These are module-level rather than function parameters because they
describe an experiment on the whole run, not a property of one mesh: set them
before calling `repair`, not per part. Several are recorded elsewhere as
*harmful* on real models (see the per-flag notes and
docs/refactor/discovered-bugs.md) and the argument for or against a step was
settled by running the pipeline without it — that is what these exist for.

All default `True` except `ENABLE_SPLIT_SEAMS`. Most of these matched the
shipping behaviour exactly when this module was created; `ENABLE_BLENDER_PART`
is the exception — it puts Blender's per-part repair into the default
sequence for the first time (owner decision, 2026-09-21; see its own note
below and docs/refactor/open-issues.md). A disabled step still appears in
`Result.steps`, with `detail` saying it was skipped, so a log never silently
omits a stage.

## The step contract

Every operation the repair sequence can run — `welder.step_weld_close_tjunctions`,
`blender.step_blender_repair`, `meshfix.step_meshfix_repair`, and
`meshlab`'s own `step_clean_null_faces`/`step_clean_merge_close`/
`step_clean_duplicate_faces`/`step_clean_unreferenced`/`step_orient` —
shares one signature: `(mesh: Mesh) -> tuple[bool, Mesh, str]`. A function
defined in another module names that module in its own name
(`step_<module>_<action>`); one defined in `meshlab.py` does not repeat
"meshlab" since the module qualifier already says that (`step_<action>`).
`repairer` composes all of them, in the order `WHOLE_MESH_STEPS` and
`_repair_part` list, without a step-specific case because of this shared
shape — naming a specific tool's filters or parameters is that tool's own
module's job, not `repairer`'s. Each function's own docstring covers only
what is specific to it; the shared parts are here, once:

- `ok=True` means the step *ran* (or was intentionally skipped), never that
  the resulting mesh is printable — a tool can succeed and still hand back
  a defective mesh, which is `scanner`'s question to answer, not this one's.
- `ok=False` stops the sequence at that step; the caller does not proceed to
  the next one.
- A disabled flag returns `(True, mesh, 'skipped (ENABLE_X=False)')` —
  unchanged input, not a failure — and the flag is checked *inside* the step,
  not by the caller.
- Every step catches what its own underlying call raises and converts it to
  `(False, mesh, detail)`, so an exception never escapes past this contract.
"""

from __future__ import annotations

#: 1/7. Split faces at T-junctions. Adds faces, moves and deletes nothing.
ENABLE_WELD = True

#: 2/8. Drop zero-area faces.
ENABLE_CLEAN_NULL_FACES = True

#: 2/9. Weld vertices within 0.1% of the bbox diagonal. **Measured harmful**:
#: on Amidara base it merges one vertex and creates two non-manifold edges in
#: a mesh that had none, and removing it changes the final result by nothing.
ENABLE_CLEAN_MERGE_CLOSE = True

#: 2/10. Remove faces duplicated after the merge.
ENABLE_CLEAN_DUPLICATE_FACES = True

#: 2/11. Drop vertices no face references.
ENABLE_CLEAN_UNREFERENCED = True

#: 3a/12. Separate edge-connected components so PyMeshFix cannot discard all
#: but the largest. Disabling this sends a multi-shell mesh in whole.
ENABLE_SPLIT_SHELLS = True

#: 3a/13. Separate regions whose winding contradicts itself. Off by default:
#: when True, `repairer.repair` does call `splitter.by_seams` on each shell
#: part. Enabling it is an experiment, not a default, because repairing the
#: resulting open regions independently is measured as destructive on real
#: models — see mandy-volume-loss.md's unconditional-seam-splitting finding.
ENABLE_SPLIT_SEAMS = False

#: 3b/14. Orient each part outward. **Measured harmful**: takes Amidara base
#: from 922 winding-seam edges to 7,659.
ENABLE_ORIENT = True

#: 3b/14a. Repair each part in Blender before PyMeshFix runs on it. Owner
#: decision, 2026-09-21: `blender.step_blender_repair` (then still wrapped
#: as `repairer.blender_part`, since removed as redundant) existed only as
#: an explicit wholesale replacement for PyMeshFix
#: (`repair(tool=blender.step_blender_repair)`) and was never reachable in
#: the default sequence — this switch puts it there, before PyMeshFix, so
#: it stops being unused. This fulfils the
#: *order* half of the open target in libs/review/CODE_REVIEW.md ("Blender
#: must appear in the final per-part route"); the defect-based selector half
#: — routing a part to one tool, the other, or both based on what it
#: actually needs — remains unimplemented, tracked in open-issues.md. Not a
#: claim that Blender specifically fixes non-manifold geometry: an earlier
#: version of this comment made that claim and the owner retracted it —
#: docs/refactor/pipeline.md's own measurement record says the opposite
#: (Blender better on holes/fins, PyMeshFix better on non-manifold geometry
#: and winding seams).
ENABLE_BLENDER_PART = True

#: 3b/15. Run PyMeshFix on each part, after Blender. Disabling this passes
#: the part through untouched by PyMeshFix specifically (Blender's own step
#: still runs if its own flag allows) — the control for measuring what
#: PyMeshFix costs on top of whatever came before it.

# each step should have it own flag! No master switch!
ENABLE_PART_TOOL = True

#: 15a. Close boundary loops before cleaning. `nbe=0` means *every* boundary
#: regardless of size, and `refine=True` is what emits "Refinement stage
#: failed to converge" on meshes that had no boundaries to begin with.
ENABLE_FILL_BOUNDARIES = True

#: 15b. `clean()` — remove self-intersecting and degenerate geometry.
#: **This is the destructive call**: it deletes 36,342 faces from a
#: watertight Amidara base, 99.6% of that loss being self-intersection
#: removal cascading through retriangulation.
#: See docs/refactor/amidara-clean-destroys.md.

# each step should have it own flag! No master switch!
ENABLE_CLEAN = True
