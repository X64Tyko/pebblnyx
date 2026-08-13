# worldtiles — a world that does not fit in RAM

The overworld example shows what the framework can **draw**. This one shows what it can
**hold**.

The field is 192x192 cells: 73,728 bytes of cell plane against 128 KB of app RAM on
`emery`. It is never resident, at any moment, in any form. It arrives sixteen WorldTiles at
a time, and its three tilesets arrive through two atlas slots.

Its payloads are not in the map's own resource. They are in **18 bank resources** of about
4 KB each, because `resource_load_byte_range` on this platform is O(offset) — it streams
from the start of the resource on every call. Held in one 75 KB blob, a WorldTile two
thirds of the way in cost 13 ms to read and loading the whole world took two seconds.
Banking caps the seek at the bank rather than the map; batching then fetches a whole run of
WorldTiles in one call, so a bank is one seek instead of eight. Both were measured, not
guessed — see [`MEASUREMENTS.md`](../../docs/MEASUREMENTS.md).

## The comparison

`field` and `plain` are the **same world** — same rows, same tilesets, same everything.
`plain` differs by one line in the manifest:

```toml
resident = true      # a slot per WorldTile: hold all of it, the way maps worked before
```

Press SELECT to swap between them at the same position. Nothing on screen changes. The
arena figure on the HUD does:

| | Resident | WorldTiles | Atlas slots |
|---|---|---|---|
| `field` — streamed | **23,678 B** | 16 of 144 | 2 of 3 |
| `plain` — held whole | **98,551 B** | 144 of 144 | 3 of 3 |

**4.2x**, for an identical picture — measured on hardware, not predicted. And note how
little room the held-whole world leaves: `emery` reports ~110 KB of heap, so it fits with
about 6 KB to spare before the game allocates anything of its own. That is the argument for
WorldTiles in one number. Not that holding a 192x192 world is impossible, but that it
consumes the watch.

If the heap refuses the 100 KB arena, the example says so and runs the streamed world
anyway. That outcome is also the result.

Both hold 26.8 fps — the PT2 ceiling — with the worst frame at 12 ms of a 37.33 ms budget.
Loading the held-whole world costs one 74 ms frame; the streamed one costs nothing you can
see. Before the payloads were banked, that same load took **1,984 ms** and every WorldTile
boundary dropped a frame.

## What is on screen

- **The residency grid**, top right: one cell per WorldTile in the map, filled when
  resident, brightest for the one the camera is in. This is the point of the example —
  when streaming works you just see a world, so the interesting state has to be drawn.
- **`A:` bottom left**: the atlas pool, a digit per atlas, `.` when its slot was evicted.
  On the field, walking from the meadow through the ruins to the hall makes an eviction
  visible as it happens.
- **The HUD line**: current map, arena KB, resident/total WorldTiles, worst streaming
  backlog, fps.

## Controls

| | |
|---|---|
| **UP / DOWN** | turn one compass point, anticlockwise / clockwise, and walk |
| **touch** | walk toward where you touched; lift to stop |
| **SELECT** | swap streamed ↔ held whole — the comparison |

Eight headings rather than four, because a diagonal crosses a WorldTile *corner* — asking
the streamer for tiles in two directions at once, which a four-way demo never does.

There is no autopilot button. It drives whenever you have left the controls alone for five
seconds and yields the moment you touch them, so putting the watch down leaves it
stress-testing itself. While it drives it works up through the speeds — 1, 2, 4, 8 tiles a
tick — and back down.

The top two speeds outrun `PNX_MAP_STREAM_BUDGET` on purpose. At 8 tiles a tick the camera
crosses a WorldTile every two ticks, the streamer cannot refill its margin, and the `m`
counter on the HUD starts climbing. That is not a bug being demonstrated; it is the bound,
and the reason a game with a dash wants `pnx_map_stream_now` rather than a bigger budget.
It is on the autopilot rather than a button because the point of an autopilot is finding
what nobody thought to try.

## The interiors

`hut`, `crypt` and `keep` are small enough that `pnx_map_load` holds them whole, so the
streaming path never runs for them at all — exactly as maps behaved before WorldTiles
existed. Warping between them and the field is the second thing this example tests, and it
is what found the eviction-order bug: a warp asks for a region drawn from a tileset nothing
resident uses, and evicting WorldTiles one at a time could never free an atlas slot.

## Content

`generate.py` writes `assets.toml`. The field's 192 rows are not something anyone types by
hand, and the band heights are a constraint rather than a decision — the generator explains
why they are exactly 64 rows. Re-run it to change the world; nothing reads it at build time.

```
python3 examples/worldtiles/generate.py
python3 tools/pnx_assets.py assets.toml --out resources \
        --header src/c/assets_gen.h --package package.json
pebble build && pebble install --emulator emery --logs
```

`tests/test_stream.c` walks this example's field end to end on the host — 36,864 steps,
asserting every visible cell is resident at every one of them — and makes the same
streamed/held-whole comparison as an automated check.
