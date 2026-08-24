# Quick start

A hands-on walkthrough: create a project, draw a tiny two-room map, and write a game
that walks between them -- ending with a real `.pbw` running in the emulator. Everything
below is real, working code, not sketched-out pseudocode: it's the same thing checked in
at [`examples/quickstart/`](../examples/quickstart/), which was built and run in the
emulator while this document was written. If you'd rather read finished code than type
it, open that directory instead and skip straight to [section 5](#5-build-and-run-it).

This is the practical companion to [`docs/EDITOR.md`](EDITOR.md) (why the editor is
built the way it is) and [`docs/DESIGN.md`](DESIGN.md) (why the framework is built the
way it is) -- both go deeper than this document tries to.

---

## 1. Prerequisites

- **Python 3** and **Pillow** (`pip install pillow`) -- the asset pipeline and the editor
  both need these. Nothing else, until you want to produce a real `.pbw`.
- **The Pebble SDK**, for an actual build. You don't need to install this by hand: the
  editor's Settings tab shows the licence, and after you accept it, it runs
  `pebble sdk install` for you (installing `pebble-tool` first via `uv`/`pipx`/`pip` if
  it's missing). See "The toolchain" in `docs/EDITOR.md` for why it works this way.
- Everything above is optional if you only want the **host tests**, which need nothing
  but a C compiler:

  ```sh
  cd tests && make test
  ```

  That's the fast loop: 724 checks in about a second, no SDK, no watch. Reach for it
  first whenever you're touching anything under `src/pnx/`.

## 2. What you're building

Two rooms, joined by a door. Walking into the door in room 1 warps you into room 2, and
back again. That's small enough to type in one sitting and still exercises every piece a
bigger game needs: a tileset, a sprite, collision, and a scene transition. Everything
past this is more of the same shapes, not a different structure -- `examples/overworld`
is the same idea with more rooms and dialogue, `resonant` (the real game this framework
is built for) is the same idea again with a title screen, a pause menu and save/resume
layered on top as more pushed states.

## 3. Create the project

```sh
python3 tools/pnx_editor.py
```

With no project open yet, the editor lands on a project screen: **create a new project**
in an empty folder, or **open** one that already has an `assets.toml`. Either way, before
the first build it stages its own copy of the framework into `<project>/src/c/pnx` --
that's the one directory in your project you never edit or commit; it's regenerated from
the editor every time, which is what keeps a game's source tree from drifting out of
sync with whatever engine version built it.

`examples/quickstart/` already has this laid out if you want to see the shape without
creating anything:

```
examples/quickstart/
  assets.toml          the manifest -- what this section and the next one write
  art/                 source art: tiles.png, hero.png
  resources/           built blobs -- generated, gitignored, not something you edit
  src/c/
    pnx -> ../../../../src/pnx     the staged engine (a symlink in this repo's own
                                    examples; the editor writes a real copy for you)
    game.h, main.c      your code -- section 4
    assets_gen.h        generated header -- built by the pipeline, never hand-edited
  wscript, package.json  ordinary Pebble build files, same as any pebble-tool project
```

## 4. Add your first assets

This is the part that has nothing to do with code: a tileset, a sprite, and a map, all
declared in `assets.toml`, the single file every editor tab is a view onto. You can do
each step through the editor's UI, or write the TOML by hand -- both end up editing the
exact same file, in place, so neither path is the "real" one.

**No art yet?** `tools/pnx_placeholder.py <out-dir>` draws a small flat-shaded tileset and
character from scratch, so you're never blocked on having a sheet before you can see
anything move. The two files this walkthrough uses (`art/tiles.png`, a 32x16 sheet of two
16x16 tiles; `art/hero.png`, a 16x72 sheet of three 16x24 frames stacked vertically) were
made the same way -- there's nothing licensed or hand-drawn about them.

**Through the editor:**

1. **Atlas tab** -- import `art/tiles.png`, set tile size to 16px, and slice. With only
   two tiles the whole sheet is the region. Use **autopick** to assign roles rather than
   typing tile ids: pick one tile as `floor`, the other as `wall`. Then, in the same tab,
   mark `wall` as **solid** -- collision is a property of the *tile role*, not of where a
   map later places it, so this one checkbox covers every map that uses this tileset.
2. **Sprites tab** -- import `art/hero.png` and slice it into three 16x24 frames stacked
   vertically. Name the animation frames `stand`, `step_a`, `step_b`, matching what the
   walk cycle in section 5 expects.
3. **Maps tab** -- paint two 10x8 rooms with the legend (`.` floor, `#` wall), then click
   a tile in room 1 and a tile in room 2 to wire a warp between them. The map canvas
   flags unreachable tiles live, as you draw, rather than waiting for a build to tell you
   a door leads nowhere.

**By hand**, the same three things are these manifest blocks -- this is exactly what the
editor writes for you, and it's short enough to type directly for a project this size:

```toml
[project]
name = "quickstart"
resources = "resources"
header = "src/c/assets_gen.h"
budget_bytes = 262144

# ---------------------------------------------------------------------------- atlas

[[atlas]]
name = "tiles"
sheet = "art/tiles.png"
tile = 16
region = [0, 0, 2, 1]
out = "tiles.bin"
autopick = ["floor", "wall"]

[[atlas.collision]]
tile = "wall"
type = "solid"

# --------------------------------------------------------------------------- sprites

[[sprite]]
name = "hero"
sheet = "art/hero.png"
frames = [[0, 0, 16, 24], [0, 24, 16, 24], [0, 48, 16, 24]]
out = "hero.bin"

[sprite.anim]
stand = 0
step_a = 1
step_b = 2

# ---------------------------------------------------------------------------- legend

[legend."."]
tile = "floor"

[legend."#"]
tile = "wall"

[legend."D"]
tile = "floor"
flags = ["warp"]        # walkable, and triggers a scene transition when stepped on

# ------------------------------------------------------------------------------ maps

[[map]]
name = "room1"
out = "map_room1.bin"
start = [2, 4]
# Destinations land one tile clear of the far door, so arriving does not immediately
# re-trigger the door that just brought you here.
warps = [{ at = [9, 4], to = ["room2", 1, 4] }]
rows = """
##########
#........#
#........#
#........#
#........D
#........#
#........#
##########
"""

[[map]]
name = "room2"
out = "map_room2.bin"
start = [1, 4]
warps = [{ at = [0, 4], to = ["room1", 8, 4] }]
rows = """
##########
#........#
#........#
#........#
D........#
#........#
#........#
##########
"""

# ---------------------------------------------------------------------------- scenes

[scene.room1]
map = "room1"
sprites = ["hero"]

[scene.room2]
map = "room2"
sprites = ["hero"]
```

Either path, run the pipeline once before writing any C, so mistakes show up here rather
than as a mysterious blank screen later:

```sh
python3 ../../tools/pnx_assets.py assets.toml --preview
```

```
building assets
  atlas tiles: 2 considered, 0 empty, 2 unique
    autopicked: floor=0, wall=1
  sprite hero: 3 frames of 16x24
  map room1: 10x8, 1 warps, 49/49 tiles reachable
  map room2: 10x8, 1 warps, 49/49 tiles reachable
  ...
  [........................................] 1560 / 262144 B (0.6%)
scene residency (what must fit in the scene arena at once)
  WORST            room1         1,204 B <- minimum scene arena
```

Two numbers from that output matter once you get to code: `49/49 tiles reachable` (the
flood-fill check -- if a door led nowhere, this line would name the cell instead of
passing silently), and `1,204 B` (the biggest scene, which sizes the arena in the next
section -- round up rather than guess, and re-read this line whenever a scene gains an
asset).

This also regenerates `src/c/assets_gen.h`, the **only** place a tile id, frame number or
map dimension is allowed to be a bare number. Everything your game code touches from here
on is a symbol from that header:

```c
#define TILE_FLOOR 0
#define TILE_WALL  1

#define HERO_STAND  0
#define HERO_STEP_A 1
#define HERO_STEP_B 2

#define MAP_ROOM1_START_X 2
#define MAP_ROOM1_START_Y 4
#define MAP_ROOM2_START_X 1
#define MAP_ROOM2_START_Y 4

typedef enum { PNX_SCENE_ROOM1, PNX_SCENE_ROOM2, PNX_SCENE_COUNT } PnxSceneId;
```

## 5. Write the game

Every pebblnyx game boils down to two things: a handful of hooks describing one mode of
play (walking around, a menu, a title screen), and a call to `pnx_app_push` that puts one
on top. That's `pnx_app` (`src/pnx/app/pnx_app.h`) -- a state stack plus the
fixed-timestep frame loop every earlier version of this framework made a game hand-roll.
It owns the accumulator, clamps catch-up after the app was covered by a notification, and
calls your `draw()` every rendered frame regardless -- a game built on it never writes a
`main` loop, an event pump, or an elapsed-time accumulator by hand.

**`src/c/game.h`** -- the struct every hook shares, and the one thing this file exports:

```c
#pragma once

#include "pnx/pnx.h"
#include "assets_gen.h"

#define MAX_SPRITES 1
#define HERO		0

typedef struct
{
	PnxArena arena;
	PnxCamera camera;

	PnxSpriteInstance sprites[MAX_SPRITES];
	uint8_t order[MAX_SPRITES];
	uint8_t sprite_count;

	int32_t hero_tx, hero_ty; // tiles
	uint8_t walk_phase;

	int8_t want_dx, want_dy; // set by field_input, consumed by field_tick
} Game;

extern const PnxAppOps field_state;

uint8_t game_boot(Game* g);
void game_shutdown(Game* g);
```

**`src/c/main.c`** -- content-specific helpers first, then the four hooks, then boot and
`main`. Nothing here is simplified for the tutorial; it's the actual file, `clang-format`
and all:

```c
#include "game.h"

#include <string.h>

static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;

// -------------------------------------------------------------------- scene loading

static bool enter_room(Game* g, uint8_t scene, int32_t tx, int32_t ty)
{
	if (!pnx_scene_load(scene))
		return false;

	PnxMap* map = pnx_scene_map();
	if (!map)
		return false;

	const int32_t T = map->tile_px;

	// Load what the first frame will show before it is shown -- otherwise the scene
	// draws once with an empty world, which reads as a flash of holes on every warp.
	pnx_camera_center(&g->camera, tx * T + T / 2, ty * T + T, pnx_tilemap_width(map),
					  pnx_tilemap_height(map));
	pnx_tilemap_stream_now(map, &g->camera);

	g->hero_tx = tx;
	g->hero_ty = ty;

	g->sprites[HERO] = (PnxSpriteInstance){
		.x		 = tx * T + T / 2,
		.y		 = ty * T + T, // feet, centre of the tile
		.sprite	 = 0,
		.frame	 = HERO_STAND,
		.palette = PNX_SPRITE_PALETTE_DEFAULT,
	};
	g->sprite_count = 1;
	return true;
}

// ------------------------------------------------------------------------ movement

static void try_move(Game* g, int32_t dx, int32_t dy)
{
	PnxMap* map = pnx_scene_map();
	if (!map)
		return;

	const int32_t nx = g->hero_tx + dx, ny = g->hero_ty + dy;
	if (pnx_map_solid(map, nx, ny))
		return;

	g->hero_tx = nx;
	g->hero_ty = ny;
	g->walk_phase ^= 1;

	const int32_t T		   = map->tile_px;
	g->sprites[HERO].x	   = nx * T + T / 2;
	g->sprites[HERO].y	   = ny * T + T;
	g->sprites[HERO].frame = g->walk_phase ? HERO_STEP_A : HERO_STEP_B;

	const PnxWarp* warp = pnx_map_warp_at(map, nx, ny);
	if (warp)
	{
		// warp->dest_map indexes the MANIFEST'S map order (room1=0, room2=1 here) -- it
		// happens to equal PnxSceneId in this file because both declaration orders are
		// alphabetical, but that is not guaranteed in general: examples/overworld's two
		// scenes come out in a different order than its maps do, so its main.c does this
		// same translation with the two sides genuinely swapped. Always translate
		// explicitly rather than casting dest_map straight to a PnxSceneId.
		const uint8_t dest = warp->dest_map == 0 ? PNX_SCENE_ROOM1 : PNX_SCENE_ROOM2;
		enter_room(g, dest, warp->dest_x, warp->dest_y);
	}
}

// ---------------------------------------------------------------- the field state
//
// Four hooks. input() runs once per rendered frame and only ever records what the
// player asked for; tick() runs at the fixed PNX_TICK_MS rate and is what actually
// moves the hero. That split is what keeps a single press from firing more than once
// when a frame carries several ticks (e.g. right after the app was covered) -- pnx_app
// owns the accumulator that makes it possible, not this file.

static void field_input(void* ctx)
{
	Game* g = (Game*)ctx;

	// Clears the previous frame's edges, so pnx_input_pressed answers about THIS frame.
	pnx_input_frame();

	PnxEvent ev;
	while (pnx_app_poll_event(&ev))
	{
		// pnx_input only records button events; anything else is ignored, which is why a
		// game can hand it the whole queue without sorting it first. A real game's input()
		// also switches on ev.type for PNX_EVENT_TOUCH_* here -- see
		// resonant/src/c/field.c for both paths at once.
		pnx_input_event(&ev);
	}

	g->want_dx = g->want_dy = 0;
	if (pnx_input_pressed(PNX_BUTTON_UP))
		g->want_dy = -1;
	else if (pnx_input_pressed(PNX_BUTTON_DOWN))
		g->want_dy = 1;
	else if (pnx_input_pressed(PNX_BUTTON_SELECT))
		g->want_dx = 1; // three buttons don't give four directions for free -- this demo
						// only walks up, down and right so the example stays short
}

static void field_tick(void* ctx)
{
	Game* g = (Game*)ctx;
	if (g->want_dx || g->want_dy)
	{
		try_move(g, g->want_dx, g->want_dy);
		g->want_dx = g->want_dy = 0;
	}
}

static void field_draw(void* ctx, PnxTarget* target)
{
	Game* g		= (Game*)ctx;
	PnxMap* map = pnx_scene_map();
	if (!map)
	{
		pnx_gfx_clear(target, 0xC0);
		return;
	}

	pnx_camera_center(&g->camera, g->sprites[HERO].x, g->sprites[HERO].y,
					  pnx_tilemap_width(map), pnx_tilemap_height(map));

	// Stream, then draw. The margin means this is usually a no-op.
	pnx_tilemap_stream(map, &g->camera);
	pnx_tilemap_draw(map, target, &g->camera);
	pnx_sprites_draw_sorted(g->sprites, g->sprite_count, g->order, target, &g->camera);
}

const PnxAppOps field_state = {
	.input = field_input,
	.tick  = field_tick,
	.draw  = field_draw,
};

// ---------------------------------------------------------------------------- boot

uint8_t game_boot(Game* g)
{
	memset(g, 0, sizeof(*g));

	if (!pnx_arena_init_max(&g->arena, "game", PNX_ARENA_HEAP_RESERVE, 4))
	{
		pnx_platform_log("arena init failed");
		return 0;
	}
	pnx_assets_init(&g->arena, RESOURCES, PNX_ASSET_COUNT);
	pnx_camera_init(&g->camera, PNX_DISPLAY_WIDTH, PNX_DISPLAY_HEIGHT);
	pnx_input_init(PNX_ORIENTATION);

	if (!pnx_scenes_load(PNX_ASSET_SCENES_SCENES))
	{
		pnx_log("scene table failed to load");
		return 0;
	}

	// Every state has to be pushed before pnx_app_frame has anything to drive.
	pnx_app_init(g);
	pnx_app_push(&field_state);

	return enter_room(g, PNX_SCENE_ROOM1, MAP_ROOM1_START_X, MAP_ROOM1_START_Y);
}

void game_shutdown(Game* g)
{
	pnx_arena_destroy(&g->arena);
}

// ---------------------------------------------------------------------------- main
//
// This is the whole thing. No accumulator, no manual event pump, no hand-written frame
// function -- pnx_app_frame (handed to pnx_platform_run below) is doing everything
// examples/empty's frame() and examples/overworld's frame() wrote out by hand.

int main(void)
{
	static Game g;
	if (!game_boot(&g))
		return 1;

	pnx_platform_run(pnx_app_frame, &g);

	game_shutdown(&g);
	return 0;
}
```

**Large buffers go in a heap arena (`pnx_arena_init_max`), not a `static` array.** Static
data shares one 64KB `uint16` ceiling with all your code; the heap has the rest of the
128KB slot. `pnx_arena_init_max` sizes the arena from whatever the platform actually has
free at startup, rather than a byte constant you pick and have to remember to raise --
see `pnx_arena.h`'s own comment for how one arena serves both persistent and per-scene
allocations without needing two.

**If you're used to hand-rolling the loop**, `examples/empty/src/c/main.c` is what the
same shape looks like *without* `pnx_app` -- a `frame()` function that pumps events,
feeds an accumulator, runs fixed ticks, and renders, all written out by hand, plus a
`main()` that calls `pnx_platform_run(frame, &g)` directly. Reading it once is worth it
for understanding what `pnx_app_frame` is actually doing underneath; writing a new game
against it instead of `pnx_app` is no longer the recommended starting point.

## 6. Build and run it

```sh
pebble build
```

This prints the size report on every build -- read it, not just on failure. `virtual_size`
in the app header is a `uint16`, and going over it fails with a `struct.error` that names
nothing useful; the report tells you which module grew before that happens.

```
module                 text   rodata    data      bss    total
--------------------------------------------------------------
pnx/assets             5838        0       1      506     6345
...
--------------------------------------------------------------
TOTAL                 11898      130      33     3557    15618
[###########.............................] 19441 / 65535 bytes (29.7%)
```

Then either the editor's Device tab (pick a platform, Build & Run) or directly:

```sh
pebble install --emulator emery
```

Press SELECT a few times -- the hero walks right, and eight steps in, through the door
into room 2. That's the whole loop: paint, wire a warp, write four hooks, watch it warp.

For real hardware instead of the emulator: set the phone's address on the editor's
Settings tab (the Pebble app's own Developer Connection screen shows it), then Device tab
-> Build & install, and Attach logs to see `pnx_log()` output streamed over. One gotcha:
logs emitted from `main()`/`game_boot` are dropped before the log stream attaches, so
`game_boot`'s own log line above won't show up even with logs running -- draw the info to
screen instead, or call `pnx_diag_flush()` from a button press once the app is up.

**No SDK at all, fastest of all:** `tools/host_harness.c` plus `tools/preview.py`'s PNG
contact sheet run the real game logic and real pixels on your laptop, with no hardware
timing. Reach for this while iterating on map or dialog content -- much faster than
waiting on a build.

**QEMU is real for logic, not for feel.** The emulator panel is genuinely running your
game -- warps, collision, HUD, all of it work exactly as they will on the watch. Frame
rate is the one thing not to trust from it: QEMU's `cortex-m33` emulation (the Time 2's
chip) measured far slower than its mature `cortex-m4` boards during this framework's own
development, so a build that looks sluggish in the emulator can still hold its ceiling
fine on real hardware. See "How much a QEMU run can be trusted" in `docs/EDITOR.md` for
the numbers behind that.

## 7. Where to go from here

A few natural next steps from this exact example, roughly in order of how much they cost:

- **A third tile role.** Give the door its own tile in the atlas instead of reusing
  `floor` -- add a tile to `art/tiles.png`, add it to `autopick`, point `[legend."D"]` at
  it. Nothing in `main.c` changes.
- **The other two directions.** `field_input` only wires UP/DOWN/SELECT. Turning three
  buttons into four directions of movement is a real design choice, not a missing line --
  see `resonant/src/c/field.c` for one answer (turn-and-step, plus touch drag on the
  platforms that have it).
- **A second state.** A pause overlay is `pnx_app_push(&pause_state)` from `field_input`
  on a button press, and `pnx_app_pop()` to return -- the field state is automatically
  suspended while it's covered, so nothing needs to check "is the pause menu open" by
  hand. `resonant/src/c/menu.c` is a real one.
- **Dialogue.** A `[dialog.*]` table in the manifest, drawn with `pnx_text_draw` and a
  `[[font]]` -- `examples/overworld/assets.toml` has both, plus the HUD font this
  walkthrough skipped entirely.

And the rest of the documentation set, once you want the reasoning rather than the steps:

- [`docs/EDITOR.md`](EDITOR.md) -- the editor's own design log: why each tab exists, what's
  a stub on purpose, what the emulator panel can and can't tell you.
- [`docs/DESIGN.md`](DESIGN.md) -- the framework's architecture and API rationale.
- [`docs/MEASUREMENTS.md`](MEASUREMENTS.md) -- every hardware number this framework's
  design decisions rest on, including the ones that overturned an assumption.
- [`docs/ROADMAP.md`](ROADMAP.md) -- milestones and current state.
- [`docs/GAME.md`](GAME.md) -- the RPG this framework is actually being built for.
- `tools/lint.sh` / `tools/lint.sh fix` -- formatting and static analysis, same portable
  slice the host tests cover.
