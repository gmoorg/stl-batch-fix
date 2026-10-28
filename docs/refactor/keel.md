# Keel support layout — summary (2026-10-26/27)

What changed, why, what was measured and what is still open. Technical
detail lives in [modules](modules.md) (`gridsupport` row, `--layout keel`
paragraph); the open item is in [TODO](TODO.md) ("Keel grid").

## How it started: the Arm_Left break

girl_130 Arm_Left (from `/mnt/sda2/assembly/print/girl_130.grid.3mf`) broke
twice at the same height, about 15 mm above the elbow, where the printed
cross-section splits into two branches (Z 14.6 → 14.8). The same arm printed
fine without the grid on 2026-10-26, with nothing else changed.

Measured and ruled out (printed G-code `/mnt/sda2/xxx/girl_130_plate_2.gcode`):

- Mesh: identical to the batch_repair output, no self-intersections or
  degenerate faces; thickness and cross-sections match Arm_Right.
- G-code at the split: walls continuous, branch walls sit on material below,
  fans/temperature/speed unchanged, retractions and travels normal.
- Grid touching the arm: never closer than 0.47 mm edge to edge in any layer
  (Z 2–26); the arm never rests on a grid top with one empty layer or less.
- The flower printed on the same plate: well away from the arm, printed fine.
- "The grid blocks painted supports": rejected by the owner (supports were
  painted with the grid in place and checked in the preview).

**The cause of the break is not known.** The only established fact is two
failures with the grid and one success without it.

## The idea (owner, 2026-10-27)

The fixed X/Y lattice puts walls where the arm does not need them and none
under pieces that fall between walls; a small pitch costs filament, and the
solid base sheet keeps painted supports off the plate. Instead: one wall
(the "keel") along the line where an elongated underside is lowest — where
its first layers start — with short ribs across it, standing on feet
instead of a sheet.

## What was built

`support_3mf.py --layout keel` → `<name>.keel.3mf`, part
`generated support keel`, from `gridsupport.build_keel`:

- Each underside patch is planned on its own: direction from its shape,
  turned to follow its lowest points; keel = a straight line along it; ribs
  every `--pitch` along the keel, spanning the area that needs support.
- Patches that are broad, branched or broken up get today's lattice; the
  report says why per patch. Specks under 0.5 mm² get no wall (reported).
- Walls stand on inverted-T feet 3 line widths wide (1.26 mm); the plate
  between walls is bare. Joining strips keep it one piece.
- Same clearance rules as the grid (gap 0.2 mm below, xy distance beside),
  same skips (variable layers, per-part overrides, cannot be one piece).
- The grid layout is unchanged (output checked identical on test fixtures).

## Measured (girl_130.3mf, plate 1, pitch 5, max-height 30)

| | Grid | Keel |
|---|---|---|
| Arm_Left wall length | 229 mm | 57 mm |
| Arm_Left support volume | 954 mm³ (0.42 walls) | 249 mm³ (0.45 walls) |
| Whole plate, filament added | +23.6 g | +18.4 g |

Arm_Left: keel at 20°, 8 ribs, all its support area within 2.5 mm of a wall.
Head, Leg_Left and most of the torso fall back to the lattice, so the
whole-plate saving is small.

Found by slicing with the Bambu Studio 02.08.02.61 CLI:

- Slanted walls exactly one line wide (0.42 mm) printed only 64 % of their
  length (single layers down to 30 %); 0.45 mm walls printed 100 %. Keel
  walls therefore default to the line width + 0.03 mm.
- Real overhang maps are speckled (1,600 specks on Arm_Left), so patches are
  grouped by closing gaps under 1 mm before planning.
- The CLI placed the plate 2.19 mm lower in y than the 3MF coordinates;
  shift before comparing sliced paths with the mesh.

## Open

- Owner: slice `girl_130.keel.3mf` in Bambu Studio, check the preview, then
  test-print Arm_Left — underside quality over the keel, how the feet hold,
  removal.
- Provisional rules to revisit after the print: the 80 % "keel stays in one
  band" share, the 1 mm grouping, 3-line feet.
- Not done: curved keels, keels for branched shapes (fingers), a G-code check
  of the gap at keel tops (only CAD statistics so far).
- The Arm_Left break itself remains unexplained.

Commits: `4833f9f` (idea note), `23150fc` (layout, tests, docs).
