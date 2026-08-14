# Pebblnyx design

Every decision here traces to a number in [`MEASUREMENTS.md`](MEASUREMENTS.md). Where
something is a guess, it says so.

---

## 1. What the framework is responsible for

A game author writes: content (a manifest), scenes, and per-tick logic. The framework
owns everything else — the loop, the framebuffer, asset residency, the audio mixer,
the save format, input backends, and lifecycle.

**Non-goals**, deliberately:

- **No dirty-rectangle or partial-redraw system.** Measured to give zero fps benefit.
- **No archetype/ECS storage machinery.** Layout measured irrelevant at every scale
  that fits in 128KB; it would cost code size and clarity for nothing.
- **No asset streaming cache.** Flash has no locality penalty and reads cost per call,
  so bulk residency wins outright.
- **No audio codec.** A Vorbis decoder alone exceeds the 64KB code budget.
- **No general-purpose scene graph.** Not enough code budget, and a tile game does not
  need one.

## 1a. Validation: strict, with a declared escape

The pipeline fails builds on content that cannot work, because a content bug on a watch does not
crash -- it presents as nothing happening, with a binary that looks fine. That only holds while the
errors are trustworthy. **A check that reports correct content as broken gets silenced wholesale,
and then it protects nothing.**

Static analysis cannot see game state. A room sealed behind a button-operated door is unreachable
at build time and correct at runtime, and no amount of flood filling will discover the button. The
answer is not to weaken the check into a warning that nobody reads. It is to give it **a declared
escape**: the author states the intent once, in the content, and it is never raised again.

    warps = [{ at = [12, 9], to = ["vault", 4, 4], gated = true }]

Three properties make this work, and they are the pattern for every check of this shape:

- **The acknowledgement lives on the declaration**, not in a side file. It cannot outlive what it
  describes -- move the warp and it travels along, delete the warp and it is gone. There is no
  fingerprint to maintain and nothing that goes stale invisibly.
- **It is content, so it is in git**, where a reviewer sees the claim next to the thing claimed.
- **The stale direction is reported**: `gated` on a warp that turns out to be reachable usually
  means the gate was removed, so the build says so without failing.

Given an escape, strictness is correct rather than merely tolerable -- an unreachable, undeclared
warp is unambiguously either a bug or an undocumented design, and both want the author's attention.

## 1b. Tile flags are the game's vocabulary, not the framework's

`pnx_map_flags` returns a byte and says nothing about what its bits mean, which is right: a game
may want `hazard`, `water`, `ledge`, `trigger`, or may have no use for `warp` at all. Only the
*names* are currently hardcoded, in a closed table of `solid` and `warp` shared between
`pnx_assets.h` and the pipeline. **Those become manifest-declared.**

One bit cannot be fully arbitrary. The pipeline's most valuable checks -- start position not inside
a wall, flood-fill reachability, a door not sealed inside a building -- all need to know which flag
blocks movement, and `pnx_map_solid` is a convenience built on the same assumption. So **bit 0 stays
a convention meaning "blocks movement"**, nameable by the game but tagged so validation still works.
Bits 1-7 are free and mean whatever the manifest says.

That keeps the checks that catch content which does not crash but silently does nothing, while
removing the framework's opinion about what a tile can be.

## 2. The master constraint: code size

`.text + .data + .bss` must fit in **65,535 bytes**, and the game shares that budget
with the framework. A complete playable slice was 8,140 bytes of `.text`, so there is
room — but not unlimited room, and a framework accretes features.

**Therefore every subsystem is opt-in at compile time, and unused ones must cost zero
bytes.** This is not a nice-to-have; it is the difference between a framework you can
build a game on top of and one that leaves you no budget.

```c
/* pnx_config.h — each game declares what it uses */
#define PNX_USE_TILEMAP    1
#define PNX_USE_SPRITES    1
#define PNX_USE_TEXT       1
#define PNX_USE_AUDIO      1
#define PNX_USE_SEQUENCER  0   /* SFX only — no music, mixer core alone */
#define PNX_USE_SAVE       1
#define PNX_USE_DIALOG     1
#define PNX_USE_DIAGNOSTICS 0  /* on-screen perf overlay; off in release */
```

The build must **report per-module `.text` against the 64KB ceiling**, so an author can
see what a feature costs before adopting it. Desktop engines never need this; this one
cannot ship without it.

Target budget, to be held honest as code lands:

| Module | Target `.text` |
|---|---|
| `platform` + `core` | ≤ 4 KB |
| `assets` | ≤ 2 KB |
| `gfx` (tilemap, sprites, text, camera) | ≤ 6 KB |
| `audio` (mixer + sequencer) | ≤ 8 KB |
| `input` | ≤ 1.5 KB |
| `save` | ≤ 2 KB |
| `app` (loop, scenes, lifecycle) | ≤ 2 KB |
| **Framework, everything on** | **≤ 26 KB** |
| Left for the game | ≥ 38 KB |

## 3. Layers

```
game code                       <- author writes this
─────────────────────────────
app        loop, state stack, lifecycle, save-on-blur
audio      mixer, voices, envelopes, sequencer, sfx priority
gfx        tilemap, sprites, text, camera
input      backends -> InputState
assets     handle-based registry, bulk residency
save       chunk packing, incremental writer, versioning
─────────────────────────────
core       fixed point, arenas, containers, diagnostics
platform   THE ONLY layer that touches Pebble APIs
```

The dependency rule is strict and one-directional. `platform` isolation exists so the
simulation and rendering logic can be compiled and tested on a host — proven viable in
the probe, where a single module touched Pebble graphics.

## 4. The loop — **built (M6), host-tested, not yet on device**

Driven by the render callback, not an independent timer, because the hardware already
paces at 37.33 ms and a second clock only fights it. The render REQUEST is now locked to
a fixed 40 ms grid rather than asked for as soon as the display will take it — see
`MEASUREMENTS.md`'s Frame pacing section for why that trades a ceiling that was never
reachable anyway for slack that protects against a frame running long.

```c
/* PnxFrameFn-shaped -- hand it straight to pnx_platform_run */
void pnx_app_frame(void *ctx, uint32_t elapsed_ms, PnxTarget *target);
```

This took an extra `ctx`/`target` over the original sketch above, once it had to match
`pnx_platform_run`'s actual callback shape rather than stand in for it.

- **Fixed 25 Hz sim tick (40 ms)** with an accumulator over *measured* elapsed time, so
  speed is independent of frame jitter. Now that the render request is ALSO locked to
  40 ms, a rendered frame carries almost exactly one tick instead of the
  roughly-one-frame-in-fourteen that used to carry none.
- **The accumulator clamp is load-bearing, not defensive.** While covered by a modal
  the app is throttled to ~0.4 fps, so a frame can arrive with a multi-second
  `elapsed`. Unclamped, the sim fast-forwards seconds every time a notification clears.
- **Went further than a clamp: throttle-aware pausing.** Between `FOCUS_LOST` and
  `FOCUS_GAINED` the accumulator is not fed at all, so a covered app does not accrue
  ticks to spend in a burst on `FOCUS_GAINED` -- it just stops, and resumes from zero.
- **A state stack, not a bare callback.** `pnx_app_push`/`pop`/`replace` own enter/exit/
  suspend/resume around whatever `input`/`tick`/`draw` a state defines; only the TOP
  state ticks, which is what makes "nothing simulates behind the menu" a property of the
  stack rather than a rule every mode's tick function has to remember to follow. Called a
  STATE and not a SCENE specifically to keep clear of `assets`' own scene (a loaded
  content chunk, an unrelated idea).
- **Full-screen redraw every frame.** No clear pass needed when an opaque tilemap
  covers the viewport.
- Edge-triggered input (an action press) is consumed by the **first tick only**;
  direction is continuous and may repeat. Otherwise one press fires up to four times.

Measured `.text + .bss`: **1,052 bytes**, against a 2KB budget. See `docs/ROADMAP.md`'s
M6 for the full account, including where resonant's own use of the stack stops short of
the whole idea (depth 2, not one state per mode) and why.

## 5. Assets, palettes and colour

A declarative manifest is the single source of truth; an offline tool emits packed
binaries plus a generated header. **No C file ever names a source PNG, a pixel offset,
a sheet layout, or a magic tile id.**

```c
PnxAtlas *atlas = pnx_assets_atlas(ATLAS_OVERWORLD);  /* one bulk read, resident */
const uint8_t *tile = pnx_atlas_tile(atlas, id);      /* pointer, no I/O */
```

Residency, not streaming: one bulk read per atlas (~518 µs for 16KB) because reads cost
**29 µs per call with no locality penalty**. Per-tile streaming would cost ~6.7 ms/frame
to save memory that is not scarce.

Scene boundaries are the only load points:

```c
pnx_scene_load(SCENE_CAVE);   /* frees the old scene's assets, loads the new set */
```

**The pipeline validates content and fails the build**, because content bugs otherwise
manifest as "nothing happens" on the watch. Already proven necessary: a door drawn
inside a sealed building produced a warp that could never fire, and nothing about the
binary looked wrong. Checks: ragged map rows, unknown legend characters, a start
position inside a solid tile, a warp not on a door tile, **a warp unreachable from the
start (flood fill)**, and a warp whose destination is solid in the target map.

### A map is authored in one of two formats, and checked identically in both

`rows` is an ASCII grid in the manifest, resolved through a legend. It is legible at a
glance — walls look like walls — and the sealed-door bug above was *visible* in the text.
It stays the right choice for a small, hand-written map, which is why the `overworld`
example still uses it.

It has two ceilings. One character per cell caps a map at the printable set, about
**ninety distinct tiles**, while the compiled cell is a u16 carrying a 10-bit index the
runtime resolves against **1024**. And a 255×255 map is ~65 KB of text: the `worldtiles`
manifest was 81 KB, of which 91% was two maps.

So a map may instead name a `source` file — a `.pnxmap`, described in
[`tools/pnx_mapfile.py`](../tools/pnx_mapfile.py). Cells are u16 indices into a per-map
**tile table**, and each entry is *(atlas, index-or-role, flip, flags)* — which is exactly
what a legend character resolves to. The legend did not go away; it stopped being spelled
in ASCII and stopped being shared across every map in the project. An entry may still name
a **role** rather than an index, so migrating a manifest does not silently downgrade a
symbolic reference that survives re-carving a sheet into a number that does not.

Two things this deliberately is not:

- **Not the compiled resource.** `map_*.bin` is derived from the source: rotated at build
  time for a landscape orientation, sliced into WorldTiles, its flag plane computed. The
  same source compiles differently per orientation, which is why the source cannot be the
  thing that ships — and why `.pnxmap` files are committed while `map_*.bin` is ignored.
- **Not a second set of rules.** Both formats resolve to cells and flags and then go
  through the *same* `finish_compile`: start-in-a-wall, warp-without-a-warp-flag,
  unreachable-warp and destination checks all apply. A second authoring path that quietly
  skipped them would be worse than no second path.

The trade is stated rather than hidden: a file buys the tile ceiling and the manifest
back, and costs a readable git diff on map changes. `pnx_mapfile.to_rows` converts a small
map back to text, so it is not a one-way door.

### Colour: device-independent source, device-specific build

Art is authored in full colour and **stays** full colour in the manifest. Quantisation to
what a given screen can show happens at build time, for a named target. Pixel data is a
palette index and never changes; only the palette is device-specific.

That is what makes better hardware free later. A screen with more than ARGB2222's 64
colours rebuilds the same sources into richer palettes with **no re-authoring, no
re-indexing, and no change to a single blob's pixel data**. It is also what lets the
editor show a truthful preview: it runs the same quantiser the pipeline does, so
"Pebble Time 2" in a dropdown shows exactly what will ship.

To be clear about how much this is worth *planning* for: almost none. Nothing here is
built in anticipation of richer hardware — it falls out of facts that are true anyway.
Art arrives as full-colour PNGs because that is what art is. The pipeline quantises at
build time because it must. Those two together **are** the device-independent pipeline;
there is no extra layer, no target abstraction, and no runtime conversion.

The only deliberate concession is that the quantiser is **one function taking a target
colour space**, rather than `>> 6` scattered through the pipeline — which is worth doing
for testability regardless. Deeper preparation would be speculative: these are
memory-in-pixel LCDs, where colour depth is a power and cost problem rather than a
software one, and 64 colours is typical of the class. We are not planning for more; we are
simply not preventing it.

**Quantise first, then derive palettes.** The ordering is not incidental — measured on the
same tileset:

| | Colours in sheet | Max per tile | Tiles over 16 | Distinct palettes |
|---|---|---|---|---|
| palettes from full-colour source | 235 | 47 | 7% | 97 |
| palettes after device quantisation | 17 | **9** | **0%** | **32** |

Deriving palettes before quantising would invent 97 palettes and force 7% of tiles through
a lossy 16-colour reduction, to produce output that then gets crushed to 64 colours anyway.
Quantising first collapses the problem: the device's own colour space is the best
compressor available, and it is free.

### Palette generation

Validated against five general-purpose tilesets (dungeon, exterior, interior, ship,
world; ~2,175 unique tiles), because the cave tileset used earlier turns out to be
atypically friendly at 17 colours. Real ones use 34-46.

The algorithm is a **greedy merge**, and the merge is what makes it work:

1. Quantise every tile to the target colour space.
2. Take each tile's set of opaque colours.
3. Sort by descending size; place each set into the first palette whose union with it
   still fits, otherwise start a new palette.
4. A tile whose own colours exceed the limit has its two nearest colours merged until
   it fits, and the pipeline says so.
5. Each tile records a one-byte palette index.

Step 3 is the whole difference. Deduplicating only *identical* colour sets gives 391
palettes for those tiles; merging any sets that fit together gives **37** — losslessly,
because a tile is perfectly happy in a palette that merely contains its colours.

| Sheet | Tiles | Exact dedup | Greedy merge |
|---|---|---|---|
| dungeon | 416 | 111 | **10** |
| exterior | 445 | 148 | **13** |
| interior | 449 | 181 | **24** |
| ship | 419 | 90 | **9** |
| world | 446 | 161 | **25** |
| all five | 2,175 | 391 | **43** |

Eight to twenty-five palettes per tileset is a number a person can look at.

**4bpp is the only depth.** An earlier draft added 6bpp for tiles too wide for a palette;
that was complexity in the wrong place. A tile needing more than 15 colours is a *content*
problem, so the pipeline repairs it — merging nearest colours in a space already quantised
from 16.7M — and reports which tiles, so the fix can happen in the art. Measured, it fires
on 10 tiles of 2,175 (0.5%). One depth means one blitter loop, no split point, and no
second palette kind.

### Cues taken from the SNES

The SNES organised CGRAM as 16 palettes of 16 colours, selected per tile, with **colour 0
of every palette transparent**. Two of those conventions are worth copying and one is not.

**Index 0 is transparent in every palette.** It costs a slot — 15 usable rather than 16 —
which measures at 43 palettes instead of 37 and **348 bytes across all five sheets, 0.1%**.
In exchange transparency stops being a per-palette decision, and the blitter discards a
transparent pixel *before* touching the palette at all, which is the cheapest possible
test in the inner loop.

**Palette selection is per tile**, exactly as the SNES tilemap carried palette bits.

Where we diverge: the SNES stored those bits **per map cell**, letting one tile appear in
several palettes across a map. We store the index on the **tile definition** instead.
That is far cheaper here — one byte per tile is ~2,175 bytes against ~23,000 for a byte
per cell across thirty maps — at the cost that a tile has one palette. Wanting the same
art in two palettes means either duplicating the tile (128 bytes) or using the draw-time
palette override, both of which are cheaper than taxing every map cell.

### Atlases should be small and semantic

An atlas per *thing* — cave environment, hero, grassland, furniture — rather than one per
source sheet. Semantically related tiles share colours, so a small atlas needs few
palettes, and the count stays legible instead of being an artefact of how an artist packed
their PNG.

It also matches how loading works: scenes are the only load point, so an atlas should be
the unit a scene wants. Extra atlases are close to free — an 8-byte header, a palette
table, and one ~29 us resource read each.

Source sheets are carved freely into these; nothing requires an atlas to correspond to a
file. Carving is also where the budget is won: five *complete* sheets are 111% of the
256KB budget at any encoding, while 128 tiles from each — the Final Fantasy 1 yardstick
for one tileset — is **32%**.

### Palettes are their own asset

Palettes are a separate blob loaded before anything else, because atlases and sprites
carry palette *indices* rather than colours. A palette used by four atlases is therefore
stored once, and loading an atlas before the table exists is a refusal with a message
rather than a screen of wrong colours.

Measured on the example: 2 palettes, **40 bytes**, shared across every atlas, sprite and
map in the project.

Sharing is **discovered, not declared**. When palettising an atlas the pipeline first
checks whether an existing palette already covers it, then whether one has room to be
extended, and only then creates a new one:

```
fit -> extend -> create
```

Deduping the palette table across all five real tilesets rather than per sheet takes
**685 palettes down to 388**, saving 4,752 bytes — so the reuse check pays for itself on
real content, just at the table level rather than by collapsing everything to one
palette.

An earlier draft of this document required a manifest declaration for sharing, on the
worry that adding an atlas could silently renumber a shared palette and corrupt another
atlas's indices. Extension only ever *appends*, so existing entries keep their index and
that cannot happen. The declaration was removed.

The binding is a default, not a constraint. Draws still take an optional palette:

```c
void pnx_sprite_draw(const PnxSprite *s, PnxTarget *t, int32_t x, int32_t y,
                     const PnxPalette *palette);   /* NULL = the atlas's own */
```

Without that override, a red slime and a blue slime could not share an atlas, because the
palette would be fixed at load for everything drawn from it. Per-entity variants are worth
the one parameter.

### Transparency and partial alpha

`GColor8` is ARGB2222 and its alpha field really does define four levels — `pebble.h`
states `3 = 100% opaque, 2 = 66%, 1 = 33%, 0 = transparent`. But the SDK ships **no colour
using the middle two**: of 65 named constants, 64 are fully opaque and one is
`GColorClear`. Partial alpha is honoured only by `GCompOpSet`, and `GCompOpAssign`
explicitly ignores opacity for 8-bit bitmaps.

None of that constrains us, because we never use those paths — we write framebuffer bytes
directly, so we *are* the compositor. Alpha bits in a framebuffer pixel have no documented
meaning, so the blitter always writes `a=3` and resolves translucency itself.

This falls out of palettes for free. **A palette entry is a full `GColor8`, so any entry
can be transparent or partially transparent.** Transparency is not a reserved index or a
special case in the format; it is a colour like any other, and a 16-entry palette can hold
several at different alphas. Soft shadows, glass and ghost sprites are ordinary art.

Blending is cheap enough not to think about: `(src*a + dst*(3-a))/3` per channel, and with
two-bit channels the entire function is a **64-entry lookup table** — src(4) x dst(4) x
alpha(4) — shared by R, G and B. Three lookups and a reassembly per pixel.

An atlas records whether any of its palette entries is non-opaque, so the common case stays
a straight indexed copy and only art that actually uses translucency pays for blending.

### Palettes are derived, never authored

The pipeline derives palettes per tile and per sprite frame, dedups them, and never asks.
Importing a colour sheet remains the entire workflow.

Authoring one is opt-in, for stable indices and runtime palette swaps:

```toml
[palette.slime_blue]
remap = { "#3a7f3a" = "#2038a0", "#1c4a1c" = "#101c60" }
```

**Remap by source colour, never by index.** You eyedropper the green out of your own sprite
and say what it becomes. Indices are an internal detail that shifts when art is added; a
variant written against `#3a7f3a` keeps meaning what it said, while one written against
index 7 would silently start recolouring something else. Indices are assigned
deterministically so builds stay reproducible and generated headers diff cleanly.

The pipeline errors if a remapped colour is not in the base palette — catching the common
mistake of recolouring a shade the sprite does not contain and wondering why nothing
changed. `--emit-palettes` dumps derived tables with hex swatches, and the editor shows
them visually.

### Palette selection lives on the entity

Which palette to draw with is **entity state**, not atlas state. Two slimes of the same
art, one green and one blue, differ by a single byte:

```c
uint8_t palette_slot;   /* 0 = the asset's own */
```

One byte per entity, and in the SoA layout the probes measured it is one more channel.

### A bounded palette table, loaded like everything else

Palettes live in a **fixed-size table in the scene arena**, sized by
`PNX_PALETTE_SLOTS` in `pnx_config.h`, and are loaded and released exactly as atlases and
maps are.

An earlier draft made them permanently resident on the grounds that every palette in the
example content came to 1,712 bytes. That reasoning does not survive being a *framework*:
1,712 bytes is a fact about this content, not about content in general, and a project
with many more tilesets or variants would grow it without any bound the runtime could see.
It is the same error as validating the tile format against a 17-colour cave tileset.
Bounded and explicit beats small-today.

The table is flat rather than an array of pointers, which keeps the hot path a multiply
instead of a chase:

```c
palette = &table->entries[slot * PNX_PALETTE_ENTRIES + tile_palette_index];
```

Loading follows the rules already in place:

- `pnx_atlas_load()` brings the atlas's own palette set with it — an atlas is never
  loaded without valid colour.
- `pnx_palette_load(asset_id)` claims a slot for a variant and returns it.
- A scene boundary resets the arena, which releases every slot at once. There is no
  eviction policy because there is no partial free anywhere else either.

Overflowing the table returns a failure and logs which scene asked for what, rather than
silently drawing in the wrong colours — the failure mode that would otherwise look like an
art bug. The asset pipeline reports palette count per scene against `PNX_PALETTE_SLOTS`,
so the ceiling is visible at build time rather than discovered on a watch.

**A slot holds a palette *set***, since an atlas may use several after merging; the tile's
own palette index selects within it. For sprites — the measured ones use 11 colours and so
need exactly one palette — that index is always 0.

Ids in the generated header (`PNX_PALETTE_SLIME_BLUE`) name assets; slots are runtime
positions a scene assigns. Entities store the slot, so the draw path stays a direct index
with no lookup:

```c
void pnx_sprite_draw(const PnxSprite *s, PnxTarget *t, int32_t x, int32_t y,
                     const PnxPalette *palette);   /* NULL = the asset's own */
```

A global tint rewrites the live table — bounded by construction at
`PNX_PALETTE_SLOTS * 16` bytes — against the 45,600 pixels a per-pixel approach would
touch.

It must also **enforce the 256KB appstore resource budget** and report the breakdown.
At 256 bytes per 16x16 tile that ceiling is roughly 900 tiles total including sprites,
fonts, audio samples and maps — the constraint most likely to shape the actual game.

## 6. Graphics

Pixel format is `GColor8` throughout, matching the framebuffer exactly, so a blit is a
copy with an optional zero test and never a conversion.

```c
void pnx_tilemap_draw(const PnxTilemap *m, PnxTarget *t, int32_t cam_x, int32_t cam_y);
void pnx_sprite_draw(const PnxSprite *s, PnxTarget *t, int32_t cam_x, int32_t cam_y);
void pnx_blit_opaque(PnxTarget *t, const uint8_t *src, int16_t w, int16_t h, int16_t x, int16_t y);
void pnx_blit_masked(PnxTarget *t, const uint8_t *src, int16_t w, int16_t h, int16_t x, int16_t y, bool mirror);
```

Mirroring is in the blitter because it is how a sprite faces both ways without a second
frame — measured to cost nothing, and it halves sprite memory.

**Text is a special case.** `graphics_draw_text` needs a `GContext`, which exists only
inside an update proc, and it costs ~4.3 ms — 12% of a frame. So text is drawn through
a narrow `platform` hook rather than the byte-level blitter, and the framework should
make it easy to draw text *rarely* (dialog panels, HUDs) rather than every frame.

## 7. Audio

A software mixer over one continuously open PCM stream. The batch API is rejected on
evidence: a **fixed 94 ms cost per submission**, 7–16 ms of app-task blocking, and
concurrent submission refused outright, which makes music-plus-effects impossible.

```c
pnx_audio_init(PNX_PCM_8KHZ_8BIT, /*lead_ms*/ 90);
pnx_music_play(SONG_OVERWORLD);
pnx_sfx_play(SFX_DOOR, /*priority*/ 2);
pnx_audio_feed();   /* once per frame */
```

- Stream buffer holds **1,024 ms** at 8 kHz/8-bit — ~25 frames of slack, so a late
  frame cannot underrun.
- **Lead time is exposed because lead *is* SFX latency.** 74 ms lead = 2 frames of
  slack and 74 ms latency; 500 ms is safe but sloppy. That is a game-design decision.
- Music is note events plus samples, not recorded audio: minutes of music for a few KB
  against ~240KB for one Vorbis minute.
- Mixing 8 kHz mono is a fraction of a millisecond against ~35 ms, so polyphony, real
  ADSR envelopes and loop points are all affordable — everything the 4-voice batch API
  denied.

## 8. Save — **built (M5), host-tested, not yet on device**

Landed close to this sketch, with the API split into two shapes for two different call
sites rather than one `begin`/`write`/`commit` triple — the actual usage turned out to
split cleanly along "is there a frame to protect":

```c
/* interactive: a player chose SAVE, and a stall would be visible */
pnx_save_begin(SLOT, &state, sizeof(state), VERSION);
while (pnx_save_pending(SLOT)) pnx_save_step(SLOT);   /* one call per rendered frame */

/* save-on-blur: ~297 ms of warning and no frame left to protect, so just finish it */
pnx_save_write(SLOT, &state, sizeof(state), VERSION);   /* begin + a blocking flush */

/* either way */
pnx_save_load(SLOT, &out, sizeof(out), VERSION, &out_bytes);
```

Driven entirely by the measurement that **writes cost ~7.3 ms per call regardless of
size**, and a 4KB save is 106 ms — about three frames:

- **Pack into full 256-byte chunks.** A 4-byte `persist_write_int` costs half of a full
  chunk, so scattering small values is ~31x worse per byte. The 8-byte header rides
  inside chunk 0 rather than spending a key of its own, for the same reason.
- **Use as few keys as possible** — rewriting one key beat spreading across keys.
  `PNX_SAVE_CHUNKS_PER_SLOT` (16) is sized directly off the "realistic 4KB save, 16
  keys" measurement.
- **Spread incrementally, one chunk per frame** — for the interactive path only. At
  7.3 ms against ~35 ms that is ~20% of a frame and never stutters; 4KB becomes 16
  invisible frames. Save-on-blur does NOT spread: it blocks, because the ~297 ms
  `will_focus` warning against a ~106 ms 4KB save means there is time to just finish,
  and no rendered frame left to protect anyway.
- Saves carry a version; the framework refuses to load one newer than the caller
  understands. Migration for an older save is left to the game — not built, because
  nothing has shipped yet for there to be an older save FROM.

Measured `.text + .bss`: **808 bytes**, against a 2KB budget. What was not anticipated
here: **two slots**, not one — `PNX_SAVE_SLOT_RESUME` (automatic, save-on-blur) and
`PNX_SAVE_SLOT_CHECKPOINT` (an explicit save, at an author-placed point), because
resonant's own design (`docs/Menu.md`) treats those as different data, not two paths to
the same record. See [`ROADMAP.md`](ROADMAP.md)'s M5 for what actually landed and what
a real game does with it.

## 9. Input — **built**

Landed simpler than a backend table over one abstract `PnxInputState`, because the actual
problem turned out not to be "touch vs buttons" but **orientation**: the three-button
cluster is bolted to the case and does not rotate with the content, so `pnx_input.h`'s real
job is addressing it by the POSITION the player reads (0 nearest the screen's origin) rather
than by which physical button that is -- `buttons_bottom` reverses which physical button is
"first" because the watch turned the other way, and a game that reads positions needs no
branch for that at all.

```c
bool pnx_input_pressed(PnxButton button);        // edge, this frame
bool pnx_input_held(PnxButton button);
int8_t pnx_input_axis(void);                     // -1/0/+1 along the cluster, level
int8_t pnx_input_axis_pressed(void);             // same axis, edge-triggered
PnxButton pnx_input_cluster(uint8_t pos);        // position -> physical button
```

A game still resolves touch itself (a relative drag for movement, a tap to confirm --
`resonant/src/c/field.c` is the reference), rather than the framework owning a stick
abstraction: touch semantics turned out to be per-game (a drag threshold that must not
double as a tap, in resonant's case) rather than something one abstraction could serve
generically. What the framework does own is the bookkeeping every game would otherwise
rewrite -- edges, hold times measured from the event's own timestamp rather than from the
frame that noticed it, since a frame can arrive carrying seconds while the app is covered.

**The occlusion problem named below is still real and still unsolved.** A finger on a
200px screen covers much of the play area during a drag; resonant's answer (drag from
touchdown, not from a fixed zone) is one candidate, not a settled one.

## 10. Scripting (deferred, designed for)

Alloy — Moddable XS with a C FFI — is the only plausible route to *broad* adoption,
since a C framework still demands C. The FFI is genuinely good: ArrayBuffers pass as
raw pointers, so C works directly on JS memory.

But **JS is ~363x slower than C** (49,277 ns/entity vs 136 ns), roughly 3.4 µs per
elementary operation. That gives ~10,000 JS operations per frame. JS can afford native
*calls* — 195 `drawBitmap` per frame is ~0.6–1.2 ms — but not native *work*: 256
entities of trivial movement costs 12.6 ms, 36% of a frame.

So the framework is C, and **JS is a candidate scripting layer for composition only**:
scene definitions, dialog trees, event handlers, item tables. Milestone 8, not now. The
C API should avoid designs that would be hostile to being wrapped — prefer handles and
flat structs over callbacks with rich context.

Pending measurement: per-call FFI overhead, which sets exactly how much work a native
call must do to be worth crossing for.

## 11. Open questions

Honest list of what is not settled. Three items this list carried for a long time are
gone because they got answered rather than because the list was tidied: **input latency**
(M0 measured it directly rather than estimating -- see `MEASUREMENTS.md`, ±74ms hits
100%), **fonts** (E7 shipped them, costs measured on real content), and **the host test
harness** (it is how every milestone since M1 has been verified before touching a watch,
724 checks and counting).

1. **Per-call FFI overhead** — needed before committing to the scripting layer (M8).
2. **Battery.** Instrumented but barely measurable: apps get only a coarse `uint8`
   percent, and the firmware itself warns the model is inaccurate. One data point takes
   hours. Also probably the wrong question — `light_enable(true)` "will rapidly deplete
   the battery" per the docs, and a reflective screen needs it indoors, which likely
   dwarfs any rendering difference.
3. **Device confirmation for M6's own code.** M5's core save-on-blur claim is now
   confirmed on device (`examples/savebench`); `pnx_app`'s throttle-aware pausing has not
   been, since the device probes so far talk to `pnx_platform_run` directly rather than
   through `pnx_app`. Landscape/screen-lock (M4c) and full audio-under-load (M4) are
   likewise still unconfirmed. See `ROADMAP.md`'s milestone tags.
4. **Why `examples/flashbench`'s single-large-resource read cost grew over a stress run
   rather than staying flat** (`examples/stressbench`). Thermal, heap fragmentation, and a
   specific costly chunk index are all plausible; none is confirmed. See
   `MEASUREMENTS.md`'s "Combined load" section.

Resolved rather than dropped: **combined-load frame cost** (`examples/stressbench`
measured it directly -- worst frame grew to 43ms under a save step, audio's own worst gap
moved with it, and zero audible glitches across the run) and the isolated glyph-blitter
and flash-offset numbers this list used to carry as open (`examples/textbench`,
`examples/flashbench` -- see `MEASUREMENTS.md`).

## 12. Decisions rejected, with evidence

Recorded so they are not revisited. Each of these looked correct beforehand:

| Rejected | Why |
|---|---|
| Dirty rectangles / partial redraw | fps identical at 228 and 28 dirty rows |
| SoA / archetype ECS | all layouts within 1.5x; pointer chasing was *fastest* on one kernel |
| Cache-line padding everywhere | +24% on narrow sweeps but **−13%** on wide ones, and 23% more RAM |
| Per-tile asset streaming | ~6.7 ms/frame versus 518 µs for one bulk load |
| Ogg/Vorbis music | decoder alone exceeds the 64KB code cap; even then ~1 min of audio fills the resource budget |
| Batch audio API for music+SFX | fixed 94 ms seam per submission; concurrent submission rejected |
| A JS-implemented engine | 363x slower than C |
| GBC-style emulation as the target | contest criteria explicitly reward "good use of new platforms"; ports are not what the platform wants |