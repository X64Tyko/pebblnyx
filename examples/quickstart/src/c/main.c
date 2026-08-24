// The QUICKSTART.md walkthrough, end to end: a walkable two-room map wired up entirely
// through pnx_app -- see docs/QUICKSTART.md section 4 for the tour.
//
// Compare this to examples/overworld/src/c/main.c, which does the same job (a scene,
// a hero, collision, one warp) but predates pnx_app: it hand-rolls the accumulator,
// the lifecycle-event handling and the top-level frame() function that this file no
// longer needs. Everything below is either content-specific (enter_room, try_move) or
// the four PnxAppOps hooks; there is no loop to write.

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
		// warp->dest_map indexes the MANIFEST'S map order (room1=0, room2=1 here) --
		// it happens to equal PnxSceneId in this file because both declaration orders are
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

	const bool ready = enter_room(g, PNX_SCENE_ROOM1, MAP_ROOM1_START_X, MAP_ROOM1_START_Y);
	pnx_log("start: ready=%d, %u B flash", (int)ready, (unsigned)pnx_assets_bytes_loaded());
	return ready;
}

void game_shutdown(Game* g)
{
	pnx_arena_destroy(&g->arena);
}

// ---------------------------------------------------------------------------- main
//
// This is the whole thing. No accumulator, no manual event pump, no hand-written frame
// function -- pnx_app_frame (pushed into pnx_platform_run below) is doing everything
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
