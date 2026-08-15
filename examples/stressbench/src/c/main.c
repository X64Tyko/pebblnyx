// Combined-load frame cost: audio + graphics + text + an incremental save step, together,
// on real hardware -- DESIGN.md's open question #3 ("each subsystem's cost is measured;
// the sum never was"), answered directly rather than left as reasoning.
//
// Every frame, continuously: a real map streamed and drawn under a continuously panning
// camera, a real sprite (with a recoloured `variants` entry, so its 1-bit build bakes and
// draws the canonical bw_variant rather than the base) walking a patrol loop, a HUD panel
// (a checkerboard of pnx_gfx_fill_rect calls, bounded to a strip rather than the whole
// screen now that real content sits under it) behind the status text, and a continuously
// looping sample feeding through the existing audio timer. Every few seconds, ON TOP of
// all of that, an incremental save (pnx_save_begin, ~2000 bytes / ~8 chunks) starts and
// spends one pnx_save_step() per frame until it finishes -- which is the actual
// M5-intended usage, and the one thing this app exists to answer: does a save step
// landing in the SAME frame as the graphics, tilemap and text load blow the ~35ms budget,
// and does it starve the audio timer enough to show up as a gap or a restart.
//
// The map and sprite exist so this app is what its own name says: M9's 1-bit blitter
// (the exclusively-2bpp ~bw path, pnx_gfx.c) never ran under combined load before this,
// because there was no real atlas/sprite/tilemap draw in the frame at all -- a synthetic
// fill_rect grid exercises pnx_gfx_fill_rect/clear, but never span_2bpp_packed, the
// palette-free BW path, or a sprite's canonical bw_variant bake. Placeholder art
// (tools/pnx_placeholder.py), not hand-authored -- the point here is load, not content.
//
// Tracks the worst single frame seen with a save step active, the worst without one, and
// audio stats immediately before/after each save cycle -- a contention signal, since the
// audio feed runs on its own timer specifically so frame work cannot block it (see
// pnx_platform.h's own note on why), and this is the first real test of whether that
// isolation holds up when the frame side is also under load.
//
// Getting the numbers off the watch: same discipline as the other pnx benchmarks. Waits
// on SELECT so `pebble logs` can be attached first; SELECT flushes to pass-through
// immediately; every save cycle logs its own result; every log line stays under 96 chars.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 512
// Room for the tileset atlas, the hero sprite (base + its bw_variant bake), the field map
// and its one WorldTile bank, and the font's glyph bitmaps -- all resident at once, which
// is the point: everything this app stresses is loaded together, the same as a real scene.
#define SCENE_BYTES (8 * 1024)

#define TEST_SLOT		   ((PnxSaveSlot)0)
#define SAVE_VERSION	   1
#define SAVE_PAYLOAD_BYTES 2000 // ~8 chunks at PNX_SAVE_CHUNK0_PAYLOAD=248 / 256 each
#define SAVE_INTERVAL_MS   3000 // a new save cycle starts this often once idle

static uint8_t s_payload[SAVE_PAYLOAD_BYTES];

// Keeps the hero inside the field's walls -- see hero_position below.
#define PATROL_MARGIN 24
#define PATROL_SPEED  2 // world pixels per frame

typedef struct
{
	PnxArena persistent, scene;
	PnxFont font;
	bool has_font;

	PnxCamera camera;
	PnxMap map;
	PnxSprite hero;
	bool has_map, has_hero;

	bool running;
	uint32_t frames;

	bool saving;
	uint32_t next_save_at_ms;
	uint32_t save_frames_this_cycle;
	PnxAudioStats stats_before_save;

	uint32_t worst_ms_with_save, worst_ms_no_save;
	uint32_t frames_with_save, frames_no_save;
	uint32_t sum_ms_with_save, sum_ms_no_save;

	char status[64];
	char result1[64], result2[64];
} App;

static void log_cycle_start(App* a)
{
	a->saving				  = true;
	a->save_frames_this_cycle = 0;
	a->stats_before_save	  = *pnx_audio_stats();
	for (size_t i = 0; i < SAVE_PAYLOAD_BYTES; i++)
		s_payload[i] = (uint8_t)(i ^ 0xA5);
	pnx_save_begin(TEST_SLOT, s_payload, SAVE_PAYLOAD_BYTES, SAVE_VERSION);
	pnx_log("stress: save cycle started -- %u bytes", (unsigned)SAVE_PAYLOAD_BYTES);
}

static void log_cycle_end(App* a)
{
	const PnxAudioStats* now_stats = pnx_audio_stats();
	pnx_log("stress: save cycle done -- %u frames, audio g +%u wg %u->%u",
			(unsigned)a->save_frames_this_cycle,
			(unsigned)(now_stats->left_playing - a->stats_before_save.left_playing),
			(unsigned)a->stats_before_save.worst_gap_ms, (unsigned)now_stats->worst_gap_ms);
	a->saving		   = false;
	a->next_save_at_ms = pnx_platform_now_ms() + SAVE_INTERVAL_MS;
}

// A checkerboard of pnx_gfx_fill_rect calls, same load shape as before this app drew real
// content -- bounded to a HUD strip along the top now, rather than the whole screen,
// since the tilemap and sprite need the rest of it to actually be visible. Tall enough to
// hold all four status lines drawn below (the last at y=85) with room for their own line
// height.
#define HUD_PANEL_H 100

static void draw_hud_panel(PnxTarget* target)
{
	static const uint8_t COLORS[2] = { 0xD5, 0xC7 }; // resonant's IN_DIM / IN_STEEL values
	const int16_t w				   = pnx_target_width(target);
	int idx						   = 0;
	for (int16_t y = 0; y < HUD_PANEL_H; y = (int16_t)(y + 16))
	{
		for (int16_t x = 0; x < w; x = (int16_t)(x + 16))
		{
			const int16_t rw = (int16_t)((x + 16 <= w) ? 16 : (w - x));
			const int16_t rh = (int16_t)((y + 16 <= HUD_PANEL_H) ? 16 : (HUD_PANEL_H - y));
			pnx_gfx_fill_rect(target, x, y, rw, rh, COLORS[idx & 1]);
			idx++;
		}
	}
}

// Walks a rectangular patrol loop just inside the field's walls, at a constant world-pixel
// speed -- real, continuous movement, so the camera streaming under it is real load too,
// not a static shot of one WorldTile. Frame index alternates step_a/step_b; mirror flips
// on the two legs walking leftward, the same convention the other examples use.
static void hero_position(uint32_t frames, const PnxMap* map, int32_t* wx, int32_t* wy,
						  uint8_t* anim_frame, bool* mirror)
{
	const int32_t x0 = PATROL_MARGIN, x1 = pnx_tilemap_width(map) - PATROL_MARGIN;
	const int32_t y0 = PATROL_MARGIN, y1 = pnx_tilemap_height(map) - PATROL_MARGIN;
	const int32_t leg_x = x1 - x0, leg_y = y1 - y0;
	const int32_t perim = 2 * (leg_x + leg_y);
	int32_t d			= (int32_t)((frames * PATROL_SPEED) % (uint32_t)perim);

	if (d < leg_x)
	{
		*wx		= x0 + d;
		*wy		= y0;
		*mirror = false;
	}
	else if ((d -= leg_x) < leg_y)
	{
		*wx		= x1;
		*wy		= y0 + d;
		*mirror = false;
	}
	else if ((d -= leg_y) < leg_x)
	{
		*wx		= x1 - d;
		*wy		= y1;
		*mirror = true;
	}
	else
	{
		d -= leg_x;
		*wx		= x0;
		*wy		= y1 - d;
		*mirror = true;
	}
	*anim_frame = (uint8_t)(1 + ((frames / 6) % 2)); // alternate step_a (1) / step_b (2)
}

static void audio_tick(void* ctx)
{
	(void)ctx;
	pnx_audio_update(pnx_platform_now_ms());
}

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	App* a			  = (App*)ctx;
	const uint32_t t0 = pnx_platform_now_ms();

	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_SELECT && !a->running)
		{
			a->running			  = true;
			a->frames			  = 0;
			a->saving			  = false;
			a->worst_ms_with_save = a->worst_ms_no_save = 0;
			a->frames_with_save = a->frames_no_save = 0;
			a->sum_ms_with_save = a->sum_ms_no_save = 0;
			a->next_save_at_ms						= pnx_platform_now_ms() + SAVE_INTERVAL_MS;
			pnx_diag_flush();
			pnx_log("stress: run started -- %u byte saves every %ums",
					(unsigned)SAVE_PAYLOAD_BYTES, (unsigned)SAVE_INTERVAL_MS);
		}
	}

	pnx_gfx_clear(target, 0xC0);

	if (a->running)
	{
		a->frames++;

		// The world, first: streamed and drawn under a continuously panning camera, with
		// the sprite walking its patrol loop -- real tilemap and sprite load, the thing
		// this app did not exercise before. The HUD panel and text draw over the top of
		// it afterwards, same as any real game's overlay.
		if (a->has_map)
		{
			int32_t hero_wx = 0, hero_wy = 0;
			uint8_t anim_frame = 0;
			bool mirror		   = false;
			hero_position(a->frames, &a->map, &hero_wx, &hero_wy, &anim_frame, &mirror);

#if PNX_USE_COLLISION
			// AABB collision, swept for real every frame: the patrol is scripted and
			// PATROL_MARGIN keeps it clear of every wall by construction, so this changes
			// nothing rendered -- only hero_wx/wy (the scripted position) ever reaches the
			// camera or the sprite draw below, the swept box is a throwaway local. It is
			// here so PNX_USE_COLLISION's real per-frame cost is part of the "everything
			// happening at once" load this app measures, not a config flag nobody calls.
			if (a->frames > 0)
			{
				int32_t prev_wx = 0, prev_wy = 0;
				uint8_t prev_frame = 0;
				bool prev_mirror   = false;
				hero_position(a->frames - 1, &a->map, &prev_wx, &prev_wy, &prev_frame,
							  &prev_mirror);
				PnxAABB box = pnx_aabb_from_feet(prev_wx, prev_wy, 12, 16);
				pnx_collision_move(&a->map, &box, hero_wx - prev_wx, hero_wy - prev_wy);
			}
#endif

			pnx_camera_center(&a->camera, hero_wx, hero_wy, pnx_tilemap_width(&a->map),
							  pnx_tilemap_height(&a->map));
			pnx_tilemap_stream(&a->map, &a->camera);
			pnx_tilemap_draw(&a->map, target, &a->camera);

			if (a->has_hero)
				pnx_sprite_draw(&a->hero, target, &a->camera, hero_wx, hero_wy, anim_frame,
								NULL, mirror);
		}

		draw_hud_panel(target);

		if (!a->saving && pnx_platform_now_ms() >= a->next_save_at_ms)
			log_cycle_start(a);
		if (a->saving)
		{
			pnx_save_step(TEST_SLOT);
			a->save_frames_this_cycle++;
			if (!pnx_save_pending(TEST_SLOT))
				log_cycle_end(a);
		}

		if (a->has_font)
		{
			pnx_format(a->status, sizeof(a->status), "frame %u  %s", (unsigned)a->frames,
					   a->saving ? "SAVING" : "idle");
			pnx_format(a->result1, sizeof(a->result1), "worst w/save %ums (%u fr)",
					   (unsigned)a->worst_ms_with_save, (unsigned)a->frames_with_save);
			pnx_format(a->result2, sizeof(a->result2), "worst no save %ums (%u fr)",
					   (unsigned)a->worst_ms_no_save, (unsigned)a->frames_no_save);
		}
	}
	else if (a->has_font)
	{
		pnx_format(a->status, sizeof(a->status), "SELECT to start (attach logs first)");
	}

	if (a->has_font)
	{
		pnx_text_draw(target, &a->font, "pnx stressbench", 10, 20, 0xFF);
		pnx_text_draw(target, &a->font, a->status, 10, 40, 0xC7);
		if (a->running)
		{
			pnx_text_draw(target, &a->font, a->result1, 10, 65, 0xF0);
			pnx_text_draw(target, &a->font, a->result2, 10, 85, 0xCC);
		}
	}

	// Measured AFTER all of this frame's work, including the save step and the synthetic
	// graphics -- this IS the combined-load number the whole app exists to produce.
	const uint32_t frame_ms = pnx_platform_now_ms() - t0;
	if (a->running)
	{
		if (a->saving)
		{
			a->frames_with_save++;
			a->sum_ms_with_save += frame_ms;
			if (frame_ms > a->worst_ms_with_save)
			{
				a->worst_ms_with_save = frame_ms;
				pnx_log("stress: new worst WITH save -- %ums (frame %u)", (unsigned)frame_ms,
						(unsigned)a->frames);
			}
		}
		else
		{
			a->frames_no_save++;
			a->sum_ms_no_save += frame_ms;
			if (frame_ms > a->worst_ms_no_save)
			{
				a->worst_ms_no_save = frame_ms;
				pnx_log("stress: new worst without save -- %ums (frame %u)", (unsigned)frame_ms,
						(unsigned)a->frames);
			}
		}
	}

	pnx_diag_frame(elapsed_ms, frame_ms);
}

int main(void)
{
	static App a;
	memset(&a, 0, sizeof(a));

	if (!pnx_arena_init(&a.persistent, "persistent", PERSIST_BYTES, 4) ||
		!pnx_arena_init(&a.scene, "scene", SCENE_BYTES, 4))
	{
		pnx_platform_log("arena init failed");
		return 1;
	}

	static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;
	pnx_assets_init(&a.persistent, &a.scene, RESOURCES, PNX_ASSET_COUNT);

	// A no-op on a 1-bit build (PnxPalette's own comment, pnx_assets.h) -- called anyway,
	// same source on every platform, matching every other example's boot order.
	if (!pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES))
		pnx_log("stress: palettes would not load");

	pnx_camera_init(&a.camera, PNX_DISPLAY_WIDTH, PNX_DISPLAY_HEIGHT);
	a.has_map = pnx_map_load(&a.map, PNX_ASSET_MAP_FIELD);
	if (!a.has_map)
		pnx_log("stress: map would not load -- running without tilemap load");
	a.has_hero = pnx_sprite_load(&a.hero, PNX_ASSET_SPRITE_HERO);
	if (!a.has_hero)
		pnx_log("stress: hero sprite would not load -- running without sprite load");

	a.has_font = pnx_font_load(&a.font, PNX_ASSET_FONT_BENCH);
	if (!a.has_font)
		pnx_log("stress: font would not load -- nothing to draw");

	if (!pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 60))
		pnx_log("stress: audio would not open");

	size_t payload = 0;
	const uint8_t* d =
		pnx_blob_load(PNX_ASSET_SAMPLE_TONE, "PW", NULL, NULL, NULL, NULL, &payload);
	if (d && payload > 8)
	{
		const uint32_t hz =
			(uint32_t)(d[0] | (d[1] << 8) | (d[2] << 16) | ((uint32_t)d[3] << 24));
		const uint32_t len = (uint32_t)(payload - 8);
		// Looped, so the mixer and the timer both have continuous work the whole run rather
		// than firing once and going idle -- an idle mixer is not the load this app tests.
		pnx_audio_play((const int8_t*)(d + 8), len, 0, hz, 90);
	}
	else
	{
		pnx_log("stress: tone sample would not load -- running without audio load");
	}

	pnx_platform_set_audio_timer(audio_tick, &a, 10);

	a.running = false;
	pnx_platform_run(frame, &a);

	pnx_audio_shutdown();
	pnx_arena_destroy(&a.scene);
	pnx_arena_destroy(&a.persistent);
	return 0;
}
