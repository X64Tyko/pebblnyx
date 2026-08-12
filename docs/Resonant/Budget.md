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
density. Note the direction this arithmetic runs: it is for choosing what to *cut*, never for
deciding what to plan. Content added late to fill a gap reads as rushed; content cut from an
over-planned whole leaves the rest coherent, so cut whole units rather than thinning everything. The full game's six Nodes do not fit at this density, which is the number that should
drive the recommendation in `World.md` to deliver four Nodes rather than six.

Art figures are estimates and marked as such. They should be replaced with pipeline output as
soon as any real art exists -- `tools/pnx_assets.py` reports exact per-asset cost on every
build, so this table has a short shelf life and that is fine.

## The multipliers worth using

**Palette swaps, and the pipeline now does them for you.** Declare recolours next to the sheet
they recolour and each variant costs a 16-byte palette instead of another copy of every frame:

```toml
[[sprite]]
name = "drone"
sheet = "art/drone.png"
frames = [[0, 0, 16, 16]]
variants = ["art/drone_ranged.png", "art/drone_swarm.png"]
```

Measured on the example's one-frame npc: 384 bytes of pixels replaced by 32 bytes of palettes.
A six-frame character sheet saves over a kilobyte per recolour. **So the six enemy classes in
`Combat.md` should be four silhouettes with variants**, not six sheets -- which is what makes
that table affordable rather than aspirational.

Two authoring rules the pipeline enforces. A variant may change any colour but must not move,
add or remove a pixel, and transparency must match -- a mismatch is a build error naming the
frame. And it must not *merge* two colours into one, which a hue shift in an art tool can do
accidentally after quantisation: recolour by remapping colours one-to-one.

**Mirrored tiles are free.** A tile that is the horizontal, vertical or 180-degree reflection
of one already in the atlas is dropped and referenced with flip bits, which cost nothing -- the
map entry carries them. Worth knowing while drawing: a symmetric tileset genuinely is cheaper.

**Carving.** Never import a whole sheet. A region of each tileset is a fraction of the cost of
all of them -- the difference between 111% of budget and comfortably under it.

**Metatiles.** The pipeline picks them automatically when quadrant reuse pays; on the small
carved regions used so far it has declined, correctly. Worth re-checking once real tilesets
exist, since full sheets measured 1.96x reuse against 1.19x on a 64-tile carve.

**Sequenced music over recorded.** Already covered, and it is a 10x difference.

**Zone recolouring, which is the largest lever of all.** Reusing an atlas with a different palette
saves the whole atlas -- ~12,000 bytes for the cost of a palette. That is a 50x return against a
recoloured sprite's 36x, and it applies per zone rather than per character. It needs a **per-map
palette remap**, not the four reserved per-cell bits: the map carries a small array mapping the
atlas's palette slots to actual slots, so an atlas using four palettes costs four bytes in the map
and one extra indirection per tile blit. Per-cell overrides stay unbuilt until something genuinely
mixes palettes inside one map.

Reuse atlases across *different maps*; reuse a *map* only for an actual revisit. Same tileset with a
new layout reads as a related place, where the same layout in new colours reads as cheap.

**Enemy pools weighted by story progress and level.** Backtracking to a level-5 zone at level 20
shifts the weights toward what was waiting there, with familiar low-level enemies still appearing
occasionally. Pure content data -- no engine work -- and it pairs with palette-swapped enemies:
a "corrupted" variant of a known silhouette reads as *this place has gone wrong* rather than as a
new place, which is the cheap version of the corruption beat.

## What to watch

The sample budget is the one that can quietly eat the game -- 20,000 bytes for six effects is
already a quarter of the slice's total. **Recommend capping the slice at four effects** and
leaning on the sequencer's noise channel for impacts, which costs nothing.

The pipeline enforces the 1.5 s cap as a build error rather than a guideline, so the failure
mode here is a bundle that will not ship rather than a surprise on device.
