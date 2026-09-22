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

With `ENABLE_CLEAN` also disabled (WELD, CLEAN_MERGE_CLOSE, ORIENT, CLEAN all
off, run through the live `repairer.repair()`, not a bare `PyTMesh` script),
`base` comes back at **320,152 faces (+4,670), 99.81%, `lost_vertices=0`** —
the same figures the investigation's bare `fix_connectivity()+fill` experiment
found. With `clean()` out of the loop, `fill_small_boundaries` alone adds
geometry and loses none; `clean()` is the entire loss. That output still
carries 11,365 unrepaired self-intersections — measured harmless for printing
elsewhere in this doc, but not zero. Disabling `ENABLE_CLEAN` is not yet an
adopted config change; this is a measurement, not a decision.

## CGAL alpha wrapping — spiked, not adopted

Tested 2026-09-21 outside the pipeline (`pip install cgal`, prebuilt wheel,
no build needed): CGAL 6.0's `alpha_wrap_3` on `base`, tuned by hand. It makes
one unconditional guarantee — watertight, 2-manifold, self-intersection-free
output on any input — by reconstructing the surface at a chosen resolution
(`alpha`) and offset distance (`offset`), not by repairing the existing
triangulation. Three points swept:

| alpha | offset | faces | vs. source | volume |
|---|---|---:|---:|---:|
| 0.42 (bbox/300) | 0.11 (bbox/1200, CGAL's suggested ratio) | 121,662 | 39% | 104.00% |
| 0.28 | 0.05 | 308,354 | **97.7%** | 102.11% |
| 0.22 | 0.035 | 523,852 | 166% | 101.45% |
| 0.15 | 0.02 | 1,197,134 | 380% | 100.77% |

`alpha=0.28` lands closest to the source's own 315,482-face resolution and
looks decent by eye, though visibly softer than the original — this is a
full remesh, not a preservation of the source triangulation, so some loss of
sharp detail is inherent even at a well-matched alpha. Not yet checked:
whether the output actually reduces the 11,365 self-intersections to zero
(the property this was tried for) or how closely the surface tracks the
source geometrically beyond matching face count and volume. Not integrated
into the pipeline; this is a spike, done with a plain script outside
`libs/`, not a `pipeconfig`-gated step.

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
| `FIXCONN_FILL` + step 15 | `open=0, nm=0, seam=0`, self-int=0 — every automated acceptance check passes; 97.49% volume clears the pipeline's `MIN_VOLUME_KEPT = 0.90` guard with room to spare (as does the current pipeline's own 97.26%), so nothing flagged it either | deleted original source geometry, by owner inspection |

The two scanner defects behind this are work items in
[open issues](open-issues.md#prevent-false-success-and-model-loss).

**Every real defect found on 2026-09-21 was found by the owner opening the
file.** Eight conclusions stated confidently from measurements were refuted:
that no evidence justified CLEAN-before-SPLIT; that CLEAN damaged the model;
that a costume01 fragment was meaningful geometry; that rejoining before repair
could not help; that the legacy PyMeshFix call differed from ours; that a
boolean union was a repair function; that `fix_connectivity` + fill repaired
`base`; and that the boolean's acceptance verified it.

## Tested and refuted: `fix_connectivity` + fill, then step 15

**Tested 2026-09-21.** The hypothesis was that the two steps are
complementary: `fix_connectivity` + fill adds geometry and leaves inverted
patches, and `clean()` removes bad faces while keeping good ones — so running
`clean()` second might delete those patches without the wholesale loss it
causes on the raw source, where it arrives with inconsistent winding.

| sequence on `base` | faces | volume |
|---|---:|---:|
| step 15 `clean()` alone | 279,140 (−36,342) | 97.26% |
| `fix_connectivity()` + `fill_small_boundaries(0, True)` | 320,152 (+4,670) | 99.81% |
| **both, in order (`libs/meshfix.repair()` on the intermediate)** | 279,202 | 97.49% |

`open=0, nm=0`, `winding_seams=0`, and self-intersections
(`justproper=True`) went 11,365 → 0 — every counter available, cleaner than
any other sequence measured in this investigation. **The owner inspected the
output and confirmed `clean()` deleted original source geometry**, not just
the patch geometry `fix_connectivity` + fill had added — the same failure
mode `clean()` has on the raw source, not a defect confined to
`FIXCONN_FILL`'s inverted-patch artifact. The hypothesis is refuted. Detail,
including why an ad hoc winding-incidence check built for this test should
not be read as corroborating or contradicting the earlier 762-flipped-pairs
count, in
[Amidara](amidara-clean-destroys.md#fix_connectivity--fill-then-step-15-on-its-output--also-refuted).

`fix_connectivity()` has never otherwise been called by this project. It is
the only operation found that moves in the same direction as the online
repair tool (which fixed 1,506 inverted normals by adding 25,478 triangles),
and it is **not a repair on its own**: the earlier, separately-measured count
found new/old edges it produced wound the same way (the same-winding defect)
while `winding_seams` reports 0. No working repair for `base` has been found.

## Tested and refuted: `fix_connectivity` + fill, then Blender

**Tested 2026-09-21.** The hypothesis: `fix_connectivity` + fill genuinely
produces the 762-flipped-pair defect above, and Blender's repair loop exists
specifically to find and fix non-manifold geometry — so give it that
intermediate, where it has a real defect to act on, instead of the pristine
source (`open=0, nm=0`), where it does nothing.

Result: **no change at all** — 320,152 faces in, 320,152 faces out,
byte-identical, `BLENDER_OK`. Refuted for a precise reason, not just "it
didn't work": Blender's own defect check (`count_defects`/`nm_faces_of` in
`libs/blender_fx/repair.blender`) asks *how many* faces share an edge, the
same question `scanner.winding_seams` asks — never *which direction* each
face walks it. The 762 pairs sit on edges shared by exactly 2 faces, wound
the same way; both tools read that as fine. Detail in
[Amidara](amidara-clean-destroys.md#blender-cannot-see-the-defect-either--same-blind-spot-as-the-scanner).

## Blocked

- **Skip repair for a part already clean and within budget** (owner decision,
  in [open issues](open-issues.md)). Blocked twice: gating on today's behaviour
  would skip repair for models that need it, and `is_clean` would wrongly pass
  `base`, whose inverted normals it cannot see.
- **Codex review of the Mandy investigation.** Requested; the Amidara
  investigation was independently reviewed by Codex this session (see
  [COLLABORATION.md](../../COLLABORATION.md)) — Mandy's is still pending.
