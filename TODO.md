# Open items

Things worth investigating, with the evidence that prompted them. Nothing here
is a known bug — those get fixed. These are questions where the right answer is
not yet clear.

---

## The per-file timeout penalises multi-part files

`TIMEOUT` (1200 s) covers the whole split → repair → merge cycle, not each
repair. A file that splits into six shells therefore does six PyMeshFix passes
under one budget, and is killed for being *multi-part* rather than for being
slow. Both timeouts in the first full-collection run were this:

```text
Assembly.stl          1,620,826 tris, 6 shells, part.0 alone 200,980 faces
                      with nm=65.  Completed 1 part in 1200 s.

whole-costume01.stl   2,004,941 tris, 3 shells, part.0 alone 892,445 faces
                      with nm=2,055.  Completed 3 parts.
```

Neither was stuck — the log shows steady progress through the parts, just not
fast enough. Both have `.timeout.stl` markers, so they are skipped until the
marker is deleted.

**Options, none obviously right:**

- **Raise `TIMEOUT`.** Costs nothing on files that finish quickly, since the
  limit is only an upper bound. These two would probably need 2400–3600 s.
  Blunt, but the timeout exists to stop a *hang*, not to cap honest work.
- **Time each part separately.** Fairer — a six-shell file gets six budgets —
  but a genuinely stuck file then runs six times longer before anything
  notices, which is the failure the timeout was added to prevent.
- **Budget by size.** Scale the limit with triangle count, so a 2M-triangle
  mesh is allowed proportionally longer. Needs a measured cost-per-triangle
  figure, and the relationship is not linear: PyMeshFix's runtime tracks defect
  count more than face count.

**What would settle it:** run the two files with `--timeout 3600` and see
whether they complete, and how long they actually take. If it is 1500 s, raise
the limit. If it is 5000 s, the per-part option matters. Neither has been
measured.

---

## Millenium_Falcon — diagnosed, no change wanted

Not a regression. The file splits into five shells and four of them repair:

```text
393,198 tris, nm=147,448 -> 5 shells

part.0   392,088 tris  nm=147,033  pymeshfix empty -> blender      -> ok
part.1       624 tris  nm=234      pymeshfix empty -> blender      -> ok
part.2       160 tris  nm=60       pymeshfix empty -> BLENDER_EMPTY -> broken
part.3       160 tris  nm=60       pymeshfix empty -> blender      -> ok
part.4       144 tris  nm=54       pymeshfix empty -> blender      -> ok

split partial: 4/5 parts ok — saved as Millenium_Falcon.failed.stl
```

`part.2` is a 160-face fragment with 60 non-manifold edges — 37% of its edges
are bad — and both tools reduce it to zero faces. `part.3` is the same size
with the same defect count and survives, so the exact geometry decides it.

One unsalvageable 0.04% fragment therefore fails the whole file, discarding
four good parts including the 392,088-face body. Options existed — drop
unrepairable debris and merge the rest, or merge what worked and flag the file
— but **the user prefers to repair such files by hand**: the `.failed.stl`
marker names the file, and the source copy beside it is what they work from.
Guessing which small shells are disposable risks silently dropping a real part.

Closed. Recorded here because the same shape will recur and the analysis
should not be repeated.
