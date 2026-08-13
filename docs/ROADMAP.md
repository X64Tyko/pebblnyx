# Roadmap

Current state: **M0-M4d complete**, editor through E15, shipped as
[v0.1.0-beta.1](https://github.com/X64Tyko/pebblnyx/releases/tag/v0.1.0-beta.1) —
installers for Linux, Windows and both macOS architectures, engine inside. M5 (save)
next. `platform`, `core`, `assets`, `gfx`, `audio` and `input` run on device and are
covered by 562 host checks plus 121 pipeline checks. Audio, landscape and map streaming
still want hardware confirmation. No emulator is possible for PT2 — see
[`EDITOR.md`](EDITOR.md).

The ordering principle: a framework with no consumer gets the abstractions wrong in ways
nobody discovers until someone tries to use it. PGE (the 2014 Pebble game engine) failed
partly this way — its animation path reallocated a bitmap from a resource on every frame
change, and its shipped example divided by zero on startup. Nobody noticed because nobody
built on it. So **a real game is built alongside the framework from M3 onward**, and every
API is exercised before it is published.

---

## M0 — Latency spike — **DONE**

Driven by [`GAME.md`](GAME.md): Legend of Dragoon's Addition system needs timed input
during an attack, and that is the one mechanic this hardware is worst at. Frames are
37.33 ms and cannot be shortened, so end-to-end latency is **≥37 ms by construction** —
and had never been measured.

**Result: viable.** Tap spread 31 ms, systematic lag +27 ms; ±74 ms window hits 100%,
±37 ms hits 75%. Touch has half the lag of buttons. Reaction cues are usable but worse —
~352 ms lag with 75 ms spread, so a cue must lead its target by ~350 ms; anticipation is
2.4x more precise and stays the default. Full numbers in
[`MEASUREMENTS.md`](MEASUREMENTS.md), design consequences in [`GAME.md`](GAME.md). Probe
lives in `../pebble-timing-probe`.

## M1 — Foundation — **DONE**

Extract `platform` and `core` from the probes; establish the machinery that keeps the
64KB budget honest.

- `platform`: framebuffer target, timers, buttons, touch, focus — the seam is the only
  place `<pebble.h>` appears, and the two implementations (`_pebble.c`, `_host.c`) are
  mutually exclusive by `#ifdef`, not merely by build convention
- `core`: 16.16 fixed point, arenas, diagnostics, **and a formatter** — unplanned, but
  Pebble's libc has no `vsnprintf` and newlib's does not link here at all
- `pnx_config.h` compile-time module selection, with unused modules costing zero bytes
- **Build reports per-module size against the 64KB ceiling**, automatically, every build
- Host test harness compiling `core` natively — 76 checks, no dependencies

**Result.** Verified on device: the example runs, reports `touch=1`, and exits with
`Still allocated <0B>`. The startup log written from `main()` *survived*, which is the
deferred ring doing the one job it exists for.

The empty example costs **6,452 bytes of 65,535 (9.8%)**, or **2,528 (3.9%)** with
diagnostics off — which verifies the zero-cost claim rather than asserting it. The size
report paid for itself immediately by exposing 754 bytes of `__udivmoddi4` pulled in by one
`uint64_t` division in the formatter. Findings in [`MEASUREMENTS.md`](MEASUREMENTS.md).

## M2 — Assets — **DONE**

Generalise the probe's pipeline into a reusable tool.

- Manifest is **external TOML** the game owns, not a Python dict inside the tool: manifests
  carry real knowledge in their comments, JSON cannot hold one, and the editor has to read
  and write this file
- Quantisation to `GColor8`, tile dedup, colour-key handling
- Generated header of handles, counts, tile roles and dialog ids — no number in it is
  something a human should type into game code
- **Validation that fails the build**, with tests asserting that it does *and* that the
  message names the actual problem, since "invalid manifest" leaves an author as stuck as
  silence
- **256KB appstore budget enforcement** with a per-category breakdown
- **`package.json` resource list generated from the manifest**, closing a duplication that
  fails silently: a blob built but not declared is simply absent from the bundle
- Runtime: handle-based registry, bulk residency, scene load/unload — **904 bytes**
- Platform seam grew resource reads, its first extension since M1
- **Map format v2**: tile flags moved to a per-tileset table with sparse overrides, cutting
  map size 49% and recovering 8.7% of the content budget at RPG scale

**Result.** The probe's content builds through the new pipeline and every payload is
**byte-identical** to what the probe produced — the acceptance criterion met provably
rather than by inspection. `examples/overworld` linked at 8,792 of 65,535 bytes (13.4%).

## M3 — Graphics + first game skeleton — **DONE**

**First job: settle the tile storage format**, because the blitter is what fixes it and
changing it later means changing the pipeline, the runtime, the editor and the art.

- **Device-independent source, device-specific build.** Art stays full colour in the
  manifest and is quantised to a named target at build time. Pixel data is an index, so a
  future screen with more than ARGB2222's 64 colours rebuilds the same sources into richer
  palettes with no re-authoring, and the editor previews truthfully by running the same
  quantiser.
- **Quantise first, then derive palettes.** On one tileset: palettes taken from full-colour
  source need 97 tables and push 7% of tiles through lossy reduction; taken after device
  quantisation, 32 tables and nothing lost. The device's colour space is the best
  compressor available and it is free.
- **Palette generation is a greedy merge** — each tile's colour set into the first palette
  whose union still fits. Losslessly: 391 palettes down to 37 across five real tilesets,
  8-25 per sheet. Merging, not deduping, is what makes the count sane.
- **Index 0 is transparent in every palette**, SNES-style. Costs 348 bytes (0.1%) and lets
  the blitter reject a pixel before it reads the palette.
- **Atlases are small and semantic** — cave env, hero, grassland — not one per source sheet.
  Related tiles share colours, so each needs few palettes, and an extra atlas costs an
  8-byte header and one ~29 µs read.
- **4bpp is the only depth.** A tile over 15 colours has its nearest pair merged and is
  reported, not accommodated with a second format; that fires on 0.5% of real tiles. One
  depth means one blitter loop and no second palette kind.
- **Palettes are their own asset**, loaded before the atlases and sprites that index them.
  Shared project-wide: 2 palettes, 40 bytes, for the whole example.
- **Content will not fit whole tilesets.** Five full sheets are 111% of budget at the best
  encoding, and the practical ceiling is ~2,000 tiles before sprites, maps, dialog or audio.
  Regions and `max_tiles` are mandatory.
- **Transparency is an ordinary palette entry.** `GColor8` has four alpha levels, but the
  SDK ships nothing using the middle two and honours them only through `GCompOpSet` --
  irrelevant here, since we write the framebuffer and *are* the compositor. Blending is a
  64-entry LUT shared across R/G/B, and atlases flag whether any entry is non-opaque so
  opaque art keeps a straight indexed copy.
- **Palettes are derived, never authored.** Variants remap by *source colour* rather than
  index, so adding art cannot silently recolour the wrong thing, and a per-entity swap
  stays a 16-byte draw-call parameter.
- **Metatiles**: tiles split into deduplicated 8x8 quadrants, drawn by a paired-row blit
  that walks 16 rows spanning two quadrants rather than issuing four 8x8 blits, which would
  have doubled the per-row cost that dominates. 1.72x on five real tilesets against 1.19x on
  a small hand-picked region -- so the pipeline sizes both layouts and picks, rather than
  making it a preference.
- Tilemap with smooth scrolling and camera clamping; sprites with transparency, mirroring,
  feet-anchored positioning and depth sort; text through the narrow platform hook.
- **Game starts here**: a walkable scene using only framework APIs.

**Result.** The overworld example is a walkable scene with **no drawing code of its own**
— tilemap, sprites, camera and text all come from the framework. Verified on device: both
scenes load, the warp round-trips, and the arena returns to exactly its previous size.

The pipeline's build-time residency prediction matched hardware: cave 9,360 B predicted
and 9,360 actual, outdoor 10,086 predicted and 10,088 actual (alignment). A scene's cost
is knowable before it runs.

| Module | Bytes |
|---|---|
| pnx/gfx | 1,442 |
| pnx/assets | 2,455 |
| pnx/core | 3,817 |
| pnx/platform | 1,525 |
| game | 1,286 |
| **app total** | **12,208 / 65,535 (18.6%)** |

**Resource management** landed here too, prompted by the observation that a scene's asset
list was living in C where the pipeline could not check or budget it. Scenes are declared
in the manifest, validated at build time, and reported with their resident cost.

**The blit measurement is in, and it contradicted the estimate.** A full screen of 4bpp
tiles plus sprites costs **~7,400 us**, 21% of the frame, where the estimate had been
2,350-3,100 us. 4bpp decode is ~2.5x an 8bpp copy per pixel, not near parity. The format
stands -- 26.8fps held with no drops, and halving the content is worth a fifth of a frame
we were spending idle -- but the number is now measured rather than inferred, and the
inner loop was rewritten off the back of it.

## M4 — Audio — **DONE** (pending device confirmation)

The largest unknown, and the most valuable thing the framework provides.

- Mixer over a continuously open PCM stream; per-frame feed; configurable lead
- Voices, sample playback with loop points, ADSR envelopes
- Sequencer: patterns, order, tempo, note events
- SFX with priority and voice stealing
- Music authored in the manifest, compiled to a resource

**Result.** Mixer, sequencer, generated instruments with ADSR, priority-based voice
stealing, and capped PCM samples. A 16-row song is 160 bytes; a 120ms effect is 1,936 --
which is why music is sequenced and PCM is for effects only, enforced by a build error
rather than a guideline.

Not planned: spoken audio. One second of PCM is 16KB against ~70KB remaining, so it does
not fit at any quality.

Outstanding: device confirmation that music and effects run together without underruns
under real load. The case that would break it is a frame arriving seconds late while a
notification covers the app -- host tests cover the arithmetic, hardware has the last word.

## M4b — Content reuse: per-map palette remap — **DONE**

Reusing an atlas at a different palette saves **~12,000 bytes per zone** for the cost of a palette,
the largest content lever the framework can offer. A map carries an array remapping the atlas's
palette slots to actual ones -- four bytes for a four-palette atlas, one extra indirection in
`pnx_tilemap_draw`, and the variant palettes come from the `Ordered` machinery sprite variants
already use.

**Not** the four reserved per-cell palette bits in the u16 map entry: those answer a different
question -- mixing palettes *inside* one map -- and stay unbuilt until something needs it.

**Result.** The overworld's cave shares the base tileset at `dungeon_ice`: 44 bytes of remap table
against 5,632 for a second copy of the atlas.

## M4c — Landscape, and screen lock — **DONE** (pending device confirmation)

Rotating the display changes what the physical buttons are FOR, which makes whole genres available:

| Cluster sits | Under | Reads as |
|---|---|---|
| Right edge (portrait) | one thumb | menus, an RPG |
| **Top edge** | both index fingers | **shoulder triggers** -- a shooter |
| **Bottom edge** | both thumbs | **flippers** -- pinball |

Only possible because the device is played off the wrist in two hands
([`PLATFORM.md`](PLATFORM.md)): a wrist-mounted watch in landscape is unreadable, a handheld one is a
tiny gamepad whose button meaning the game chooses.

**Rotate the assets at build time, not the framebuffer at run time.** The pipeline emits atlases,
maps, sprites and glyphs already turned, so the engine's ordinary portrait blit produces a correct
landscape image. Rotating at render time would have meant a `step` field on `PnxRow`, a multiply per
pixel across four span writers, and -- the actual risk -- strided framebuffer writes whose cost was
never measured, with a 45,600-byte offscreen buffer as the fallback. Pre-rotation removes the
question instead of answering it, and works on a round display for free.

**Result.** `orientation` in `[project]`: `portrait` (`buttons_right`), `buttons_top`,
`buttons_bottom`, named for where the cluster ends up rather than which way something turned --
`landscape_left` only starts an argument about whether the device or the image rotated. A
`--orientation` override builds one manifest either way, so "the same content compiles to either
orientation" is tested rather than asserted.

- **Cost to the engine: one field, not the zero this promised.** Glyphs turn with everything else,
  and a turned glyph blits like any other rectangle -- but the next one is no longer to its right.
  So a font carries an advance axis and `pnx_text` walks it. The better shape anyway: it is what a
  vertical script needs, so Japanese set top-to-bottom is the same field with unrotated glyphs.
- **The touch transform was not needed and would have been wrong.** It assumed a landscape game
  thinks in rotated coordinates; pre-rotation means it draws and collides in the framebuffer's
  frame, which is the frame the device already reports touches in. Rotating them lands a tap where
  the pixel is not. `tests/test_input.c` paints a pixel and checks a touch at those coordinates
  names it.
- **Every blob is stamped**, songs and palettes included. Stamping only geometry would make
  "orientation-free" and "built portrait" the same byte, and a stale atlas indistinguishable from a
  legitimate sample. Checked in `load_blob_4`, the one door every blob comes through.
- **`pnx_input` addresses the cluster by position as the player reads it**, because the buttons do
  not rotate. `buttons_top` and `buttons_bottom` are not mirrors -- turned the other way, the
  physically-DOWN button falls under the left hand -- so one menu reads correctly in all three.
- **Screen lock**: BACK stops dismissing the app while still reaching the game as an event, and the
  backlight is held, because going dark mid-turn off the wrist reads as a crash.

**A bug fell out of the invariance check.** Maps came out 60 bytes larger in landscape, which
rotation cannot cause. `compute_tile_flags` walked the u16 tile plane and the u8 flag plane with one
`zip`, pairing each tile's low byte with one cell's flags and its high byte with the next cell's --
so flag defaults came from a scrambled tally that depended on cell order. Never wrong pixels, since
`finish_map` decodes properly and overrides every disagreement; just hundreds of overrides it did
not need. Fixed: the overworld's maps fell **3,267 → 2,388 bytes**, and ties now break toward the
lower flag value rather than toward whichever cell was seen first.

Outstanding: device confirmation. A host cannot tell you whether a landscape screen is readable in
the hand, whether the backlight hold survives a notification, or whether swallowing BACK feels like
protection or a trap.

## M4d — Multi-atlas maps and WorldTile streaming — **DONE** (pending device confirmation)

Three limits arrived together as content grew past the first example's scale: a map could draw
from exactly one atlas, a map was resident whole or not at all, and an atlas imported by mistake
could only be removed by hand-editing the manifest.

**A map draws from several atlases by partitioning its tile id space, not by spending a bit per
cell.** The cell's existing ten bits become a map-global id, and the map's atlas table says where
each atlas's slice begins. That costs the draw loop a walk of a table with at most eight entries
and leaves the four per-cell palette bits M4b reserved exactly where they were. 1,024 ids covers
four full atlases; past that the pipeline names the atlases and their tile counts and stops.

**A map is stored as a grid of WorldTiles** -- square blocks of map cells that are the unit of
residency. Deliberately *not* called a MegaTile: `metatile` already means the deduplicated 8x8
quadrant inside an atlas, and two names two letters apart in the same headers is a reading hazard
for as long as the code lives. A metatile is art; a WorldTile is a piece of the world.

- **One format, adaptive policy.** Every map is sliced, including small ones. When the whole grid
  fits the pool, all of it loads at map load and nothing is ever evicted -- a 32x24 map costs what
  it always did and the runtime has no second code path for "small".
- **The unit is what makes this legal**, given `pnx_assets.h`'s residency rule. A 128-byte tile is
  6.7 ms/frame at 725 tiles; a 516-byte WorldTile is ~45 µs paid at a boundary. Same fitted model,
  opposite conclusion, because the call cost amortises over 256 cells.
- **Atlases stream too**, pinned by the WorldTiles that name them and evicted when nothing resident
  depends on them. The pipeline checks that the union of atlases across any simultaneously-resident
  window fits the pool, and fails the build naming the corner of the map that does not -- a runtime
  thrash turned into a build error.
- **Pool slots are not a uniform stride.** With a slot per atlas nothing is ever evicted, so each
  slot is exactly its atlas's size: the example's ship at 29,596 B beside water at 8,116 costs
  37 KB rather than the 59 KB two slots of the larger would. Only a map that really streams its
  atlases pays for slots sized for the biggest.
- **Collision stopped depending on the atlas.** The flag table ships with the MAP, indexed by its
  own tile ids. A streamed atlas can be gone when the player walks toward a wall, so reading flags
  out of one was never going to hold. A few hundred bytes for collision that never depends on what
  the renderer happens to have loaded.
- **A scene no longer lists its map's tilesets.** The map owns them; a scene listing one loads a
  second resident copy, which is not a mismatch the runtime can see -- it just quietly costs twice
  the atlas. The pipeline refuses it and says which line to delete.

**Result.** Map format v8. The overworld's `deck` map draws from two atlases, and a second example
walks a 192x192 world that cannot fit in RAM. 562 host checks and 121 pipeline checks pass.

| Module | Before | After | Δ |
|---|---|---|---|
| pnx/assets | 3,549 | 5,369 | +1,820 |
| pnx/gfx | 2,192 | 2,552 | +360 |
| game | 1,340 | 1,510 | +170 |
| **app total** | **15,157** | **18,349 / 65,535 (28.0%)** | **+3,192** |

**~2.4 KB of engine to buy maps that do not have to fit in RAM.** Worth stating plainly rather than
buried: that is a fifth of what the framework cost before this, spent on a pool allocator, an
eviction policy and a second blob-loading destination. It pays for itself the first time a map
exceeds ~30 KB of cells, and costs a project whose maps all fit exactly that much and nothing back.

**A build-order bug fell out of verifying it.** An atlas named `ship` and a map named `ship` both
become `RESOURCE_ID_SHIP` in the SDK's generated header -- the manifest's own handles are prefixed
by kind and never collide, but `package.json` resource names are not. The app failed to compile with
a redefinition warning naming neither asset and pointing at a generated file. The pipeline refuses
the collision now and names both.

### `examples/worldtiles`, and the two bugs it found

A second example, because the first one's maps all fit and a streamer that never streams is not
tested by anything. A 192x192 field -- 73,728 bytes of cell plane, more than half `emery`'s app RAM
-- in three atlas bands, plus three interiors warped between. Full reasoning in its
[README](../examples/worldtiles/README.md).

**It carries its own control, which is the part worth copying.** `resident = true` on a map gives it
a slot per WorldTile: what a map cost before any of this existed. `field` and `plain` are the same
rows drawn from the same tilesets and differ by that one line, so SELECT swaps between them at the
same position and the only thing that changes is the number:

| | Resident | WorldTiles | Atlas slots |
|---|---|---|---|
| `field` — streamed | **23,678 B** | 16 of 144 | 2 of 3 |
| `plain` — held whole | **98,551 B** | 144 of 144 | 3 of 3 |

4.2x, for an identical picture -- and `emery` reports ~111 KB of heap, so the held-whole world fits
with about 12 KB to spare before the game allocates anything of its own. That is the argument in one
number, and it is now a build-time line on every map that streams rather than a claim in a document.

Two real bugs, both found by `tests/test_stream.c` walking the field end to end:

- **A warp deadlocked the atlas pool.** WorldTiles were evicted one at a time, on demand, and each
  still-resident one pinned the old atlases -- so a jump to a region drawn from a tileset nothing
  resident used could never free a slot, and the whole window failed to load. Walking never showed
  it, because the window moves a tile at a time and the old WorldTiles drain gradually. It took a
  jump to a distant part of the map, which is exactly what a warp is. Fixed by evicting everything
  outside the window *before* loading anything, which also took the streamer's worst backlog from
  one WorldTile to zero.
- **A map that fits was still being filled lazily.** "Resident" described the allocation and not the
  contents, so a small map -- every map in the overworld example -- still read flash as the player
  walked. `pnx_map_load` now fills any map whose pools can hold it before it returns, and the
  streaming calls early-out on it entirely. Small maps behave exactly as they did before WorldTiles,
  which is what they always should have.

**E15 falls out of the same work**: `remove_atlas` in the editor, refusing while a map, a scene or a
painted legend character still depends on the atlas and saying which. A legend entry nobody paints
with is a dangling reference rather than a dependency, the same view the pipeline takes.

### Device confirmation: the RAM claim holds, the flash model does not

`examples/worldtiles` ran on hardware. **The memory result is exactly as designed** -- 23,514 B
streamed against 97,351 B held whole, 26.8 fps held while walking, the streamer never once behind
(`missing` stayed 0 even at eight tiles per tick), and `Still allocated <0B>` at exit.

**The read cost is another matter.** Loads came in 50-280x over the predicted figure, and the
multiplier grows with how deep into the resource the read starts: the same 16-WorldTile window costs
46 ms near the map's origin and 305 ms two thirds of the way through it. That points at
`resource_load_byte_range` being **O(offset)** -- streaming from the start of the resource on every
call -- which the original 29 µs/call figure could not have caught, having been measured over a 16KB
resource where every offset is small. Numbers and the fit in [`MEASUREMENTS.md`](MEASUREMENTS.md).

In practice: walking is unaffected, crossing a WorldTile boundary drops one frame (47-63 ms against
8 ms of ordinary work, 24.2 fps while sprinting), and scene loads pay for it -- 305 ms for a warp
into the middle of the field, 2 s to hold the world whole.

### The layout answer: banks, and batched runs

Two changes, both aimed at the term that actually dominates.

**WorldTile payloads left the map's resource.** They live in **bank resources** of ~8KB, whose asset
ids run consecutively from a `first_bank_asset` in the map header, so bank *i* is that plus *i*. A
seek is now capped by the bank rather than by the map: 4KB instead of 74KB on the field, and the
map's own resource drops from 75,232 bytes to 1,000 -- it holds only what stays resident.

**Payloads are padded to the pool's slot stride**, which pays for itself twice. A WorldTile's home
becomes arithmetic -- bank `i >> bank_shift`, offset `(i & mask) * slot_bytes` -- so the per-tile
offset table leaves the resident preamble entirely. And a run of consecutive WorldTiles is then
contiguous at *both* ends, in the bank and in the pool, which is what makes **batching** a single
ranged read: a whole-map load is one read per bank instead of one per tile (18 against 144), and a
streaming window fetches each row-run in one call.

Banks are stamped like every other blob and checked once at map load rather than per read -- M4c's
rule holds, and a bank is geometry, so one left from a build in the other orientation would be a
scrambled world. Checking per read would have put a seek to offset 0 in front of every fetch, on the
one platform where the seek *is* the cost.

**A side effect worth having**: `field` and `plain` are the same world, so their banks are
byte-identical and the `.pbpack` deduplicates them. The example ships two 192x192 worlds for 103 KB
rather than 177 KB.

**Confirmed on hardware, and it settled the diagnosis.** Same watch, same content:

| | Before | After | |
|---|---|---|---|
| hold the 192x192 world | 1,984 ms | **74 ms** | 26.8x |
| warp into the middle of the field | 305 ms | **12 ms** | 25.4x |
| worst frame while walking | 47-63 ms | **8-12 ms** | 5.2x |
| frames dropped crossing a WorldTile | one, every time | **none** | -- |

Nothing about the byte count changed and the call count only fell 148 to 41, so neither explains a
27x drop. What changed is how far into a resource each read starts. Per read: **1.8 ms against
13.4 ms**. Walking now holds 26.8 fps -- the PT2 ceiling -- with the worst frame at 12 ms of a
37.33 ms budget, and the held-whole world reads nothing at all once loaded.

One observation to keep: the streamer's backlog reached 3 WorldTiles on a diagonal crossing a
WorldTile *corner*, which asks for tiles in two directions at once. No holes, no dropped frame, so
`PNX_MAP_STREAM_BUDGET` at 4 has room to spare -- and raising it is now cheap if a game wants it.

Still wanted: the flash probe that sweeps offset independently of length. The before/after is strong
but still read off session logs, and `MEASUREMENTS.md` says so.

### Footprint: the WorldTile size is chosen, not defaulted

`worldtile = "auto"` is now the default, and the pipeline picks by arithmetic -- the same bargain the
atlas `metatiles` key already offers, and for the same reason: the cheapest size depends on the
screen and the map, so a constant is right for one shape of content and quietly wrong for the rest.

Three terms, pulling different ways. The pool grows as the SQUARE of the size; the per-slot
descriptors follow it; the lookup array is one byte per WorldTile in the whole map and grows as the
size shrinks. **Which way the answer goes depends on whether the map streams.** A streaming map holds
a fixed window, so a bigger WorldTile means a bigger margin ring of world nobody can see -- it wants
*small*. A map held whole has no ring, every term scales with the count, and it wants *large*.

On the 192x192 field, 200x228 screen, 16px tiles:

| | Picked | Resident | Was (fixed 16) |
|---|---|---|---|
| streaming | **8** | 17,967 B | 22,491 B |
| held whole | **32** | 93,715 B | 94,255 B |

The WorldTile part of the streamed scene halved -- 8,720 B to 4,376 B -- and the scene fell 20%. What
it exposed is that **WorldTiles were never the expensive part**: the atlas pool is a flat 12,496 B of
the field's 18 KB, so the remaining footprint question is about tilesets, not tiling.

**A caution the example makes visible.** `field` and `plain` now tile differently, so their banks are
no longer byte-identical and the `.pbpack` stopped deduplicating them -- resources went 103 KB to
178 KB. That is an artifact of shipping the same world twice to compare it, not a property of
auto-sizing, but it is the kind of thing worth seeing before it surprises someone.

**And smaller WorldTiles exposed a bug batching had introduced.** `PNX_MAP_STREAM_BUDGET` counted
WorldTiles, but a run of consecutive ones is a single read -- so a batched fetch was charged four or
five times over and the streamer ran at a quarter of the I/O it was paying for. Invisible while
WorldTiles were large and few; on device at eight tiles a tick the backlog went from 3 to **12** the
moment the pipeline started choosing smaller ones, for no extra reads. The budget now counts reads.
`tests/test_stream.c` sprints 40 WorldTiles and asserts the backlog stays at zero, because nothing
about this shows up in a frame time or a byte count.

### Is it worth the space?

The engine cost is **3,076 bytes** -- `pnx/assets` grew 2,596 and `pnx/gfx` 480, measured from the
device size report. Against the RAM it buys, on a 16px grid and a 200x228 screen:

| Map | Streamed | Held whole | |
|---|---|---|---|
| 16x16 | 537 | 537 | fits its pool: held whole, streaming never runs |
| 32x24 | 1,836 | 1,836 | same |
| 48x48 | 2,888 | 4,833 | 1,945 B cheaper |
| 96x96 | 3,320 | 18,657 | 15,337 B cheaper |
| 192x192 | 4,376 | 74,628 | **70,252 B cheaper** |
| 255x255 | 4,824 | 132,672 | 127,848 B cheaper |

**Streaming never costs more RAM than holding whole, at any size.** Below the pool it is the same
number, because a map that fits its pool IS held whole and the streaming path never executes; above
it, streaming is strictly cheaper by a margin that grows quadratically. Streamed residency is
O(screen), held-whole is O(map area).

So the 3,076 bytes is the only cost, it is paid once by the framework rather than per map, and the
question is just whether a project has any map above roughly 48x48 cells. If it does, the saving
passes the code cost almost immediately and then runs away with it -- 23x over on the 192x192 field.
If every map fits one screen, the feature is inert rather than expensive: no pool, no eviction, no
reads, the same bytes as before it existed.

*(An earlier version of this table called 64x48 a "break-even" and said smaller maps "cost more than
they save". That was comparing per-map RAM against a one-time binary cost, which is not a comparison
that means anything -- and it read as though streaming penalised small maps, which it does not.)*

### While confirming that: the resource ceilings, read from the SDK

Three limits, two of them spelled 256, and the pipeline was only reporting one:

| | | |
|---|---|---|
| `MAX_RESOURCES_SIZE_APPSTORE` | 256 KB | bytes in the whole `.pbpack` -- a warning |
| `MAX_RESOURCES_SIZE` | **1 MB** | bytes -- the hard error |
| pbpack `table_size` | **256 entries** | resources, whatever they weigh |

The byte limits apply to the pack as a whole (`os.stat(resources_path).st_size` in the SDK's
`report_memory_usage.py`), not to any single resource. The entry count is the one that started
mattering with banking: a map is now one resource plus a bank per few WorldTiles, so a project of
large maps runs out of *entries* long before it runs out of bytes -- and exceeding it was a bare
traceback out of the SDK's packer. The budget report now prints both ceilings and the entry count,
and refuses a build that would overflow the table with a message naming the banks.

## M5 — Save

- Chunk packing into 256-byte units, minimum key count
- Incremental writer, one chunk per frame
- Save-on-blur via the focus hook
- Versioning with refusal to load a newer save

Done when: the game saves and restores across a cold start, and a save spread across
frames shows no visible hitch.

## M6 — App framework

- Scene stack with enter/exit/suspend
- Lifecycle: focus handling, clamped accumulator, throttle-aware pausing
- Optional on-screen diagnostics overlay, compiled out in release

Done when: the game handles a notification mid-play and resumes correctly.

## M7 — Publish

- Reference documentation generated from headers
- A second, structurally different example (the first game will have biased the API)
- Licence chosen; contribution guide; a template project
- Ship the game to the appstore

## M8 — Scripting layer (conditional)

Only if the FFI overhead measurement supports it.

- C API surfaced to JavaScript through Alloy's FFI
- JS for scene definitions, dialog trees, event handlers, item tables — composition
  only, never per-entity work
- A template Alloy project with the `.fxBuildFFI` wiring the SDK template omits

Gate: JS must be able to express a scene without exceeding ~10,000 operations/frame.

## M9 — The rest of the Pebble family

Reachable because the engine fits in 20% of one watch's budget. The matrix below is read
from the SDK itself (`sdk-core/pebble/common/tools/pebble_sdk_platform.py`), not recalled:

| Platform | Screen | Colour | App RAM | Speaker | Touch |
|---|---|---|---|---|---|
| `emery` | 200x228 rect | 64 | 128 KB | yes | yes |
| `gabbro` | **260x260 round** | 64 | 128 KB | **no** | yes |
| `flint` | 144x168 rect | **1-bit** | 64 KB | yes | no |
| `basalt` | 144x168 rect | 64 | 64 KB | no | no |
| `chalk` | 180x180 round | 64 | 64 KB | no | no |
| `diorite` | 144x168 rect | **1-bit** | 64 KB | no | no |
| `aplite` | 144x168 rect | 1-bit | **24 KB** | no | no |

`flint` and `gabbro` are the new hardware; the watch names are inferred from their
capabilities and want confirming. Two consequences stand out before any work starts:
**`gabbro` has no speaker tag**, so the flagship round watch cannot use M4 at all, and
**`gabbro` is 260x260** -- 1.48x emery's pixel count, against a fill rate only ever measured
on emery.

The work, in dependency order:

1. **Resolution independence.** 200x228 is currently hardcoded in the host target, and the
   camera clamp and tilemap viewport assume it. Drive all three from
   `PBL_DISPLAY_WIDTH`/`HEIGHT`.
2. **Round displays.** The platform seam already anticipated this: `PnxRow` carries
   `min_x`/`max_x` because that is what `gbitmap_get_data_row_info` returns per row on a
   round display. Verify the blitter honours them and that nothing assumes a rectangle.
3. **1-bit output**, for `flint`, `diorite` and `aplite`. The largest engine change: the
   4bpp indexed path needs a 1-bit sibling, thresholded or dithered *in the pipeline* and
   shipped as per-platform blobs. See the risk below -- most of this cost is not engine work.
4. **Audio only where there is a speaker** (`emery`, `flint`). `PNX_USE_AUDIO` already gates
   it; what is untested is a build with it off.
5. **Memory.** At ~13.4 KB static the engine fits every platform but `aplite`, where 24 KB
   total leaves ~11 KB for game code, statics *and* heap. Note that moving statics to the
   heap buys much less on the 64 KB platforms, because there both come from the same 64 KB
   rather than from 128 KB. **That 13.4 KB is an `emery` build with everything compiled in**
   -- see "The `.pbw` is seven apps in a zip" below, which is reason to re-measure it per
   platform before the `aplite` verdict is taken as settled.
6. **Pipeline.** Per-platform resources and per-platform atlas carves -- a 144x168 screen
   wants a different carve budget than 260x260 -- plus `targetPlatforms` in `package.json`
   and per-platform ceilings in the size report.
7. **Tests.** Parameterise the host target over the matrix instead of hardcoding one screen.

Done when: one game source builds and runs on `emery`, one round platform and one 1-bit
platform, with the size report inside each ceiling.

**Sequencing.** This has to land before M7 ships to the appstore, or the game ships for one
watch out of seven. But it wants M6 settled first, since the app framework is what a port
would otherwise churn.

**The real risk is content, not code.** 1-bit art is a separate art pass, not a downscale of
colour art, and three of seven platforms need it. Decide whether those platforms get their
own art or are simply out of scope before building the 1-bit path, not after.

### Author once: one game, one build, seven watches

The goal is not seven ports. It is one set of sprites, one set of game logic, one
`pebble build`, and a single `.pbw` that installs everywhere. What varies, and how each
difference is absorbed without the game knowing:

| Varies | Absorbed by | Art or logic change |
|---|---|---|
| Screen 144x168 to 260x260 | camera shows more or less world | none |
| Round corners | safe-area rect from per-row bounds | none |
| 64 colour vs 1-bit | **palette ink mask** (below) | none |
| Speaker or not | API stubs to a no-op | none |
| Touch or not | abstract actions, not buttons or taps | none |
| 24 KB app RAM (`aplite`) | per-platform compilation, maybe -- see below | open question |

**1-bit is a palette property, not an art asset.** This falls out of a decision already
made: art is 4bpp indexed, so shape and colour are already separate. A 1-bit platform needs
one extra field per palette -- a 16-bit mask saying which of the 16 indices are ink and which
are paper. Two bytes. Index 0 stays transparent as it already is, so nothing else changes:
tile and sprite blobs ship **byte-identical to all seven platforms**, and only the palette
resource and the span writer differ. The pipeline proposes a split by luminance and the
editor lets you flip individual entries against a live 1-bit preview, because thresholding
by luminance alone will vanish any figure that matches its ground.

Note what this avoids. Per-pixel thresholding needs separate 1-bit art; dithering survives
on backgrounds but shimmers on anything that moves and destroys readability at 16x16. Neither
is necessary when the decision can be made 16 entries at a time.

**Blocker to clear first: opt-outs must stub, not delete.** `PNX_USE_AUDIO=0` removes the
declarations, so game code calling `pnx_music_play` fails to compile rather than doing
nothing -- which forces `#if` into game logic and breaks author-once immediately. The same
fault already breaks `PNX_USE_DIAGNOSTICS=0` in two of the three examples. The rule the
framework needs: **a disabled subsystem keeps its entire API as inline no-ops returning a
safe value.** Cheap to do, and it is what makes `gabbro` having no speaker a non-event for
the game.

**Packaging.** One bundle via `targetPlatforms` plus the SDK's per-platform resources. The
pipeline has to budget per platform rather than once, since the appstore resource cap is
131,072 bytes on `aplite` against 262,144 everywhere else.

### `~` tagging: how "per-platform resources" actually works

The mechanism the paragraph above waves at has a name, and it is worth writing down before
the 1-bit work starts, because it decides how much of that work is naming and how much is
engine. Read from `sdk-core/pebble/common/waftools/resources/find_resource_filename.py`,
not recalled.

**A resource is declared once, untagged, and the SDK picks the file per platform.**
`package.json` names `tiles.bin`; the build globs for `tiles*.bin`, reads `~`-separated tags
off each candidate, and takes the one matching the target platform most closely. So
`tiles.bin` beside `tiles~bw.bin` ships 4bpp to the colour watches and 1-bit to the others,
from one manifest entry, with no `#if` anywhere and one `pebble build`.

The resolution rules, all of which bite:

- **Specificity is a count**, not a priority order: the number of the candidate's tags that
  appear in the platform's tag set. Most matches wins.
- **Any unknown tag disqualifies a candidate outright.** `tiles~bw.bin` is not merely
  outranked on `emery`, it is invisible -- which is what makes the untagged file a genuine
  fallback rather than a tie-breaker.
- **A tie is a hard build failure**, naming the ambiguous files. This is the trap: `bw` and
  `144w` both apply to `diorite`, so shipping `tiles~bw.bin` *and* `tiles~144w.bin` fails
  the build rather than picking one. Combine into `tiles~bw~144w.bin` or stay on one axis.
- **The generic name may not contain `~`** -- `bld.fatal`, immediately.
- No match at all falls back to the untagged file, so adding a tagged variant can never
  break a platform that has none.

Those four are not read off the source and assumed: the resolution was re-run over the tag
tables for all seven platforms. `tiles.bin` + `tiles~bw.bin` resolves to the tagged file on
`flint`, `diorite` and `aplite` and to the untagged one everywhere else, and adding
`tiles~144w.bin` to that pair fails the build on exactly the three `bw` platforms.

The media entry itself does not change: this project already declares resources as
`{"type": "raw", "name": "PALETTES", "file": "palettes.bin"}`, and that stays untagged
whatever variants sit beside it. `targetPlatforms` is `["emery"]` in every example today and
is the other half of the packaging change.

The tags each platform answers to, read from `pebble_platforms[...]["TAGS"]`:

| Platform | Colour | Shape | Size | Other |
|---|---|---|---|---|
| `emery` | `color` | `rect` | `200w` `228h` | `speaker` `touch` `mic` `strap` `strappower` `health` `compass` |
| `gabbro` | `color` | `round` | `260w` `260h` | `touch` `mic` `health` `compass` |
| `flint` | `bw` | `rect` | `144w` `168h` | `speaker` `mic` `health` `compass` |
| `basalt` | `color` | `rect` | `144w` `168h` | `mic` `strap` `strappower` `health` `compass` |
| `chalk` | `color` | `round` | `180w` `180h` | `mic` `strap` `strappower` `health` `compass` |
| `diorite` | `bw` | `rect` | `144w` `168h` | `mic` `strap` `health` |
| `aplite` | `bw` | `rect` | `144w` `168h` | `compass` |

Each platform also answers to its own name as a tag. **This table is the source for the
capability columns above**: `gabbro` carrying no `speaker` tag is where "the flagship round
watch cannot use M4" comes from, and `touch` appearing only on `emery` and `gabbro` is the
same fact for input.

**What it changes for this pipeline.** Three things, in the order they matter:

1. **It is the answer to per-platform atlas carves** (item 6 above). A 144x168 screen wanting
   a different carve than 260x260 is `world~144w.bin` beside `world~260w.bin`, not a second
   build.
2. **It makes a 1-bit pixel plane affordable.** The current plan ships blobs byte-identical
   to all seven platforms and absorbs 1-bit in the palette, which is right while resources
   fit. If `aplite`'s 131,072 cap ever binds, `~bw` is the escape hatch that does not cost
   author-once: measured on `examples/overworld`, packing pixels 4bpp -> 1bpp saves ~54 KB
   of its 78 KB, against ~300 bytes for dropping palette data.
3. **The pipeline has to emit the tags itself.** Tagging is resolved by the SDK's waf over
   files on disk, so `pnx_assets` would write `tiles~bw.bin` and keep the `package.json`
   media entry untagged. That is a real change to blob naming and to the size report, which
   would need to total per platform rather than once -- not merely a file-naming convention.

**Unverified.** Whether `flint` and `gabbro` accept these tags on real hardware, and whether
the SDK's own `bw` output path expects 1-bit resources in a particular format. Both are read
from SDK 4.17 on this machine and neither has been run on a watch.

### The `.pbw` is seven apps in a zip, not one app that adapts

This is the other half of `~` tagging and the more consequential half, because it applies to
the **engine** rather than to the content. `targetPlatforms` does not select which platforms
one binary claims to support -- it selects how many times the C is compiled.

Read from `examples/audiotest/build/audiotest.pbw`, which is a zip containing:

```
appinfo.json
emery/pebble-app.bin          <- compiled for emery
emery/app_resources.pbpack    <- resources resolved for emery
emery/manifest.json
```

Add `flint` to `targetPlatforms` and there is a second directory beside it with its own
binary and its own pack. `~` tags choose what goes in the pack; **the preprocessor chooses
what goes in the binary**, and the SDK hands each compile its own defines:

| Platform | Defines beyond the platform name |
|---|---|
| `emery` | `PBL_COLOR` `PBL_RECT` `PBL_TOUCH` `PBL_SPEAKER` `PBL_MICROPHONE` `PBL_SMARTSTRAP` `PBL_SMARTSTRAP_POWER` `PBL_HEALTH` `PBL_COMPASS` `PBL_RGB_BACKLIGHT` `PBL_DISPLAY_WIDTH=200` `PBL_DISPLAY_HEIGHT=228` |
| `gabbro` | `PBL_COLOR` `PBL_ROUND` `PBL_TOUCH` `PBL_MICROPHONE` `PBL_HEALTH` `PBL_COMPASS` `PBL_DISPLAY_WIDTH=260` `PBL_DISPLAY_HEIGHT=260` |
| `flint` | `PBL_BW` `PBL_RECT` `PBL_SPEAKER` `PBL_MICROPHONE` `PBL_HEALTH` `PBL_COMPASS` `PBL_DISPLAY_WIDTH=144` `PBL_DISPLAY_HEIGHT=168` |
| `basalt` | `PBL_COLOR` `PBL_RECT` `PBL_MICROPHONE` `PBL_SMARTSTRAP` `PBL_SMARTSTRAP_POWER` `PBL_HEALTH` `PBL_COMPASS` `PBL_SDK_FROZEN` `144x168` |
| `chalk` | `PBL_COLOR` `PBL_ROUND` `PBL_MICROPHONE` `PBL_SMARTSTRAP` `PBL_SMARTSTRAP_POWER` `PBL_HEALTH` `PBL_COMPASS` `PBL_SDK_FROZEN` `180x180` |
| `diorite` | `PBL_BW` `PBL_RECT` `PBL_MICROPHONE` `PBL_SMARTSTRAP` `PBL_HEALTH` `PBL_SDK_FROZEN` `144x168` |
| `aplite` | `PBL_BW` `PBL_RECT` `PBL_COMPASS` `PBL_SDK_FROZEN` `144x168` |

**What this changes, in the order it matters:**

1. **The 1-bit path and the 4bpp path never coexist.** Item 3 above calls 1-bit "the largest
   engine change" and assumes a sibling to the indexed path. It is still a second path, but
   it is `#if PBL_BW` and the colour watches never carry a byte of it. The cost is source
   complexity, not size on any device.
2. **`aplite` being out of scope deserves re-measuring before it is believed.** The ~13.4 KB
   static figure was measured on `emery` with audio, touch, colour blitting and diagnostics
   all compiled in. On `aplite` every one of those is absent from the defines. The 24 KB
   conclusion below rests on a number that does not describe the binary `aplite` would
   actually get, and nobody has built one.
3. **Capability gating stops being the game's problem.** `PNX_USE_AUDIO` is a hand-set
   opt-out today; `PBL_SPEAKER` is present on exactly `emery` and `flint`, so the engine can
   gate itself and be right by construction. Same for `PBL_TOUCH` and the input layer.
4. **`PBL_DISPLAY_WIDTH`/`HEIGHT` are compile-time constants, not runtime values.** Item 1's
   resolution independence can keep statically sized buffers -- a screen-sized array stays a
   fixed-size array, sized differently per binary, with no allocation added.

**And it raises the stakes on the blocker below.** "Opt-outs must stub, not delete" is
currently written as a tidiness problem. It is not: once the engine really does compile
subsystems out per platform, the stub rule is the only thing keeping one game source
compiling for seven targets. It should land before any `#if PBL_*` goes into the engine, not
after.

**Unverified, and worth an experiment before M9 is planned in detail.** Nothing here has
been compiled for a second platform -- every example is `targetPlatforms: ["emery"]`. The
cheap test is to add `diorite` or `flint`, build, and read the size report the wscript
already prints per platform (`for platform in ctx.env.TARGET_PLATFORMS`). That answers the
`aplite` question with a number instead of an estimate, and it is a one-line manifest change
plus whatever the compile turns up.

**What author-once cannot absorb.** `aplite`'s 24 KB was called a hard exclusion rather than
a tuning problem. **That is now an open question, not a conclusion** -- it was reasoned from
an `emery` build carrying audio, touch and colour blitting, none of which `aplite` compiles
at all. Settle it with a build, not an argument. What does not change is screen size: it is
only free for a game that can show more or less world -- a fixed play area would have to
letterbox or scale, and integer scaling is the only kind that stays crisp. An RPG absorbs
this; a puzzle grid would not.

Done when: one game source with no `#if` in it, one build, one `.pbw`, running on `emery`,
`gabbro` and `flint` -- colour rect, colour round, and 1-bit. Note "no `#if` in it" is a
constraint on the **game**, not the engine: per-platform compilation makes `#if PBL_*` the
right tool inside `src/pnx`, and the point of the stub rule is that none of it reaches game
source.

---

## Editor track (parallel)

A visual editor for levels, assets, testing and packaging — architecture and reasoning in
[`EDITOR.md`](EDITOR.md). It runs **alongside** the engine milestones rather than after
them, because it is a GUI over the asset manifest and depends on M2's schema rather than on
any runtime code. Staged so each piece is independently useful:

| | | Depends on | Land it |
|---|---|---|---|
| **E1** | Inspector: tilesets with ids, rendered maps, budget, validation errors | M2 | alongside M2 |
| **E2** | Map editor: paint tiles, place entities, wire warps, live reachability | E1 | once real maps exist |
| **E3** | Emulator panel: noVNC + build/install/run/logs | E1 | after M3 |
| **E4** | Asset import: sheet slicing, colour key, quantisation and dedup preview | E1 | when the pain justifies it |
| **E5** | Package button: validate, build, enforce budget, emit `.pbw` | E1 | with E3 |
| **E6** | Music editor: tracker view over the sequencer model | M4 | last |
| **E7** | Font import: drop a TTF, rasterise glyphs at a chosen pixel size, preview legibility, emit an atlas plus width table. **Multiple fonts** -- a small one for the HUD, a larger one for dialogue -- are the same system with a glyph map each, so plan for N from the start rather than retrofitting | E4 | **DONE** |
| **E8** | One-file editor executable with a native window | E1 | **DONE** |
| **E9** | Settings tab: detect the Pebble SDK, show its licence, install it on request | E8 | **DONE** |
| **E10** | Open any project folder; `.pknproj`; the engine ships in the editor and stages into a build | E8 | **DONE** |
| **E11** | Release CI: tested builds and installers for Linux, Windows, macOS x86_64 and arm64 | E10 | **DONE** |
| **E12** | Sprite editor painting in ARGB2222; code editor over the project tree (stubs) | E10 | **DONE** |
| **E13** | IDE shell: activity rail, contextual toolbar, shared output panel, status bar | E12 | **DONE** |
| **E14** | Live budget while editing; per-tile import selection; opt-in engine editing; C highlighting and symbol checking | E13 | **DONE** |
| **E15** | Remove an atlas, refusing while anything still draws with it; multi-atlas maps in the preview and the tile picker | E14, M4d | **DONE** |

**E7 exists because a font is the one asset a person cannot author by hand at this scale.** At
6x12 most typefaces are illegible -- hinting dominates at small sizes -- so the editor has to
*show* the rasterisation at target size before it is committed rather than just accept a file.

**Result.** Text is an ordinary asset: `[[font]]` in the manifest, a `PF` blob, and a glyph blitter
in `gfx` that draws during the frame like a sprite. The old path went through the SDK at **~4.3 ms
a draw, 12% of the frame**, and could only run *after* the framebuffer was released, so nothing
could ever be drawn over text. The SDK hook stays as an escape hatch until the M6/M7 cleanup.

- **Depth is per font.** `depth = 1` is crisp; `depth = 2` is antialiased over three coverage
  levels, blended against the destination through a 32-byte ARGB2222 table. The HUD stays sharp
  and dialogue can be smooth without a format break. The blend LUT M3 anticipated lands here,
  because this is the first thing that needed it.
- **Glyphs are trimmed to their inked box**, not padded to a uniform cell. A wash at 12px, roughly
  half at 24px, because most glyphs are nowhere near the full line box.
- **`charset = "auto"` derives the glyph set from the dialog pages** the pipeline already reads,
  with `extra` for runtime strings no conversation contains. The example's HUD face carries 40
  glyphs, not 95.
- **A licence is required and its absence fails the build**, with the build printing what it
  redistributes and under what terms.
- **The editor is where a font is judged.** Threshold, size and depth re-rasterise live against a
  200x228 canvas showing real map tiles, a real dialogue box and real dialog pages -- and it counts
  glyphs that rasterised blank, which is how an imported font is usually quietly broken. At
  threshold 230 the example face loses three glyphs and still builds clean; only looking at it
  catches that.

Costs on the overworld example: HUD 12px 1bpp **757 B / 40 glyphs**, dialogue 16px 2bpp
**902 B / 27 glyphs**. Engine growth is 662 B in `gfx` and 993 B in `assets`; the app is
**14,784 of 65,535 bytes (22.6%)**.

**Not yet measured on hardware.** The glyph blit should be far cheaper than 4.3 ms -- it writes
one byte per set pixel with no palette read -- but that is reasoning, not a number, and the whole
justification for this work is a measurement. It wants timing on a watch before the claim is made.

**E8** falls out of the same goal: the editor is one ~20 MB executable with a native window, so
using it needs no Python, no Pillow and no terminal. It drives the OS's own webview rather than
bundling Chromium.

**E11 ships that executable in the form each platform expects to install from** — an Inno Setup
wizard on Windows (per-user, so no UAC prompt an unsigned installer has no business asking for), a
`.dmg` containing the `.app` beside an `/Applications` symlink, and a `.tar.gz` on Linux with a
`.desktop` entry and an `install.sh` that writes two files under `$HOME`. First public build:
[v0.1.0-beta.1](https://github.com/X64Tyko/pebblnyx/releases/tag/v0.1.0-beta.1). Runner labels are
pinned to the oldest GA image, and macOS taught us why that needs watching: a retired label is
never scheduled, so the job does not fail, it queues until GitHub's 24-hour timeout while the
release job waits on it forever.

**E9 answers the rest of it, and the licence decided the design.** The Pebble Developer License
grants a **non-transferable, non-sublicensable** licence *to the user* and §5(f) prohibits
distributing the SDK -- so shipping it inside the editor was never available, however convenient.
What is available: show the terms, take a real acceptance, and drive Pebble's own `pebble sdk
install`, so the download goes from Pebble to the user and the editor never holds a copy. One
useful discovery -- **the SDK carries its own ARM toolchain**, so it is a single ~767MB install
rather than two. Full reasoning and the quoted clauses in [`EDITOR.md`](EDITOR.md).

**Stack is settled by a measurement, not a preference:** the emulator is QEMU with a VNC
display and `pebble-tool` already ships `websockify`, so a browser UI embeds the watch
screen for free. Any other stack means writing a VNC client.

**Two sequencing risks**, recorded because they are easy to walk into:

- The editor must not delay the game. Adoption follows a shipped game, not tooling.
  Build what speeds *our* content work now; defer what serves hypothetical developers.
- E2 encodes assumptions about what a map is. If the game later needs layered maps or
  scripted events, an editor built too early gets rewritten. E1 makes no such
  commitment, which is why it is safe to build first.

---

## Upstream contributions worth making regardless

Found while probing; each currently wastes other people's time:

1. **`pebble new-project --alloy` omits `.fxBuildFFI`** in `mdbl.c`, so every declared
   FFI native is silently unreachable — no build error, `Natives` simply undefined.
2. Omitting the manifest `ffi` block also fails silently.
3. The build reports resources against the 256KB appstore cap rather than the 1MB hard
   cap, making the real ceiling look 4x closer.
4. Exceeding the 64KB virtual-size cap fails with a bare
   `struct.error: 'H' format requires 0 <= number <= 65535` and no indication of cause.
5. `MAX_APP_BINARY_SIZE` is **131,072** for `emery` and `gabbro`, but `inject_metadata.py`
   still packs `virtual_size` as `"<H"` -- so those platforms advertise a binary cap their
   own metadata format cannot express. Either the field needs widening or the cap is wrong;
   as it stands the second 64KB is unreachable. Same root cause as (4).