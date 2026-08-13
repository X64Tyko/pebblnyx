// M4d example: a world that does not fit in RAM.
//
// The overworld example shows what the framework can DRAW. This one shows what it can
// HOLD. The field is 192x192 cells -- 73,728 bytes of cell plane against 128 KB of app
// RAM on `emery`, and 75,232 bytes of resource -- so it is never resident, at any moment,
// in any form. It arrives sixteen WorldTiles at a time, and its three tilesets arrive
// through two atlas slots.
//
// The reason this is a separate example rather than a bigger overworld is that the thing
// worth looking at is invisible in ordinary play. When streaming works you see a world;
// when it fails you see holes. So the interesting state is drawn ON SCREEN: which
// WorldTiles are resident, which atlas slots hold what, and how many bytes have come off
// flash since the app started. The autopilot exists for the same reason -- a streaming
// bug that only appears after crossing forty WorldTile boundaries is not one anybody
// finds by walking there.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

// The comparison this example exists to make, measured rather than estimated -- these are
// what the scenes actually occupy, and the pipeline prints the same figures at build time:
//
//   field  192x192, streamed   23,678 B     16 of 144 WorldTiles, 3 atlases in 2 slots
//   plain  192x192, held whole 98,551 B    144 of 144 WorldTiles, 3 atlases in 3 slots
//
// Same world, same tilesets, same rows. `plain` differs from `field` by `resident = true`
// in the manifest and nothing else, so 4.2x is the price of holding it rather than
// streaming it.
//
// **And note how little room is left.** `emery` reports ~111 KB of heap, so the held-whole
// world fits with about 12 KB to spare -- before the game allocates anything of its own.
// That is the argument for WorldTiles in one number: not that holding a 192x192 world is
// impossible, but that it consumes the watch and leaves nothing to build a game with.
#define FIELD_ARENA_BYTES  (26 * 1024)
#define PLAIN_ARENA_BYTES  (100 * 1024)
#define PERSIST_ARENA_BYTES 1024

#define MAX_SPRITES 4
#define HERO 0

// Four speeds, in tiles per tick. The top two exist to outrun PNX_MAP_STREAM_BUDGET on
// purpose: at 8 tiles a tick the camera crosses a WorldTile every two ticks, the streamer
// cannot refill its margin, and the HUD's `m` counter starts climbing. That is not a bug
// being demonstrated -- it is the bound being shown, and the reason a game that wants a
// dash needs pnx_map_stream_now rather than a bigger budget.
static const uint8_t SPEEDS[] = { 1, 2, 4, 8 };
#define SPEED_COUNT ((uint8_t)(sizeof(SPEEDS) / sizeof(SPEEDS[0])))

// Eight compass headings, clockwise from north. Eight rather than four because a diagonal
// crosses a WorldTile CORNER, which asks the streamer for tiles in two directions at once
// and is the case a four-way demo never reaches.
static const int8_t HEADING[8][2] = {
  { 0, -1}, { 1, -1}, { 1,  0}, { 1,  1},
  { 0,  1}, {-1,  1}, {-1,  0}, {-1, -1},
};
#define HEADING_COUNT 8

// How long the player has to leave the buttons alone before the autopilot takes over
// again. Long enough not to fight someone thinking about their next move, short enough
// that putting the watch down leaves it stress-testing itself.
#define AUTOPILOT_IDLE_MS 5000

typedef struct {
  PnxArena persistent, scene;
  PnxCamera camera;

  PnxSpriteInstance sprites[MAX_SPRITES];
  uint8_t order[MAX_SPRITES];
  uint8_t sprite_count;

  uint8_t current_scene;
  int32_t hero_tx, hero_ty;
  uint8_t heading;              // index into HEADING
  bool walking;
  uint8_t walk_phase;
  uint8_t speed;                // index into SPEEDS
  bool autopilot;
  uint32_t last_input_ms;

  uint8_t missing;              // WorldTiles the last stream call could not reach
  uint32_t worst_missing;
  uint32_t accumulator_ms, ticks;
  char hud[48];
  size_t scene_bytes;           // arena used by the current scene, for the comparison
  bool have_plain;              // false when the big arena would not fit
  bool ready;
} Game;

static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;

// A warp's dest_map indexes the MANIFEST's map order; scenes are sorted alphabetically.
// The two are not the same order and there is no reason they should be, so the mapping is
// written down once here rather than inferred at each warp.
//
//   manifest maps:  0 field  1 plain  2 hut  3 crypt  4 keep
static const uint8_t SCENE_FOR_MAP[] = {
  PNX_SCENE_FIELD, PNX_SCENE_PLAIN, PNX_SCENE_HUT, PNX_SCENE_CRYPT, PNX_SCENE_KEEP,
};
static const char *const MAP_NAME[] = { "field", "plain", "hut", "crypt", "keep" };

static uint8_t map_of_scene(uint8_t scene) {
  for (uint8_t i = 0; i < sizeof(SCENE_FOR_MAP); i++) {
    if (SCENE_FOR_MAP[i] == scene) return i;
  }
  return 0;
}

// ------------------------------------------------------------------- scene loading

static void place_hero(Game *g, int32_t tx, int32_t ty, int32_t T) {
  g->hero_tx = tx;
  g->hero_ty = ty;
  g->sprites[HERO].x = tx * T + T / 2;   // feet, centre of the tile
  g->sprites[HERO].y = ty * T + T;
}

static bool enter_scene(Game *g, uint8_t scene, int32_t tx, int32_t ty) {
  if (!pnx_scene_load(scene)) return false;

  PnxMap *map = pnx_scene_map();
  if (!map) return false;
  const int32_t T = map->tile_px;

  g->current_scene = scene;
  g->sprite_count = 0;
  g->sprites[HERO] = (PnxSpriteInstance){
    .sprite = 0, .frame = 0, .palette = PNX_SPRITE_PALETTE_DEFAULT,
  };
  g->sprite_count = 1;
  place_hero(g, tx, ty, T);

  // Blocking, not budgeted. A warp has no previous frame to show, so spreading the reads
  // over several frames would put the holes on screen instead of in the loading pause
  // nobody sees. Sixteen WorldTiles and two atlases is ~1.5 ms.
  pnx_camera_center(&g->camera, g->sprites[HERO].x, g->sprites[HERO].y,
                    pnx_tilemap_width(map), pnx_tilemap_height(map));
  g->missing = pnx_tilemap_stream_now(map, &g->camera);
  if (g->missing) {
    pnx_log("scene %u: %u WorldTiles still missing after a blocking stream",
            scene, g->missing);
  }

  // Captured after the blocking stream, so it is what the scene ACTUALLY holds rather
  // than what it had allocated before any WorldTile arrived. This is the number the whole
  // comparison turns on, so it is read from the arena rather than recomputed.
  g->scene_bytes = g->scene.used;

  pnx_log("scene %u (%s): %ux%u, %u/%u WorldTiles resident, %u B arena, %u B read",
          scene, MAP_NAME[map_of_scene(scene)], map->w, map->h,
          pnx_map_resident(map), (unsigned)(map->wt_cols * map->wt_rows),
          (unsigned)g->scene_bytes, (unsigned)pnx_assets_bytes_loaded());
  return true;
}

// The A/B, on one button. `field` and `plain` are the same 192x192 world drawn from the
// same three tilesets; the only difference is `resident = true` in the manifest. Swapping
// between them at the same hero position changes nothing on screen and everything in the
// arena figure, which is the most direct way to state what WorldTiles buy.
static void toggle_world(Game *g) {
  if (!g->have_plain) {
    pnx_log("held-whole world unavailable: %u B of arena would not allocate",
            (unsigned)PLAIN_ARENA_BYTES);
    return;
  }
  const bool streaming = (g->current_scene == PNX_SCENE_FIELD);
  const uint8_t to = streaming ? PNX_SCENE_PLAIN : PNX_SCENE_FIELD;
  const int32_t tx = g->hero_tx, ty = g->hero_ty;
  if (!enter_scene(g, to, tx, ty)) {
    pnx_log("could not enter %s -- staying put", MAP_NAME[map_of_scene(to)]);
    enter_scene(g, streaming ? PNX_SCENE_FIELD : PNX_SCENE_PLAIN, tx, ty);
  }
}

// ------------------------------------------------------------------------ movement

// One step, or as far along it as the world allows. Returns false when blocked, which is
// what turns the autopilot rather than stopping it.
static bool try_move(Game *g, int32_t dx, int32_t dy) {
  PnxMap *map = pnx_scene_map();
  if (!map || (!dx && !dy)) return false;

  const int32_t nx = g->hero_tx + dx, ny = g->hero_ty + dy;
  if (dx < 0) g->sprites[HERO].flags |= PNX_SPRITE_MIRROR;
  if (dx > 0) g->sprites[HERO].flags &= (uint8_t)~PNX_SPRITE_MIRROR;

  // A cell whose WorldTile is not resident reads as solid, so this also stops the hero at
  // the edge of what has loaded rather than walking into a void. With the streamer keeping
  // up it never fires; at speed 8 it is what `miss` on the HUD is counting.
  if (pnx_map_solid(map, nx, ny)) return false;

  place_hero(g, nx, ny, map->tile_px);
  g->walk_phase = (uint8_t)((g->walk_phase + 1) % 3);
  g->sprites[HERO].frame = g->walk_phase;

  const PnxWarp *warp = pnx_map_warp_at(map, nx, ny);
  if (warp && warp->dest_map < sizeof(SCENE_FOR_MAP)) {
    pnx_log("warp -> %s at %u,%u", MAP_NAME[warp->dest_map],
            warp->dest_x, warp->dest_y);
    enter_scene(g, SCENE_FOR_MAP[warp->dest_map], warp->dest_x, warp->dest_y);
    return true;
  }
  return true;
}

// One step along the current heading. Blocked, it turns rather than stopping -- so a wall
// deflects the walk instead of ending it, which is what keeps the autopilot moving and
// what makes manual walking into scenery feel like sliding rather than sticking.
static bool step_heading(Game *g) {
  const int8_t dx = HEADING[g->heading][0], dy = HEADING[g->heading][1];
  if (try_move(g, dx, dy)) return true;

  // A diagonal blocked head-on usually has one of its two components free; try those
  // before giving up, which is what stops a diagonal walk dying on every fence post.
  if (dx && dy) {
    if (try_move(g, dx, 0)) return true;
    if (try_move(g, 0, dy)) return true;
  }
  return false;
}

// The patrol. It turns on contact and keeps a bias, so it works its way across the whole
// field rather than pacing one corner -- which is the only way a streaming bug at
// WorldTile 97 ever gets found.
static void autopilot(Game *g) {
  if (step_heading(g)) return;
  for (uint8_t turn = 1; turn < HEADING_COUNT; turn++) {
    g->heading = (uint8_t)((g->heading + 1) % HEADING_COUNT);
    if (step_heading(g)) return;
  }
}

// --------------------------------------------------------------------------- the HUD
//
// The residency grid is the point of this example. One cell per WorldTile in the map,
// filled when resident and outlined when not, with the camera's own WorldTile marked --
// so "sixteen of a hundred and forty-four" is a shape rather than a number, and an
// eviction that takes the wrong tile is visible the moment it happens.

#define GRID_CELL 4
#define GRID_PAD  1

static void draw_residency(PnxTarget *t, const PnxMap *m, const PnxCamera *cam) {
  const int32_t span = (int32_t)m->tile_px * m->worldtile;
  const int32_t cam_wx = cam->x / span, cam_wy = cam->y / span;

  const int16_t gw = (int16_t)(m->wt_cols * GRID_CELL);
  const int16_t gh = (int16_t)(m->wt_rows * GRID_CELL);
  const int16_t x0 = (int16_t)(200 - gw - 3);
  const int16_t y0 = 3;

  pnx_gfx_fill_rect(t, x0 - 2, y0 - 2, (int16_t)(gw + 4), (int16_t)(gh + 4), 0xC0);

  for (uint8_t wy = 0; wy < m->wt_rows; wy++) {
    for (uint8_t wx = 0; wx < m->wt_cols; wx++) {
      const bool live = m->wt_slot[(uint32_t)wy * m->wt_cols + wx] != PNX_MAP_NO_SLOT;
      const bool here = (wx == cam_wx && wy == cam_wy);
      const uint8_t shade = here ? 0xFF : (live ? 0xEA : 0xD5);
      pnx_gfx_fill_rect(t, x0 + wx * GRID_CELL, y0 + wy * GRID_CELL,
                        GRID_CELL - GRID_PAD, GRID_CELL - GRID_PAD, shade);
    }
  }
}

// The atlas pool, as one bar per slot. Which tileset is in which slot only matters when
// there are fewer slots than atlases -- so on the interiors this is one full bar and
// says nothing, and on the field it is where an eviction becomes visible.
static void draw_atlas_pool(PnxTarget *t, const PnxMap *m, const PnxFont *f) {
  if (!f) return;
  char line[24];
  char *p = line;
  *p++ = 'A';
  *p++ = ':';
  for (uint8_t i = 0; i < m->atlas_count && p < line + sizeof(line) - 3; i++) {
    // '0'..'n' for a resident atlas, '.' for one whose slot has been evicted.
    *p++ = (m->atlas[i].slot == PNX_MAP_NO_SLOT) ? '.' : (char)('0' + i);
  }
  *p = '\0';
  pnx_text_draw(t, f, line, 2, 228 - 4, 0xFF);
}

// --------------------------------------------------------------------------- frame

static void frame(void *ctx, uint32_t elapsed_ms, PnxTarget *target) {
  Game *g = (Game *)ctx;
  const uint32_t work_start = pnx_platform_now_ms();

  // UP and DOWN steer, turning the heading one compass point each way; SELECT is the
  // streamed/held-whole comparison, because that is what this example is for. A touch
  // points the walk straight at wherever you touched, which is how you cross 192 tiles
  // without turning ninety times.
  //
  // Nothing here toggles the autopilot: it yields to any input and takes over again after
  // AUTOPILOT_IDLE_MS, so putting the watch down leaves it stress-testing itself. A
  // control for it would be a control for something the player never needs to ask for.
  PnxEvent ev;
  const uint32_t now = pnx_platform_now_ms();
  while (pnx_platform_poll_event(&ev)) {
    if (ev.type == PNX_EVENT_TOUCH_DOWN || ev.type == PNX_EVENT_TOUCH_MOVE) {
      // The heading is from the HERO to the touch, not from the screen's middle: the
      // camera clamps at the map's edges, so the two part company exactly where precise
      // steering matters most.
      const int32_t hx = g->sprites[HERO].x - g->camera.x;
      const int32_t hy = g->sprites[HERO].y - g->camera.y;
      const int32_t dx = ev.x - hx, dy = ev.y - hy;

      // Nearest of the eight, by comparing each axis against half the other. The
      // threshold is what makes a diagonal a third of the circle rather than a knife edge
      // nobody can hit with a fingertip.
      const int32_t ax = dx < 0 ? -dx : dx, ay = dy < 0 ? -dy : dy;
      const int8_t sx = (int8_t)(ax * 2 > ay ? (dx < 0 ? -1 : 1) : 0);
      const int8_t sy = (int8_t)(ay * 2 > ax ? (dy < 0 ? -1 : 1) : 0);
      for (uint8_t i = 0; i < HEADING_COUNT; i++) {
        if (HEADING[i][0] == sx && HEADING[i][1] == sy) { g->heading = i; break; }
      }
      g->walking = (sx || sy);
      g->autopilot = false;
      g->last_input_ms = now;
      continue;
    }
    if (ev.type == PNX_EVENT_TOUCH_UP) {
      g->walking = false;
      g->last_input_ms = now;
      continue;
    }
    if (ev.type != PNX_EVENT_BUTTON_DOWN) continue;

    g->autopilot = false;
    g->last_input_ms = now;
    switch (ev.button) {
      case PNX_BUTTON_UP:
        g->heading = (uint8_t)((g->heading + HEADING_COUNT - 1) % HEADING_COUNT);
        g->walking = true;
        break;
      case PNX_BUTTON_DOWN:
        g->heading = (uint8_t)((g->heading + 1) % HEADING_COUNT);
        g->walking = true;
        break;
      case PNX_BUTTON_SELECT:
        toggle_world(g);
        break;
      default: break;
    }
  }

  // Back to patrolling once the player has left it alone. Speed resets with it, so an
  // unattended watch always ends up at the gentle end rather than wherever it was left.
  if (!g->autopilot && now - g->last_input_ms > AUTOPILOT_IDLE_MS) {
    g->autopilot = true;
    g->walking = false;
    g->speed = 0;
  }

  g->accumulator_ms += elapsed_ms;
  const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
  if (g->accumulator_ms > max_ms) g->accumulator_ms = max_ms;

  while (g->accumulator_ms >= PNX_TICK_MS) {
    g->accumulator_ms -= PNX_TICK_MS;
    g->ticks++;

    // The autopilot works its way up through the speeds and back down, so the stress case
    // that outruns PNX_MAP_STREAM_BUDGET happens on its own. It used to be on a button,
    // which meant it only happened if someone thought to press one -- and the whole point
    // of an autopilot is finding what nobody thought to try.
    if (g->autopilot && g->ticks % 150 == 0) {
      g->speed = (uint8_t)((g->speed + 1) % SPEED_COUNT);
    }

    if (g->autopilot) {
      for (uint8_t step = 0; step < SPEEDS[g->speed]; step++) autopilot(g);
    } else if (g->walking) {
      // Manual walking stays at one tile a tick. Steering is for looking at the world;
      // the speeds exist to break the streamer, and doing that by accident while trying
      // to turn a corner would read as the world tearing rather than as a bound.
      step_heading(g);
    }
  }

  if (!g->ready) {
    pnx_gfx_clear(target, 0xC0);
  } else {
    PnxMap *map = pnx_scene_map();

    pnx_camera_center(&g->camera, g->sprites[HERO].x, g->sprites[HERO].y,
                      pnx_tilemap_width(map), pnx_tilemap_height(map));

    // Stream, then draw, in that order and every frame. The margin means this is usually
    // a no-op; when it is not, it is a handful of ~45 us reads against a 37.33 ms frame.
    g->missing = pnx_tilemap_stream(map, &g->camera);
    if (g->missing > g->worst_missing) g->worst_missing = g->missing;

    pnx_tilemap_draw(map, target, &g->camera);
    pnx_sprites_draw_sorted(g->sprites, g->sprite_count, g->order, target, &g->camera);

    const PnxFont *hud = pnx_scene_font(0);
    if (hud) {
      if (g->hud[0]) pnx_text_draw(target, hud, g->hud, 2, 2 + hud->baseline, 0xFF);
      draw_atlas_pool(target, map, hud);
    }
    draw_residency(target, map, &g->camera);
  }

  pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);

  // Every ~1s. The two numbers that matter are `WT` -- how much of the world is held --
  // and `read`, which climbs whenever an atlas or a WorldTile crosses the flash boundary.
  // A `read` that keeps climbing while standing still would mean the streamer is
  // thrashing; one that only moves when you do is the thing working.
  const PnxFrameStats *st = pnx_diag_stats();
  if (st && g->ticks % 25 == 0 && st->frames && g->ready) {
    const PnxMap *m = pnx_scene_map();
    // The arena figure leads, because it is the answer. Everything after it is context
    // for whether that answer was bought at any cost worth caring about.
    pnx_format(g->hud, sizeof(g->hud), "%s %uKB %u/%u m%u %u.%ufps",
               MAP_NAME[map_of_scene(g->current_scene)],
               (unsigned)(g->scene_bytes / 1024),
               m ? pnx_map_resident(m) : 0,
               m ? (unsigned)(m->wt_cols * m->wt_rows) : 0,
               (unsigned)g->worst_missing,
               (unsigned)(st->fps_x10 / 10), (unsigned)(st->fps_x10 % 10));
    if (g->ticks == 25) pnx_diag_flush();
    pnx_log("%s at %d,%d speed %u read %uKB worst %uus",
            g->hud, (int)g->hero_tx, (int)g->hero_ty, SPEEDS[g->speed],
            (unsigned)(pnx_assets_bytes_loaded() / 1024), (unsigned)st->worst_us);
  }
}

static void post_frame(void *ctx) { (void)ctx; }

// ---------------------------------------------------------------------------- main

int main(void) {
  static Game g;
  memset(&g, 0, sizeof(g));
  g.autopilot = true;
  g.heading = 2;                 // east, so the patrol starts across the map rather than into a wall

  if (!pnx_arena_init(&g.persistent, "persistent", PERSIST_ARENA_BYTES, 4)) {
    pnx_platform_log("arena init failed");
    return 1;
  }

  // Ask for the arena the HELD-WHOLE world needs, and fall back to the streamed one's if
  // the heap will not give it. Both outcomes are the result: on a watch where 96 KB
  // allocates, SELECT swaps between them and the HUD shows 23 against 95; on one where it
  // does not, the comparison has already been made and the app still runs -- which a hard
  // failure at startup would not have told anyone.
  g.have_plain = pnx_arena_init(&g.scene, "scene", PLAIN_ARENA_BYTES, 4);
  if (!g.have_plain && !pnx_arena_init(&g.scene, "scene", FIELD_ARENA_BYTES, 4)) {
    pnx_platform_log("arena init failed");
    return 1;
  }

  pnx_assets_init(&g.persistent, &g.scene, RESOURCES, PNX_ASSET_COUNT);
  pnx_assets_expect_orientation(PNX_ORIENTATION);
  pnx_camera_init(&g.camera, 200, 228);

  if (!pnx_scenes_load(PNX_ASSET_SCENES_SCENES)) {
    pnx_log("scene table failed to load");
  } else {
    g.ready = enter_scene(&g, PNX_SCENE_FIELD,
                          MAP_FIELD_START_X, MAP_FIELD_START_Y);
  }

  // The claim this example exists to make, in one line, at startup.
  pnx_log("field: %u cells = %u B of plane, resident in %u B",
          (unsigned)(MAP_FIELD_W * MAP_FIELD_H),
          (unsigned)(MAP_FIELD_W * MAP_FIELD_H * 2), (unsigned)g.scene_bytes);
  pnx_log("held whole would be %u B -- %s",
          (unsigned)PLAIN_ARENA_BYTES,
          g.have_plain ? "press SELECT to load it" : "the heap refused that arena");

  pnx_platform_set_post_frame_fn(post_frame);
  pnx_platform_run(frame, &g);

  pnx_arena_destroy(&g.scene);
  pnx_arena_destroy(&g.persistent);
  return 0;
}
