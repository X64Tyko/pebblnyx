# Roadmap

Current state: **M0-M3 complete**, editor E1 usable (maps, transitions, asset
import, multi-atlas). No emulator is possible for PT2 -- see EDITOR.md. M4 (audio) next. `platform`, `core`, `assets` and
`gfx` exist, run on device, and are covered by 182 host checks. The editor track (E1)
is next.

The ordering principle: a framework with no consumer gets the abstractions wrong in
ways nobody discovers until someone tries to use it. PGE (the 2014 Pebble game engine)
failed partly this way — its animation path reallocated a bitmap from a resource on
every frame change, and its shipped example divided by zero on startup. Nobody noticed
because nobody built on it.

So **a real game is built alongside the framework from M3 onward**, as its first
consumer. Every API gets exercised before it is published.

---

## M0 — Latency spike — **DONE**

Driven by [`GAME.md`](GAME.md): Legend of Dragoon's Addition system needs timed input
during an attack, and that is the one mechanic this hardware is worst at. Frames are
37.33 ms and cannot be shortened, and end-to-end input latency is **≥37 ms by
construction, likely ~74 ms** but never actually measured.

- Measure real touch-to-pixel and button-to-pixel latency
- Test whether a 2–3 frame (74–111 ms) timing window is playable
- Compare tap-timed against held-timed input
- Try a leading visual cue, so the player anticipates rather than reacts

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
`Still allocated <0B>` — the platform layer's teardown leaks nothing. Critically, the
startup log written from `main()` *survived*, which is the deferred ring doing the one
job it exists for.

The empty example costs **6,392 bytes of 65,535 (9.8%)**; with diagnostics
off, **2,468 (3.8%)**, which verifies the zero-cost claim rather than asserting it. The
size report paid for itself immediately by exposing 754 bytes of `__udivmoddi4` pulled in
by one `uint64_t` division in the formatter. Findings in
[`MEASUREMENTS.md`](MEASUREMENTS.md).

Deferred to the milestone that first needs them, rather than guessed at now: resources,
persist and speaker are not yet in the seam.

## M2 — Assets — **DONE**

Generalise the probe's pipeline into a reusable tool.

- Manifest is now **external TOML** the game owns, not a Python dict inside the tool.
  TOML because manifests carry real knowledge in their comments and JSON cannot hold
  one — and because the editor will need to read and write this file
- Quantisation to `GColor8`, tile dedup, colour-key handling
- Generated header: handles, counts, tile roles, dialog ids. No number in it is
  something a human should ever type into game code
- **Validation that fails the build**, with **22 tests asserting that it does** — and
  asserting the message names the actual problem, since an error saying only "invalid
  manifest" leaves the author as stuck as silence
- **256KB appstore budget enforcement** with a per-category breakdown
- **`package.json` resource list generated from the manifest**, closing a duplication
  that would otherwise fail silently: a blob built but not declared is simply absent
  from the bundle, with no build-time complaint
- Runtime: handle-based registry, bulk residency, scene load/unload — **904 bytes**
- Platform seam grew resource reads, its first extension since M1
- **Map format v2**: tile flags moved to a per-tileset table with sparse overrides,
  cutting map size 49% and recovering 8.7% of the content budget at RPG scale

**Result.** The probe's content builds through the new pipeline and every payload is
**byte-identical** to what the probe produced — which is the acceptance criterion met
provably rather than by inspection. The runtime is exercised by 124 host checks reading
those same blobs, and the `examples/overworld` app links at 8,792 of 65,535 bytes
(13.4%).

Not yet run on hardware: the example builds and its data is verified on the host, but
nothing has confirmed the pixels on a watch.

Audio samples and fonts are in the schema's shape but not implemented — they land with
M4 and M3 respectively, when there is something to consume them.

## M3 — Graphics + first game skeleton — **DONE**

**First job: settle the tile storage format**, because the blitter is what fixes it and
changing it later means changing the pipeline, the runtime, the editor and the art.

- **Device-independent source, device-specific build.** Art stays full colour in the
  manifest; quantisation to a named target happens at build time. Pixel data is an index
  and never changes, so a future screen with more than ARGB2222's 64 colours rebuilds the
  same sources into richer palettes with no re-authoring. The editor previews truthfully
  by running the same quantiser.
- **Quantise first, then derive palettes.** Measured on one tileset: palettes taken from
  full-colour source need 97 tables and force 7% of tiles through lossy reduction;
  taken after device quantisation, 32 tables and nothing lost. The device's colour space
  is the best compressor available and it is free.
- **Palette generation is a greedy merge**: place each tile's colour set into the first
  palette whose union still fits. Losslessly takes 391 palettes down to 37 across five
  real tilesets, 8-25 per sheet. Merging, not deduping, is what makes the count sane.
- **SNES convention: index 0 transparent in every palette.** Costs 348 bytes (0.1%) and
  makes the blitter reject a transparent pixel before it reads the palette.
- **Atlases are small and semantic** — cave env, hero, grassland, furniture — not one per
  source sheet. Related tiles share colours, so each needs few palettes. Extra atlases
  cost an 8-byte header and one ~29 us read.
- **4bpp is the only depth.** A tile over 15 colours is repaired by merging its nearest
  pair and reported, not accommodated with a second format. Fires on 0.5% of real tiles.
  One depth means one blitter loop, no split point, no second palette kind.
- **Palettes are their own asset**, loaded before atlases and sprites, which carry
  indices rather than colours. Shared across the project: 2 palettes / 40 bytes for the
  whole example.
- **Content will not fit whole tilesets.** Five full sheets are 111% of budget at the best
  encoding; the practical ceiling is ~2,000 tiles before sprites, maps, dialog or audio.
  Regions and `max_tiles` are mandatory, not optional, and the budget report should make
  that obvious early.
- **Transparency and partial alpha are ordinary palette entries.** `GColor8` defines four
  alpha levels but the SDK ships nothing using the middle two, and honours them only via
  `GCompOpSet`. Irrelevant to us: we write the framebuffer directly and are the
  compositor. Blending is a 64-entry LUT shared across R/G/B; atlases flag whether any
  entry is non-opaque so opaque art keeps a straight indexed copy.
- **Palettes derived, never authored.** Variants remap by *source colour*, not index, so
  adding art cannot silently recolour the wrong thing. Palette is a draw-call parameter,
  which keeps per-entity swaps at 16 bytes a variant.
- **Measure the blit** before locking the format. Two loops (4bpp, 6bpp) against the two
  measured reference points: 2,350 µs to copy a full screen of tiles, 3,100 µs to compute
  every pixel. An interpolation until confirmed.
- **Metatiles implemented.** Pipeline splits tiles into deduplicated 8x8 quadrants;
  loader parses the layout; the tilemap draws through a paired-row blit that walks 16
  rows spanning two quadrants rather than issuing four 8x8 blits, which would have
  doubled the per-row cost that dominates. Measured 1.72x on five real tilesets, 1.19x on
  a small hand-picked region -- so the pipeline sizes both layouts and picks. Original
  note follows:
- **Metatiles were opt-in.** An independent ~2.7x that multiplies with the depth saving,
  but it changes how art is authored, so it is a `metatiles = true` a project turns on.
  The pipeline should report what a tileset would save either way; both layouts supported.
- Tilemap with smooth scrolling and camera clamping
- Sprites with transparency, mirroring, feet-anchored positioning, depth sort
- Text through the narrow platform hook
- **Game starts here**: a walkable scene using only framework APIs

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
   rather than from 128 KB.
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
| 24 KB app RAM (`aplite`) | nothing -- see below | out of scope |

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

**What author-once cannot absorb.** `aplite`'s 24 KB is a hard exclusion, not a tuning
problem. And screen size is only free for a game that can show more or less world -- a fixed
play area would have to letterbox or scale, and integer scaling is the only kind that stays
crisp. An RPG absorbs this; a puzzle grid would not.

Done when: one game source with no `#if` in it, one build, one `.pbw`, running on `emery`,
`gabbro` and `flint` -- colour rect, colour round, and 1-bit.

---

## Editor track (parallel)

A visual editor for levels, assets, testing and packaging — see
[`EDITOR.md`](EDITOR.md) for the architecture and the reasoning.

It runs **alongside** the engine milestones rather than after them, because it is a GUI
over the asset manifest and therefore depends on M2's schema rather than on any runtime
code. Staged so each piece is independently useful:

| | | Depends on | Land it |
|---|---|---|---|
| **E1** | Inspector: tilesets with ids, rendered maps, budget, validation errors | M2 | alongside M2 |
| **E2** | Map editor: paint tiles, place entities, wire warps, live reachability | E1 | once real maps exist |
| **E3** | Emulator panel: noVNC + build/install/run/logs | E1 | after M3 |
| **E4** | Asset import: sheet slicing, colour key, quantisation and dedup preview | E1 | when the pain justifies it |
| **E5** | Package button: validate, build, enforce budget, emit `.pbw` | E1 | with E3 |
| **E6** | Music editor: tracker view over the sequencer model | M4 | last |
| **E7** | Font import: drop a TTF, rasterise glyphs at a chosen pixel size, preview legibility, emit an atlas plus width table | E4 | with the first text work |

**E7 exists because a font is the one asset a person cannot author by hand at this scale.** At
6x12 most typefaces are illegible -- hinting dominates at small sizes, and a pixel font designed
for it beats a scaled Helvetica outright. So the editor has to *show* the rasterisation at the
target size before it is committed, not just accept a file. Two things fall out of that: the glyph
set should be derived from the content the pipeline already reads, with a manifest override for
runtime strings like damage numbers that appear in no dialogue; and font licensing needs a note,
since shipping rasterised glyphs is redistribution even when the outlines stay behind.

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