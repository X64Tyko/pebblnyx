# Roadmap

Current state: **M0-M4d complete**, editor through E17 (E1-E5 and E7-E17 done; E3 was
superseded, E6 not started). [v0.1.0-beta.1](https://github.com/X64Tyko/pebblnyx/releases/tag/v0.1.0-beta.1)
shipped installers for Linux, Windows and both macOS architectures at editor E15; E16 and
E17 have landed since. **M5 (save) and
M6 (app framework) are built and host-tested**, landed together because M6's lifecycle is
what save-on-blur actually hangs off of; both still want the same device confirmation
M4/M4c/M4d are already waiting on. M7 (publish) next. `platform`, `core`, `assets`, `gfx`,
`audio` and `input` run on device; `save` and `app` are built and host-tested but not yet
run on one. 724 host checks plus 361 pipeline-validation checks cover all of it. Audio,
landscape, map streaming, save and the app-state stack still want hardware confirmation. No
emulator is possible for PT2 — see [`EDITOR.md`](EDITOR.md).

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

### Device confirmation found a second problem, and it's fixed

The RAM design landed exactly as measured. Flash reads did not: loads came in 50-280x over
the predicted figure, because `resource_load_byte_range` turned out to be **O(offset)**, not
O(length) -- invisible in the original per-call estimate, which was measured over a resource
small enough that every offset was small. Fixed by moving WorldTile payloads into ~8KB bank
resources (capping the seek) and padding them to the pool's slot stride, which turns a run of
consecutive WorldTiles into one batched read instead of one per tile. Confirmed on hardware
at **26.8x** faster to hold the 192x192 world and **5.2x** on the worst per-frame read.
`worldtile = "auto"` now picks the tiling granularity by the same cost model that motivated
banking, and the pipeline enforces the two resource-count ceilings banking made newly
reachable. Full numbers, both diagnoses, and the footprint table are in
[`MEASUREMENTS.md`](MEASUREMENTS.md#worldtile-streaming-m4d).

## M4e — Subtractive synth — **SPIKE MEASURED, not landed**

Three detuned oscillators an instrument, a resonant filter with its own envelope, an LFO
routed to pitch/volume/duty/cutoff, a pitch envelope, and sends into one global reverb and
one chorus. Four instrument slots, swappable mid-song -- a note finishes on the instrument
it started with.

Built as a spike and measured on device rather than reasoned about, because audio was the
one category with no measured cost and the most room to blow the budget. Numbers, the two
wrong turns, and the headroom finding are in
[`MEASUREMENTS.md`](MEASUREMENTS.md#synth-cpu-what-a-subtractive-voice-actually-costs-spike).

**It fits: 17.2% of a core, 6.4 ms per 37 ms frame against ~35 ms free.** 5,522 B of heap
(5,266 B of it effect delay lines) and 3,615 B of app.

What the measurement decided, which reasoning had got wrong in both directions:

- **Resonance and the pitch envelope are free** -- 78 ns and 13 ns. Drums as a pitch sweep
  rather than PCM, and a filter that actually sounds like one, cost nothing.
- **A bare voice is the largest single item at 38%**, not the effects. One oscillator and
  one envelope is 204 cycles; the render loop is sample-major and cycles four voices'
  state through registers every sample. Voice-major block processing is the untried fix.
- **Headroom binds before CPU.** Four detuned voices plus wet peaked at 163 against the
  mixer's 127 and were audibly rough. This is the argument for a 16-bit output format.

**The sequencer drives it.** A song may carry a table of packed 48-byte instruments,
appended after the patterns and detected by trailing payload -- the music header's four
bytes were all spoken for. Additive in both directions: a song built before this exists
still loads, and a song carrying the table still plays through the plain mixer in a build
with `PNX_USE_SYNTH=0`. The record width is stored, so a song from a newer pipeline is
refused rather than misread.

A channel maps 1:1 onto a synth slot -- both are four, and both are the same musical idea.
The instrument is decoded at NOTE-ON rather than at load, which costs 48 bytes of work a
few times a second instead of a resident decoded table, and makes swapping an instrument
into a slot mid-song fall out for free: the slot is written just before the note starts and
`pnx_synth_note_on` copies it, so a note already sounding keeps the instrument it began
with.

Synth voices sum into the **same accumulator** as sampled ones, before the shared clamp and
conversion -- one output stage, so a synth note and a PCM blip cannot land at different
levels. Getting that wrong once produced a synth with no caller: the linker dropped the
render loop and the result was silence with no error anywhere.

Still to land: `PNX_USE_SYNTH` is 0 by default until the CPU is confirmed on device after
the voice-major restructure, and the editor (E6) has no UI for any of it.

Done when: the packed instrument round-trips through the editor, and the device confirms
the render cost after the restructure.

## M5 — Save — **DONE** (pending device confirmation)

**The chunk size was never a choice.** `PNX_PERSIST_KEY_BYTES` (256) is the platform's own
per-key cap, so a save larger than one key had no option but to spend more calls -- and a
persist write costs ~7ms per call almost regardless of size (`docs/MEASUREMENTS.md`), so
the thing worth minimising is calls, not bytes. The header rides inside chunk 0 (8 bytes:
magic, version, chunk count, payload length, checksum) rather than spending a key of its
own, which is why `PNX_SAVE_CHUNKS_PER_SLOT` (16) times a key lands almost exactly on
MEASUREMENTS.md's *"realistic 4KB save, 16 keys"* case -- that measurement sized this
constant, not the other way round.

**Two slots, one writer.** `PNX_SAVE_SLOT_RESUME` and `PNX_SAVE_SLOT_CHECKPOINT` each get a
fixed range of keys so they can never collide, but only one save may be in flight at a
time -- a second `pnx_save_begin` abandons the first rather than interleaving their chunks.
That is a deliberate simplification: a game does not save two things at once, and modelling
that it could would cost a queue nobody would ever fill.

**Two ways to drive it, because the two situations that call for a write have opposite
budgets.** `pnx_save_begin` + `pnx_save_step` once per frame spreads the ~7ms/chunk cost
across rendered frames, for the interactive case (a player choosing SAVE) where a stall is
visible. `pnx_save_write` (begin + a blocking flush) is for save-on-blur, where the ~297ms
`will_focus` warning against a ~106ms 4KB save (MEASUREMENTS.md) means there is room to just
finish it, and no frame left to protect anyway -- the display is about to stop accepting
them.

**Versioning refuses a save it does not understand**, not one it merely predates: a stored
version newer than the caller's is rejected outright; older or equal loads as-is, with no
migration built in -- that is the game's problem to solve if it ever needs to.

**resonant's own save is deliberately thin.** `SaveData` (game.h) is a beat, Van's stats, a
tile and a facing, and `game_save_on_blur` (main.c) only ever builds one while `g->beat` is
`BEAT_WALK` or `BEAT_DONE` with no dialog open -- every other beat is a dialog page or a
battle turn, states this format was never asked to describe. `enter_sector4` took a starting
tile so `game_continue` re-enters through the exact setup a fresh game does rather than a
second copy of it. `PAUSE_SAVE` (the interactive `CHECKPOINT` write) stays dimmed in
`menu.c` -- not because save does not exist any more, but because the cold open has no
author-placed save point yet ("NO POINT"); that gate arrives with the content that needs it.

**Result:** `tests/test_save.c` proves the chunking, the checksum catching a torn write, and
version refusal, all at the framework level with synthetic payloads sized to actually span
several chunks. `tools/host_harness.c`'s "save" section proves the game-level claim
end-to-end: save-on-blur mid-walk, into a **different** `Game` struct simulating a cold
start, CONTINUE landing on the exact saved tile with the exact saved stats.

Outstanding: device confirmation of the actual persist timing this was sized against, and
`PAUSE_SAVE`'s checkpoint path once resonant has a beat that places one.

Done when: the game saves and restores across a cold start — **yes**, proven on host. A
save spread across frames shows no visible hitch — proven at the framework level (a
600-byte host-test payload spans multiple chunks, one `pnx_save_step` per simulated frame);
resonant's own save is small enough to finish inside `pnx_save_begin`'s first chunk, so it
does not itself exercise the multi-frame spread, only the framework does.

## M6 — App framework — **DONE** (pending device confirmation)

**Called a STATE, not a scene.** `assets/pnx_assets.h` already owns `pnx_scene_*` for a
loaded content chunk; reusing the name for "a mode of play" would have made every sentence
about either one ambiguous. `pnx_app.c` is a fixed-depth stack (`PNX_APP_MAX_DEPTH`, 4) of
states, each an ops table of eight optional hooks: `enter`/`exit` for push/pop, `suspend`/
`resume`, and `input`/`tick`/`frame`/`draw` for the frame loop.

**The fixed-timestep loop moved from every game's `main.c` into the framework.** The clamped
accumulator (`PNX_MAX_CATCHUP_TICKS`) is unchanged math, just owned in one place now instead
of hand-rolled per project. What is new is **throttle-aware pausing**: between `FOCUS_LOST`
and `FOCUS_GAINED` the accumulator is not fed at all, so a covered app stops ticking outright
rather than accruing seconds to spend in a burst on return -- stricter than the old
clamp-only approach, which still let a few catch-up ticks through.

**`suspend`/`resume` fire for two different reasons on purpose.** A state is suspended when
something is pushed on top of it (the pause menu opening over the field) or when the OS
takes focus from the whole app. Most hooks do not need to tell the two apart; one that does
calls `pnx_app_covered()`. This is what lets resonant's `game_save_on_blur` be the exact same
function pointer on both `resonant_base_state` and menu.c's `pause_state` -- a notification
during the pause menu saves exactly as one in the field would, and neither ever saves just
because the *other* one opened.

**`frame` is the one hook that runs even while covered**, for the one thing found that must
not stop: resonant's sequencer, which advances against the wall clock specifically so a
notification does not slow the music down with it (`audio.c`).

**resonant's integration is honest about its own shape.** `main.c`'s old hand-rolled
accumulator and mode-switch dispatch are gone, replaced by `pnx_app_frame`; boot pushes one
state, `resonant_base_state`, wrapping the title/story/options mode-switch exactly as it
already existed. The pause/characters overlay in `menu.c` is the one place that became a
*genuinely* pushed second state -- "nothing simulates behind the menu" (`docs/Menu.md`) used
to be a rule every mode-handler had to remember, and is now just what NOT being on top of
the stack means. Depth stops at 2, not 5: OPTIONS is reachable from both the title and the
pause menu, and turning it into a third stack entry either loses the pause-menu cursor
position on the way back (`pnx_app_push` re-enters through `enter`, which resets it) or
needs real surgery on `title.c`/`menu.c` input -- not worth the regression risk on a working
cold open this pass. Left as follow-up, not silently dropped.

**The optional diagnostics overlay was already done and undocumented.** `PNX_USE_DIAGNOSTICS`
compiles the frame-stats machinery out entirely; `Options.show_fps` and `ui_fps_draw`
(gated behind it, drawn through the glyph blitter like everything else) are the on-screen
half. Nothing new was needed here — this milestone's bullet was unchecked only because
nobody had written down that the pieces already satisfied it.

**Result:** `tests/test_app.c` asserts exact enter/exit/suspend/resume ordering across two
synthetic states, push-overflow refusal (and that the state suspended for a refused push is
correctly resumed rather than left paused), accumulator reset on every transition, ticks
stopping at zero on `FOCUS_LOST` regardless of how much elapsed time that frame carries, and
that non-lifecycle events are forwarded intact while focus events never are. `tools/
host_harness.c` exercises push/pop for real through the pause menu and confirms both halves
of the "Done when" below.

Outstanding: device confirmation -- the covered-means-zero-ticks behaviour is new and has
not been watched against a real notification -- and the deeper stack integration noted
above, if a later milestone decides it is worth the risk.

Also landed in the same pass, because it touches the same frame loop: the render period on
device is now locked to a fixed 40ms (25fps) rather than requested as fast as the display
will allow. See `docs/MEASUREMENTS.md`'s Frame pacing section for why that trades a small,
never-realised ceiling for slack that actually protects against a frame running long.

Done when: the game handles a notification mid-play and resumes correctly — proven on host
via the same `FOCUS_LOST`/`FOCUS_GAINED` sequence M5's harness scenario uses: ticks stop,
save-on-blur fires, and `FOCUS_GAINED` resumes ticking from a zeroed accumulator rather than
a caught-up one. Device confirmation is the one thing a host cannot give this.

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
platform, with the size report inside each ceiling. Broader done-when, once the packaging
mechanics in `PORTING.md` are actually used: one game source with no `#if` in it, one
build, one `.pbw`, running on `emery`, `gabbro` and `flint` -- colour rect, colour round,
and 1-bit; per-platform compilation makes `#if PBL_*` the right tool inside `src/pnx`
itself, and the point of the stub rule below is that none of it reaches game source.

**Sequencing.** This has to land before M7 ships to the appstore, or the game ships for one
watch out of seven. But it wants M6 settled first, since the app framework is what a port
would otherwise churn. (M6 is now done -- see above.)

**The real risk is content, not code.** 1-bit art is a separate art pass, not a downscale of
colour art, and three of seven platforms need it. Decide whether those platforms get their
own art or are simply out of scope before building the 1-bit path, not after.

**Before writing any of this code, read [`PORTING.md`](PORTING.md).** It works out how the
SDK's own packaging (`~`-tagged per-platform resources, and the fact that `targetPlatforms`
compiles the C once per platform rather than once total) has to shape the pipeline and the
`PNX_USE_*` opt-out rule, gathered before the work starts specifically so it does not have
to be re-derived during it. Headline findings: 1-bit is a **palette** property, not a
second art pass, so tile/sprite blobs stay byte-identical across all seven platforms; and
`aplite`'s 24KB verdict was reasoned from an `emery` build carrying subsystems `aplite`
would never compile in, so it is an open question a real build must settle, not a
conclusion already reached.

---

## Editor track (parallel)

A visual editor for levels, assets, testing and packaging — architecture and reasoning in
[`EDITOR.md`](EDITOR.md). It runs **alongside** the engine milestones rather than after
them, because it is a GUI over the asset manifest and depends on M2's schema rather than on
any runtime code. Staged so each piece is independently useful:

| | | Depends on | Land it |
|---|---|---|---|
| **E1** | Inspector: tilesets with ids, rendered maps, budget, validation errors | M2 | **DONE** |
| **E2** | Map editor: paint tiles, place entities, wire warps, live reachability | E1 | **DONE** |
| **E3** | Emulator panel: noVNC + build/install/run/logs | E1 | **SUPERSEDED** -- no QEMU target exists for this platform, see `EDITOR.md` |
| **E4** | Asset import: sheet slicing, colour key, quantisation and dedup preview | E1 | **DONE** |
| **E5** | Package button: validate, build, enforce budget, emit `.pbw` | E1 | **DONE** |
| **E6** | Music editor: tracker view over the sequencer model | M4 | not started |
| **E7** | Font import: drop a TTF, rasterise glyphs at a chosen pixel size, preview legibility, emit an atlas plus width table. **Multiple fonts** -- a small one for the HUD, a larger one for dialogue -- are the same system with a glyph map each, so plan for N from the start rather than retrofitting | E4 | **DONE** |
| **E8** | One-file editor executable with a native window | E1 | **DONE** |
| **E9** | Settings tab: detect the Pebble SDK, show its licence, install it on request | E8 | **DONE** |
| **E10** | Open any project folder; `.pknproj`; the engine ships in the editor and stages into a build | E8 | **DONE** |
| **E11** | Release CI: tested builds and installers for Linux, Windows, macOS x86_64 and arm64 | E10 | **DONE** |
| **E12** | Sprite editor painting in ARGB2222; code editor over the project tree (stubs) | E10 | **DONE** |
| **E13** | IDE shell: activity rail, contextual toolbar, shared output panel, status bar | E12 | **DONE** |
| **E14** | Live budget while editing; per-tile import selection; opt-in engine editing; C highlighting and symbol checking | E13 | **DONE** |
| **E15** | Remove an atlas, refusing while anything still draws with it; multi-atlas maps in the preview and the tile picker | E14, M4d | **DONE** |
| **E16** | Everything the manifest could express and the editor could not: scenes, dialog, sprite declarations, map delete/rename, map palette and streaming keys, atlas metatiles and variants, font removal, project settings | E14 | **DONE** |
| **E17** | Per-map legend, then maps in their own `.pnxmap` files; sprite frames picked off a sheet, and single-frame editing in place | E16 | **DONE** |

**E16 was an audit, not a feature.** The editor could paint maps and import tilesets, and
everything else the manifest holds was reachable only by hand-editing TOML — scenes above
all, which are the framework's *only* load point, so a map could be drawn, painted and
built and still be unreachable from the game. Two bugs fell out of that audit and both had
been committed: the `overworld` example did not build on `main` (a legend character pinned
to an atlas one of its maps does not use), and **every map created in the editor produced
an unbuildable manifest**, because the generated scene restated the atlases the map already
streams — which M4d had made an error.

**E17 removed the format's own ceiling.** One character per cell capped a map at ~90
distinct tiles while the compiled cell already carried a 10-bit index the runtime resolves
against 1024. Making the legend per-map bought a factor of the project's map count; moving
cells into a `.pnxmap` removed the ceiling. `rows` is not deprecated — `overworld` stays in
it as the readable example — and both formats go through the same checks, verified by
compiling one map each way and comparing the bytes. Migrating `worldtiles` took its
manifest from 81 KB to 7 KB.

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