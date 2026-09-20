### Proposed — derive `_BBOX_TOLERANCE_FLOOR` from the percentage

The one item here that is straightforwardly a bug rather than a design tension.
`_BBOX_TOLERANCE_FLOOR = 0.1` exists to stop `BBOX_TOLERANCE_PCT = 0.7%`
flagging sub-micron noise on small models, but on a 4.1 mm part it *is* 2.43% —
three times looser than the percentage it is meant to floor. It should be
derived from that percentage rather than competing with it.

**Explicitly not proposed:** making `MIN_LAYER` or `MERGE_DIST` relative. They
are physical properties of the printer; a 4 mm part and a 200 mm part are
printed by the same machine. The defect is the missing record, not the values.

