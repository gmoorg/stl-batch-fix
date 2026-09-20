### Idea, not yet designed — debris by bounding box, not face count

**Recorded as an idea. Not decided, not built.**

`_MIN_SHELL_FACES = 100` is a triangle count standing in for physical size. It
was measured honestly — the smallest real shell seen was 750 faces, so 100
keeps every real part with 7.5x margin — but a dense speck carries thousands of
faces in a sub-millimetre box. That is exactly the Leia shape: a million
triangles per millimetre of extent. A high-detail sculpt's debris sails past a
face floor.

**The insight that dissolves it**: after the split, each part *is* its own
mesh, so its bounding box can be measured directly — `mesh_io.dimensions()`
already exists. No fraction-of-the-parent arithmetic, no constant meaning
different things at different scales, because the measurement is on the part
itself.

Sketch, to be worked out later: drop a part whose bounding box is under some
small multiple of `MIN_LAYER` in **every** dimension. Open question whether
that multiple is a constant with its reasoning written down (something near
"under 2 mm in all three axes") or a per-run setting.

**Whatever is dropped must be recorded as dropped** — count, largest extent,
total volume. Two reasons. The volume guard exists to catch deleted geometry
and would otherwise see a deliberate drop as loss, sending the file down the
seam-split recovery path for something that was chosen. And a speck at 5 mm may
matter at 300%: "dropped 443 shells, largest 47 faces, total 0.02 mm^3" is
checkable later, "the volume changed" is not.

