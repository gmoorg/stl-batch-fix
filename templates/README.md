# Bambu Studio print-process templates

Reference copies of the owner's Bambu Studio process presets (X1C, 0.4 mm
nozzle). Nothing in the code reads them; they are evidence for print-failure
analysis (see `check_3mf.py` / `support_3mf.py`).

| File | What it is |
|---|---|
| `Miniatures.v2.json` | The user preset the owner prints with. Stores only the keys it sets; everything else comes from `inherits: "0.20mm Standard @BBL X1C"`. |
| `Miniatures.flat.json` | The values Bambu actually uses: the preset flattened together with every profile it inherits (200 keys). |
| `Standard.json` | The flattened system profile `0.20mm Standard @BBL X1C`, the base the owner used when the printer was new and does not remember print problems with. |
| `overrides.json` | Only the keys where the miniatures preset differs from `Standard.json`, with the miniatures values. |
| `~overrides.json` | The same keys with the original `Standard.json` values. |

Checked 2026-10-17 (Bambu 2.8.0.6):

- `Miniatures.flat.json` matches every value in `Miniatures.v2.json`.
- `overrides.json` holds exactly the keys where `Miniatures.flat.json`
  differs from `Standard.json`.
- `~overrides.json` holds the same keys except `brim_type`,
  `support_critical_regions_only` and `version`, which `Standard.json` does
  not set at all.

If the preset changes, re-export all of these files together, or they will
stop matching.
