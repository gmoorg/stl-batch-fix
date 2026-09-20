### Discussion point — a flawless small shell may still be a print hazard

**Raised 2026-09-16. To revisit. Nothing decided, nothing measured.**

Two questions have been treated as one and are not:

1. **Is this shell meaningful?** — about the shell itself: topology, size.
2. **Can it be printed where it sits?** — about the *arrangement*: where this
   shell is relative to the others and to the plate.

**The case that separates them.** A pin, an eyeball, a magnet peg is *supposed*
to be its own shell, unattached, and that is correct geometry. If it is
flawless, it should be kept however small it is — the earlier measurement
supports that: Falcon parts 3 and 4 are 36 and 38 faces, 0.0% nm, and both
committed fine. Small and clean is not debris.

**But position decides whether keeping it is safe.** An eyeball nested inside a
head prints fine — the head builds around it. The same eyeball sitting in empty
space 5 mm away has nothing beneath it: it needs support, or it drops. A
dropped part is not a lost part. It becomes a blob the nozzle drags through,
stringing across the plate, and it can destroy the entire print — the whole
plate, not just that piece.

**Same geometry, same topology, opposite outcomes.** Nothing measured so far
distinguishes them. `scan` sees a clean mesh either way; a face floor sees a
small one either way; the nm ratio sees 0.0% either way. Every signal discussed
so far is a property of the shell in isolation, and this failure is a property
of the *relationship between shells*.

**A cheap detectable version**, to be argued later rather than assumed: once
parts are separate meshes, bounding-box containment and overlap are trivial to
compute. A small shell whose box is **inside or intersecting** another shell's
is a feature — nested detail, an inset eye, a peg in its socket. A small shell
sitting in **clear space**, touching nothing, is either debris or something
that will need support. That is a different classification from either of the
signals recorded above, and it uses data we will already have.

**Open, and not to be guessed at**: whether this tool should act on it at all.
It may be that the right output is a *warning* rather than a drop or a fix —
the slicer is where support is decided, and a repair tool silently deleting a
correctly-modelled eyeball would be worse than saying "three loose shells, none
supported, check before printing". Deleting geometry because it might need
support is exactly the class of over-reach the volume guard exists to catch.

