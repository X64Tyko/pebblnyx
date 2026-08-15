# Measured platform facts

Everything in [`DESIGN.md`](DESIGN.md) rests on these. All taken on real Pebble Time 2
hardware (SiFli SF32LB52JUD6, Cortex-M33, 200x228, platform `emery`), SDK 4.17,
cross-checked against the PebbleOS source where a mechanism mattered.

Numbers marked **(replicated)** were reproduced across separate runs and can be
trusted tightly. Anything else is a single measurement.

## The numbers, in one place

The figures that get looked up. Each links to the section that derives it and says what it
cost to find out.

| | | |
|---|---|---|
| Frame period | **37.33 ms**, fixed — 26.8 fps | [pacing](#frame-pacing) |
| Free CPU per frame | **~35 ms** | [pacing](#frame-pacing) |
| Full screen of 4bpp tiles + sprites | **~5.1 ms** after rework, was 7.4 | [render](#render-cost-full-screen-200x228--45600-px) |
| App RAM / static cap | 128 KB heap, **64 KB** statics | [memory](#memory) |
| App binary ceiling | **65,535 B** — a u16 that fails obscurely | [memory](#memory) |
| Resource bytes | 256 KB appstore, **1 MB** device | [memory](#memory) |
| Resource entries | **256** per `.pbpack` | [memory](#memory) |
| Flash read | scales with OFFSET within a resource; a single **large** resource (220KB) reads flat at ~40ms regardless of offset -- open | [flash](#flash--resource-reads) |
| Persist write | **~7 ms per call**, any size | [persist](#persist-writes-are-116x-slower-than-reads-replicated-3-runs) |
| Throttled when covered | **~0.4 fps**, not suspended | [lifecycle](#lifecycle-throttled-not-suspended) |
| Button latency | +27 ms systematic, 31 ms spread | [input](#input-timing-m0-spike) |
| Touch latency | half of buttons | [input](#input-timing-m0-spike) |
| Tile encoding | 16x16, 4bpp, metatiles at scale | [tiles](#tile-encoding-16x16-with-metatiles-measured-against-the-alternatives) |
| Text draw | glyph blit **~17us + ~8us/glyph**, SDK **~703us + ~51us/glyph** (10-17x) | [text](#glyph-blitter-vs-sdk-text-measured-on-device-examplestextbench) |
| Framework baseline | **6,452 B** empty, 2,528 without diagnostics | [baseline](#baseline-framework-cost-m1-examplesempty) |
| Audio | batch API unusable; **stream and mix** | [audio](#audio) |

**Five places the estimate was wrong**, which is the argument for measuring at all: 4bpp
decode is 2.5x an 8bpp copy rather than near parity; a full screen cost 7,400 µs against an
estimated 2,350; the batch audio API costs ~94 ms a submission; metatiles pay 1.72x at scale
but 1.19x on a small carve; and a ranged flash read is O(offset), which made a map load 279x
its predicted cost.

---

# Platform

*What the hardware does, and what it refuses to do.*

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

**Consequence for the frame loop (M6): request 40ms, not "as soon as possible."**
Asking for the next frame immediately after drawing converges on the 37.33ms floor with
no slack -- a frame that runs a millisecond long arrives a millisecond late, one for one,
because there was nothing held back to absorb it into. `PNX_FRAME_MS` (pnx_config.h)
locks the request to a fixed 40ms grid instead, ~2.67ms looser than the floor, banked as
slack on every single frame rather than spent chasing a ceiling that cannot move. It also
happens to land render cadence almost exactly on `PNX_TICK_MS` (also 40ms), so a rendered
frame now carries close to exactly one sim tick instead of the roughly-one-frame-in-
fourteen that carried none before this. See `pnx_platform_pebble.c`'s frame loop for the
scheduler (targets the grid, not a flat offset from "now," so it self-corrects rather than
drifting) and `docs/ROADMAP.md`'s M6 for how it fits with the rest of the app lifecycle.
Not yet confirmed on device -- like the rest of M5/M6, it is sized against the numbers
above rather than measured itself.

## Render cost (full screen, 200x228 = 45,600 px)

| Workload | Cost | % of ~35 ms |
|---|---|---|
| `memset` per row | 200 µs (~228 MB/s) | ~1% |
| 64 sprites 16x16, masked, + clear | 1,350–1,600 µs | 4% |
| 725 tile blits (full-screen 16px tilemap) | 2,350 µs | 7% |
| Computed value per pixel (pessimal) | 3,100 µs | 9% |
| One SDK text draw (`graphics_draw_text`), one HUD line | ~1,367-4,300 µs, length-dependent | 4-12% |
| **4bpp tilemap + sprites, full screen** | **~5,100 µs** | **14.5%** |
| One glyph-blitter text draw, one HUD line | **~146-177 µs** | 0.4-0.5% |

`LayerUpdateProc` is the **only** way to obtain a `GContext` — there is no
`graphics_context_create()`. Rendering goes through `graphics_capture_frame_buffer()`
plus `gbitmap_get_data_row_info()`, writing `GBitmapFormat8Bit` bytes directly.

**The glyph blitter (E7) is measured now, at both depths** -- see "Glyph blitter vs SDK
text" below for the full breakdown (it scales with string length on both paths, from very
different starting costs, so a single number was never going to be the honest answer).
The reasoning that it should be far cheaper than the SDK path held up, and undersold it:
SDK text carries a several-hundred-µs fixed cost before a single glyph is drawn. 2bpp is
confirmed more expensive than 1bpp, as reasoned -- intermediate coverage levels read the
destination and blend three channels -- but both remain an order of magnitude or more
cheaper than SDK text at every length tested.

## Memory

| Budget | Limit |
|---|---|
| **`.text` + `.data` + `.bss`** | **65,535 bytes** |
| App RAM total | 131,072 bytes |
| Heap available to a small app | ~121,000 bytes |
| Resources — hard cap | 1,048,576 bytes |
| Resources — appstore cap | **262,144 bytes** |
| Resources — **entries** | **256** per `.pbpack` |
| Persist quota | 1,048,576 bytes, **256 bytes per key** |

**Three of those are spelled 256 and mean different things**, which is easy to trip over.
The appstore cap is 256 KB of bytes across the whole pack and only warns; the hard error is
at 1 MB (`report_memory_usage.py` stats the pack file, so neither is per-resource); and the
pack's table holds 256 *entries* whatever they weigh (`pbpack.py`, `table_size = 768 if
is_system else 256`). Reporting only the appstore figure makes the real ceiling look four
times closer than it is. The entry count went from irrelevant to binding when a map became
one resource plus a bank per few WorldTiles — a project of large maps runs out of entries
long before bytes, and overflowing it is a bare traceback out of the SDK's packer.

The 64KB figure is the one that constrains design. `MAX_APP_MEMORY_SIZE` is 128KB, but
the app header stores virtual size — defined as `.text + .data + .bss` — in a
**uint16** (`inject_metadata.py`, `VIRTUAL_SIZE_ADDR = 0x80`). Exceeding it fails late
with a bare `struct.error: 'H' format requires 0 <= number <= 65535` and no indication
of what overflowed. Everything above 64KB must come from the heap.

For scale: a complete playable slice — tilemap, sprites, collision, camera, dialog,
input, save — was **8,140 bytes of `.text`**.

The framebuffer is not in the app's budget; the compositor owns it.

## Flash / resource reads

**A ranged read costs by where it STARTS, not by how much it moves.**
`resource_load_byte_range` streams from the front of the resource on every call, so the
same read is cheap near the top of a blob and expensive near the bottom.

| Pattern | Throughput | µs/call |
|---|---|---|
| one 16KB call | 30,848 KB/s | 518 |
| 256 sequential 64B calls | 2,048 KB/s | 30 |
| 256 **scattered** 64B calls | 2,204 KB/s | 28 |

Within a small resource that looks like `cost ≈ 29 µs + bytes ÷ 33 MB/s`, and **call count
dominates** — the same bytes cost 15x more through small calls — with **no locality
penalty**, scattered being marginally *faster* than sequential. Reading 128KB (larger than
any cache) gave ~18.6 MB/s against ~36 MB/s for a repeated 16KB block, so ~18–19 MB/s is
real throughput and the higher figure was cache-inflated.

That model is a **special case**. It was probed over a 16KB resource, where every offset is
small enough for the seek to vanish into per-call noise. Over a 75KB one it is wrong by up
to 279x, and wrong in a way that grows with depth:

| Load (192x192 map, `examples/worldtiles`) | Bytes | Calls | Predicted | **Actual** |
|---|---|---|---|---|
| 9 WorldTiles at the map's origin | 13,281 | 16 | 0.9 ms | **46 ms** |
| 16 WorldTiles at tile 142,52 | 22,563 | 19 | 1.2 ms | **305 ms** |
| all 144 WorldTiles | 96,108 | 148 | 7.2 ms | **1,984 ms** |

The same 16-tile window costs 46 ms near the origin and 305 ms two thirds of the way in —
1.7x the bytes, the same call count, 6.6x the time. Neither a per-call constant nor a
throughput figure can produce that. Fitting `cost ≈ Σ offsetᵢ ÷ T` gives T ≈ 2.8 MB/s and
reproduces every row within 2x.

### What it means for design

Split map payloads into small resources and read consecutive runs in one call. Both target
the offset term; neither changes the byte count. Same hardware, same content:

| | Before | After | |
|---|---|---|---|
| hold the whole 192x192 world | 1,984 ms | **74 ms** | 26.8x |
| warp into the middle of it | 305 ms | **12 ms** | 25.4x |
| worst frame while walking | 47–63 ms | **8–12 ms** | 5.2x |
| frames dropped crossing a WorldTile | one, every time | **none** | — |
| per read | 13.4 ms | **1.8 ms** | 7.4x |

Which is also the confirmation the O(offset) reading needed: the bytes never changed, and
148 calls to 41 cannot explain 27x on its own. What is left at 1.8 ms is transfer rather
than seek — 74 KB at ~1.4 MB/s.

The same reasoning picks the streaming unit. A 128-byte tile read per drawn tile is 6.7 ms
a frame at 725 tiles; a 516-byte WorldTile amortises one call over 256 cells and pays it at
a boundary rather than every frame. That is why `pnx_assets.h` still says
residency-not-streaming and maps stream anyway — the rule is unchanged, the unit is not.

**The probe landed, and it did not settle the model -- it complicated it.**
`examples/flashbench` reads one 220KB raw resource at seven offsets (0-200KB, fixed 256B)
plus two longer reads (2048B) at the first and last offset, each point averaged over 30
calls. Measured on `emery`, 2026-08-14:

| Offset | Length | Cost |
|---|---|---|
| 0 KB | 256 B | 40.4 ms |
| 25 KB | 256 B | 40.4 ms |
| 50 KB | 256 B | 41.9 ms |
| 75 KB | 256 B | 41.0 ms |
| 100 KB | 256 B | 40.8 ms |
| 150 KB | 256 B | 40.1 ms |
| 200 KB | 256 B | 40.7 ms |
| 0 KB | 2,048 B | 41.0 ms |
| 200 KB | 2,048 B | 41.2 ms |

**Flat.** ±5% across the whole sweep, independent of both offset and length -- the
`cost ≈ Σoffset ÷ T` model fitted above predicts near-zero at 0KB and ~73ms at 200KB for
this resource, and neither happened. This is not the O(offset) pattern; it is something
else, and the something else is expensive: ~40ms is roughly **1,400x** the ~29µs baseline
and higher than even the worst pre-banking WorldTile read (305ms, but for a 22KB window
across 19 calls -- **per call** that read was ~16ms, still half of flashbench's flat 40ms).

**Best explanation, not a confirmed one:** this is the largest single resource anything in
this repo has read from directly -- 220KB, against ~8KB for a WorldTile bank and a few KB
for everything else. `pbpack.py` stores a CRC per resource entry at build time
(`ResourcePackTableEntry`). If the device validates that CRC by hashing the resource's
**entire content** on every access, rather than just the requested range, that is
O(total resource size) and would produce exactly this: flat across offset and length
within one resource, scaling with that resource's own size, and reproducing the observed
magnitude at a plausible ~5.6 MB/s hash throughput (220KB / 40ms). Not verified against
PebbleOS firmware source, which was not available to check directly.

**Actionable regardless of the mechanism: a single large resource is expensive to read
from at all, uniformly across it.** This lands on the same answer M4d's banking already
gave -- split large payloads into several smaller resources -- for a second, independent
reason. The O(offset) finding above says why a small streaming unit beats one big
resource for a *given offset*; this says a large single resource is expensive even at
offset 0.

**Resolved: it is O(resource size), not O(offset).** `flashbench` v2 reads five resources
of different total sizes (8/32/75/150/220 KB), each at a near-origin offset and a
two-thirds-deep one, fixed 256B length throughout -- the same design that would kill or
confirm the CRC hypothesis. Measured on `emery`, 2026-08-14, 30 calls per point:

| Size | Near | Deep | Deep/near |
|---|---|---|---|
| 8 KB | 2,266 µs | 2,433 µs | 1.0x |
| 32 KB | 7,100 µs | 7,033 µs | 0.9x |
| 75 KB | 14,666 µs | 15,066 µs | 1.0x |
| 150 KB | 28,300 µs | 29,000 µs | 1.0x |
| 220 KB | 40,600 µs | 40,000 µs | 0.9x |

Offset genuinely does not matter, at any size tested -- near and deep agree within noise
at every point, matching v1's flat sweep exactly. Size does: a linear fit across the five
(least-squares, averaging near/deep per size) gives

```
cost ≈ 1,257 µs + 179 µs/KB          (R² good: 4 of 5 points within 2%, 8KB within 13%)
```

implying an effective **~5.7 MB/s** whole-resource touch rate -- close to the ~5.6 MB/s
guessed from the single-resource magnitude above, now backed by five independent sizes
instead of one. This is consistent with the CRC-of-entire-content hypothesis (a per-call
validation pass over the whole resource, not the requested range) though the mechanism
itself is still not verified against PebbleOS firmware source -- what changed is the
*shape*: O(size), not O(offset), is no longer the best guess, it is what the controlled
sweep shows.

**This also reopens the original WorldTile numbers, and mostly resolves them.** Two of
the three original rows are a 75KB-resource read, so the new model predicts
1,257 + 179×75 ≈ **14.7 ms/call** for every one of them, regardless of where in the
resource each individual tile lived:

| Row | Actual | Actual /call | Predicted (75KB) | Ratio |
|---|---|---|---|---|
| 9 WorldTiles at the map's origin | 46 ms / 16 calls | 2.9 ms | 14.7 ms | **0.20x** |
| 16 WorldTiles at tile 142,52 (2/3 in) | 305 ms / 19 calls | 16.1 ms | 14.7 ms | 1.09x |
| all 144 WorldTiles | 1,984 ms / 148 calls | 13.4 ms | 14.7 ms | 0.91x |

The "depth" and "all 144" rows land within 10% of the O(size) prediction -- a good match
for a coarse, session-log-inferred number being checked against a cleanly bracketed one.
The "origin" row is the actual outlier, and it is **cheaper** than the model by 5x, not
more expensive -- the opposite of what the original O(offset) story needed to be true.

**Ruled out: it is not pack position either.** v2 declared its five sizes in ascending
order, and `pbpack.py`'s own `finalize()` assigns offsets by walking resources in
declaration order, each placed right after the last -- so the biggest resource was also
always the one with the most other content in front of it. Size and position were never
actually separated. `flashbench` v3 does: five **identical** 8KB probes (the real
WorldTile bank size), each declared behind a different amount of padding -- 0, 50, 100,
150, 200 KB. Verified directly against the built `.pbpack` (deserialized with the SDK's
own `pbpack.py`) that the probes land at exactly those offsets before trusting the run --
a first attempt filled every probe and padding file with the same content and the packer
silently deduplicated all of them onto two physical copies, which would have made the
result meaningless. Measured on `emery`, 2026-08-14, 30 calls per point:

| Preceding | Cost |
|---|---|
| 0 KB | 2,566 µs |
| 50 KB | 2,066 µs |
| 100 KB | 2,366 µs |
| 150 KB | 2,466 µs |
| 200 KB | 2,800 µs |

Flat -- 200KB/0KB ratio 1.0x, fitted slope ~1 µs/KB of preceding content, noise. Position
in the pack does not matter. This closes the question rather than reopening it: size is
confirmed as the driver against both alternatives that could have explained it away
(offset within a resource, and now position within the pack), and the "origin" row's
5x-cheaper anomaly goes back to being explained by call mix rather than position -- that
first batch of WorldTiles necessarily also paid for loading whichever atlas(es) the
initial view touched (each its own, much smaller resource, not yet resident), while
"depth" and "all 144" reused already-resident atlases and paid the full map-resource cost
on every call. Not confirmed at the individual-call level -- only the row totals survived
from that original session-log measurement -- but no longer competing against a live
alternative mechanism.

**Bottom line:** the O(offset) framing that motivated M4d's banking fix was very likely
the wrong mechanism, arrived at from one coarse measurement with an atlas-loading
confound -- but the fix it produced (split large payloads into small resources) is still
exactly correct, for the mechanism this sweep actually found: cost scales with a
resource's own size on *every* call to it, independent of both where in the resource you
read and where in the pack the resource itself sits, so the way to make reads cheap is to
keep the resources themselves small.

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

### M5 confirmed on device (`examples/savebench`)

The numbers above are from an earlier probe measuring raw `persist_write_data` calls
directly. `pnx_save` (M5) was sized against them, but had never itself run on a watch --
host tests prove its chunking against a mocked persist store, not real timing. This is
the module's own real cost, measured with `pnx_save_write`/`begin`/`step` doing their own
chunking, versioning and checksum work on top of the raw writes:

| Payload | Chunks | Cost | 
|---|---|---|
| 24 B | 1 | 4.6 ms |
| 256 B | 2 | 6.6 ms |
| 1,024 B | 5 | 18.2 ms |
| 4,000 B | 16 | 85.2 ms |

Implied **~5.4 ms/chunk** (from the 1-chunk and 16-chunk points) -- close to the probe's
"same key" figure above (5.0 ms) rather than its "rotating keys" one (7.3 ms), which
tracks: `pnx_save` always writes the same small run of keys for a given slot, never a
genuinely fresh key each time. The module's own overhead (chunk header, checksum) is
small next to the underlying persist cost, not a second cost worth separating out.

**Incremental spread** (`pnx_save_begin` + one `pnx_save_step` per frame, the 15 chunks
after the first): 15 frames, worst single frame **39 ms** -- against a ~35ms nominal free
budget and the 40ms locked frame period, that one frame used essentially the whole
period with nothing to spare. One chunk in fifteen costing 5-8x the ~5.4ms average is a
real tail, not a rounding error, and worth watching rather than dismissing as noise from
one sample.

**The core M5 claim, confirmed against a REAL notification, not a synthetic event:**
armed, then the watch was actually covered --

```
FOCUS_LOST -> save done in 4ms (ok)
```

Four milliseconds, against the ~297ms `will_focus` warning above -- enormous margin, and
the first time this specific claim ("save-on-blur finishes before the app is actually
covered") has been checked against real firmware rather than reasoned from the probe's
297ms/106ms comparison. The reported total covered duration for this run is not quoted
here -- a bug in the benchmark (timing from when the event was *processed* rather than
its own delivery timestamp) meant it could understate a throttled app's true covered
time, and is fixed for the next run. It does not affect the 4ms figure above, which is
bracketed entirely inside one callback with no dependency on event timing.

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
- **Timers are not throttled with rendering.** Music kept playing at full speed and full
  tempo through a notification on device — no slowdown, no gap. The 0.4fps figure is the
  *render* rate; the audio timer keeps its own cadence. This is the payoff for moving audio
  off the render loop onto `app_timer`: had it stayed there, every notification would have
  stalled the mixer for seconds. It also means the sequencer's 4-row catch-up clamp never
  fires for notifications, only for something that stalls timers outright.

**Corroborated on the real module**, not just this probe: `examples/audiotest`, extended
to snapshot `PnxAudioStats` across a real `FOCUS_LOST`/`FOCUS_GAINED`, was covered by a
real notification with music playing and reported no change worth trusting either way --
the run's own event-timing had a bug (see "M5 confirmed on device" above; the same
`ev.time_ms` fix applies here) that could make a genuinely long cover read as near-zero,
so the quantitative delta is not reported. What is worth recording: nothing paused,
stopped or audibly glitched during the cover, by direct observation while it happened.
Consistent with the finding above; not yet a second independent confirmation of it.

## Combined load: does everything at once fit the frame? (`examples/stressbench`)

DESIGN.md's open questions used to name this directly: every subsystem's cost is measured
alone; the sum, everything running at once against one frame budget, never was.
`examples/stressbench` runs a synthetic full-screen graphics load (a fill_rect
checkerboard, not a real tilemap -- that is already measured on its own above) plus a
continuously looping audio sample plus a glyph HUD line every frame, and fires a real
~2000-byte incremental save (one `pnx_save_step` per frame, ~8 chunks) every three
seconds on top of all of it. `emery`, 2026-08-14:

| | Worst frame | Audio worst-gap trend |
|---|---|---|
| No save step active | 28 ms | -- |
| Save step active | grew 28 -> 31 -> **43 ms** over ~15 save cycles | 43 -> 51 -> 54 ms |
| Audio restarts (`left_playing`) the whole run | **0** | -- |

**A save step landing in the same frame as graphics and text measurably costs more, and
it crept up rather than staying flat** -- 28ms early in the run, 43ms by the fifteenth
save cycle, against a 35ms nominal free budget and a 40ms locked frame period: that worst
frame used effectively the whole period. The audio timer's own worst gap moved in step
with it (43 -> 51 -> 54ms across the same cycles that produced the worse frame times) --
a real, if modest, contention signal: heavy frame-side work does measurably compete with
the audio timer for the CPU, exactly the mechanism `MEASUREMENTS.md`'s own audio section
already names as a risk ("a 10ms audio timer measured at 54-59ms on device" when
something else flooded the event loop).

**And it never produced an audible glitch.** `left_playing` (a restart the ear would
catch) stayed at exactly 0 for the entire run, across every save cycle including the ones
with the worst frame times. The stream's buffer depth is what absorbs a longer gap
without it becoming silence -- see "Streaming audio" below for why a gap and an audible
glitch are different claims. So the honest read is: combined load is a real, growing cost
worth watching (the 43ms frame is not nothing), but it did not cross into the failure
mode -- audible discontinuity -- this milestone actually cares about, at least across
this one run's ~15 cycles.

**Not yet done:** why the worst frame grew over the run rather than staying flat is
unexplained -- thermal, heap fragmentation in the mixer, or something specific to a
particular chunk index are all plausible and none is confirmed. A longer run, and reading
which chunk index correlates with the worst frames, would settle it.

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


---

# Content and encoding

*How art, maps and text are stored, and why each format won.*

## Tile encoding: 16x16 with metatiles, measured against the alternatives

Re-measured after mirror-aware dedup landed, because that change moved the numbers the
original metatile decision rested on. Five full sheets, mirrors collapsed in every column,
so the comparison is fair:

| Sheet | Unique 16x16 | Quadrants, exact | Quadrants, with flips |
|---|---|---|---|
| world | 439 | 1,334 | 1,306 |
| dungeon | 403 | 1,294 | 1,243 |
| exterior | 441 | 1,389 | 1,359 |
| interior | 443 | 1,368 | 1,327 |
| ship | 367 | 1,101 | 1,050 |
| **pooled** | **2,093** | **6,486 (1.29x)** | **6,285 (1.33x)** |

**Quadrant reuse is 1.29x, not 1.96x.** The earlier figure predates mirror-aware dedup, and
the two optimisations overlap: a quadrant that is another's mirror was already being
collapsed before metatiling saw it. Introducing mirror dedup therefore *reduced* what
metatiling adds -- worth knowing generally, since stacked size optimisations are rarely
additive.

### Total cost, atlas plus map

Map cells are u16, so a native 8x8 grid needs four times the cells for the same world area.
`world.png` at three map sizes:

| Map cells | Flat 16x16 | Metatiled | Metatiled + quadrant flips | Native 8x8 |
|---|---|---|---|---|
| 768 (32x24) | 57,728 | 47,736 (-17%) | **46,840 (-19%)** | 47,936 |
| 12,000 | 80,192 | 70,200 (-12%) | **69,304 (-14%)** | 137,792 |

**Metatiled 16x16 wins at every scale, and native 8x8 collapses as maps grow.** Four times the
cells beats any atlas saving the smaller grid can offer -- at 12,000 cells native 8x8 costs
twice the metatiled encoding. Tile size is already a per-atlas manifest setting, so `tile = 8`
remains available; it is simply the wrong default for map-heavy content, which an RPG is.

Sprites are unaffected either way: sprite frames carry their own dimensions and are not tied
to the tile grid. The hero is 16x24.

### Palette-swap dedup: sound idea, wrong art

A tile that is another tile recoloured needs no second bitmap -- store the shape once and let
each use name its own palette. Detected by numbering colours in first-appearance raster order,
which makes the index pattern a signature independent of the colours themselves.

| Dedup key | Tiles | Bytes |
|---|---|---|
| exact pixels | 1,451 | 185,728 |
| exact + mirrors (current) | 1,380 | 176,640 |
| shape only | 1,430 | 183,040 |
| **shape + mirrors** | **1,359** | **173,952 (-1.5%)** |

**1.5%, and nearer 1.3% net** once each swap's own palette is counted -- as a *size optimisation
applied to arbitrary art*. That is the wrong frame to judge it in, and this measurement should
not be read as a verdict on the feature.

**It is an import affordance, not a compressor.** These are commercial asset packs where every
tile was drawn individually, so shapes almost never recur. Art authored knowing the pipeline
collapses recolours looks completely different: export a sheet and its variants, and the second
one costs a palette instead of a bitmap. The measurement describes the input, not the ceiling.

The economics on *deliberate* variants are large and worth stating, because they are what the
feature is for:

| | Cost now | With shape dedup |
|---|---|---|
| Recoloured 16x16 tile | 128 B | ~0 B + a shared palette |
| Recoloured sprite (3 frames, 16x24) | 576 B | ~16-32 B |
| Recoloured 48-tile tileset | 6,144 B | tens of bytes |

**Sprites are the cheap half and the runtime already supports them.** `frame_palette[]` is per
frame and `PnxSpriteInstance.palette` is per instance, so a palette-swapped enemy already
renders correctly -- that decision shipped. What never shipped is the authoring side:
`finish_sprite` performs no frame dedup at all, not even exact, so two recoloured sheets store
both sets of pixels. Closing that is **pipeline-only work with no engine change**, unlike the
tile side which still needs the reserved palette bits wired and a per-map palette table.

Two requirements for it to deliver, both learned above: report the saving on every build, since
a feature whose value depends on artists knowing it exists has to teach itself; and apply the
colour floor, so an accidental shape collision is not silently merged and reported as intent.

**The match is a colour-to-colour bijection, not an offset.** Each distinct source colour takes
the next index on first appearance, so two colours can never collapse into one and an arbitrary
recolour is caught -- green to brown while the outline stays black. An offset would also be
meaningless here: GColor8 is ARGB2222 packed bit fields, so adding 1 steps blue and then carries
into green. Numeric proximity has no perceptual meaning.

**Transparency must be pinned to index 0**, not numbered by raster order. With it free, two tiles
can match where one's transparent pixels align with the other's opaque ones, and storing a single
bitmap for both renders one with holes. On these sheets pinning changes the count by zero, so the
requirement is latent rather than benign -- exactly the shape of bug that ships because the test
data never triggers it.

**And most of the 1.5% is coincidence.** Only 16 signature groups are shared by more than one
distinct tile, and filtered by how much colour a group actually has:

| Minimum colours | Groups |
|---|---|
| >= 2 | 16 |
| >= 4 | 14 |
| >= 6 | **3** |

At any threshold where a match plausibly means "the artist recoloured this", there are three. The
rest are simple low-colour tiles sharing a spatial pattern -- lossless to merge, but not evidence
of palette-swapped art. An editor reporting these needs a colour floor, or it will claim two
unrelated flat tiles are related.

Note the measurement trap: mirroring a tile changes raster order, so the signature of a
mirrored tile is NOT the mirror of its signature. It has to be re-derived per orientation. The
first attempt got this wrong and reported shape+mirrors as *worse* than exact+mirrors, which is
impossible for a strictly more aggressive key -- the contradiction is what exposed the bug.

### Where these optimisations live

Each one is pipeline analysis plus one field in the map entry plus a few lines in the blitter.
The pipeline can only exploit redundancy the map format can express:

| Optimisation | Pipeline | Map entry | Blitter |
|---|---|---|---|
| Mirror dedup | yes | flip bits | done |
| Metatiles | yes | atlas-level | done |
| Palette swap | yes | palette bits (reserved) | **not wired** |

`tile_palette[]` holds one palette per tile, so two cells sharing a shape with different
palettes need the cell to carry the palette. That is what the four reserved bits are for, and
`pnx_tilemap_draw` ignores them today. Four bits is fifteen overrides, against 37 merged
palettes -- so it would want a per-map palette table, which is the GBC's 8-per-bank model.

### What this changed

Metatiles as implemented save **9-18%** depending on map size, against a threshold of 25% --
so auto-selection could never fire. The threshold was calibrated on 1.96x reuse and is now
**0.12**, and is overridable per atlas: `metatiles` takes `true`, `false`, `"auto"`, or a
fraction that sets that atlas's own threshold. Reuse is a property of how the art was drawn,
so an artist tuning one tileset should not have to argue with a global constant.

### Metatiles work by sharing ACROSS tiles, not by symmetry within one

Distinct quadrants needed per 16x16 tile, over the 2,093 unique tiles in five sheets:

| Quadrants | Exact | With flips |
|---|---|---|
| 1 | 1.3% | 1.5% |
| 2 | 2.4% | 3.5% |
| 3 | 7.8% | 9.1% |
| 4 | **88.5%** | 85.9% |

A 4-way symmetric tile does collapse to a single quadrant referenced four times -- 40 bytes
against 128 -- but **only 1.5% of real tiles are built that way**, and 86% need all four
distinct. Were quadrants shared only within a tile, metatiling would be a net LOSS: 270,888
bytes against 267,904 flat, because the 8-byte definition outweighs the rare symmetry.

So the entire saving comes from quadrants recurring across *different* tiles -- one grass
corner in forty tiles -- which is also why the effect scales with sheet size. Five full sheets
give 1.29x; a 60-tile carve gives 1.04-1.12x. **Metatile value is a function of how many tiles
share the atlas, not of how the individual tiles are drawn.**

**Quadrant flips would add 2-4 points and cost nothing.** Storing a quadrant once and
referencing it flipped brings reuse from 1.29x to 1.33x, and the flip bits are free: 6,486
quadrants needs 13 bits of the u16 table entry, leaving three spare. Not implemented yet.

Given the distribution above, the size case for quadrant flips is thin -- they barely move
within-tile reuse and only slightly improve cross-tile collision odds, and the palette
constraint will erode even that. **The reason to implement them is parity, not size:** a
metatiled atlas cannot honour map-level flips at all today, so placing a mirrored tile in an
editor would force a choice between metatiles and flips on the same atlas.

It would also un-block flipping a whole metatile from the map, which is currently refused.
That is not a draw-order problem as first assumed -- it is a 2-bit permutation. Flip X reads
quadrants (TR, TL, BR, BL) instead of (TL, TR, BL, BR) with each one's X bit toggled, so once
the table carries flip bits both cases fall out of the same code. The 35%
render cost it buys is affordable: 14.5% of the frame becomes 19.7%, and resources are the
binding constraint while frame time is not.

On the small carved regions the examples use, reuse is 1.04-1.12x and metatiles still lose --
correctly declined. The pipeline reports the verdict and the margin on every build.

## Map format: u16 cells

Map cells were one byte of tile index. They are now u16, which is what lets the pipeline's
deduplication survive into the map:

| Bits | Field |
|---|---|
| 0-9 | tile index (1,024, up from 255) |
| 10 | flip X |
| 11 | flip Y |
| 12 | rotate (transpose) |
| 13 | warp |
| 14 | extended (a per-cell tag in a WorldTile side table) |
| 15 | free |

Bits 12-15 were "reserved -- per-cell palette index" when this section was first written, and
never wired that way -- `tile_palette[]` (a per-*tile*, not per-*cell*, remap) covered the
recolouring case that would have wanted them first. What actually landed there, later, is
unrelated to a palette: see [Tile-driven collision, rotate and EXTENDED
tags](#tile-driven-collision-rotate-and-extended-tags) for the whole story and its own
footprint numbers. Collision and warp both used to be a THIRD table alongside this one
(a map-owned flag plane, one byte per map-global tile id, described lower in this section) --
that table is gone; warp and rotate are cell bits now, and collision moved to being a
property of the ART TILE instead of the map.

The ten bits index the **map's** id space rather than one atlas's, which is what lets a map
draw from several tilesets without spending a bit per cell on saying which — the map's atlas
table partitions the range instead. Blob v8 when these numbers were first taken, **v12 now**
-- the layout around the cells has moved on several times since, but the cell's own ten
tile-index bits have not.

Doubling the cells costs 3,223 bytes across the two example maps, up from ~2,071. That buys 128
bytes back for every tile a mirrored pair no longer needs its own copy of, and the flip bits are
what make mirror dedup expressible at all -- a pipeline can only exploit redundancy the format
can carry.

Flip cost 60 bytes of blitter (1,382 to 1,442). Half of it already existed as a `mirror` bool
for sprite facing; that became a `uint8_t flip` with `PNX_FLIP_X == 1`, so every existing `true`
still means the same thing and no call site changed. **Flip Y is one index inversion** choosing
the source row -- no second span writer, no per-pixel cost.

A per-cell palette remains unbuilt, and no longer has bits reserved for it specifically --
`tile_palette[]`'s existing per-*tile* remap (see "Palette variants" below) already covers
the case a per-cell one would have, at a fraction of the cost: one byte per art tile in a
map's own atlas rather than one nibble per cell of the whole map. Fifteen distinct remaps
(what four bits would give) against 37 merged palettes was never going to be enough headroom
to be worth spending a cell bit on.

**`PNX_BLOB_VERSION` must be bumped in `pnx_assets.h` and `pnx_assets.py` together.** Changing
only the pipeline made the runtime guard refuse every blob, which is correct behaviour -- but the
host test then continued past the failed load and segfaulted on a NULL palette table, which
reads as a code bug and is not one.

## Tile-driven collision, rotate and EXTENDED tags

Collision moved from a hand-typed `PnxSegment`/flag-byte-by-id scheme to a property of the ART
TILE: `[[atlas.collision]]` declares SOLID (the whole tile), SCALED (an inset rect) or COMPLEX (a
1bpp mask, defaulting to the tile's own opacity or authored explicitly) once per tile, baked into
`PnxAtlas.tile_flags` -- a byte that existed and was already loaded by the C reader since M4d but
had no consumer anywhere in the engine before this. The cell's own u16 gained ROTATE (transpose,
wired into `pnx_blit_4bpp`) and kept WARP, both genuinely per-*placement* now rather than
per-tile-id; a new EXTENDED bit names an arbitrary per-cell tag in the same sparse WorldTile side
table the old per-cell collision overrides used to occupy. Full design in `pnx_assets.h`'s own
`PNX_MAP_ROTATE`/`PNX_COLLISION_*` comments; this section is the bill.

**Measured old vs new on `examples/stressbench`**, the one example built specifically to answer
"what does everything cost running at once" (see "Combined load" below) -- rebuilt for this with
every one of the modes actually placed in its map (SOLID *and* SCALED *and* COMPLEX tiles, a
rotated cell, an EXTENDED-tagged one) and `PNX_USE_COLLISION` on, sweeping the walking sprite's
AABB against the map for real every frame rather than declaring the module and never calling it.
Old = the commit immediately before this work (`f9eab21`); new = `HEAD`. `emery`, host `pebble
build`'s own size report, both otherwise-identical builds:

| Module | Before | After | Δ |
|---|---|---|---|
| `pnx/assets` | 3,709 | 3,841 | **+132** |
| `pnx/gfx` | 960 | 1,038 | **+78** |
| `pnx/collision` | -- | 380 | **+380 (new)** |
| `pnx/platform` | 1,849 | 1,834 | **-15** |
| `game` (stressbench's own code) | 3,784 | 3,884 | +100 |
| everything else (`audio`, `core`, `tilemap`, `text`, `save`, `sprites`, libc) | 5,047 | 5,047 | 0 |
| **module total** | **15,413** | **16,088** | **+675** |
| **app total incl. headers/padding** | **16,256 / 65,535 (24.8%)** | **16,944 / 65,535 (25.9%)** | **+688 (1.05%)** |

**`pnx/collision` is the whole cost of turning the module on**, not a per-call marginal --
`pnx_collision_move`'s axis-separated sweep plus `pnx_aabb_overlap`, linked because game code
actually calls it now. `pnx/assets` (+132 B) is the collision-mode/rotate/EXTENDED parsing and
the new `pnx_atlas_tile_scaled_rect`/`pnx_atlas_tile_complex_mask`/`pnx_map_extended` accessors --
paid by every project regardless of whether a single manifest declares a SCALED tile, since none
of it is `#if`-gated (collision is now a normal part of loading an atlas, not an opt-in feature).
`pnx/gfx` (+78 B) is `PNX_FLIP_ROTATE`'s per-pixel path in `pnx_blit_4bpp`. **`pnx/platform`
actually got smaller** (-15 B): the BACK-button investigation this work's pinball example forced
(see `pnx_platform_pebble.c`'s `back_click`) replaced a raw click handler that manually tracked
`s_screen_locked` and called `pnx_platform_quit()` with a single-click handler the SDK's own
default dismissal already covers -- a real simplification, not noise.

**Resource bytes, same before/after pair, printed by the pipeline's own budget report:**

| Resource | Before | After | Δ |
|---|---|---|---|
| atlas `tiles` | 792 B | 836 B | **+44** |
| map `field` (resident preamble) | 52 B | 44 B | -8 |
| bank `field_0` (WorldTile payloads) | 848 B | 856 B | +8 |
| **total** | **5,230 B** | **5,274 B** | **+44** |

The atlas's +44 B is exactly one SCALED rect entry (6 B) plus one COMPLEX mask entry (2 + 32 B
for a 16x16 tile's `(16*16+7)/8` packed bits) plus their two u16 counts -- the sparse tail tables
`finish_atlas` appends, present at all only because this atlas actually declares both modes. The
map and bank numbers move in opposite directions by the same 8 bytes: the old map-owned flag
table and its per-WorldTile override bytes are gone (every map gets smaller for free), and the
one EXTENDED-tagged cell in this map's WorldTile adds its own sparse entry back (2-byte count +
3-byte `x,y,value` triple) -- a wash here, but only because this map happens to use exactly one
tag; a map using none would show the map/bank saving with nothing to offset it, and a map using
many would show the reverse.

**`pnx/physics`, measured directly rather than by comparison** (there is no "before" -- pinball
is a new example): 622 B on `emery` in a real, shipped `.pbw`, with `examples/pinball/game.c`
actually calling `pnx_physics_collide_segment`/`pnx_physics_collide_flipper` every tick against
real table geometry, not merely declaring `PNX_USE_PHYSICS`. Like `pnx/collision` above, this is
the cost of the whole module once anything in a project calls into it -- a game that never sets
`PNX_USE_PHYSICS`/`PNX_USE_COLLISION` pays none of either.

**In one line: the tile-driven collision format itself costs ~1% of the 64KB app ceiling and a
handful of resource bytes per collision-carrying tile, and the two new opt-in modules cost
roughly 380-620 B each, only when a project actually calls into them.** Consistent with M4d's own
finding for WorldTile streaming -- a framework feature is cheap in the aggregate and free for a
project that never touches it, because none of this is `#if`-gated except the two genuinely
optional physics/collision modules.

## WorldTile streaming (M4d)

`examples/worldtiles` is a 192x192 field -- 73,728 bytes of cell plane, more than half
`emery`'s app RAM -- built specifically to exercise the streaming path a smaller example
never touches. Design in `docs/ROADMAP.md`'s M4d; the numbers below are what confirming it
on hardware actually found.

**The RAM design landed exactly as measured.** 23,514 B streamed against 97,351 B held
whole, 26.8fps held while walking, the streamer never once behind (`missing` stayed 0 at
eight WorldTiles/tick), `Still allocated <0B>` at exit.

### The flash-read model was wrong

Loads came in **50-280x over the predicted figure**, and the multiplier grew with how deep
into the resource the read started: the same 16-WorldTile window cost 46 ms near the map's
origin and 305 ms two thirds of the way through it. That points at
`resource_load_byte_range` being **O(offset)**, not O(length) -- streaming from the start
of the resource on every call. The original ~29 µs/call figure (see Flash / resource reads
above) could not have caught this: it was measured over a 16KB resource where every offset
is small.

In practice: walking itself was unaffected, crossing a WorldTile boundary dropped one frame
(47-63 ms against 8 ms of ordinary work, 24.2fps while sprinting), and scene loads paid for
it directly -- 305 ms for a warp into the middle of the field, 2 s to hold the world whole.

### The fix: banks, and batched runs

Two changes, aimed at the term that actually dominates:

- **WorldTile payloads left the map's resource for bank resources of ~8KB**, whose asset
  ids run consecutively from a `first_bank_asset` in the map header. A seek is now capped
  by the bank rather than by the map -- 4KB instead of 74KB on the field -- and the map's
  own resource dropped from 75,232 bytes to 1,000.
- **Payloads are padded to the pool's slot stride.** A WorldTile's home becomes arithmetic
  (bank `i >> bank_shift`, offset `(i & mask) * slot_bytes`), which drops the per-tile
  offset table, and makes a run of consecutive WorldTiles contiguous at both ends -- in the
  bank and in the pool -- which is what turns a run into one **batched** ranged read: a
  whole-map load became one read per bank instead of one per tile (18 against 144).

Confirmed on hardware, same watch, same content:

| | Before | After | |
|---|---|---|---|
| hold the 192x192 world | 1,984 ms | **74 ms** | 26.8x |
| warp into the middle of the field | 305 ms | **12 ms** | 25.4x |
| worst frame while walking | 47-63 ms | **8-12 ms** | 5.2x |
| frames dropped crossing a WorldTile | one, every time | **none** | -- |

The byte count barely moved and the call count only fell 148 to 41 -- neither explains a
27x drop. What changed is how far into a resource each read starts: **1.8 ms/read against
13.4 ms** before. Walking now holds 26.8fps, the PT2 ceiling, with the worst frame at 12 ms
of a 37.33 ms budget.

One thing to keep watching: the streamer's backlog reached 3 WorldTiles crossing a WorldTile
*corner* (tiles needed in two directions at once). No holes, no dropped frame, so
`PNX_MAP_STREAM_BUDGET` at 4 has room to spare. Still wanted: a flash probe that sweeps
offset independently of length -- the before/after above is strong but still read off
session logs, not a dedicated instrument.

A side effect worth having: `field` and `plain` (the same world, drawn two ways) have
byte-identical banks, so the `.pbpack` deduplicates them -- two 192x192 worlds for 103 KB
rather than 177 KB.

### WorldTile size is chosen, not defaulted

`worldtile = "auto"` picks the size by arithmetic, the same bargain the atlas `metatiles`
key already offers: the pool grows as the SQUARE of the size, the per-slot descriptors
follow it, and the per-WorldTile lookup array grows as the size SHRINKS -- and which way the
answer goes depends on whether the map streams. A streaming map holds a fixed window, so a
bigger WorldTile means a bigger margin ring of world nobody can see (wants *small*); a map
held whole has no ring and every term scales with the count (wants *large*).

On the 192x192 field, 200x228 screen, 16px tiles:

| | Picked | Resident | Was (fixed 16) |
|---|---|---|---|
| streaming | **8** | 17,967 B | 22,491 B |
| held whole | **32** | 93,715 B | 94,255 B |

The streamed scene's WorldTile cost halved (8,720 B → 4,376 B), and it exposed that
WorldTiles were never the expensive part of a streamed scene: the atlas pool is a flat
12,496 B of the field's 18 KB, so the remaining footprint question is about tilesets, not
tiling.

**A batching bug this exposed:** `PNX_MAP_STREAM_BUDGET` counted WorldTiles, but a run of
consecutive ones is a single read -- so a batched fetch was charged four or five times over
and the streamer ran at a quarter of the I/O it was actually paying for. Invisible while
WorldTiles were large and few; on device at eight tiles/tick the backlog went from 3 to
**12** the moment the pipeline started choosing smaller ones, for no extra reads. The budget
now counts reads, not WorldTiles; `tests/test_stream.c` sprints 40 and asserts the backlog
stays at zero.

### Is it worth the space?

The engine cost is **3,076 bytes** (`pnx/assets` +2,596, `pnx/gfx` +480). Against the RAM it
buys, on a 16px grid and a 200x228 screen:

| Map | Streamed | Held whole | |
|---|---|---|---|
| 16x16 | 537 | 537 | fits its pool: held whole, streaming never runs |
| 32x24 | 1,836 | 1,836 | same |
| 48x48 | 2,888 | 4,833 | 1,945 B cheaper |
| 96x96 | 3,320 | 18,657 | 15,337 B cheaper |
| 192x192 | 4,376 | 74,628 | **70,252 B cheaper** |
| 255x255 | 4,824 | 132,672 | 127,848 B cheaper |

Streaming never costs more RAM than holding whole, at any size: below the pool it's the same
number (a map that fits its pool IS held whole, streaming never executes), above it,
streaming is strictly cheaper by a margin that grows quadratically. Streamed residency is
O(screen); held-whole is O(map area). So the 3,076 bytes is the only cost, paid once by the
framework rather than per map -- and it passes back almost immediately on any map above
roughly 48x48 cells, running away with it at 192x192 (23x over).

Two functional bugs `tests/test_stream.c` found along the way (a warp deadlocking the
atlas pool, and a "resident" map still reading flash lazily) are recorded with
`docs/ROADMAP.md`'s M4d rather than here, since they are bugs in behaviour rather than
measurements.

### The resource ceilings this made newly reachable

Three limits, two of them spelled 256, and the pipeline was only reporting one:

| | | |
|---|---|---|
| `MAX_RESOURCES_SIZE_APPSTORE` | 256 KB | bytes in the whole `.pbpack` -- a warning |
| `MAX_RESOURCES_SIZE` | **1 MB** | bytes -- the hard error |
| pbpack `table_size` | **256 entries** | resources, whatever they weigh |

The byte limits apply to the pack as a whole (`os.stat(resources_path).st_size` in the
SDK's `report_memory_usage.py`), not to any single resource. The entry count is the one
banking made newly reachable: a map is now one resource plus a bank per few WorldTiles, so
a project of large maps runs out of *entries* long before it runs out of bytes -- and
exceeding it was a bare traceback out of the SDK's packer. The budget report now prints
both ceilings and the entry count, and refuses a build that would overflow the table.

## Palette variants: one atlas, many zones

Measured on the example's cave tileset:

| | Bytes |
|---|---|
| A second copy of the atlas | **5,632** |
| The variant instead: palette table in the map | **44** |
| Plus one new shared palette | 16 |
| Code, in `pnx_tilemap_draw` | **44** |

**128x**, and it is the largest content lever in the pipeline. A map optionally carries one byte per
atlas tile naming the palette slot to use instead of the atlas's own; the pixel data is the atlas's
either way. One branch and one index per tile blit.

Positional palette order is what makes it work. An ordinary palette is a set that `palette_bytes`
sorts and `pack_unit_4bpp` indexes by the same sort -- so two recolours sort differently and would
pack to different pixel data, sharing nothing. Variant palettes are `Ordered`, pinning index k to
entry k, and `merge_palettes` leaves them alone: merging one would reorder it and silently remap
every pixel of every zone using it.

The pipeline refuses a variant that is not a consistent recolour -- a moved pixel, a change in what is
transparent, one base colour mapping to two, or two base colours flattened into one. Each of those
would corrupt shared pixel data rather than merely look wrong.

## Font costs (E7)

Measured on `examples/overworld` with Liberation Sans, glyph sets derived from the
manifest's dialog pages:

| Face | Size | Depth | Glyphs | Bitmaps | Blob |
|---|---|---|---|---|---|
| `hud` | 12px | 1bpp | 40 | 331 B | **757 B** |
| `dialogue` | 16px | 2bpp | 27 | 580 B | **902 B** |

Three things the numbers show:

- **`charset = "auto"` is most of the saving.** The HUD face carries 40 glyphs because
  that is what the dialog pages plus `extra` actually use. All 95 printable ASCII would
  roughly double it for characters the game never draws.
- **The index is a real fraction of a small font.** 8 bytes a glyph is 320 B of the HUD
  face's 757 — bigger than a third of it. That is the price of trimming glyphs to their
  inked box and carrying per-glyph bearings, and it is still the cheaper side of the
  trade: uniform cells cost more in bitmap than they save in index, and the margin widens
  with size.
- **2bpp is not 2x.** The dialogue face is 27 glyphs at 16px against 40 at 12px, so the
  depths are not directly comparable — but the bitmap block is 580 B for 27 antialiased
  glyphs against 331 B for 40 crisp ones, which is roughly the doubling per glyph you
  would expect and no worse.

Engine cost of the whole feature: **+662 B in `gfx`** (the blitter, both span writers, the
32-byte blend table, wrapping and measuring) and **+993 B in `assets`** (the loader, its
validation, and two more scene slots). The overworld example went from 12,208 to
**14,784 of 65,535 bytes (22.6%)**.

## Glyph blitter vs SDK text: measured, on device (`examples/textbench`)

Every figure touching the glyph blitter's own cost until now was inferred from a
whole-FRAME total in an app that was also doing other work at the same time (the ~4.3 ms
SDK figure above, `~4.3 ms a call` in the audio section below) -- bounding the answer
without isolating it. `examples/textbench` exists to isolate it: the same one-line
HUD-shaped string (`"LV5  HP 42/60"`, 13 chars, matching Gothic 14 / `PNX_TEXT_SMALL`)
drawn through both paths, back to back on the same watch, each call bracketed with
`pnx_platform_now_ms()` and repeated many times to smooth the 1ms clock (see
"Manufactured precision" above) -- 270 SDK calls and 2,700 glyph calls per run, interleaved
in three rounds (SDK, glyph, SDK, glyph, SDK, glyph) so a slow drift across the ~8s run
lands on both conditions rather than favouring whichever went first.

**(replicated, 3 runs)** on `emery`, 2026-08-14:

| | Run 1 | Run 2 | Run 3 | Mean |
|---|---|---|---|---|
| SDK text (`pnx_platform_text_draw`) | 1,374 us | 1,451 us | 1,277 us | **1,367 us** |
| Glyph blit (`pnx_text_draw`) | 147 us | 146 us | 146 us | **146 us** |

**SDK is the noisier of the two by a wide margin** -- 174 us run-to-run spread (13% of its
mean) against the glyph blitter's 1 us (0.7%), consistent with one being a real OS text
layout call and the other a tight blit loop with nothing else to vary. Run 3's own three
rounds show the same pattern in miniature and confirm the interleaving is doing its job:
SDK 1,355 / 1,222 / 1,255 us against glyph's 147 / 144 / 146 us -- the SDK side visibly
noisier round to round, the glyph side barely moving, in the same run.

**The glyph blitter costs ~146 us/call. SDK text costs ~1,367 us/call for this string --
9.4x more.** At the locked 37.33 ms frame floor that is 0.4% of a frame for the blitter
against 3.7% for SDK text.

**This does not simply replace the ~4.3 ms figure elsewhere in this document.** That
number came from `examples/audiotest`'s pre-E7 HUD, which drew a taller, multi-line block
through 5 separate `graphics_draw_text` calls with word-wrap -- more glyphs and more
layout work per call than one short line. SDK text's cost scales with content the way any
text layout does; 146 us for the glyph blitter is the isolated, controlled number, and the
~4.3 ms figure remains the honest cost of what that specific older HUD actually drew. The
two are different content, not a contradiction -- and the ratio (SDK several times more
expensive than the blitter, whatever the string, and noticeably less consistent call to
call) is the number that generalises, not either absolute figure alone.

**What this settles for E7:** the roadmap's "should be far cheaper... but that is
reasoning, not a number" is now a number. A game drawing HUD text every frame through the
glyph blitter pays a fraction of a percent of the frame budget for it, reliably; the same
text through the SDK hook would cost several percent AND vary by double digits run to run,
and can only run after the framebuffer is released besides.

### Does it scale with glyph count? Yes, on both paths -- but from very different starts

The single-string number above answers "how much does one call cost", not "is that call
mostly overhead or mostly content" -- and a game draws text of very different lengths in
very different places, so that distinction matters more than the headline ratio does.
`examples/textbench` was extended to run three string lengths (5, 13 and 20 glyphs, the
13-glyph one being the exact string measured above) through both paths in one run:

| Glyphs | SDK | Glyph blit | Ratio |
|---|---|---|---|
| 5 | 958 us | 57 us | **16.8x** |
| 13 | 1,466 us | 149 us | 9.8x |
| 20 | 1,733 us | 177 us | 9.8x |

Per call, deliberately -- the app batches 3 SDK calls and 30 glyph calls per frame (the
SDK side is already ~2.9ms/frame at that batch size; the glyph side is cheap enough that
more calls just buys better averaging), so a subphase's TOTAL wall-clock time is not this
ratio: 120 SDK calls at 958us against 1,200 glyph calls at 57us is 114,960us against
68,400us, only 1.68x, because the glyph subphase did 10x the draws in that same wall time.
That is the batch-size choice showing through, not a second finding -- the per-call number
is the one that means anything for a game, since it answers "what does drawing this string
once cost", independent of how many times either path happens to run in a frame.

Single run, not yet replicated the way the 13-glyph figure above is -- but the 13-glyph
column here (1,466 us / 149 us) lands close to that run's 1,367/146 us mean, well inside
the ~13%/~1% run-to-run noise already characterised, so it is read as consistent with it
rather than a contradiction.

**Both paths scale with length -- neither cost is flat per-call overhead.** A two-point
fit between the 5- and 20-glyph tiers (not a regression; three points is thin for one, but
enough to separate "mostly overhead" from "mostly content"):

| | Fixed, per call | Marginal, per glyph |
|---|---|---|
| SDK text | **~703 us** | ~51 us |
| Glyph blit | **~17 us** | ~8 us |

**The SDK's fixed cost alone (~703 us) is already larger than the glyph blitter's ENTIRE
cost at 20 glyphs (177 us).** That is what the ratio column above is actually showing:
it is not roughly constant, it is highest for the SHORTEST string (16.8x at 5 glyphs) and
settles toward ~9.8x as strings get longer, because SDK's large fixed cost dominates a
short call disproportionately while the marginal, per-glyph term is where the two paths
are closest (~51 us against ~8 us, still 6.4x, but the least dramatic comparison in this
data). A two-digit HP number is the case the glyph blitter wins hardest; a full dialogue
line is where it wins least, and even there it is still an order of magnitude.

### 2bpp (antialiased) against 1bpp: confirmed more expensive, still an order of magnitude better than SDK

`examples/textbench` was extended again to add a second font built at `depth = 2` (the
same setting resonant's actual dialogue face uses) alongside the existing `depth = 1`
one, run through the same three string lengths in the same session. `emery`,
2026-08-14, a separate run from the SDK-vs-1bpp table above:

| Glyphs | SDK | Glyph blit 1bpp | Glyph blit 2bpp |
|---|---|---|---|
| 5 | 816 us | 61 us | 64 us |
| 13 | 1,333 us | 152 us | 194 us |
| 20 | 1,641 us | 190 us | 250 us |

Two-point fit (5/20 glyphs, same method as above):

| | Fixed, per call | Marginal, per glyph |
|---|---|---|
| SDK text | ~541 us | ~55 us |
| Glyph blit 1bpp | ~21 us | ~8 us |
| Glyph blit 2bpp | ~4 us | ~12 us |

This SDK run (816/1,333/1,641) landed noticeably below the earlier one (958/1,466/1,733)
-- another data point for "SDK is the noisier path," not a new finding. The 1bpp glyph
numbers (61/152/190) sit close to the earlier 1bpp-only run (57/149/177), consistent with
the tight run-to-run spread already characterised for that path.

**2bpp costs 5-32% more than 1bpp, growing with string length** -- 1.05x at 5 glyphs,
1.28x at 13, 1.32x at 20 -- which is what "reads the destination, blends three channels"
predicts: a per-PIXEL cost that scales with how much is drawn, not a fixed tax per call.
The marginal-cost row shows it cleanest (2bpp ~12us/glyph against 1bpp's ~8us/glyph, a
50% premium) and is the more trustworthy of the two fitted numbers -- the fixed-cost split
is noisy at this sample size, visibly so here: it puts 2bpp's fixed cost BELOW 1bpp's
(~4us against ~21us), which has no physical explanation and is exactly the kind of
two-point-fit artifact the method's own caveat warns about when the two tiers land close
together, as they do here at 5 glyphs (64us vs 61us, 3us apart).

**Even 2bpp remains an order of magnitude cheaper than SDK text at every length tested**
-- 12.8x at 5 glyphs, 6.9x at 13 (resonant's dialogue face's own depth), 6.6x at 20. The
E7 verdict in `ROADMAP.md` holds at both depths.

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
audio, which was long the category with no measured cost; the sequencer's byte costs
are below, and the synth's CPU and RAM are measured under "Synth CPU".

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
| metatiles, *after* mirror dedup | 6,285 | **1.33x** | see below |

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

So auto-selection requires a minimum saving before choosing metatiles, not merely a positive
one. Small atlases stay flat; large ones pay the render cost to buy back content budget.

**Both figures above are superseded.** They predate mirror-aware dedup, which overlaps with
quadrant dedup and reduced what metatiling adds -- the real saving is 9-18% and the threshold
is now 0.12, overridable per atlas. See "Tile encoding" above for the re-measurement; this row
is kept because the 42% figure is quoted elsewhere and should not be trusted.

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
| Default (`PNX_USE_DIAGNOSTICS=1`) | **6,392 B** (9.8%) | platform 1,369 + core 3,743 + game 446 |
| `PNX_USE_DIAGNOSTICS=0` | **2,468 B** (3.8%) | core falls to 92 B — the arena alone |
| M2 `examples/overworld` | **8,580 B** (13.1%) | adds assets 824 B + a naive renderer |

The delta is the point: switching one module off reclaims **3,920 bytes**, including the
deferred log ring's 2,304 bytes of `.bss` and the formatter that only it pulled in. That
is the compile-time module selection working as specified, not merely as intended.

The log ring is the largest single static allocation in an otherwise empty app — about a
third of its footprint — which is why `PNX_LOG_LINES` and `PNX_LOG_LINE_LEN` are tunable.


---

# Audio

*The largest unknown, and the one where the platform API had to be abandoned.*

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
time, and feeding is lead-based at 60 ms from a dedicated timer. Verified in host tests: a
stall inside the queue produces no deficit, and one beyond it is reported rather than hidden.

### Static RAM: buffers, not code

The mixer's cost is almost entirely buffers, and it took four of them before anyone looked:

| | Before | After |
|---|---|---|
| `.text` | 3,420 | 3,356 |
| `.bss` | **8,007** | **2,375** |
| Module total | 11,432 | 5,736, then **3,672** once the buffers moved to the heap |

The four were an int16 accumulator, an 8-bit output, a 16-bit widening of that output, and a
copy of whatever the device refused -- 7,168 bytes holding the same signal at four stages.
One buffer serves all of them: each stage is no wider than the last, so output overwrites the
accumulator in place, and a short write leaves its remainder where it lies with an index
marking how far the device got. Chunk also dropped from 1,024 samples to 768, which is still
ample against the ~512 a 32 ms feed needs at 16 kHz.

Worth generalising: on a platform where `.text` and statics share a 64 KB ceiling, a buffer
per pipeline stage is the expensive habit, not the code. The audiotest app fell from 21,128 to
15,420 bytes on the collapse, and to **13,384** once the single remaining buffer moved to the
heap -- **37% of the whole app** -- with no change to the audio.

## Synth CPU: what a subtractive voice actually costs (spike)

Measured on device with `examples/synthspike`, 16 kHz, four voices sounding, each
configuration rendering ~19 s of audio and divided down -- `time_ms()` is 1 ms and this is
nanoseconds, so it is timed by repetition, never by multiplying a millisecond clock.

| Configuration | ns/sample | % of a core |
|---|---|---|
| everything on | **10,732** | **17.2%** |
| bare voice (1 osc, no filter/LFO/effects) | 4,554 | 7.3% |
| silence (loop, no voices) | 468 | 0.75% |

Attributed by turning each feature off and re-running:

| | ns/sample | share |
|---|---|---|
| **four bare voices** | **4,086** | **38%** |
| filter + its envelope | 2,315 | 22% |
| reverb | 1,234 | 12% |
| 2nd oscillator | 1,055 | 10% |
| 3rd oscillator | 612 | 6% |
| LFO | 345 | 3% |
| chorus | 342 | 3% |
| loop overhead | 468 | 4% |
| resonance | 78 | 0.7% |
| pitch envelope | 13 | 0.1% |

**It fits.** 17.2% of a core is 6.4 ms per 37 ms frame against ~35 ms free -- 18% of the
budget for a full subtractive synth on four channels. RAM is 5,522 B of heap, of which
**5,266 B is the reverb and chorus delay lines**, and 3,615 B of app.

Three things worth carrying forward:

- **A bare voice is 1,022 ns -- 204 cycles for one oscillator and one envelope.** The
  largest single item is the thing no feature flag covers. The loop is sample-major
  (`for each sample: for each voice`), which cycles four voices' state through registers
  every sample; voice-major block processing is the standard fix and is untried here.
- **Resonance and the pitch envelope are free**, at 78 ns and 13 ns. A filter that sounds
  like one, and drums that are a pitch sweep rather than 16,000 bytes a second of PCM,
  both cost nothing.
- **Effects must be global sends.** Per-instrument reverb would be four sets of delay
  lines -- ~20 KB against the 5,266 B one instance costs.

### Two wrong turns, recorded because both looked right

**The pitch was recomputed per sample, per oscillator.** First device run measured 14,573
ns/sample (23% of a core), ~242 cycles per oscillator. `note_hz_q16` is a table lookup, two
widening multiplies, three divisions and a conditional shift, and it ran twelve times a
sample to support an LFO moving at 20 Hz. Recomputing every 32 samples -- a 500 Hz control
rate, 25 updates per vibrato cycle -- took it to 10,654. That one was a real 27% win.

**The filter's 64-bit multiplies were not the problem.** The filter is the most expensive
feature, and `(int64_t)a * b` three times per voice per sample looked like the obvious
cause, so it was rewritten into 32-bit Q12. It changed nothing: 10,654 -> 10,732. GCC was
already emitting `SMULL`. The rewrite was kept because clamping the filter STATE is a real
correctness fix -- an unbounded resonant integrator wraps sign and is heard as a burst of
noise, not as a loud filter -- but it bought no speed, and it was asserted from reading the
code rather than from a measurement.

### Aliasing: a 64-entry table is not enough on its own

After clipping and underrun were both counted and both zero, the audio was still harsh.
That leaves aliasing, and the arithmetic says why: a 64-entry table carries harmonics 1..32,
so at C6 twenty-five of them sit above the 8 kHz Nyquist and fold back as inharmonic noise.
Three detuned saws -- the example lead -- is the worst case there is for it.

| Note | Harmonics reaching past Nyquist |
|---|---|
| C2 65 Hz | 0 of 32 |
| C4 262 Hz | 2 of 32 |
| C5 523 Hz | 17 of 32 |
| C6 1047 Hz | **25 of 32** |

Fixed with band-limited tables, one per waveform per octave, built additively from a Q12
sine table so no libm is needed. Each octave carries only the harmonics that stay under
Nyquist at the TOP of its range -- 31 below C4, 4 above C6. Measured after: **at most 0.04%
of energy above the intended harmonic limit, at every octave.**

Costs 2 KB of tables and a table selection per pitch update, not per sample, so the render
loop is unchanged. The mip is chosen per OSCILLATOR rather than per voice, because
`octave` shifts an oscillator away from the played note and a pad an octave up would
otherwise read the wrong table.

A square with a duty other than 50% still uses the threshold comparison -- pulse width
cannot be tabled without a table per duty -- so PWM keeps its hard edges, which is the
chiptune sound and is wanted. A plain 50% square now reads the band-limited table.

### The frame rate drop was debug text, not the synth

Enabling the synth took `audiotest` from 26.8 fps to 22, and the arithmetic matched almost
exactly: the synth renders 7.9 ms a frame and the period grew 8.1 ms. That fit was a
coincidence of magnitudes, and taking it as proof cost several device round trips.

The app was drawing its HUD with five `pnx_platform_text_draw` calls. That path goes
through the SDK at **~4.3 ms a call -- 21.5 ms, 58% of a 37.3 ms frame**, before any audio
work at all. The diagnostics were the most expensive thing in the app; the synth was
merely what pushed the total past the deadline.

| | ms/frame | of a 37.3 ms period |
|---|---|---|
| 5 SDK text draws + synth | 29.4 | 79% |
| 1 glyph-blitter draw + synth | 12.2 | 33% |
| 1 glyph-blitter draw, synth off | 4.3 | 12% |

**This is what E7 was for**, and the example predated it. A game drawing text through
`pnx_text_draw` pays a blit; one drawing through the platform pays a system call it can
only make after the framebuffer is released -- which is also why the HUD had to live in a
post-frame hook and could never be drawn over.

The crackle followed the frame rate: at 22 fps the period is 45 ms, so the audio timer is
serviced irregularly and in bursts, and the lead was sized for 37. Restoring the frame rate
restored the audio, with no change to the synth at all.

**Worth generalising: measure the frame, not the feature you just added.** Three of the
four things blamed for this -- 64-bit multiplies in the filter, envelope state on the
stack, block-boundary discontinuities -- were wrong, and the last of those was an artefact
of a broken measurement harness rather than anything in the audio.

### Headroom, not CPU, is what binds

Four detuned voices plus wet effects peaked at **163 against the 127** the mixer clamps its
accumulator to. Detuned oscillators constructively interfere -- that is what makes them
sound thick -- and the loud case is exactly the case that clips. It was heard as roughness
long before any number said so.

One bit of output shift lands the peak at 82-87 of 127, which is loud with room. Three bits
was the first attempt and was wrong in the other direction: peak 12 of 127 trades clipping
for quantisation noise, spending three and a half bits of an eight-bit output on silence.

**This is the argument for `PNX_AUDIO_16KHZ_16BIT` for anything using the synth.** Four
voices plus reverb do not comfortably fit an 8-bit output, and the fix at 8-bit is always
some flavour of giving up dynamic range.

Measured through the real mixer, on the example song:

| Synth headroom | Peak (127 = full scale) | Samples clipped |
|---|---|---|
| `>>0` | **239** | 8,598 of 192,512 — **4.5%** |
| `>>1` | 120 | 0 |

Four detuned voices at velocity **255** with both sends up still reach 155 and clip at
`>>1`. That is the ceiling, and it is asserted in `tests/test_audio.c` rather than left to
be rediscovered: a song using every channel at full velocity does not fit 8 bits, and the
remedy is a wider output format, not more attenuation.

**The clamp is now counted.** `PnxAudioStats` carries `clipped` and `peak`, because the
clamp had always been silent -- "the mix is too hot" and "the mix is fine" produced the
same clean build and the same clean log, and the difference was only audible as harshness
that could equally have been aliasing, a bad sample, or an underrun. Distinguishing those
by ear from a description cost several device round trips; `pk120 clip0` on the watch
settles it without one.

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

## Streaming audio: what actually governs continuity

Getting a clean stream took eight rounds of device testing, and most of the statistics
added along the way measured the wrong thing. Recorded so the next person does not repeat
it.

**The trap.** The obvious health check is "have we written as many bytes as playback has
consumed", comparing a running total against elapsed time. It is useless: a buffer can
empty and refill repeatedly while the running total stays perfectly ahead. That figure read
zero deficit for eight rounds while the stream was starving several times a second.

**The number that matters is the interval between feeds.** A lead only survives stalls
*shorter than itself*. Measured on device, feeding from the render loop gave 140ms intervals
in steady state and 305ms during startup, against a 120ms lead -- so the buffer emptied
constantly. Nothing else revealed this.

**Audio must not ride the render loop.** Rendering is gated at ~37.33ms and a frame can be
much later than that. Feeding from a dedicated `app_timer` decoupled the two and brought
the interval to ~32ms.

**A 1ms self-rearming frame timer starves every other timer in the app.** The frame loop
re-armed at 1ms on the reasoning that the display gates rendering anyway, so asking sooner
costs nothing. It costs the event loop: a 10ms audio timer measured 54-59ms until the frame
timer was moved to 16ms, after which it measured ~32ms. Frame rate was unchanged, because
16ms still beats the display's gate.

**Feeding inside the framebuffer capture window is audible.** `graphics_capture_frame_buffer`
blocks the compositor, which is why the SDK will not draw text there. Audio belongs after
the release for the same reason.

**A short write is normal and must not be discarded.** `speaker_stream_write` returns bytes
accepted; a full buffer accepts less. Dropping the remainder puts a hole in the waveform
that the mixer cannot regenerate, because voice phases have already advanced. Carry it.

**Dynamic mix gain is worse than fixed.** Dividing by the active voice count steps the whole
mix when a voice starts -- a pop per beat. Gliding to that target spreads the step into an
amplitude warble at 42% of a row. A fixed shift is stable by construction; headroom belongs
in instrument volumes, as trackers have always done it.

**~~8-bit output beats 16-bit on this device.~~** *Retracted.* Every format comparison in
this section was taken through the scrambled mapping described below, so each label named a
different format than the one being heard. The reasoning that a 16-bit stream carries no
extra information from an 8-bit mixer still holds -- it is the same samples shifted left
eight, at twice the bandwidth -- but the listening results proved nothing. **Rate is what
mattered, not depth**: see below.

**Buffer depth does not govern quality.** Swept 20ms to 250ms of lead with no audible
difference, so the lead should be chosen for latency -- how long after a trigger a sound is
heard -- and nothing else.

### The actual cause: a positional format table

All of the above is real, and none of it was the fault being heard. The platform mapped
our `PnxAudioFormat` to the SDK's `SpeakerPcmFormat` through a **positional array**, while
the enum's comments had been reordered and its members had not. Every entry was wrong:

| We asked for | Device was opened as |
|---|---|
| 16k/16 | 16kHz **8-bit** |
| 16k/8 | 16kHz **16-bit** |
| 8k/16 | 8kHz **8-bit** |
| 8k/8 | 8kHz **16-bit** |

So while the mixer wrote 8-bit samples, the device read them as 16-bit little-endian,
turning every *pair* of samples into one wrong value. That is the static and the thrum, and
no amount of work inside the mixer could have touched it. It also explains why the format
sweep gave the results it did -- the labels and the behaviour were two positions apart.

The fix is designated initialisers keyed by our own enum plus a `_Static_assert` on the
table's size, so the mapping cannot drift from the enum again. **Never map to a platform
constant by position.**

The lesson is not about audio. Every measurement in this section was taken *above* the
platform boundary and every one read correct, because they were correct -- the bug lived in
the one line that translates our vocabulary into the device's, which is exactly the place a
platform seam exists to isolate and therefore the last place anything gets checked.

### Resolved: the sample rate was the rest of it

With the mapping fixed, **16kHz sounds clean on device** -- confirmed by ear on the real
watch, playing music with sound effects over it. Two changes closed the remaining gap:

**16kHz, not 8kHz.** 8kHz puts the Nyquist limit at 4kHz, and a generated waveform has
harmonics well above that. They fold back as inharmonic components, which is not heard as
dullness but as *harshness and fast ticking* on high notes -- and only on high notes, which
is why a sustained A4 test tone sounded fine while the lead did not. 16kHz doubles the limit
to 8kHz and the folding largely stops. This was never fairly tested earlier because the
scrambled table meant "8kHz" and "16kHz" both opened something else.

**Wavetables must begin at amplitude zero.** A new note starts at phase 0, and phase 0 was
the triangle's trough at -100, so every onset stepped the output to full negative amplitude.
A 6ms envelope is 48 samples at 8kHz and percussion asks for 1ms; neither hides a step that
large. Rotating each table to start at a zero crossing makes the onset silent regardless of
how fast the envelope opens. The square wave has no zero to start from and so still depends
on its envelope -- a square lead clicks more than a triangle one.

A one-pole low-pass was added while chasing this and is **no longer load-bearing**: the build
sounds good with it disabled. It is kept at a default 5kHz cutoff as a balance -- bright
enough to leave the waveforms alone, low enough to take the edge off the top of a range --
for one multiply and one shift per sample. `pnx_audio_set_lowpass(0)` turns it off.

**The requested lead is not the queue you get.** Measured on the host: an 80ms lead settles
at 52-58ms actually queued, because quantum alignment defers each write until 256 bytes have
accrued and a single feed is capped at one chunk. So a stall must fit inside roughly 0.7x the
configured lead -- ask for about 1.5x the stall you intend to survive. A 40ms stall against
an 80ms lead holds; 60ms does not.

The mixer's own output was verifiably clean throughout: 3 seconds captured on the host showed
zero sample-to-sample deltas above 6 across 47,679 samples and flat peak amplitude in all 148
windows. That capture was right, and it is why the search eventually went below the mixer.

Every quantity reachable from inside the app now reads correct, so the next step is not more
instrumentation -- it is recording the speaker and looking at a spectrum. A fixed frequency
implicates the device's DAC or power path; one that tracks the feed interval is still ours.

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


---

# Scripting

*Whether a script layer can carry gameplay.*

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
