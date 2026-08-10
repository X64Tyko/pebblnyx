# Layer rules

Dependencies run strictly downward. A module may include from its own directory and
from any directory below it in this list, and never upward or sideways.

```
app        loop, scene stack, lifecycle
audio      mixer, sequencer, sfx
gfx        tilemap, sprites, text, camera
input      backends -> PnxInputState
assets     handle-based registry, residency
save       chunk packing, incremental writer
core       fixed point, arenas, containers, diagnostics
platform   the only layer that touches Pebble APIs
```

Two rules that matter more than the rest:

**Only `platform/` may include `<pebble.h>`.** Everything above it works in world
coordinates, byte buffers and framework types. This is what allows `core`, `gfx`,
`audio` and `save` to be compiled and tested on a host, and it is the seam that would
let the framework target something other than PebbleOS later.

**Every module is opt-in via `pnx_config.h` and must cost zero bytes when disabled.**
`.text + .data + .bss` is capped at 65,535 bytes for the framework *and* the game
together, so a module that cannot be compiled out is a module that taxes every game
that does not use it. See [`../../docs/DESIGN.md`](../../docs/DESIGN.md) section 2.

Naming: all public symbols are prefixed `pnx_`, all public types `Pnx`.