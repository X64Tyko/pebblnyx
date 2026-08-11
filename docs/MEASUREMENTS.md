# Measured platform facts

Everything in [`DESIGN.md`](DESIGN.md) rests on these. All taken on real Pebble Time 2
hardware (SiFli SF32LB52JUD6, Cortex-M33, 200x228, platform `emery`), SDK 4.17,
cross-checked against the PebbleOS source where a mechanism mattered.

Numbers marked **(replicated)** were reproduced across separate runs and can be
trusted tightly. Anything else is a single measurement.

---

## Frame pacing

| | |
|---|---|
| Frame period | **37.33 ms — 26.8 fps, fixed** |
| Free CPU per frame | **~35 ms** |
| `app_timer_register(1)` latency | 1.0–1.5 ms |
| Partial redraw (228 → 28 dirty rows) | **no change to fps** |

Not a configurable limiter. `src/fw/applib/app.c` sets `framebuffer_render_pending`
after each render and refuses to render again until the
kernel → compositor → display-DMA chain clears it. The flag is firmware-internal with
no SDK escape hatch, and `compositor.c` contains no throttle at all. Likely two 60 Hz
VCOM periods (33.3 ms, from `board_obelix.c`) plus ~4 ms of compositor work.

Because the period is fixed, CPU work is absorbed into idle wait rather than costing
frame rate. Decomposing a frame, the parts always summed to the same total: as
in-proc time rose, render-wait fell by exactly as much.

## Render cost (full screen, 200x228 = 45,600 px)

| Workload | Cost | % of ~35 ms |
|---|---|---|
| `memset` per row | 200 µs (~228 MB/s) | ~1% |
| 64 sprites 16x16, masked, + clear | 1,350–1,600 µs | 4% |
| 725 tile blits (full-screen 16px tilemap) | 2,350 µs | 7% |
| Computed value per pixel (pessimal) | 3,100 µs | 9% |
| One text draw | ~4,300 µs | 12% |
| **4bpp tilemap + sprites, full screen** | **~5,100 µs** | **14.5%** |

`LayerUpdateProc` is the **only** way to obtain a `GContext` — there is no
`graphics_context_create()`. Rendering goes through `graphics_capture_frame_buffer()`
plus `gbitmap_get_data_row_info()`, writing `GBitmapFormat8Bit` bytes directly.

## Memory

| Budget | Limit |
|---|---|
| **`.text` + `.data` + `.bss`** | **65,535 bytes** |
| App RAM total | 131,072 bytes |
| Heap available to a small app | ~121,000 bytes |
| Resources — hard cap | 1,048,576 bytes |
| Resources — appstore cap | **262,144 bytes** |
| Persist quota | 1,048,576 bytes, **256 bytes per key** |

The 64KB figure is the one that constrains design. `MAX_APP_MEMORY_SIZE` is 128KB, but
the app header stores virtual size — defined as `.text + .data + .bss` — in a
**uint16** (`inject_metadata.py`, `VIRTUAL_SIZE_ADDR = 0x80`). Exceeding it fails late
with a bare `struct.error: 'H' format requires 0 <= number <= 65535` and no indication
of what overflowed. Everything above 64KB must come from the heap.

For scale: a complete playable slice — tilemap, sprites, collision, camera, dialog,
input, save — was **8,140 bytes of `.text`**.

The framebuffer is not in the app's budget; the compositor owns it.

## Entity layout: it does not matter

Three layouts, three kernels, 64–2048 entities, working sets to ~114KB. ns/entity at
n=1536, 52-byte → 64-byte stride:

| Kernel | AoS | SoA | Pointer table (shuffled) |
|---|---|---|---|
| all channels | **209 → 237** | 251 → 251 | 222 → 217 |
| position only | 134 → 131 | 160 → 158 | 153 → 143 |
| stream one field | **50 → 38** | 38 → 38 | 54 → 46 |

Everything within ~1.5x. **Pointer chasing was the fastest on the wide kernel.**
On-chip SRAM has near-flat access cost — no DRAM means no ~200-cycle miss penalty for
locality to win back.

Cache-line padding is a trade with a sign that depends on access width: +24% on a
single-field sweep, **−13%** on an all-channel one, 23% more RAM either way.

**Methodology warning.** Two earlier conclusions here were *reversed* once measured
properly. Effects are 5–25%, small enough that any uncontrolled variable swamps them.
A/B inside one session with a control that should not change.

## Flash / resource reads

| Pattern | Throughput | µs/call |
|---|---|---|
| one 16KB call | 30,848 KB/s | 518 |
| 256 sequential 64B calls | 2,048 KB/s | 30 |
| 256 **scattered** 64B calls | 2,204 KB/s | 28 |

Fits `cost ≈ 29 µs + bytes ÷ 33 MB/s` almost exactly. Two conclusions:

**Call count dominates** — the same bytes cost 15x more through small calls.
**There is no locality penalty** — scattered was marginally *faster* than sequential.

Reading 128KB (larger than any on-chip cache) gave ~18.6 MB/s versus ~36 MB/s for a
repeatedly-read 16KB block, so **~18–19 MB/s is real flash throughput** and the higher
figure was cache-inflated. Cold vs warm showed no systematic difference at 128KB.

## Persist: writes are ~116x slower than reads **(replicated, 3 runs)**

| Operation | Avg | Spread |
|---|---|---|
| read 256 B | 70 µs | 56% (at clock resolution) |
| **write 256 B**, rotating keys | **7,335 µs** | 3.7% |
| write 256 B, same key | 5,007 µs | 1.3% |
| **`persist_write_int` (4 bytes)** | **3,512 µs** | 1.8% |
| `persist_delete` | ~2,900 µs | — |
| **realistic 4KB save, 16 keys** | **106 ms** | 0.9% |

**Cost is per call, not per byte.** Four bytes costs half of 256 — 877 µs/byte versus
28 µs/byte, **31x worse per byte**. Rewriting one key beats spreading across keys,
implying per-key index work. Extrapolated: 8KB = 229 ms, 32KB = ~918 ms.

## Audio

The batch API (`speaker_play_tracks`) mixes 4 monophonic tracks with pitch-shifted,
looping PCM samples — a SNES-shaped sampler. It is unusable for music plus effects:

| Property | Measured |
|---|---|
| Seam when chaining phrases | **94, 94, 94, 99, 94, 94, 102, 99 ms** |
| `speaker_play_tracks()` blocks the app task | 7–16 ms |
| Resubmit while playing | **REJECTED**, returns immediately |

Eight trials clustered at 94 ms with almost no spread: a *fixed per-submission cost*,
not jitter. With no layering and no position query, every route to music + SFX is
blocked.

Streaming works and is the answer:

| Property | Measured (8 kHz / 8-bit) |
|---|---|
| `speaker_stream_open()` | 8 ms, once |
| Buffer capacity | **8,192 bytes = 1,024 ms of audio** |
| 3 s run, feeding per frame | 82 feeds (36.8 ms cadence), **0 short writes, 0 underrun** |

~25 consecutive frames could be dropped before the speaker runs dry. Capacity is in
*bytes*, so 16 kHz/16-bit gives 256 ms (~7 frames). **Buffer lead is SFX latency** —
that is a game-design knob, not an implementation detail.

## Lifecycle: throttled, not suspended

```
will_focus(OUT)  ->  297 ms cover animation  ->  LOST focus
...
REGAINED focus after 4,574 ms, 2 frames rendered while covered
```

Two frames where 37 ms cadence would give ~123: the app is throttled to roughly
**0.4 fps**, not suspended or killed. Consequences:

- A fixed-timestep accumulator **must** be clamped, or those frames' multi-second
  `elapsed` values fast-forward the sim by seconds on return.
- Battery while covered is effectively free.
- **Save-on-blur is viable**: ~297 ms of warning against a 106 ms 4KB save.

## Alloy (JavaScript): viable as scripting, not as an engine

The XS engine lives in **firmware**, not the app. An Alloy app measured **276 bytes of
footprint with 130,796 bytes of heap free**; the JS ships as a `.xsa` resource
(7–12KB). Poco's drawing primitives (`drawBitmap`, `drawMasked`, `clip`, `origin`,
`fillRectangle`, `drawText`) are native C, and `Bitmap.RGB332` matches the panel
format exactly.

But the interpreter is slow **(replicated, <0.5% apart)**:

| | JS | C | Ratio |
|---|---|---|---|
| position kernel, per entity | **49,277 ns** | 136 ns | **363x** |
| elementary op (`acc = (acc+i)|0`) | **3,360 ns** | — | ~810 cycles |

~3.4 µs per elementary JS operation gives a budget of roughly **10,000 JS operations
per frame**. 256 entities of trivial movement costs 12.6 ms — 36% of a frame. So JS
can afford native *calls* (195 `drawBitmap` ≈ 0.6–1.2 ms is fine) but not native
*work*.

**FFI mechanics.** Declared in the mod manifest's `ffi` block with explicit types;
`void*` maps to an ArrayBuffer passed as a raw pointer, so C operates directly on JS
memory. Two traps, both silent:

1. Omitting the `ffi` block compiles fine and leaves `Natives` undefined.
2. **`pebble new-project --alloy` generates an `mdbl.c` that never sets
   `.fxBuildFFI`**, so registration never happens and the natives are stripped by
   `--gc-sections`. The official `helloffi` example sets it; the template does not.
   Fix: `.fxBuildFFI = fxBuildFFI` in the `ModdableCreationRecord`.

## Input timing (M0 spike)

A marker sweeps toward a target; the player taps as it crosses. Offsets are timestamped
in the event handler — the earliest software-visible moment. Absolute touch-to-photon
latency is *not* measurable in software; what matters and what this captures is whether
a human can reliably hit a window.

| Mode | n | mean lag | spread (sd) | range | ±37ms (1f) | ±74ms (2f) |
|---|---|---|---|---|---|---|
| **sweep + touch** | 40 | **+27 ms** | **31 ms** | −45..+96 | 75% | **100%** |
| sweep + button | 35 | +53 ms | 33 ms | −11..+159 | 77% | 97% |
| flash + touch (reaction) | 24 | +351 ms | 165 ms | −301..+788 | — | — |
| flash + touch, **rerun** | 41 | **+352 ms** | **75 ms** | +60..+637 | 73% | 85% (92% at 3f) |

Hit rates are computed against the *mean*, not zero: a constant lag is correctable by
shifting the window, so it should not count as a miss. Only the spread is uncorrectable.

**Touch has half the lag of buttons** (+27 vs +53 ms) at identical precision. Use touch
for anything timed.

**Reaction lag is ~352 ms, replicated to within 1 ms** across two runs — human reaction
time plus the input path. That figure is solid.

**Reaction precision is ~75 ms, not 165 ms.** The first run's spread was inflated by
*negative* offsets: taps landing before the cue appeared. Those were guesses rather than
reactions, and they vanish in the rerun. Worth noting as a behaviour in its own right —
with an unpredictable cue, players will pre-empt rather than wait, which is a design
hazard about predictability rather than latency.

So reaction is **usable but clearly worse**: a 3-frame window hits 92% provided the
window is offset by ~352 ms. Anticipation is 2.4x more precise (31 vs 75 ms spread) and
should be the default, but a reaction prompt — a dodge cue, say — is not ruled out.
The requirement is that any reaction cue must *lead its target by ~350 ms*, which suits
a discrete prompt and suits a combo whose timing is defined by an animation much less.

**Judgment resolution is 1 ms even though the display is 37 ms.** Input is timestamped
in the handler; only visual feedback is frame-quantised. Graded results (perfect / good
/ miss) are therefore expressible despite the frame rate.

## Traps worth not rediscovering

- **`pebble install --logs` attaches after `init()` runs.** Anything logged during
  startup is silently discarded. Draw diagnostics to the screen, or defer them to a
  button press. This cost time three separate times.
- **`time_ms()` has 1 ms resolution.** Sub-millisecond work must be timed by
  repetition. Never fabricate microseconds by multiplying a millisecond clock.
- **A fixed-timestep loop replaying one input snapshot double-fires edge events.**
  One button press paged through an entire conversation until the action edge was
  consumed by the first tick only.
- **Benchmark kernels need an observable side effect.** The compiler silently deleted
  a 16KB write-only array; only a read kept it alive.
- **The build reports resources against the 256KB appstore limit**, not the 1MB hard
  cap, which makes the real ceiling look 4x closer than it is.
- **Pebble's libc has `snprintf` but no `vsnprintf`.** There is no way to forward a
  `va_list` to the platform's formatter, so any variadic logging needs its own. Reaching
  for newlib's instead fails in two ways at once — its locale object is a *multiple
  definition* of libpebble's `setlocale`, and it drags in `_sbrk`/`_write`/`_read`/
  `_exit`, none of which exist here. The link error names neither cause. `core/pnx_fmt`
  exists for this.
- **A single `uint64_t` division costs 754 bytes.** It emits calls to `__udivmoddi4`,
  the software 64-bit divide helper — over 1% of the whole app budget. Nothing on this
  platform is 64-bit; keep arithmetic 32-bit and the helper never links.
- **Summing symbol sizes undercounts the app by ~11%.** `.header`, the build-id note,
  `.init`/`.fini` and inter-section padding occupy space but belong to no symbol: 760
  bytes of a 7,272-byte app. Budget against allocated ELF *sections*, which is what
  `tools/size_report.py` does and what the SDK's own footprint number reports.
- **16.16 fixed point holds ±32,767 whole units, and an int64 intermediate does not
  extend that.** The wider intermediate protects the *product* mid-calculation; the
  result still has to fit. `300 * 200` silently returns −5,536.

## Asset pipeline (M2)

The probe's content through the new pipeline, `examples/overworld`:

| Category | Bytes | Share |
|---|---|---|
| atlas (64 unique 16x16 tiles + flag table) | 16,456 | 85% |
| sprites (3 hero frames + 1 npc) | 1,552 | 8% |
| maps (32x24 and 24x16) | 1,192 | 6% |
| dialog (2 entries, 4 pages) | 138 | 1% |
| **total** | **19,338** | 7.4% of the 256KB appstore budget |

**Tile dedup is what makes a real sheet usable.** The source is 1280x1248 — 1,560KB raw,
past even the 1MB device ceiling. Region selection plus dedup brings 256 candidate tiles
down to 64 unique. Compression would not help: resources are stored already packed.

**Atlas data dominates this example at 80%**, but that is an artefact of a demo with two
maps and no music. Scaled to a real game it is not the binding constraint.

Taking **128 unique 16x16 overworld blocks** as the yardstick for one complete tileset --
the figure commonly cited for Final Fantasy 1, though the exact count is not something we
have verified from the ROM -- a Chrono Trigger / Legend of Dragoon shaped RPG prices out
as:

| Category | Bytes | Share | Assumption |
|---|---|---|---|
| tilesets | 97,536 | 37.2% | 3 sets x 127 tiles |
| **maps** | **46,080** | **17.6%** | 30 maps @ 32x24, 2 bytes/cell |
| npc sprites | 23,040 | 8.8% | 20 npcs x 3 frames |
| party sprites | 13,824 | 5.3% | 4 chars x 3 frames x 3 facings, mirrored |
| dialog | 10,000 | 3.8% | 50 conversations |
| **total** | **190,480** | **72.7%** | leaves ~70KB for audio, fonts, battle art |

Three consequences. Tiles are affordable but not free at 37%. **Maps were the surprise at
17.6%**, second only to tilesets — since fixed, see below. And roughly 70KB is left for
audio, the category with no measured cost yet and the one most able to blow the budget.

### What the NES actually did, and what it costs us not to

The tile *count* turns out to be the least interesting thing about FF1. Two architectural
choices make its tileset an order of magnitude cheaper than ours, and both are available
to us:

FF1 has no CHR-ROM at all. Graphics live in program ROM and are streamed into 8KB of
CHR-RAM, so the working set is 512 8x8 tiles — 256 background (of which the font eats
64–96) and 256 sprite. Its 128 16x16 overworld blocks are **compositions**: each is four
references into that 8x8 pool, and the pool is far smaller than 4 x 128 because edges and
corners repeat constantly.

| | |
|---|---|
| FF1 overworld art, ≤192 unique 8x8 at 2bpp | **3,072 B** |
| Same content as 128 flat 16x16 tiles at 8bpp | **32,768 B** |
| | **10.7x** |

That factor decomposes exactly: **4x from bit depth** (our 8bpp `GColor8` against the
NES's 2bpp) and **2.7x from metatile reuse** (512 sub-tile slots drawn from ~192 unique).
The two are independent and multiply.

Three tilesets, as a share of the 256KB budget:

| Storage | Bytes | Budget |
|---|---|---|
| 8bpp flat — what we do today | 98,304 | 37.5% |
| 4bpp + 16-colour palette | 49,152 | 18.8% |
| 2bpp + 4-colour palette | 24,576 | 9.4% |
| 4bpp + metatiles | 18,204 | 6.9% |

### 4bpp is lossless on real art, so it can be the silent default

The worry with palettised storage is that it becomes a thing authors have to think about.
Measured against the actual sheets, it does not: `GColor8` already crushes 16.7M colours
to 64, and a 16x16 chunk of pixel art simply does not use many of them.

| Sheet | Distinct colours |
|---|---|
| Stardew mines tileset, worst single 16x16 tile | **9** |
| ...whole 220-tile atlas | 17 of 64 |
| Cecil, per 16x24 frame | 11 |
| FFRK npc, per 16x24 frame | 11 |

**Not one tile or frame of the real content exceeds 16 colours** — the worst is 9, and the
distribution peaks at 2–3. With a per-unit palette, 4bpp is bit-exact, not a compromise:
same pixels, 1.95x smaller (56,320 → 28,892 B on that atlas, palettes included). Only 32
distinct palettes are needed across 220 tiles, so the palette table is noise.

Those are three sheets of clean commercial pixel art, though — the friendliest possible
sample. Against **synthetic adversarial input**, the picture is more interesting:

| Content | Max colours/tile | Mean | Tiles over 16 |
|---|---|---|---|
| clean pixel art | 9 | 8.2 | 0% |
| dithered | 4 | 2.5 | 0% |
| smooth gradient | 2 | 1.2 | 0% |
| photographic / noisy | 7 | 6.1 | 0% |
| anti-aliased, 40 shapes | 19 | 7.4 | 5% |
| anti-aliased, 400 shapes | 37 | — | 44% |
| anti-aliased, 1000 shapes | 27 | — | 47% |

**The `GColor8` ceiling is doing the compression for us.** Smooth content collapses — a
gradient averages 1.2 colours per tile — because two bits per channel means a 16x16
window of any *smooth* image lands on almost no distinct values. The intuition that
photographic sources would be the hard case is simply wrong.

The one genuinely hard case is **many distinct hues meeting inside one tile**, which is
what anti-aliased composite art produces at shape boundaries. It tops out near 47% of
tiles affected, not 100%.

That kills the obvious fallback rule. An atlas-wide "if any tile needs more than 16
colours, store the whole atlas at 8bpp" would throw away the entire saving because three
tiles out of sixty-four were busy:

| Strategy | 3/64 tiles hard | 30/64 tiles hard |
|---|---|---|
| atlas-wide fallback to 8bpp | **1.00x** (nothing saved) | 1.00x |
| **per-tile mixed depth** | **1.90x** | **1.36x** |

So the decision is **per tile, not per atlas**, and it degrades gracefully instead of
falling off a cliff: 1.95x on clean art, 1.36x on the worst thing tried, never 1.0x. A
tile that needs 17+ colours is simply stored raw next to its neighbours.

Caveat worth keeping: those adversarial figures are synthetic. What is durable is the
*mechanism* — why smooth content is cheap and why colliding hues are not — rather than
the exact percentages.

### Quantise before choosing palettes, not after

The same 241-tile sheet, measured both ways:

| Palettes derived from | Colours in sheet | Max per tile | Tiles over 16 | Distinct palettes |
|---|---|---|---|---|
| full-colour source | 235 | 47 | 7% | 97 |
| after device quantisation | 17 | **9** | **0%** | **32** |

Choosing palettes in full colour would build 97 tables and push 7% of tiles through a
lossy 16-colour reduction — to produce output that gets crushed to 64 colours immediately
afterwards anyway. The device's own colour space is the cheapest compressor in the
pipeline, and running it first turns a lossy problem into a lossless one.

This is why source art stays full colour and quantisation is a *build target* rather than
an import step: the ordering has to be re-run per device, and on a richer screen the same
sources yield richer palettes with no re-authoring.

### Real tilesets, not the convenient one (near worst case)

Five general-purpose 480x256 tilesets — dungeon, exterior, interior, ship, world;
**~2,175 unique tiles** after dedup. This is the set the format decisions are validated
against, because the Stardew mines tileset used earlier turns out to be atypically
friendly (a dark cave set with 17 colours).

| Sheet | Unique | Colours | Max/tile | Tiles >16 col |
|---|---|---|---|---|
| dungeon | 416 | 38 | 15 | 0 |
| exterior | 445 | 41 | 14 | 0 |
| interior | 449 | 44 | 19 | 3 |
| ship | 419 | 34 | 19 | 1 |
| world | 446 | 46 | 19 | 2 |
| **union** | | **47 of 64** | | **6 of 2,175 (0.3%)** |

Three things fall out.

**Real tilesets use 34–46 colours, not 17.** Any scheme depending on a whole tileset
fitting a small palette does not survive contact with them.

**Only 0.3% of tiles exceed 16 colours.** So 4bpp is the right primary encoding, with a
tiny minority stored 6bpp inline — mixed depth earns its complexity here.

**Palette generation is a greedy merge, not a dedup.** Deduplicating only *identical*
tile colour sets gives 391 palettes for those 2,175 tiles. Merging any sets whose union
still fits gives **37** — losslessly, since a tile is content in any palette containing
its colours. Per sheet that is 8-25 palettes, a number a person can actually look at.

| Sheet | Tiles | Exact dedup | Greedy merge |
|---|---|---|---|
| dungeon | 416 | 111 | 10 |
| exterior | 445 | 148 | 13 |
| interior | 449 | 181 | 24 |
| ship | 419 | 90 | 9 |
| world | 446 | 161 | 25 |
| **all five** | **2,175** | **391** | **43** |

**Reserving index 0 as transparent costs 0.1%.** Following the SNES convention drops
usable colours to 15, which raises palettes 37 -> 43 and hard tiles 6 -> 10, for a total
of 281,893 B against 281,545 — **348 bytes**. In exchange transparency is uniform and the
blitter rejects a transparent pixel before reading the palette at all.

### 4bpp decode costs 2.5x an 8bpp copy, not parity

Measured on device: a full screen of 4bpp tiles plus depth-sorted sprites runs at
**~7,400 µs** (7,259-8,407 across samples), holding 26.6-26.8fps with no dropped frames.

The estimate had been that decode would land between the two reference points --
2,350 µs to copy a screen of 8bpp tiles, 3,100 µs to compute every pixel. It came in at
more than double the upper bound. Normalised per pixel, against the probe's 725 8px tiles
(45,600 px at 51.5 ns/px), our ~224 16px tiles are ~57,344 px at **129 ns/px**.

The estimate was wrong because it priced the arithmetic and ignored everything around it:
a branch on `mirror` per pixel, and each source byte read twice because two 4bpp pixels
share one. The inner loop was rewritten to hoist the mirror test into two loops and read
each byte once, processing nibble pairs.

Rewriting the loop -- hoisting the mirror test into two loops and reading each source
byte once instead of twice -- brought it to **~5,100 us, 14.5% of the frame**, or 89 ns/px.
Decode now costs ~1.7x an 8bpp copy rather than 2.5x.

**It is affordable either way**, and the frame rate never left its 26.8fps ceiling. But
the lesson is the one this project keeps relearning: an interpolation between two measured
points is not a measurement, and the first gap was 2.4x.

### Metatiles: 1.72x, but only at scale

Composing each 16x16 tile from four deduplicated 8x8 quadrants, measured across the five
full tilesets (2,175 tiles, 8,700 quadrant slots):

| | Unique | Reuse | Total |
|---|---|---|---|
| flat 16x16 @ 4bpp | 2,175 | — | 279,088 B |
| **metatiles, u16 indices** | 4,436 | 1.96x | **162,215 B (1.72x)** |

117KB, 45% of the content budget. The palette constraint -- a quadrant must fit its
tile's palette -- costs almost nothing (4,308 unique without it), because tiles sharing
quadrants tend to share palettes anyway.

**Reuse scales with tileset size.** The example's 64-tile hand-picked region gets only
1.19x, where the 9-byte definitions nearly cancel the saving (7,488 B against 8,192 flat).

**And metatiles are not free at runtime.** Measured on device, the same scene costs
**~6,900 us metatiled against ~5,100 us flat -- 35% more frame time**. Each tile row
becomes two clipped spans instead of one, so span setup doubles even though the
paired-row blit keeps row lookups the same. The design note claiming four 8x8 blits would
"double the per-row cost" fixed the half that was named and missed the half that was not.

That reframes the choice as a trade rather than a saving:

| Scale | Space saved | Render cost | Worth it |
|---|---|---|---|
| 64-tile region | 8.6% (764 B) | +35% | **no** |
| five full tilesets | 42% (117 KB) | +35% | yes |

So auto-selection requires a **25% minimum saving** before choosing metatiles, not merely
a positive one. Small atlases stay flat; large ones pay the render cost to buy back a
large fraction of the content budget.

Also visible: a frame containing a **scene load peaks at 31,000 µs** -- 83% of the frame
period, from flash reads. It does not drop a frame here, but a larger scene would, so a
transition should mask the load rather than assume it fits.

### Implemented result

The pipeline and runtime now ship 4bpp with shared palettes. On the overworld example:

| | Before (8bpp) | After (4bpp) |
|---|---|---|
| tiles atlas | 16,456 B | **8,328 B** |
| hero sprite | 1,160 B | 588 B |
| npc sprite | 392 B | 204 B |
| palettes | — | 40 B (2, shared by everything) |
| **total content** | **19,338 B** | **10,490 B** |

**1.84x**, losslessly, with no tile needing repair. The asset runtime grew 904 -> 1,238
bytes for palette handling and nibble decode; the example app sits at 9,428 of 65,535
(14.4%).

### Encoding compared on that content

| Encoding | All five sheets | vs 256KB budget |
|---|---|---|
| 8bpp flat | 556,800 B | 212% |
| 6bpp uniform | 417,920 B | 159% |
| **4bpp, per-tile palettes** | **291,913 B** | **111%** |
| 4bpp windowed (4x16 + selector) | never applicable | — |

Per-tile palettes beat uniform 6bpp by **126KB — half the entire content budget**.

**And even the best encoding does not fit five full tilesets.** At 111% of budget, a game
cannot ship complete sheets; it ships *carvings* of them. Measured, with a carve that
prefers palette-compatible tiles:

| Tiles per sheet | Total | Palettes | Bytes | Budget |
|---|---|---|---|---|
| 64 | 320 | 69 | 42,384 | 16.2% |
| **128** | **640** | **119** | **84,464** | **32.2%** |
| 192 | 960 | 180 | 126,720 | 48.3% |
| 256 | 1,280 | 222 | 168,672 | 64.3% |

At 128 tiles per tileset — the Final Fantasy 1 yardstick — **five complete tilesets cost
32% of budget**, less than the earlier estimate for *three* at 8bpp. Palette count scales
sub-linearly (0.22 per tile at 320 tiles, 0.17 at 1,280) because a palette-aware carve
keeps reusing what it already has.

The practical ceiling is roughly **2,000 tiles** within 256KB before sprites, maps, dialog
or audio, so budgeting by tile count is the first thing a project should do.

### Depth chosen automatically, 4bpp or 6bpp

Sizing each atlas under both depths and taking the smallest lossless option:

| Atlas | Chosen | Bytes | vs 8bpp flat |
|---|---|---|---|
| real tileset (Stardew mines) | 4bpp, 32 palettes | 28,672 | **1.96x** |
| anti-aliased, moderate | 4bpp, 128 palettes | 25,088 | 1.84x |
| anti-aliased, dense | 6bpp | 45,952 | 1.33x |

**8bpp is never needed**, because 6bpp addresses the device's entire 64-colour space. That
is what makes a per-atlas choice safe where an atlas-wide 8bpp fallback was not: the worst
case still saves a third rather than saving nothing.

**The CPU to pay for this is already sitting idle.** A full-screen tilemap blit measures
2,350 µs and the pessimal case — computing every pixel rather than copying it — measures
3,100 µs, against ~35 ms of free time per frame. Unpacking nibbles through a palette
lands between those two, so the whole trade costs on the order of **2% of a frame to
reclaim 19% of the content budget**.

This is the platform's defining trade stated plainly: rendering is capped at 26.8fps by
firmware no matter what we do, so CPU spent on decode is CPU that would otherwise be
spent waiting. The NES compressed its tiles because ROM was scarce and the CPU was not
fast enough to do much else. We would compress ours because storage is scarce and the CPU
is **90% idle by construction** — the same technique, reached from the opposite direction,
and affordable at a more generous 4bpp than the NES could manage.

**Half the map cost was redundant, and is now gone.** The format stored `w*h` tile bytes
*and* `w*h` flag bytes, but flags are a property of the tile in almost every case. Blob
format v2 moves them to a per-tileset table on the atlas, with sparse per-cell overrides
for the exceptions — a door drawn on an ordinary accent tile is exactly such a case, and
the probe's content has precisely one.

Measured on the example: maps fell **2,330 → 1,192 bytes (49%)** for 64 bytes of flag
table. At the 30-map scale above that is 46,080 → ~23,300, recovering **8.7% of the
content budget**. Each tile's default is derived as its most common flag value across
every map, so the manifest needed no new syntax and the legend stays the one place
behaviour is written down.

The catch, stated in the pipeline as a build error rather than left to be discovered:
maps now assume a **single tileset**, because tile indices and the flag table share one
index space. Multi-atlas maps need a design that does not exist yet.

## Baseline framework cost (M1, `examples/empty`)

Measured with `tools/size_report.py` against the 65,535-byte ceiling.

| Configuration | Footprint | Notes |
|---|---|---|
| Default (`PNX_USE_DIAGNOSTICS=1`) | **6,296 B** (9.6%) | platform 1,277 + core 3,743 + game 446 |
| `PNX_USE_DIAGNOSTICS=0` | **2,376 B** (3.6%) | core falls to 92 B — the arena alone |
| M2 `examples/overworld` | **8,580 B** (13.1%) | adds assets 824 B + a naive renderer |

The delta is the point: switching one module off reclaims **3,920 bytes**, including the
deferred log ring's 2,304 bytes of `.bss` and the formatter that only it pulled in. That
is the compile-time module selection working as specified, not merely as intended.

The log ring is the largest single static allocation in an otherwise empty app — about a
third of its footprint — which is why `PNX_LOG_LINES` and `PNX_LOG_LINE_LEN` are tunable.

## Audio costs (M4)

| | Bytes | |
|---|---|---|
| Song: 16 rows x 4 channels, 2 instruments, 112bpm | **160** | a row is 2 bytes per channel |
| Sample: 120ms blip at 16kHz 8-bit | **1,936** | 12x a whole song pattern |
| One second of PCM | **16,000** | |

**This ratio is the entire argument for a sequencer.** With roughly 70KB left after art,
four seconds of recorded audio would consume the whole remaining content budget, while a
song is a few hundred bytes. So music is sequenced from generated waveforms -- which cost
nothing in resources at all -- and PCM is reserved for short effects.

The pipeline enforces that rather than advising it: samples over **1.5 s** are a build
error naming the cost, because the alternative is discovering it as a bundle that will not
ship.

Instruments are one cycle of a waveform generated at init and looped. Pitch needs no new
code -- an L-sample cycle played at `note_hz * L` advances exactly `note_hz` cycles per
second, which the mixer's existing resampling already does.

**No fill-level query exists**, so underrun is inferred from bytes written against elapsed
time, and feeding is lead-based at 120 ms. Verified in host tests across a simulated 37 ms
frame cadence: zero deficit, and a stall inside the lead produces none while a stall beyond
it is reported rather than hidden.


## Audio streaming: short writes are normal, discarding them is not

First device run of the mixer sounded badly broken, and the log said why:

    voices 5  deficit 7456 | short 392 feeds 479

**82% of writes were short**, and the first implementation *discarded* whatever the device
would not accept. The voices' phase had already advanced past those samples, so the next
frame mixed from a later point -- a discontinuity in the waveform on four writes in five.

A short write is not an error. It means the buffer is full, which is the healthy state.
The fix is to carry the remainder and offer it again before mixing anything new; mixing
ahead of a pending remainder would also reorder the stream.

**And the 16-bit format was pure waste.** The mixer accumulates and clamps in 8-bit, so a
16-bit stream is the same samples shifted left eight -- identical audio at twice the
bandwidth. That doubling is what kept the buffer permanently full: a 120 ms lead is 3,840
bytes at 32 KB/s but only 1,920 at 16 KB/s. 8-bit is now the default, and the mixer records
the buffer depth it discovers at the first short write, since the device offers no way to
ask.

The `deficit 7456` was a startup artefact, constant from the first sample: the gap between
opening the stream and the first feed, while assets loaded. Not an ongoing underrun.

## MIDI as a music source: the reduction is the hard part

A real SNES-era arrangement, analysed to size the problem:

| | |
|---|---|
| Tracks with notes | 11, on 11 separate MIDI channels |
| Note-ons | 5,440 over ~131 bars |
| Pitch range | MIDI 26-89, over five octaves |
| 16th-note slots needing >4 simultaneous notes | **646 of 1,380 (47%)**, peaking at 9 |

Parsing MIDI is straightforward. **Fitting it into four channels is not** -- nearly half of
a real arrangement is too thick, so an importer must choose what to drop and say so. A
silent reduction would make a song sound wrong with no indication why, which is the same
class of failure as an unreachable warp: the output looks fine and simply is not.

That argues for reduction driven by declared intent -- which tracks are melody, bass,
harmony -- rather than a generic algorithm guessing.
