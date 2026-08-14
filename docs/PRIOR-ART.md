# Prior art: Pebblemon and the GBC graphics engine

[Pebblemon](https://github.com/HarrisonAllen/pebblemon-watchface) by Harrison Allen is the
most accomplished game on this platform -- a Johto-region Pokemon clone with all 251 creatures,
running on every Pebble from the original to Round 2. Its renderer is a separate library,
[pebble-gbc-graphics](https://github.com/HarrisonAllen/pebble-gbc-graphics), which emulates
Game Boy Color rendering on a Pebble.

It is the only serious point of comparison pebblnyx has, and it is worth studying rather than
re-deriving.

## Measured cost

Both engine versions were compiled for `emery` **by the real SDK build** -- dropped into a
Pebble project and built with waf, so the flags are the SDK's own, not approximations. Sizes
are from `arm-none-eabi-size` on the resulting objects.

| | pebblnyx `pnx/gfx` | GBC basic (2bpp) | GBC advanced (4bpp) |
|---|---|---|---|
| `.text` | **1,382** | 3,466 | 4,498 |
| `.bss` | **0** | 0 | 0 |
| Runtime RAM | **0** | ~8,480 heap | up to ~41,472 heap |

Both engines allocate everything on the heap and take their sizing at construction, which is
the same conclusion we reached for the audio buffers -- and worth noting as independent
confirmation that it is the right shape on this platform.

Pebblemon calls `GBC_Graphics_ctor(window, 1)`: the basic engine with one VRAM bank. That
works out to:

| | Bytes |
|---|---|
| VRAM, 1 bank (256 tiles x 16 B) | 4,096 |
| Background tilemap + attrmap (32x32 each) | 2,048 |
| Window tilemap + attrmap | 2,048 |
| Two palette banks | 64 |
| OAM (40 sprites x 4 B) | 160 |
| **Total** | **~8,480** |

The advanced engine at full configuration -- 4 VRAM banks of 4bpp tiles, 4 background layers
-- reaches ~41,472 bytes, a third of the entire 128 KB slot.

## The architectural difference

**They emulate VRAM; we read from flash.** The GBC engine keeps a bank of tile bitmaps in RAM
and indexes into it, which is what a Game Boy does and what makes runtime tile mutation free --
animated water, text composed into tiles, palette cycling on specific slots. pebblnyx keeps
atlases in flash and blits from them, which is why our renderer costs no RAM at all.

Neither is wrong. Ours follows from a measurement they did not have: **flash reads cost ~29 us
per call with no locality penalty**, so re-reading a tile is cheap and a resident copy buys
little. Their model buys mutation; ours buys 8 KB. For an RPG that does not rewrite its
tileset every frame, ours is the better trade -- but if Resonant ever wants animated water or
tile-composed text, this is the cost we would be paying to avoid.

## Features worth taking, ranked by value per byte

**1. A per-row render callback.** Their line-compare and HBlank interrupts let a game change
palettes mid-frame, which is how you get gradient skies, parallax bands, screen wipes and
damage flashes essentially for free. **We already render row by row** through
`pnx_target_row`, so a callback between rows is a few dozen bytes. This is the single highest
value item on the list and we have none of it.

**2. A per-tile attribute byte.** Theirs carries palette index, VRAM bank, flip X, flip Y and
a hide bit. **Flip X/Y is the prize** -- two bits that halve the tileset for any symmetric art,
which is a *content budget* win and content is our binding constraint, not code. We mirror
sprites at draw time but tilemaps have no per-tile attributes at all.

**3. A non-scrolling window layer.** A second tilemap layer at fixed offset, for HUD and
dialogue. We would draw those by hand today. Our tilemap already takes a camera offset, so a
layer pinned at zero is nearly free.

**4. Per-layer scroll offsets.** Follows from (3); gives parallax for the cost of two shorts
per layer.

## Features not worth taking

**VRAM banks.** 4 KB minimum to buy runtime tile mutation that flash reads already provide at
29 us. This is the whole reason our renderer is 0 bytes of RAM and theirs is 8 KB.

**Alpha blend modes** (add, subtract, average, AND, OR, XOR). Cheap on RGB, expensive on
indexed colour: averaging two indices needs a 64x64 lookup table, which is 4 KB to avoid
converting to RGB and back. Not worth it for a tile-based RPG.

**40 sprites with no per-line limit.** More than we need, and our depth-sorted feet-anchored
sprites are better suited to an RPG overworld. Their variable sprite dimensions -- up to 15x15
tiles -- are genuinely more flexible than our fixed frames, and worth revisiting if a boss
needs to be larger than a sprite.

## The thing to study before M9

**Pebblemon already ships on all seven platforms**, including the 1-bit ones. That is the
author-once problem from [`ROADMAP.md`](ROADMAP.md) solved in shipping code, and the mechanism
is the same shape as the palette ink-mask proposed there: colour lives in small per-tile
palettes, so a 1-bit target remaps palette entries rather than needing separate art.

Before building our 1-bit path, read how theirs handles it. It is the one place where prior art
on this platform is ahead of us, and re-deriving it would be a waste.

## Pebblemon's actual footprint

Read from the `.pbw` committed in its repo -- shipped binaries, not a build of mine.
`virtual_size` is at offset `0x80` in the app header and is the number directly comparable to
our size report. This version targets four platforms and predates `emery`.

| Platform | `virtual_size` (static RAM) | Resources (flash) |
|---|---|---|
| `aplite` | **13,972** | 19,302 |
| `diorite` | ~13,976 | 19,302 |
| `basalt` | **18,806** | 19,327 |
| `chalk` | ~18,882 | 19,327 |

**Only 154 bytes of that is `.bss`** on basalt -- `virtual_size` 18,806 against `load_size`
18,652. Everything else the game needs is heap, which is what allocating VRAM, tilemaps and
OAM in the constructor buys. Independent confirmation of the shape.

### Resource composition

Uncompressed on disk; the pbpack ships at 19,302 after compression. Tiles are 8x8 at 2bpp,
so **16 bytes each**.

| | Bytes | Tiles |
|---|---|---|
| Spritesheet | 8,640 | **540** |
| World tilesheet | 2,592 | **162** |
| Animation tilesheet | 304 | **19** |
| **All tile data** | **11,536** | **721** |
| Five area maps + location table | 3,514 | |
| Two fonts (TTF) | 9,060 | |
| Menu icon | 956 | |
| Total on disk | 25,066 | |

### Persistence

Two keys, written with a single `persist_write_data` each: one save struct and one settings
struct. Against our measured ~7 ms per persist call regardless of size, that is about as cheap
as saving gets.

## What this says about our headroom

**Their entire game is 18,806 bytes of static RAM. Our framework demo is 13,384.** That is the
sobering comparison and it is a fair one -- though ours includes a software audio mixer, which
Pebblemon has no equivalent of, and theirs includes a complete game.

Against `basalt`'s 64 KB that is 29% for a finished game. Against `emery`'s 128 KB it would be
15%. **Static RAM is not what will run out.**

**Fonts were 9,060 bytes -- 36% of their resource payload**, the single largest line item, and
bigger than every tile in the game combined, when this was written. E7 has since landed
pebblnyx's own font pipeline and it undercuts that badly: resonant ships two faces, a HUD
font and a dialogue font, `charset = "auto"` deriving the glyph set from the actual dialogue
rather than shipping a full alphabet, at **1,234 + 1,691 = 2,925 bytes** for both (built
resource sizes, `resonant/resources/font_hud.bin` and `font_dialogue.bin`) -- less than a
third of Pebblemon's 9,060 for two TTFs. The gap is the format, not the content: theirs ships
a general-purpose TTF outline per face; ours ships pre-rasterised, per-glyph-trimmed bitmaps
of exactly the codepoints a project's own dialogue uses, at whatever pixel size and bit depth
it chose. The example in `MEASUREMENTS.md`'s Font costs (E7) section is smaller still (757 +
902 B) because it carries fewer glyphs.

**Our tiles cost 8x theirs, each.** 4bpp at 16x16 is 128 bytes per tile; 2bpp at 8x8 is 16.
Per unit of screen area we pay 2x for the extra colour depth, and we lose again on reuse
because larger tiles deduplicate worse -- their whole overworld is 162 tiles in 2,592 bytes,
where our 48-tile carve is ~6,144.

That gap is exactly what metatiles close: the pipeline already composes 16x16 tiles from
deduplicated 8x8 quadrants and measured **1.96x reuse on full sheets** against 1.19x on the
small carves used so far, which is why it currently declines. **Their engine is natively 8x8,
which is the entire reason their art budget goes so far.** Once real tilesets exist, metatiles
stop being an optimisation and become the thing that makes the art budget work.

## Not measured

Pebblemon's frame rate, and its heap high-water mark. Both need the app built and run on
device.
