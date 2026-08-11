# Content budget

## The framing to correct

`Script.md` opens with *"~3,100 bytes (still <1.2% of 256KB)"*. True, and it points at the
wrong resource. **Dialogue is free. Art is not.** Every measured figure in
[`../MEASUREMENTS.md`](../MEASUREMENTS.md) says the same thing: five complete tilesets came to
111% of the appstore budget on their own, which is why carving exists.

Text is the one thing that can be written without counting. Worth knowing, because it means
the script can grow substantially -- the pacing gaps in `Slice.md` cost nothing to fill.

## Where the 256KB actually goes

Measured from the audiotest build, which is a real reference rather than an estimate:

| | Bytes | Note |
|---|---|---|
| One 1.5 s sample | ~24,000 | 16 kHz 8-bit |
| Explosion sample | 6,416 | measured |
| Laser sample | 1,456 | measured |
| A 22-bar song | 1,852 | measured, four channels |
| A 16-colour palette | 8 | measured |
| Whole slice dialogue | ~3,400 | from `Script.md` |

**A song is 1,852 bytes and a single sound effect is three times that.** The sequencer exists
for exactly this reason, and it means Resonant can afford distinct music per zone, per boss,
and for the outro without noticing -- while a dozen combat sound effects would hurt.

## Slice estimate

| Category | Items | Est. bytes |
|---|---|---|
| `ATLAS_OUTER_RUINS` | carved region, ~48 tiles | ~12,000 |
| `ATLAS_INNER_SANCTUM` | carved region, ~48 tiles | ~12,000 |
| Character sprites | Van, Mage, GRD, ~4 frames each | ~9,000 |
| Enemy sprites | 4 silhouettes, palette-swapped | ~8,000 |
| Equipment icons | 8 icons, one 16x16 atlas | ~1,000 |
| UI and font glyphs | dialogue box, meters | ~4,000 |
| Music | 4 tracks | ~7,500 |
| Samples | 6 effects | ~20,000 |
| Dialogue | full slice | ~3,400 |
| Palettes | ~12 | ~100 |
| **Total** | | **~77,000** |

**About 30% of the appstore budget**, leaving room for roughly three more zones of the same
density. The full game's six Nodes do not fit at this density, which is the number that should
drive the recommendation in `World.md` to deliver four Nodes rather than six.

Art figures are estimates and marked as such. They should be replaced with pipeline output as
soon as any real art exists -- `tools/pnx_assets.py` reports exact per-asset cost on every
build, so this table has a short shelf life and that is fine.

## The multipliers worth using

**Palette swaps.** A recoloured enemy costs **8 bytes**, not a sprite. Four silhouettes across
three palettes each reads as twelve enemy types for the price of four. This is the single
largest lever available and the engine already supports per-entity palette override.

**Carving.** Never import a whole sheet. A region of each tileset is a fraction of the cost of
all of them -- the difference between 111% of budget and comfortably under it.

**Metatiles.** The pipeline picks them automatically when quadrant reuse pays; on the small
carved regions used so far it has declined, correctly. Worth re-checking once real tilesets
exist, since full sheets measured 1.96x reuse against 1.19x on a 64-tile carve.

**Sequenced music over recorded.** Already covered, and it is a 10x difference.

## What to watch

The sample budget is the one that can quietly eat the game -- 20,000 bytes for six effects is
already a quarter of the slice's total. **Recommend capping the slice at four effects** and
leaning on the sequencer's noise channel for impacts, which costs nothing.

The pipeline enforces the 1.5 s cap as a build error rather than a guideline, so the failure
mode here is a bundle that will not ship rather than a surprise on device.
