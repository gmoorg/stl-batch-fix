# Discovered bugs

Where the known model-destroying bugs stand. This page is **status only** — the
measurements live in the investigations, and the work items live in
[open issues](open-issues.md). **Update it whenever either investigation
moves.**

Last updated 2026-09-21.

## Mandy — diagnosed, not fixed

Part 0 loses its hair inside PyMeshFix: 562,249 → 391,442 faces, **85.82%
volume**. The merged result scores `open=0, nm=0` with all 39 components
present, so only the volume guard notices.

Ruled out as causes: CLEAN/SPLIT ordering (both orders identical), seam
splitting (part 0 has no closed loop in the real pipeline; hair and body share
one seam region). The only mechanism that isolates the hair is cutting the 67
non-manifold edges, which is unimplemented and damages the body in both tested
forms.

Fixed along the way: `by_seams` no longer uses `MIN_SHELL_FACES` as a split
gate (`fc05bad`). Real bug; does not help Mandy.

**Status: unfixed.** Needs a mechanism that follows the hair/body boundary.
→ [Mandy volume loss](mandy-volume-loss.md)

## Amidara — the pipeline destroys printable models

`base` arrives watertight and comes back **broken** at 279,168 faces, scoring
**96.93% — a pass**. `hands_2` scores **100.00%** and contains **fabricated
geometry**. Both confirmed by inspection.

Not our steps (97.26% with CLEAN and orientation disabled) and not a regression
(the legacy script destroys it identically). The deletion is self-intersection
removal, cascading 3.2x. Self-intersection does not stop a print — the head
printed undecimated at 11.71% against `base`'s 3.60%. `base` is not clean
after all: 1,506 inverted normals, which `is_clean` does not test. Four repair
attempts failed; three left the repaired faces inverted.

**Status: unfixed, no viable repair identified.**
→ [Amidara](amidara-clean-destroys.md)

## What CLEAN steps 9 and 10 are worth

Measured 2026-09-21 with the new step switches, running every sphere probe with
`merge_close_vertices` and `remove_duplicate_faces` on, then off:

| probe | 9+10 on | 9+10 off | |
|---|---|---|---|
| `sphere_doubles` | 760f, 50.0% | 1,520f, 100.0% | **differs** |
| `sphere_allbad` | 916f, 50.0% | 1,832f, 100.0% | **differs** |
| the other ten spheres | — | — | identical |

The filters do what they were built for: with them off, both coincident spheres
survive and the model comes back at double thickness; with them on, one sphere
remains. The 50% reading is the *correct* result being scored as destruction,
which is the volume guard's known false positive on these fixtures.

But the scope is narrow. Steps 9 and 10 change nothing on any other probe, and
nothing on either real Amidara model. Their only demonstrated benefit is the
coincident-duplicate-shell defect — for which a survey of **1,450 real models
found zero instances**. Step 9 is separately recorded as harmful: on
`Amidara_..._base` it merges one vertex and creates two non-manifold edges in a
watertight mesh.

## The measurement gap behind both

**The pipeline's checks cannot detect the damage it causes.**

| case | what the checks said | what was true |
|---|---|---|
| Mandy | `open=0, nm=0`, all components | hair deleted |
| `hands_2` | 100.00% volume, `open=0` | geometry invented |
| `FIXCONN_FILL` | `seam=0` | 762 flipped face pairs |

The two scanner defects behind this are work items in
[open issues](open-issues.md#prevent-false-success-and-model-loss).

**Every real defect found on 2026-09-21 was found by the owner opening the
file.** Eight conclusions stated confidently from measurements were refuted:
that no evidence justified CLEAN-before-SPLIT; that CLEAN damaged the model;
that a costume01 fragment was meaningful geometry; that rejoining before repair
could not help; that the legacy PyMeshFix call differed from ours; that a
boolean union was a repair function; that `fix_connectivity` + fill repaired
`base`; and that the boolean's acceptance verified it.

## Next experiment the owner wants run

**`fix_connectivity` + fill, then the current step 15 on its output.** The two
may be complementary: the first adds geometry and leaves inverted patches, and
`clean()` removes bad faces while keeping good ones — so running it second
might delete those patches without the wholesale loss it causes on the raw
source, where it arrives with inconsistent winding.

| sequence on `base` | faces | volume |
|---|---:|---:|
| step 15 `clean()` alone | 279,140 (−36,342) | 97.26% |
| `fix_connectivity()` + `fill_small_boundaries(0, True)` | 320,152 (+4,670) | **99.81%** |
| **both, in order** | untested | |

`fix_connectivity()` has never been called by this project. It is the only
operation found that moves in the same direction as the online repair tool
(which fixed 1,506 inverted normals by adding 25,478 triangles), and it is
**not a repair on its own**: 762 of its 4,592 new faces are wound backwards
while `winding_seams` reports 0. Judge the combination by inspection, not by
the counters. Detail in [open issues](open-issues.md).

## Blocked

- **Skip repair for a part already clean and within budget** (owner decision,
  in [open issues](open-issues.md)). Blocked twice: gating on today's behaviour
  would skip repair for models that need it, and `is_clean` would wrongly pass
  `base`, whose inverted normals it cannot see.
- **Codex review of both investigations.** Requested; rate-limited at the time
  of writing.
