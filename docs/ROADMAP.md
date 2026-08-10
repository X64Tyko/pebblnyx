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

The empty example costs **6,296 bytes of 65,535 (9.6%)**; with diagnostics
off, **2,376 (3.6%)**, which verifies the zero-cost claim rather than asserting it. The
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
- **Validation that fails the build**, with **18 tests asserting that it does** — and
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
| pnx/gfx | 942 |
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

## M4 — Audio

The largest unknown, and the most valuable thing the framework provides.

- Mixer over a continuously open PCM stream; per-frame feed; configurable lead
- Voices, sample playback with loop points, ADSR envelopes
- Sequencer: patterns, order, tempo, note events
- SFX with priority and voice stealing
- Music authored in the manifest, compiled to a resource

Done when: the game has music and effects simultaneously with no underruns.

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