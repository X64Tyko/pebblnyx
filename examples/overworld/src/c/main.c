// M3 example: a walkable scene built only from framework APIs.
//
// There is no drawing code here and no asset loading sequence -- the scene declares what
// it needs in assets.toml, the framework loads it, and the tilemap and sprite modules do
// the rest. That is the milestone's acceptance criterion: if a game still needs its own
// blitter, the framework has not earned the name.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

// Sized from the pipeline's own scene residency report, which prints the worst case at
// build time -- 10,086 B for the outdoor scene. Rounded up rather than guessed.
#define SCENE_ARENA_BYTES	(14 * 1024)
#define PERSIST_ARENA_BYTES 1024

#define MAX_SPRITES 8

typedef struct
{
	PnxArena persistent, scene;
	PnxCamera camera;

	PnxSpriteInstance sprites[MAX_SPRITES];
	uint8_t order[MAX_SPRITES];
	uint8_t sprite_count;

	uint8_t current_scene;
	int32_t hero_tx, hero_ty;  // tiles
	uint8_t walk_phase;

	uint32_t accumulator_ms, ticks;
	char hud[40];
	int8_t input_dx, input_dy;
	bool ready;
} Game;

static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;

// The hero is always instance 0, so the camera and movement have something stable to
// follow without searching the list every frame.
#define HERO 0

// ------------------------------------------------------------------- scene loading

static bool enter_scene(Game* g, uint8_t scene, int32_t tx, int32_t ty)
{
	if (!pnx_scene_load(scene))
		return false;

	PnxMap* map = pnx_scene_map();
	if (!map)
		return false;

	// The tile size comes from the MAP now, not from a scene atlas: a map draws from
	// several tilesets and streams them, so there is no single "the" atlas to ask, and
	// nothing is resident until the streamer has run.
	const int32_t T = map->tile_px;

	// Load what the first frame will show before it is shown. Without this the scene draws
	// once with an empty world, which reads as a flash of holes on every warp.
	pnx_camera_center(&g->camera, tx * T + T / 2, ty * T + T, pnx_tilemap_width(map),
					  pnx_tilemap_height(map));
	pnx_tilemap_stream_now(map, &g->camera);

	g->current_scene = scene;
	g->hero_tx = tx;
	g->hero_ty = ty;
	g->sprite_count = 0;

	g->sprites[HERO] = (PnxSpriteInstance){
		.x = tx * T + T / 2,
		.y = ty * T + T,  // feet, centre of the tile
		.sprite = 0,
		.frame = HERO_STAND,
		.palette = PNX_SPRITE_PALETTE_DEFAULT,
	};
	g->sprite_count = 1;

	// The npc only exists in scenes that declared it, which the framework reports rather
	// than the game assuming.
	if (pnx_scene_sprite_count() > 1)
	{
		g->sprites[g->sprite_count++] = (PnxSpriteInstance){
			.x = 8 * T + T / 2,
			.y = 6 * T + T,
			.sprite = 1,
			.frame = 0,
			.palette = PNX_SPRITE_PALETTE_DEFAULT,
		};
	}
	return true;
}

// ------------------------------------------------------------------------ movement

static void try_move(Game* g, int32_t dx, int32_t dy)
{
	PnxMap* map = pnx_scene_map();
	if (!map)
		return;

	const int32_t nx = g->hero_tx + dx, ny = g->hero_ty + dy;
	if (dx < 0)
		g->sprites[HERO].flags |= PNX_SPRITE_MIRROR;
	if (dx > 0)
		g->sprites[HERO].flags &= (uint8_t)~PNX_SPRITE_MIRROR;

	if (pnx_map_solid(map, nx, ny))
		return;

	g->hero_tx = nx;
	g->hero_ty = ny;
	g->walk_phase ^= 1;

	const int32_t T = map->tile_px;
	g->sprites[HERO].x = nx * T + T / 2;
	g->sprites[HERO].y = ny * T + T;
	g->sprites[HERO].frame = g->walk_phase ? HERO_STEP_A : HERO_STEP_B;

	const PnxWarp* warp = pnx_map_warp_at(map, nx, ny);
	if (warp)
	{
		// dest_map indexes the manifest's map order; scenes are declared in the same order
		// here, so one maps onto the other.
		const uint8_t dest = warp->dest_map == 0 ? PNX_SCENE_OUTDOOR : PNX_SCENE_CAVE;
		pnx_log("warp -> scene %u at %u,%u", dest, warp->dest_x, warp->dest_y);
		enter_scene(g, dest, warp->dest_x, warp->dest_y);
	}
}

// --------------------------------------------------------------------------- frame

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	Game* g = (Game*)ctx;
	const uint32_t work_start = pnx_platform_now_ms();

	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		if (ev.type != PNX_EVENT_BUTTON_DOWN)
			continue;
		switch (ev.button)
		{
			case PNX_BUTTON_UP:
				g->input_dy = -1;
				break;
			case PNX_BUTTON_DOWN:
				g->input_dy = 1;
				break;
			case PNX_BUTTON_SELECT:
				g->input_dx = 1;
				break;
			default:
				break;
		}
	}

	// While covered by a modal the app is throttled to ~0.4fps, so a frame can arrive
	// carrying seconds. Without this clamp the sim fast-forwards through all of it.
	g->accumulator_ms += elapsed_ms;
	const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
	if (g->accumulator_ms > max_ms)
		g->accumulator_ms = max_ms;

	while (g->accumulator_ms >= PNX_TICK_MS)
	{
		g->accumulator_ms -= PNX_TICK_MS;
		g->ticks++;

		// Consumed on the first tick only: a frame may carry several, and replaying one
		// input snapshot across all of them makes a single press fire repeatedly.
		if (g->input_dx || g->input_dy)
		{
			try_move(g, g->input_dx, g->input_dy);
			g->input_dx = g->input_dy = 0;
		}
	}

	if (!g->ready)
	{
		pnx_gfx_clear(target, 0xC0);
	}
	else
	{
		PnxMap* map = pnx_scene_map();

		pnx_camera_center(&g->camera, g->sprites[HERO].x, g->sprites[HERO].y,
						  pnx_tilemap_width(map), pnx_tilemap_height(map));

		// Stream, then draw. The margin means this is usually a no-op -- what it costs when
		// it is not is a couple of ~44 us reads, against the 37.33 ms frame.
		pnx_tilemap_stream(map, &g->camera);
		pnx_tilemap_draw(map, target, &g->camera);
		pnx_sprites_draw_sorted(g->sprites, g->sprite_count, g->order, target, &g->camera);

		// The HUD draws HERE, inside the frame, like anything else. It used to be deferred
		// to post_frame because the SDK would not render text into a held framebuffer -- so
		// text could never have anything drawn over it, and each draw cost ~4.3 ms. A glyph
		// blit is an ordinary blit.
		//
		// y is the BASELINE, so adding the font's own baseline positions from the top edge.
		const PnxFont* hud = pnx_scene_font(0);
		if (hud && g->hud[0])
		{
			pnx_text_draw(target, hud, g->hud, 2, 2 + hud->baseline, 0xFF);
		}
	}

	pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);

	// Report the render cost every 2s. This is the M3 measurement: how long a full frame
	// of 4bpp tilemap plus depth-sorted sprites actually takes, against the 2,350 us
	// measured for the same screen of 8bpp tiles and ~35 ms of free time per frame.
	const PnxFrameStats* st = pnx_diag_stats();
	if (st && g->ticks % 50 == 0 && st->frames)
	{
		pnx_format(g->hud, sizeof(g->hud), "%u.%ufps work %uus", (unsigned)(st->fps_x10 / 10),
				   (unsigned)(st->fps_x10 % 10), (unsigned)st->work_us);
		if (g->ticks == 50)
			pnx_diag_flush();
		pnx_log("render: %s worst %uus", g->hud, (unsigned)st->worst_us);
	}
}

// Runs after the framebuffer is released. The HUD used to be drawn here, because the
// SDK would not render text into a held framebuffer; it now draws in the frame itself
// through the glyph blitter. Nothing needs this hook any more, and it stays only to keep
// the audio feed off the capture window -- see the note on PnxPostFrameFn.
static void post_frame(void* ctx)
{
	(void)ctx;
}

// ---------------------------------------------------------------------------- main

int main(void)
{
	static Game g;
	memset(&g, 0, sizeof(g));

	if (!pnx_arena_init(&g.persistent, "persistent", PERSIST_ARENA_BYTES, 4) ||
		!pnx_arena_init(&g.scene, "scene", SCENE_ARENA_BYTES, 4))
	{
		pnx_platform_log("arena init failed");
		return 1;
	}
	pnx_assets_init(&g.persistent, &g.scene, RESOURCES, PNX_ASSET_COUNT);
	pnx_camera_init(&g.camera, 200, 228);

	if (!pnx_scenes_load(PNX_ASSET_SCENES_SCENES))
	{
		pnx_log("scene table failed to load");
	}
	else
	{
		g.ready = enter_scene(&g, PNX_SCENE_OUTDOOR, MAP_OUTDOOR_START_X, MAP_OUTDOOR_START_Y);
	}
	pnx_log("start: ready=%d, %u palettes, %u B flash", (int)g.ready, pnx_palette_count(),
			(unsigned)pnx_assets_bytes_loaded());

	pnx_platform_set_post_frame_fn(post_frame);
	pnx_platform_run(frame, &g);

	pnx_arena_destroy(&g.scene);
	pnx_arena_destroy(&g.persistent);
	return 0;
}
