# Blog outline

Two related posts. The first is the stronger and more original claim; the second applies
the same idea to art. Written alongside shipping, not before.

---

# Post 1 — The memory hierarchy authors your paradigm

## Thesis

Programming paradigms are usually described as ideas — better ways to think about
structure. A lot of them are more honestly described as **responses to the read/write
characteristics of the media of their era.** When those characteristics change, the
paradigms built on them stop paying, and paradigms they displaced become viable again.

Data-oriented design is the clearest case. Struct-of-arrays layouts, cache-line
alignment, avoiding pointer chasing, entity-component-system storage — none of these
are truths about program structure. They are all one thing: **strategies for hiding a
steep memory hierarchy.** A DRAM miss costs a couple of hundred cycles, so you organise
data to avoid them, and an entire architectural style follows.

Flatten the hierarchy and the whole edifice loses its purchase.

## The evidence

This device has no DRAM. SRAM close to the core, QSPI flash behind it, and nothing in
between. Measured on it:

**Layout stopped mattering.** Three storage models — packed structs, per-channel arrays,
and a *shuffled pointer table* — across three kernels and 64 to 2048 entities:

| Kernel | AoS | SoA | Pointer table |
|---|---|---|---|
| all channels | **209** | 251 | 222 |
| position only | 134 | 160 | 153 |
| stream one field | 50 | **38** | 54 |

ns per entity. Everything within 1.5x. **Pointer chasing — the thing DoD exists to
avoid — was faster than per-channel arrays on two of three kernels.**

**Cache-line alignment stopped being a rule.** Padding structs to 64 bytes won 24% on a
single-field sweep and *lost* 13% on an all-channel one. Not a technique, a trade.

**Locality stopped mattering in flash too.** 256 scattered 64-byte reads came out
marginally *faster* than 256 sequential ones. Cost is 29 µs per call plus throughput;
call count is everything, access order nothing.

Three separate places where the received wisdom simply had nothing to grip.

## The other half: the write asymmetry

Reads and writes are not symmetric here, and not by a little:

| | |
|---|---|
| persist read, 256 B | ~70 µs |
| persist write, 256 B | **7,335 µs** |

Roughly **two orders of magnitude**, and the write cost is *per call*, not per byte — a
4-byte write costs half of a 256-byte one. (The exact ratio is soft: the read is near
the clock's resolution. The order of magnitude is not.)

That asymmetry has its own paradigms, and they are old ones: log-structured storage,
append-only logs, copy-on-write, immutable snapshots with structural sharing. Ideas from
the early nineties — LFS is 1991 — that were designed for exactly this shape of media
and then spent thirty years being niche because spinning disks and then DRAM-backed
caches made them unnecessary.

Our save system converged on this independently: pack into full-size chunks, minimise
key count, batch and defer, write incrementally across frames. That is a log-structured
design arrived at by measurement rather than by reading the literature — which is
itself the point.

## What comes back

If pointer chasing is free and locality is irrelevant, things that game development
abandoned become available again:

- **Pointer-rich structures.** Trees, graphs, adjacency lists, linked structures. The
  reason to flatten them into arrays was locality, and locality is gone.
- **Indirection and polymorphism.** Virtual dispatch is a dependent load. Dependent
  loads are cheap here.
- **Immutable and persistent structures.** Structural sharing is pointer-heavy by
  construction, and pairs naturally with the write asymmetry.
- **Simply writing it the readable way**, which is the recommendation this project
  actually landed on.

## What does not come back — the honest counterweight

The post is stronger for including these:

- **Code size is brutally 1985.** `.text + .data + .bss` capped at 65,535 bytes. That is
  a storage constraint, not a hierarchy one, and it has not softened at all.
- **Interpretation is still expensive.** JavaScript measured 363x slower than C, ~810
  cycles for an integer add. Though note the likely cause is *instruction* fetch from
  flash — which, if true, is the same media story pointing the other way: the data
  hierarchy flattened, the instruction hierarchy did not.
- One device is not a trend. The honest framing is that this is a place where you can
  see the effect unusually clearly, not a proof about computing generally.

## Why it generalises anyway

The flattening is not unique to a watch. Unified memory on Apple silicon, persistent
memory, NVMe, CXL — the industry is broadly moving toward shallower hierarchies and
non-volatile media with asymmetric read/write costs. A microcontroller with SRAM and
flash is a small, legible instance of a large trend, which makes it a good place to test
the claim without a datacentre.

## Structure

1. Paradigms as media artefacts, not ideas
2. DoD as the clearest case: what it is actually for
3. This device: no DRAM, flat access, and three benchmarks that found nothing
4. The write asymmetry, and the old ideas it revives
5. What comes back
6. What does not — code size, interpretation, and the limits of one device
7. The practical upshot: write it readably, measure before you optimise, and know which
   era's constraints you are actually programming against

---

# Post 2 — Not borrowed

The same argument applied to aesthetics. **Form follows media** in art as much as in
code: borrow another era's constraints and you get a costume.

The temptation on a 64-colour handheld is to make a SNES game. But the device is
portrait (200x228 — no console was ever this shape), reflective and always-on (no glow,
no phosphor, so the CRT vocabulary of scanlines and bloom is not just unnecessary but
wrong), locked at 26.8 fps (a cadence, not a limitation), worn rather than sat in front
of (sessions are minutes, and it is never really off), and touched by a finger that
covers a third of the screen.

Chrono Trigger and Legend of Dragoon are references for *structure and feeling* —
encounter design, battle rhythm, party dynamics — not for pixels.

Caveat: this post is only credible if the game ships and is good. Write it after.

## Material to capture as we build

Do this in the moment; it cannot be reconstructed.

- [ ] Screenshots of a deliberately SNES-imitating version, for the contrast
- [ ] Photographs of the screen **in sunlight and in a dim room** — a simulator
      screenshot cannot convey a reflective display, and that is the whole argument
- [ ] Video of the relative drag stick, showing the occlusion problem honestly
- [ ] Audio: the 94 ms batch-API seam versus the software mixer
- [ ] The M0 latency result, whichever way it goes
- [ ] Frame-timing logs per milestone, so the performance claims have receipts