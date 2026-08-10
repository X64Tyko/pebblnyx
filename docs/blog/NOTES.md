# Running notes for the post

Raw material, captured as it happens. Add to the top. Most of this is worthless in six
months if not written down now — the numbers survive in `MEASUREMENTS.md`, but the
*wrong turns* are what make the post worth reading, and those get quietly forgotten
once the code is right.

Format: what I believed, what happened, what the number was.

---

## The six overturned assumptions

Draft material for section 3. Each one is a small story ending in a number.

**1. "Optimise the render loop."**
Assumed the frame rate would be won or lost in the blitter. It is capped at 26.8 fps by
flow control in `applib/app.c` that no SDK call can bypass, and roughly 35 ms of every
37.33 ms frame is idle wait. Rendering a full-screen tilemap costs 2.35 ms — 7% of the
budget. There was never anything to optimise.

**2. "Partial redraw will help."**
The obvious optimisation: only redraw what changed. Measured at 228, 114, 57 and 28
dirty rows — **26.7–26.8 fps at every one.** The display driver honours dirty rows, but
transfer length is not what sets the period. An entire subsystem, deleted before it was
written.

**3. "Data-oriented layout will matter."**
Built a full benchmark: struct-of-arrays vs packed structs vs a shuffled pointer table,
three kernels, 64 to 2048 entities. Everything landed within 1.5x, and **pointer
chasing — the thing DoD exists to avoid — was the fastest on one kernel.** On-chip SRAM
has near-flat access cost. There is no memory latency to hide.

**4. "Flash will punish random access."**
The last place locality could plausibly matter. Scattered 64-byte reads came out
*marginally faster* than sequential ones. The cost is 29 µs per call plus throughput;
call count is everything and access order is nothing.

**5. "The audio API looks capable."**
Four mixed tracks with pitch-shifted samples — a SNES-shaped sampler, exactly the right
tool. It is unusable for music with sound effects: a **fixed 94 ms cost per submission**
(eight trials: 94, 94, 94, 99, 94, 94, 102, 99) and concurrent submission rejected
outright. Had to write a software mixer over a raw PCM stream instead.

**6. "JavaScript could be the framework's surface."**
Alloy puts a JS engine in firmware — an app is 276 bytes with 130 KB of heap free, and
Poco's drawing primitives are native C. Genuinely attractive. Then: **49,277 ns per
entity against C's 136. 363x.** About 3.4 µs per elementary operation, ~810 cycles for
an integer add. JS can afford native *calls*, not native *work*.

## Media-thesis material

Numbers that support "the memory hierarchy authors the paradigm". Keep adding.

**The read/write ratio needs care.** Averaged over three runs: read 256 B = ~70 µs,
write 256 B = 7,335 µs, so ~105x. A single early run gave 62 µs / 7,171 µs = 116x. The
read figure sits near the 1 ms clock's resolution (56% spread across runs) while the
writes are tight (3.7%). **Claim two orders of magnitude, not a precise multiple** —
and say why, because the reason is itself a good aside about measuring near a clock's
floor.

**Write cost is per call, not per byte.** 4 bytes costs half of 256 bytes: 877 µs/byte
versus 28 µs/byte, ~31x worse per byte. This is the property that forces log-structured
thinking — batching is not an optimisation, it is the only sane way to use the medium.

**Rewriting one key beat spreading across keys** (5,007 vs 7,335 µs), implying per-key
index work. Another nudge toward append-and-compact over update-in-place.

**Instruction fetch may be the JS story.** 810 cycles for an integer add is far worse
than a normal interpreter penalty. If the cause is bytecode fetched from QSPI rather
than RAM, then the data hierarchy flattened while the instruction hierarchy did not —
which is a neat complication for the thesis rather than a hole in it. **Unverified.**
Worth a targeted test before claiming it in print.

## An assumption that survived (worth including for balance)

**Timed input was expected to be the mechanic the hardware vetoed.** It was the one
thing flagged as a risk before any framework code was written. Measured: 31 ms spread,
100% at a 2-frame window. It works.

Two things worth telling anyway. **Touch is twice as fast as the buttons** (+27 vs
+53 ms lag) — the physical control is *slower* than the touchscreen, which is the
opposite of the intuition. And a claim I had to walk back within the hour: I wrote that **reaction cues are
unusable** based on a 165 ms spread. A rerun with more trials gave 75 ms. The first
run's spread was inflated by *negative* offsets — taps before the cue existed, i.e.
guesses — and once those stopped, reaction turned out to be merely worse rather than
hopeless. The lag figure replicated to within 1 ms (351 vs 352) while the spread halved,
which is a tidy illustration of how a summary statistic can be dominated by a behaviour
you did not intend to measure.

The surviving conclusion is softer and more useful: anticipation is 2.4x more precise,
so Additions should use it, but a reaction prompt is viable if its cue leads the target
by ~350 ms.

Also a nice detail for the media thesis: **judgment resolution is 1 ms while the
display is 37 ms.** The input path is far finer than the output path, so a game can
grade timing more precisely than it can show it.

## Methodology moments worth telling

**Two conclusions reversed themselves.** Cache-line padding was first measured as a
pure loss, then — with a runtime toggle, same session, same entity count, and an
untouched control — measured as a 24% *win* on narrow access and a 13% loss on wide.
The first comparison had spanned two runs at different entity counts. On this device
the real effects are 5–25%, small enough that any uncontrolled variable swamps them.
Good section on why A/B inside one session beats careful-looking cross-run comparison.

**Three separate times, startup logging was silently discarded.** `pebble install
--logs` attaches its stream after `init()` has already run. Chased it as a log-level
filter first, which was wrong. The fix in the end was to draw diagnostics on the watch
face instead of logging them.

**Manufactured precision.** An early version multiplied a millisecond clock by 1000 and
presented microseconds. It reported a map load as "0 µs" — which is not fast, it is a
1 ms clock. Anything sub-millisecond has to be measured by repetition.

**A benchmark that measured nothing.** The compiler noticed a 16 KB array was
write-only and deleted it. Only a read kept it alive. Worth a paragraph on how easy it
is to benchmark an empty loop with total confidence.

## Content-pipeline stories

**The building nobody could enter.** A door drawn inside a sealed rectangle of wall
tiles. The map compiled, the binary was correct, the warp simply never fired and
nothing looked wrong. Fixed by teaching the pipeline to flood-fill from the player start
and refuse to build if any warp is unreachable. The general point: content bugs should
fail at build time, because on-device they present as *nothing happening*.

**Guessed tile ids.** The first map used tile 0/1/2 for floor/wall/accent because those
were the first indices. It looked like noise. Now the pipeline measures flatness,
contrast and edge energy and picks semantically. First attempt at that picked a pure
black floor, so it also has to penalise near-black — a flat *visible* tile beats a flat
one. Good small example of tooling encoding taste.

**The hero has no facing, and cannot.** Assumed three sprite frames were three
directions. Pixel analysis: they differ by 85–96 pixels in the lower half and 7–11 in
the upper. Same pose, moving legs. It is a walk cycle in one direction, so the character
can animate and mirror but never face away. A constraint discovered by measuring the
art rather than looking at it.


## The NES trade, run in reverse

Final Fantasy 1 stores no graphics in a dedicated ROM at all: art lives in program ROM
and is streamed into 8KB of CHR-RAM as the game runs. Its 128 overworld blocks are
compositions of 8x8 tiles at 2 bits per pixel. Between the bit depth and the reuse, its
tileset is about **10.7x smaller** than the same content stored the obvious modern way,
as flat 16x16 tiles at one byte per pixel.

The obvious reading is "old hardware forced clever encoding." The more interesting one is
that the same encoding is correct on this watch, for an inverted reason.

The NES compressed because ROM was expensive and its CPU could not have afforded much
more decoding than it already did. Pebble Time 2 has the opposite balance and the same
conclusion. Rendering is capped at 26.8fps by firmware flow control, so roughly 35 ms of
every frame is spent waiting no matter what the app does — the CPU is **90% idle by
construction**. Meanwhile the appstore caps resources at 256KB. Spending idle cycles to
unpack nibbles is not a sacrifice; it is using something that would otherwise be thrown
away to buy something genuinely scarce.

Which means we can afford a *more generous* version of the old technique than the machine
that invented it: 4bpp and sixteen colours where the NES managed 2bpp and four.

The general shape, and the thesis for the post: a constraint that has moved does not
retire the technique built for it. It re-prices it. Techniques get filed away as
"obsolete" when what actually happened is that one term in the trade changed sign.
