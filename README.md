# Pebblnyx

A game framework for the Pebble Time 2 and Round 2, written in C.

The goal is that someone can build a game without writing resource loaders, an
audio mixer, a fixed-timestep loop, a save format, or a framebuffer blitter — and
without needing to know why each of those has to be built a particular way on this
hardware.

The first game built on it is an RPG crossing Chrono Trigger and Legend of Dragoon —
see [`docs/GAME.md`](docs/GAME.md), which is also why a few framework features exist.

**Status: playable slice running on hardware.** Platform, core, assets, graphics and an
audio mixer are built and verified on a real Pebble Time 2; there is a visual editor for
maps and asset import. Save and the app-state/lifecycle framework are built and host-tested
end to end but not yet confirmed on device — see [`docs/ROADMAP.md`](docs/ROADMAP.md)'s M5
and M6. The pipeline deduplicates mirrored tiles, composes 16x16 tiles from shared 8x8
quadrants when that pays, and collapses palette-swapped sprite recolours to one bitmap plus
a palette each. Every number in this repo was measured on the device, and the ones
that overturned a design decision are recorded as such in
[`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md) — including several where the estimate was
wrong by more than 2x.

| | |
|---|---|
| Smallest complete game | 6,452 of 65,535 static bytes (9.8%) — 2,528 (3.9%) with diagnostics off |
| Playable slice | 15,157 of 65,535 static bytes (23.1%), 19,239 of 262,144 resource bytes (7.3%) |
| Runtime memory | 15,157 static + 115,915 bytes of heap, of one 128KB slot |
| Frame cost | ~5,100 µs of ~35,000 available, holding the 26.8fps ceiling |
| Render cadence | locked to 40ms (25fps), ~2.67ms of slack under the 37.33ms display floor |
| Tests | 724 host checks + 361 pipeline-validation checks |

---

## Why this exists

The raw SDK path is C, manual memory management, manual framebuffer writes, and
manual input wiring. That is a steep entry cost for someone who wants to make a game
rather than an engine. The one prior attempt at a Pebble game engine (PGE, 2014) is
abandoned, will not compile against the current SDK, and made choices — per-frame
bitmap allocation, drifting open-loop timing — that this hardware punishes.

Meanwhile the platform is live: 23,000+ Time 2 units built, ~14,000 Round 2
pre-orders, 2,120 apps and watchfaces created for the new watches, and an official
dev contest that explicitly rewards "good use of new platforms." Touch and audio
both shipped recently and are essentially unexplored for games. The games category
is thin.

**The commercial opportunity here is zero.** This is infrastructure: PebbleOS is
fully open source and the community can outlive any single vendor, so a well-made
open framework becomes permanent platform furniture. Read
[`docs/DESIGN.md`](docs/DESIGN.md) for what that implies about scope.

## What makes this platform unusual

Six things were measured that contradict the design you would otherwise reach for.
Each one removed work from the plan rather than adding it:

| Finding | Consequence |
|---|---|
| Rendering is capped at **26.8 fps** and cannot be raised | Design to a fixed pace; stop optimising for frame rate |
| **~35 ms of free CPU** every frame | Render cost is nearly irrelevant; simulate freely |
| **Partial redraw does not help** frame rate | No dirty rectangles. Full-screen redraw every frame |
| Entity layout (SoA / AoS / pointer chasing) is **within 1.5x** | Write it readably. No archetype ECS machinery |
| Flash reads have **no locality penalty**, cost is per call | Bulk-load and keep resident. No streaming cache |
| **`.text` + statics capped at 64KB**, not 128KB | Code size is the scarcest resource; features must be opt-in |

The full evidence, including the ones that went the other way (persist writes are
116x slower than reads; the batch audio API cannot do music plus sound effects), is
in [`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md).

## Layout

```
docs/PLATFORM.md      how these games are actually played: off the wrist, in two hands
docs/GAME.md          the game this is built for, and what it demands
docs/EDITOR.md        the visual editor: levels, assets, packaging -- no embedded emulator, see inside
docs/DESIGN.md        architecture, rationale, API sketches
docs/MEASUREMENTS.md  the measured platform facts everything rests on
docs/ROADMAP.md       milestones and current state
docs/PORTING.md       reference for M9: per-platform packaging, before that work starts
docs/blog/            notes toward a writeup on old techniques, re-priced

src/pnx/platform/     THE ONLY layer that touches Pebble APIs
src/pnx/core/         fixed point, arenas, containers, diagnostics
src/pnx/assets/       handle-based asset registry
src/pnx/gfx/          blitter with X/Y flip, camera, tilemap, sprites with depth sort
src/pnx/audio/        software mixer over a streamed PCM buffer
src/pnx/input/        button edges, hold times, orientation-aware cluster mapping
src/pnx/save/         chunk-packed persistence, versioned, spread across frames
src/pnx/app/          state stack, fixed-timestep loop, focus-aware lifecycle

tests/                724 host checks, run with a normal compiler
tools/pnx_assets.py   the asset pipeline: manifest -> blobs + generated header
tools/pnx_editor.py   visual editor: maps, transitions, asset import, build
tools/pnx_preview.py  renders the shipped blobs as an HTML report
tools/size_report.py  per-module bytes against the 64KB ceiling
examples/empty/       the smallest complete game, and the size baseline
examples/overworld/   two scenes, two tilesets, walkable, with transitions
```

## Building

Host tests need only a C compiler. They exist because the platform seam does: anything
above `platform/` should be debuggable in a second on a laptop rather than in a minute
over Bluetooth, behind a log stream that drops the first messages.

```sh
cd tests && make test
```

Content is built from a manifest, then the app builds for the watch and prints its own
size breakdown:

```sh
cd examples/overworld
python3 ../../tools/pnx_assets.py assets.toml --package package.json
pebble build
```

Or open the editor, which does both and lets you draw maps:

```sh
python3 tools/pnx_editor.py          # finds the example on its own
```

```
module                 text   rodata    data      bss    total
--------------------------------------------------------------
pnx/core               1470        0       0     2347     3817
pnx/assets             2522        0       0      305     2827
pnx/platform           1274        0       0      299     1573
pnx/gfx                1442        0       0        0     1442
game                   1090        0       0      232     1322
(sdk/libc)                0      130       0        0      130
--------------------------------------------------------------
TOTAL                  7798      130       0     3183    11111
(headers/padding)                                         2245

[########................................] 13356 / 65535 bytes (20.4%)
```

**Static bytes and resource bytes are separate budgets.** The report above counts what the
linker places, which `virtual_size` caps at 65,535; resources live in flash against a 256KB
appstore limit. The rest of the 128KB slot is heap -- 117,716 bytes free in the slice above --
so a large buffer belongs there rather than in a static array. Audio's buffers were moved for
exactly that reason and the module fell from 11,432 bytes to 3,672.

That report is not optional decoration. `virtual_size` in the app header is a **uint16**,
so code, constants and static data together must stay under 65,535 bytes — and going over
fails with `struct.error: 'H' format requires 0 <= number <= 65535`, naming nothing. The
breakdown is in front of you on every build so the question "which module grew?" always
has an answer.

To check what a module actually costs, turn it off:

```sh
PNX_DEFINES=PNX_USE_DIAGNOSTICS=0 pebble build   # 6,452 B -> 2,528 B
```

A game project reaches the framework through a symlink at `src/c/pnx`, which keeps every
source file inside the project tree where waf can resolve it. See
`examples/empty/wscript`.

The pipeline **fails the build on content that cannot work** — a warp on a tile that
triggers nothing, a door sealed inside a wall, a destination inside a wall or in a closed
pocket. That matters because content bugs do not crash on a watch; they present as nothing
happening, with a binary that looks perfectly fine. There are 23 tests asserting the build
does fail, and that the message names the actual problem.

Checks that game state can legitimately contradict take a **declared escape** rather than being
softened into warnings — `gated = true` on a warp says "reachable only once the game opens it",
and it is never raised again. The acknowledgement lives on the declaration, so it cannot outlive
what it describes. See [`docs/DESIGN.md`](docs/DESIGN.md).

## Provenance

The design is derived from four probe projects, kept as the reference for anything
asserted here:

- `pebble-perf-probe` — frame pacing, render throughput, memory layout, flash access
- `pebble-tile-probe` — a playable slice: tilemap, sprites, maps, dialog, plus the
  audio, persist, battery and lifecycle benchmarks
- `pebble-alloy-probe` — whether a JavaScript-surfaced framework is viable
- `Pebblsidian` — the original voice-note app; where the SDK work started

## Licence

MIT — see [`LICENSE`](LICENSE). Permissive because the point is adoption.

Example art under `examples/overworld/art/` is a separate matter: some of it is
third-party, and its provenance is only partly recorded. See
[`art/CREDITS.md`](examples/overworld/art/CREDITS.md) before reusing it. The framework
itself depends on none of it — `tools/pnx_placeholder.py` regenerates a working example
from nothing.