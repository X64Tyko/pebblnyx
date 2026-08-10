# The editor

A visual tool for building levels, managing assets, testing in an embedded emulator,
and packaging a build.

---

## Why it belongs in the plan

The framework's stated goal is that someone can build a game without thinking about the
code underneath. **Code is only half of what stops people.** The other half is content:
slicing sheets, choosing tile ids, hand-authoring ASCII maps, wiring warps, keeping
under a 256KB budget. A framework that removes the C but leaves the content work in a
text editor has moved the barrier, not lowered it.

It also pays for itself immediately on this project. Several things already done by hand
are exactly what the editor automates: rendering tile previews to a PNG to pick ids,
tracing why a warp did not fire, checking the resource budget after each change.

## The stack question is already answered

The Pebble emulator is **QEMU with a VNC display**, and `pebble-tool` ships
**websockify** — a WebSocket-to-TCP bridge whose entire purpose is putting VNC in a
browser. From `pebble_tool/sdk/emulator.py`, a running emulator exposes:

| Port | Purpose |
|---|---|
| QEMU VNC | the watch screen |
| QEMU monitor | machine control |
| QEMU gdb | debugging |
| pypkjs WebSocket | install, logs, screenshots (the pebble protocol) |

All of it recorded in a state file, so several tools can attach to one running emulator
rather than fighting over it.

**So the editor should be a local web app.** Not a preference — it is the only stack
where the emulator screen embeds for free, via noVNC over the websockify bridge that is
already installed. Every alternative means writing a VNC client.

That also lines up with what already exists: the asset pipeline is Python, `pebble` is
Python, websockify is Python. The editor is a Python server plus a browser UI, reusing
the pipeline as a library rather than reimplementing it.

## Architecture

```
browser UI
  ├─ manifest editor      tilesets, sprites, maps, dialog, audio
  ├─ map canvas           paint tiles, place entities, wire warps
  ├─ emulator panel       noVNC  ->  websockify  ->  QEMU VNC
  └─ build panel          validate, budget, package
        │
   local Python server
  ├─ pipeline (imported, not shelled out)
  ├─ pebble build / install --emulator
  └─ emulator lifecycle + log stream
```

**The editor is a GUI over the manifest.** That framing keeps it small: the manifest is
already the single source of truth, the pipeline already validates and packs, and the
generated header is already the contract with the C. The editor reads and writes one
file and calls existing code. It should never become a second, parallel definition of
what the content is.

## Staging

Ordered so each stage is independently useful, rather than a big-bang.

**E1 — Inspector (read-only).** Load a manifest, render every tileset with ids and
semantic tags, render each map, show the resource budget by category against the 256KB
cap, and list validation errors. No editing. This is the stage that replaces things
currently done by hand, and it is worth building even if nothing after it happens.

**E2 — Map editor.** Paint tiles, set flags, place entities, wire warps by clicking
source and destination. Validation runs live — the reachability flood fill highlights
unreachable doors *as you draw*, rather than at build time.

**E3 — Emulator panel.** noVNC embedded, plus build → install → run → logs, with the log
stream in a pane beside the screen. Removes the push-to-device cycle for everything that
does not need real hardware.

**E4 — Asset import.** Drop a sheet in; slice it, pick a colour key, preview
quantisation to the 64-colour palette, preview dedup savings, choose frames. Currently
the fiddliest manual work by a distance.

**E5 — Package.** One button: validate, build, enforce the budget, produce the `.pbw`,
report size breakdown against every cap.

**E6 — Music editor.** A tracker view over the sequencer's data model. Only meaningful
after M4 exists, and probably the largest single piece.

## Honest scope warning

**A full editor can easily exceed the engine in effort.** E1–E3 are modest because they
mostly surface work the pipeline already does. E4 and E6 are real applications in their
own right — sheet slicing with a good UX, and a tracker, are each weeks.

Two sequencing risks worth naming:

1. **The editor must not delay the game.** The market research was unambiguous that
   adoption follows a shipped game, not tooling. Tooling that speeds *our* content work
   earns its place immediately; tooling built for hypothetical other developers should
   wait until there is evidence they exist.
2. **Do not build the editor before the content model is proven.** E2's map editor
   encodes assumptions about what a map *is*. If the game later needs layered maps,
   scripted events or per-tile metadata, an editor built too early gets rewritten. E1 is
   safe now because inspection makes no such commitment.

Recommended: **E1 alongside M2, E3 after M3, E2 once the first real maps exist, and
E4–E6 only when the pain justifies them.**

## Open questions

- **Does the editor own the manifest file, or edit it in place?** In place keeps git
  diffs meaningful and lets hand-editing continue to work, but constrains the UI to what
  the format can express. Leaning in place.
- **One emulator instance shared with the CLI, or its own?** The state file suggests
  sharing is intended and would avoid surprising port conflicts.
- **How much does the emulator actually tell us?** It will not reproduce the 26.8fps
  ceiling, real flash timing, touch behaviour, or audio. Useful for content and layout
  iteration; misleading for anything measured. The UI should say so rather than let
  someone tune a timing window against QEMU.


## The embedded emulator is not currently possible

Checked against the installed SDK: `sdk-core/pebble/` ships QEMU images for **aplite,
basalt, chalk and diorite** only. There is none for **emery** (Pebble Time 2), nor for
gabbro or flint. The three new platforms have real firmware on real hardware and no
emulated target.

So "test without pushing to device" cannot be delivered by embedding the Pebble emulator,
because the emulator does not cover the platform this framework targets. Three options
exist, in increasing order of effort:

1. **Run the game logic on the host platform seam** and draw its framebuffer into the
   editor. This already works -- `pnx_platform_host.c` renders to a flat buffer and the
   host tests assert on its pixels. It is not an emulator: no firmware, no 26.8fps
   ceiling, no flash timing. But for checking that a map is walkable and a warp fires, it
   is both sufficient and far faster than a device round trip.
2. **Build for basalt as a proxy.** Wrong screen (144x168 vs 200x228) and wrong colour
   depth handling, so it would flatter or distort results. Not worth it.
3. **Wait for or build an emery QEMU target.** PebbleOS is open source and the board is
   `obelix`, so it is possible in principle and a large project in practice.

Option 1 is the one worth building, and it is mostly already built. Recorded here so the
roadmap stops promising an emulator it cannot deliver.
