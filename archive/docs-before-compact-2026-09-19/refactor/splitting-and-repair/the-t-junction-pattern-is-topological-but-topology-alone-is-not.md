### The T-junction pattern is topological, but topology alone is not sufficient

**2026-09-17, from the user's sketch.** The defect has an exact topological
description, and it is worth recording because it narrows the search far better
than the current prefilter:

```text
      b            X = [a, b, c]      spans the full edge (a,c)
     / \           L = [a, M, d]
    a---M---c      R = [M, c, d]
     \ /
      d            X shares ONE vertex with L (a) and ONE with R (c);
                   both are corners of X and together they are X's edge.
                   L and R share TWO vertices (M, d) — an edge.
                   M is the shared vertex that is NOT in X.
```

Verified against `sphere_tjunction`: X=[335,358,339], L=[339,345,350],
R=[345,358,350] — a=339, c=358, M=345, d=350, and X does use edge (339,358).

**Measured against `welder` on every fixture — identical, including the
negatives:**

| fixture | topological | `welder` | open edges |
|---|---|---|---|
| correct | 0 | 0 | 0 |
| tjunction | 1 | 1 | 3 |
| tjunction_many | **150** | **150** | 450 |
| fin | 0 | 0 | 2 |
| degenerate | 0 | 0 | 1 |
| allbad | **160** | **160** | 484 |
| doubles | 0 | 0 | 0 |

`fin` and `degenerate` are the important rows: they *have* open edges and no
T-junctions, and the pattern correctly finds none.

**But topology alone is NOT sufficient, and this is the answer to "can we drop
the tolerance entirely" — no.** Constructed counter-example: three faces in
exactly that arrangement with M **5 mm off** the line a-c. The pattern fires;
`welder` correctly does not. That is a hole with a triangle fan across it, not
a T-junction.

The reason is structural: the defect *is* the absence of any topological link
between M and X, so only coordinates can say whether M lies on X's edge. The
pattern is a **necessary condition**, not a sufficient one.

**What this changes about the tolerance fix:** the pattern is a better
prefilter than "any open-boundary vertex against any open edge". Narrowed to
the three-face arrangements, the distance test is no longer scanning broadly —
it is deciding among already-plausible matches. At that point the question is
not "is this float noise" but "is M close enough to a-c that splitting X at it
is the right repair", which is naturally a fraction of **|ac|**, the edge's own
length, rather than of the bounding-box diagonal.

A false positive of the earlier analysis, recorded so it is not repeated: a
first attempt at this test searched for X sharing *exactly one* vertex with
each of L and R without requiring (a,c) to be X's edge, and reported 3 hits on
a defect-free sphere. Those were phantom matches from a mis-stated pattern, not
evidence that topology is ambiguous.

#### Tested at tolerance = 1000 mm, ~30x the model's own diameter

The user's test, and it separates the fixtures into two groups.

**Unaffected across nine orders of magnitude** — `correct`, `tjunction`,
`degenerate`, `doubles` give *identical* answers at 1e-6 and at 1000. The
open-edge prefilter is why: no open edges means no candidates, so the distance
test never runs. `tjunction` finds exactly its 1 junction either way, because
there is only one three-face arrangement to consider.

**Catastrophic where open edges are many:**

| fixture | faces | nm | open | volume |
|---|---|---|---|---|
| `tjunction_many` | 910 -> **2,834** | **524** | 79 | +2394.3 (was +4094.9) |
| `allbad` | 1,684 -> **3,911** | **531** | 130 | **-5174.7** |
| `fin` | 761 -> 762 | **1** | 0 | +4094.9 |

`tjunction_many` finds **425 junctions instead of 150** and triples the face
count, splitting faces at vertices nowhere near their edges — with 450 open
edges and 532 candidates, a 1000 mm tolerance matches almost everything. `fin`
gains a non-manifold edge where it had none.

**And the topological pattern is immune to all of it:**

| fixture | topological | geometric @1e-6 | geometric @1000 |
|---|---|---|---|
| tjunction_many | **150** | 150 | 425 |
| allbad | **160** | 160 | 472 |
| fin | **0** | 0 | 1 |

It produces the correct answer with **no tolerance at all**, on every fixture,
including the negative. On this evidence it is not merely a better prefilter —
it is a strictly better detector, the one known exception being the
constructed case where M sits 5 mm off the line and the pattern still fires.

#### The counter-example was invalid, and the real limit is ambiguity

**The user rejected the 5 mm counter-example, correctly.** The argument: if two
triangles touch a third, they must share *edges* with it, or there is a tear.
Checked — that "mesh" was **7 open edges out of 8 and 2 shells**: three
triangles joined at two single vertices, not a surface. A properly stated
pattern gives **0** there, not 1.

A second attempt, built edge-connected (1 shell), did produce 2 hits — but
those came from a **bug in my statement of the pattern**, not from the pattern
itself. I had never required M to lie *between* a and c topologically, i.e.
that L owns edge (a,M) and R owns (M,c). Adding that condition takes the
off-line case to **0**.

**So topology does not produce false positives on edge-connected geometry. It
produces an AMBIGUITY.** With the between-ness condition added, the
single-junction fixture yields **three** hits, not one:

| hit | X | claims M is | |
|---|---|---|---|
| 1 | 199, spans 339-358 | **345** | correct |
| 2 | 200 | 358 | |
| 3 | 760 | 339 | |

The three open edges are (339,358) and its two halves (339,345), (345,358). The
pattern is **symmetric** across them — the three faces and three open edges are
combinatorially identical under relabelling — so each nominates a different
vertex as M. Topology cannot tell them apart. What distinguishes them is purely
geometric: 345 lies *between* 339 and 358, and 358 does not lie between 339 and
345.

**This re-characterises the tolerance.** It is not a detection threshold and it
is not rejecting bad candidates. It is a **tie-breaker among three symmetric
ones**, and the right form is therefore a *comparison* — "which of these
vertices sits closest to its opposing edge" — not a threshold. Comparisons need
no units, which would remove the scale bug rather than relocate it.

#### `find` under-reports when one face carries several junctions

**The user's prediction: the algorithm fails when there is more than one
T-junction on the same face of a-b-c. Confirmed.**

Built on a real sphere (two hand-built attempts were malformed the same way the
counter-example was — vertex-connected, not edge-connected — and the user
caught the pattern both times): one spanning edge subdivided **twice**, at
t=1/3 and t=2/3.

| | result |
|---|---|
| `find` | **1 junction** — M=383 at t=0.667; the one at t=0.333 is missed |
| `repair` | **correct** — splits=2, rounds=3, ending `open=0 nm=0` |

The cause is one line:

```python
found[face] = TJunction(...)
break                       # one per face, then stop
```

Which of the two is found is **arbitrary** — iteration order over a set.

`repair` recovers because the round loop re-searches: round 1 splits at one
vertex, which changes the edge map, and round 2 finds the other. `MAX_ROUNDS`
gives it room. So `rounds > 2` is the signal that this case occurred —
`tjunction_many` converges in 2, this took 3.

`find`'s docstring does say "one per affected face", so it is documented rather
than a surprise. But **any caller using `find` for reporting** — counting
defects, or deciding whether repair is worth running — gets a silently low
number.

Undecided: whether `find` should report all junctions per face (keyed by
`(face, vertex)` rather than `face`). `repair` must keep one-per-face-per-round
because splitting invalidates the face index, so the two would diverge, which
is a real cost.

#### A related prefilter assumption, found while testing

`welder` scans **open edges only**, which assumes the spanning edge is open. In
one constructed fixture the spanning edge (0,1) was shared by two faces — so it
looked properly paired — while the three *halves* were the open ones, and
`find` returned 0. That fixture was artificial, but the assumption is real and
is not stated anywhere in the module.

**So the tolerance fix has three candidate shapes now:**

1. *Scale the existing tolerance* by edge length — fixes the unit, keeps a
   number to calibrate.
2. *Make the topological pattern the detector* with a distance sanity check —
   more work, and the ambiguity above means the check is load-bearing after all.
3. *Keep the geometric search but make the decision a comparison* — pick the
   candidate with the smallest distance to its edge rather than the first one
   under a threshold. Removes the unit question entirely and fixes the
   arbitrary-`break` problem in the same change.

The third now looks best and was not on the list before this discussion. Not
yet decided.

---

