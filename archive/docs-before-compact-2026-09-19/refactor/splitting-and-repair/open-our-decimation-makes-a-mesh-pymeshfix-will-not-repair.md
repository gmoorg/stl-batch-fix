### OPEN — our decimation makes a mesh PyMeshFix will not repair

**2026-09-17, found by decimating Mandy's 2.06M-face original ourselves rather
than using Bambu's `-simp`.** The user's question was whether our decimator
avoids the holes Bambu's simplify produces. On that narrow question it wins;
on the pipeline as a whole it currently loses badly.

| | faces | nm | open | open-edge lengths |
|---|---|---|---|---|
| original | 2,061,994 | 266 | 84 | 0.005-0.038 mm, 23 loops |
| **ours** (fast_simplification) | 188,940 | **76** | **6** | 0.007-**0.020** mm |
| **Bambu** | 188,940 | **29** | **34** | 0.016-**2.41** mm |

**Ours leaves better holes and worse topology.** Bambu's open edges reach
**2.41 mm** — visible damage, and what the user described as "triangles just
erased from the surface". Ours are all under 0.02 mm, below one layer. Both
decimations look correct to the eye.

**But the repair sequence then destroys ours:**

| input | output | volume kept |
|---|---|---|
| Bambu's `-simp` | 188,432f | **99.99%** — confirmed clean by Bambu |
| **ours** | **108,862f** | **46.27%** |

Traced to one step. PyMeshFix takes part 0 from 125,478 faces to 45,554, and
says why on stderr:

    WARNING- forceNormalConsistence: Basic_TMesh was not orientable.
             Cut performed.

**Our part 0 is not orientable; Bambu's is.** That is the whole difference —
and it is not seam count, because ours has *fewer* seams (4/0 against 5/1).
A non-orientable surface cannot be given a consistent inside, so PyMeshFix cuts
it apart and keeps one piece. This is the head-deletion case, on a model where
nothing looks wrong.

**What was tried, none of it sufficient:**

| | result |
|---|---|
| `fill_holes=False` | 42.4% — the hole filling is not the cause |
| `by_geometry` first | 42.5% — unchanged |
| `re_orient_faces_coherently` | **raises** on this mesh |
| **`meshing_repair_non_manifold_edges`** | **84.8%** — nm to 0, 92 new open edges, still not orientable |
| that plus vertices, plus `by_geometry`, or applied twice | 84.8%, no further gain |

So `repair_non_manifold_edges` roughly halves the loss and then plateaus: the
mesh is still non-orientable and PyMeshFix still cuts.

**What this does not settle**, and matters before anything is changed:

- **Whether the fault is the decimator or the repair.** `fast_simplification`
  produced a non-orientable surface from an orientable-enough input; that may
  be a flaw in it, in our use of it, or simply what quadric collapse does to a
  mesh with 266 pre-existing non-manifold edges.
- **Whether the PyMeshLab rung helps.** It does not: 272 nm and 74 open at the
  same target, worse than either.
- **Whether orientability is detectable before the fact.** `scanner` has no
  such check, and `volume` and `winding_seams` both look *better* on ours.

Until then the pipeline's decimation step should be treated as unproven on real
input, and `mandy_ours_repaired.stl` is a damaged file, not a result.

---

