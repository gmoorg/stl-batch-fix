### Absolute constants mean different things at different scales

Raised by the user after inspecting the Leia output: *"it feels weird to use the
same value for Leia and big models like Mandy."* The measurement backs it — the
same constant spans **41x** as a fraction of the model:

```text
model                   diag mm   MERGE 0.01   FLOOR 0.1   MIN_LAYER 0.6
Leia/Neck_Cuff              4.1      0.2430%     2.4296%         14.578%
Leia/Head_Without           4.8      0.2074%     2.0739%         12.443%
Leia/Branches               8.6      0.1158%     1.1577%          6.946%
Mandy_Body                108.6      0.0092%     0.0921%          0.553%
whole-costume01           113.7      0.0088%     0.0880%          0.528%
Millenium_Falcon          197.7      0.0051%     0.0506%          0.303%
```

**`_BBOX_TOLERANCE_FLOOR = 0.1` inverts its own purpose.** It exists to stop
`BBOX_TOLERANCE_PCT = 0.7%` flagging sub-micron noise on small models. On the
Neck_Cuff it is 2.43% — more than three times looser than the percentage it is
meant to floor. On small models it is not a floor, it is a much wider ceiling.
It should be *derived* from the percentage, not compete with it.

**`MIN_LAYER = 0.6` is 14.6% of the Neck_Cuff's diagonal.** The print-scale
gate accepts any open boundary below one layer height as unprintable-anyway; on
a 4 mm part that waves through a gap spanning a seventh of the model. The
constant is genuinely a printer property, so the value is right — but its
*meaning* changes with scale, which is the real defect.

**`_VOLUME_MIN_MEANINGFUL = 50.0` switches the safety check off entirely.**
Neck_Cuff's bbox volume is 12 mm³ and Head_Without's is 21 mm³, both below the
threshold, so the volume-loss check that catches PyMeshFix deleting a region
**never runs** on them. Mandy, the model it was built for, sits 3,526x above
it. That is not a tolerance being wrong; it is a guard being absent on exactly
the models least able to survive without it.

**`MERGE_DIST = 0.01` is about authoring noise, not printing**, so it should
scale with the model or with `min_feature` rather than being fixed in mm.

Nothing broke on Leia despite all four being wrong for it — the parts are dense
enough (`min_feature` 0.003–0.011 mm) that PyMeshFix had ample real geometry to
work with, and all 19 returned `ok`. The constants were wrong and the meshes
were good enough to absorb it.

