// Host tests for WorldTile streaming, against the worldtiles example's own blobs.
//
// The claim under test is not "a big map loads". It is that a map far larger than RAM can
// be walked end to end with **every visible cell resident at every step** -- which is the
// only thing that distinguishes streaming that works from streaming that leaves holes,
// and the one property no amount of reading the code establishes.
//
// So this walks the whole 192x192 field, one tile at a time, exactly the way the example
// does: stream, then look. 36,864 steps, and any cell the camera can see that is not
// resident fails the test at the coordinate it happened.
//
// The A/B against a world of the same size held whole is the second half. `field` and
// `plain` are the same rows drawn from the same tilesets, differing only by
// `resident = true` in the manifest, so comparing what they cost in arena is comparing
// one thing.

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/gfx/pnx_gfx.h"
#include "../src/pnx/gfx/pnx_tilemap.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

#define WT_DIR "../examples/worldtiles/resources/"
#include "../examples/worldtiles/src/c/assets_gen.h"

extern int s_failures;
extern int s_checks;

#define S_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define S_CHECK_EQ(a, b)                                                                     \
	do                                                                                       \
	{                                                                                        \
		s_checks++;                                                                          \
		const long _a = (long)(a), _b = (long)(b);                                           \
		if (_a != _b)                                                                        \
		{                                                                                    \
			printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n", __FILE__, __LINE__, #a, #b, _a, \
				   _b);                                                                      \
			s_failures++;                                                                    \
		}                                                                                    \
	} while (0)

void test_stream(void);

// The pipeline emits the file list in asset-id order, so this needs no maintenance when
// the manifest gains an asset -- and a map's WorldTile banks are assets, of which this
// example has thirty-six.
static const char* s_wt_files[] = PNX_ASSET_FILE_TABLE;
static char s_wt_paths[PNX_ASSET_COUNT][64];
static uint32_t s_resources[PNX_ASSET_COUNT];

static bool register_worldtiles(void)
{
	pnx_host_reset();
	for (uint32_t i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_resources[i] = i + 1;
		snprintf(s_wt_paths[i], sizeof(s_wt_paths[i]), "%s%s", WT_DIR, s_wt_files[i]);
		FILE* f = fopen(s_wt_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP stream: %s not built -- run examples/worldtiles/generate.py "
				"then the pipeline\n",
				s_wt_paths[i]);
			return false;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_wt_paths[i]);
	}
	return true;
}

// Every cell the camera can see must be resident. Returns the first coordinate that is
// not, packed, or -1 -- a coordinate rather than a bool because "somewhere in a 192x192
// map there was a hole" is not a report anybody can act on.
static int32_t first_hole(const PnxMap* m, const PnxCamera* cam)
{
	const int32_t T = m->tile_px;
	const int32_t x0 = pnx_floor_div(cam->x, T), y0 = pnx_floor_div(cam->y, T);
	const int32_t x1 = x0 + cam->view_w / T + 1, y1 = y0 + cam->view_h / T + 1;

	for (int32_t ty = y0; ty <= y1; ty++)
	{
		if (ty < 0 || ty >= m->h)
			continue;
		for (int32_t tx = x0; tx <= x1; tx++)
		{
			if (tx < 0 || tx >= m->w)
				continue;
			if (pnx_map_tile(m, tx, ty) == PNX_MAP_NO_CELL)
				return ty * 1000 + tx;
		}
	}
	return -1;
}

// A serpentine over the whole map at one tile per step, which is how the example's
// autopilot moves and therefore what the streamer is actually asked for.
static void walk_everything(PnxMap* m, PnxCamera* cam, int32_t step, int32_t* out_holes,
							uint32_t* out_worst_missing)
{
	int32_t holes = 0;
	uint32_t worst = 0;
	const int32_t w = pnx_tilemap_width(m), h = pnx_tilemap_height(m);

	for (int32_t ty = 0; ty < m->h; ty += step)
	{
		const bool east = ((ty / step) & 1) == 0;
		for (int32_t i = 0; i < m->w; i += step)
		{
			const int32_t tx = east ? i : (m->w - 1 - i);
			pnx_camera_center(cam, tx * m->tile_px, ty * m->tile_px, w, h);
			const uint8_t missing = pnx_map_stream(m, cam->x, cam->y, cam->view_w, cam->view_h);
			if (missing > worst)
				worst = missing;
			if (first_hole(m, cam) >= 0)
			{
				if (holes == 0)
				{
					printf("  ... first hole at tile %d,%d (missing %u)\n", (int)tx, (int)ty,
						   missing);
				}
				holes++;
			}
		}
	}
	*out_holes = holes;
	*out_worst_missing = worst;
}

void test_stream(void)
{
	printf("stream\n");
	if (!register_worldtiles())
		return;

	PnxArena persistent, scene;
	if (!pnx_arena_init(&persistent, "st-persistent", 4 * 1024, 4) ||
		!pnx_arena_init(&scene, "st-scene", 128 * 1024, 4))
	{
		return;
	}
	S_CHECK(pnx_assets_init(&persistent, &scene, s_resources, PNX_ASSET_COUNT));
	S_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));

	PnxCamera cam;
	pnx_camera_init(&cam, 200, 228);

	// --- the streamed world
	PnxMap field;
	S_CHECK(pnx_map_load(&field, PNX_ASSET_MAP_FIELD));
	S_CHECK_EQ(field.w, MAP_FIELD_W);
	S_CHECK_EQ(field.h, MAP_FIELD_H);
	S_CHECK_EQ(field.atlas_count, 3);

	// The WorldTile size is chosen per map by the pipeline, so the grid is asserted against
	// the map's own dimensions rather than against a number -- a hardcoded 144 would only
	// have been testing whichever size was in favour the day it was written.
	S_CHECK_EQ(field.wt_cols, (MAP_FIELD_W + field.worldtile - 1) / field.worldtile);
	S_CHECK_EQ(field.wt_rows, (MAP_FIELD_H + field.worldtile - 1) / field.worldtile);

	// The two numbers that make this a streaming test rather than a big-map test: fewer
	// WorldTile slots than WorldTiles, and fewer atlas slots than atlases.
	S_CHECK(field.slot_count < field.wt_cols * field.wt_rows);
	S_CHECK(field.atlas_slots < field.atlas_count);

	// Too large to be held whole, so nothing is resident until it is asked for -- and
	// drawing it in that state must be a harmless no-op rather than a read through NULL.
	// This is the one mistake the API can still invite, draw before stream, and only a map
	// this size can be in that state at all.
	S_CHECK_EQ(pnx_map_resident(&field), 0);
	S_CHECK(pnx_map_tile(&field, 0, 0) == PNX_MAP_NO_CELL);
	{
		PnxTarget* t = pnx_host_target();
		pnx_gfx_clear(t, 0x40);
		pnx_tilemap_draw(&field, t, &cam);
		int dirty = 0;
		for (int16_t y = 0; y < 228; y++)
		{
			PnxRow row = pnx_target_row(t, y);
			for (int16_t x = 0; row.data && x < 200; x++)
				if (row.data[x] != 0x40)
					dirty++;
		}
		S_CHECK_EQ(dirty, 0);
	}

	// --- the payloads are not in the map's own resource
	//
	// The point of banking is that a ranged read never seeks far, so what is worth asserting
	// is the thing that makes that true: the map's resource holds only the resident preamble,
	// and the banks it names all exist and are small.
	size_t map_res_bytes = 0;
	S_CHECK(pnx_platform_resource_size(s_resources[PNX_ASSET_MAP_FIELD], &map_res_bytes));
	S_CHECK(map_res_bytes < 4096);
	S_CHECK(field.first_bank_asset > PNX_ASSET_MAP_FIELD);

	const uint16_t banks =
		(uint16_t)((((field.wt_cols * field.wt_rows) - 1) >> field.bank_shift) + 1);
	size_t worst_bank = 0;
	for (uint16_t b = 0; b < banks; b++)
	{
		size_t sz = 0;
		S_CHECK(pnx_platform_resource_size(s_resources[field.first_bank_asset + b], &sz));
		if (sz > worst_bank)
			worst_bank = sz;
	}
	// The whole payload region is 74 KB; no single read may have to seek past one bank of it.
	S_CHECK(worst_bank < 8 * 1024);
	printf("  ... %u banks, largest %u B (the plane is %u B)\n", banks, (unsigned)worst_bank,
		   MAP_FIELD_W * MAP_FIELD_H * 2);

	S_CHECK_EQ(pnx_map_stream_now(&field, 0, 0, 200, 228), 0);
	const size_t field_bytes = scene.used;
	const uint32_t read_after_load = pnx_assets_bytes_loaded();

	// --- walk all of it, checking for holes at every step
	int32_t holes = 0;
	uint32_t worst_missing = 0;
	walk_everything(&field, &cam, 1, &holes, &worst_missing);
	S_CHECK_EQ(holes, 0);

	// `missing` is allowed to be non-zero and the margin is why: a serpentine turn wants
	// several WorldTiles at once, more than PNX_MAP_STREAM_BUDGET reads in a frame, and the
	// ring absorbs the difference. What must never happen is a hole, which is asserted
	// above. Bounding it anyway, because a streamer falling a whole window behind is a
	// different thing from one running a tile late.
	S_CHECK(worst_missing < 4);
	printf("  ... walked %dx%d, worst backlog %u WorldTiles, no holes\n", MAP_FIELD_W,
		   MAP_FIELD_H, worst_missing);

	// Walking 192x192 tiles must not grow the arena by a byte: the pools are allocated once
	// at map load and reused. A leak here would be invisible on a small map and fatal on
	// this one.
	S_CHECK_EQ(scene.used, field_bytes);

	// Atlases really did stream. Three atlases through two slots means at least one had to
	// be read more than once, so the running total must exceed a single copy of everything.
	const uint32_t read_after_walk = pnx_assets_bytes_loaded();
	S_CHECK(read_after_walk > read_after_load);
	S_CHECK(pnx_map_resident(&field) <= field.slot_count);

	// --- sprinting: eight tiles a tick, which crosses a whole WorldTile per tick at the
	//     size the pipeline picks. The budgeted call has to keep up, and the reason it can
	//     is that its budget counts READS -- a row of WorldTiles is one of them.
	//
	//     Charging per WorldTile instead throttled the streamer to a quarter of the I/O it
	//     was paying for, and did it invisibly: the backlog went 3 to 12 on the same content
	//     at the same speed the moment WorldTiles got smaller. Pinned here because nothing
	//     about it is visible in a frame time or a byte count.
	{
		const int32_t step = 8 * field.tile_px;
		uint32_t worst_sprint = 0, reads = 0;
		for (int32_t i = 0; i < 40; i++)
		{
			pnx_camera_center(&cam, (i + 4) * step, (i + 4) * step, pnx_tilemap_width(&field),
							  pnx_tilemap_height(&field));
			const uint32_t before_reads = pnx_host_resource_reads();
			const uint8_t missing =
				pnx_map_stream(&field, cam.x, cam.y, cam.view_w, cam.view_h);
			reads += pnx_host_resource_reads() - before_reads;
			if (missing > worst_sprint)
				worst_sprint = missing;
		}
		S_CHECK_EQ(worst_sprint, 0);
		S_CHECK(reads <= 40 * PNX_MAP_STREAM_BUDGET);
		printf("  ... sprinted 40 WorldTiles, worst backlog %u, %u reads\n", worst_sprint,
			   (unsigned)reads);
	}

	// --- and a hostile case: jumping a whole screen at a time, which no player does but a
	//     warp does. The budgeted call is allowed to fall behind here -- that is what the
	//     blocking one is for -- so what is checked is that the blocking one catches up.
	pnx_camera_center(&cam, MAP_FIELD_W * field.tile_px, MAP_FIELD_H * field.tile_px,
					  pnx_tilemap_width(&field), pnx_tilemap_height(&field));
	S_CHECK_EQ(pnx_map_stream_now(&field, cam.x, cam.y, cam.view_w, cam.view_h), 0);
	S_CHECK_EQ(first_hole(&field, &cam), -1);
	S_CHECK_EQ(scene.used, field_bytes);

	// --- warping between the small maps
	//
	// Done here, while `field` is still valid: the arena reset below frees everything the
	// maps point into at once, which is the whole bargain of an arena and the reason the
	// interiors are checked against the field before it happens.
	//
	// These are small enough that pnx_map_load holds them whole, so the streaming path
	// never runs for them at all -- which is exactly how they behaved before WorldTiles
	// existed, and worth pinning beside a map that does stream.
	const uint16_t small[] = { PNX_ASSET_MAP_HUT, PNX_ASSET_MAP_CRYPT, PNX_ASSET_MAP_KEEP };
	for (uint8_t i = 0; i < 3; i++)
	{
		PnxMap m;
		S_CHECK(pnx_map_load(&m, small[i]));
		S_CHECK_EQ(m.slot_count, m.wt_cols * m.wt_rows);
		S_CHECK_EQ(pnx_map_resident(&m), m.wt_cols * m.wt_rows);  // whole, before any stream
		S_CHECK_EQ(pnx_map_stream_now(&m, 0, 0, 200, 228), 0);	  // and the stream is a no-op

		// Its warp must land on a walkable tile of the field, which is what makes the round
		// trip real rather than a door into a wall.
		//
		// Streaming to the destination FIRST is not test bookkeeping -- it is the sequence a
		// warp actually performs, and it has to be: a cell whose WorldTile is not resident
		// reads as solid, so asking about a corner of the world the camera has never visited
		// would answer "wall" for every destination and pass nothing.
		S_CHECK_EQ(m.warp_count, 1);
		if (m.warp_count)
		{
			const int32_t dx = m.warps[0].dest_x * field.tile_px;
			const int32_t dy = m.warps[0].dest_y * field.tile_px;
			pnx_camera_center(&cam, dx, dy, pnx_tilemap_width(&field),
							  pnx_tilemap_height(&field));
			S_CHECK_EQ(pnx_map_stream_now(&field, cam.x, cam.y, cam.view_w, cam.view_h), 0);
			S_CHECK(!pnx_map_solid(&field, m.warps[0].dest_x, m.warps[0].dest_y));
		}
	}

	// ...and the field's own doors sit on tiles the player can stand on, carrying the warp
	// flag. The pipeline checks both; this confirms the checks describe the shipped bytes.
	S_CHECK_EQ(field.warp_count, 3);
	for (uint8_t i = 0; i < field.warp_count; i++)
	{
		S_CHECK(field.warps[i].dest_map < 5);

		const int32_t dx = field.warps[i].x * field.tile_px;
		const int32_t dy = field.warps[i].y * field.tile_px;
		pnx_camera_center(&cam, dx, dy, pnx_tilemap_width(&field), pnx_tilemap_height(&field));
		S_CHECK_EQ(pnx_map_stream_now(&field, cam.x, cam.y, cam.view_w, cam.view_h), 0);
		S_CHECK(!pnx_map_solid(&field, field.warps[i].x, field.warps[i].y));
		S_CHECK(pnx_map_flags(&field, field.warps[i].x, field.warps[i].y) & PNX_TILE_WARP);
		S_CHECK(pnx_map_warp_at(&field, field.warps[i].x, field.warps[i].y) != NULL);
	}

	// --- the same world, held whole
	//
	// `plain` differs from `field` by `resident = true` and nothing else. Loading it into a
	// fresh arena is the comparison the example puts on a button.
	//
	// Everything above this line is reading through `field`, whose pools live in the arena
	// being reset -- so nothing may touch it after here.
	pnx_arena_reset(&scene);
	S_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));

	const uint32_t reads_before = pnx_host_resource_reads();
	PnxMap plain;
	S_CHECK(pnx_map_load(&plain, PNX_ASSET_MAP_PLAIN));
	const uint32_t load_reads = pnx_host_resource_reads() - reads_before;
	S_CHECK_EQ(plain.w, field.w);
	S_CHECK_EQ(plain.h, field.h);

	// The same world, tiled DIFFERENTLY, and that is the pipeline being right rather than
	// inconsistent: a streaming map holds a fixed window, so a big WorldTile means a big
	// margin ring of world nobody can see; a map held whole has no ring, so a big WorldTile
	// just means fewer per-tile headers. The two modes want opposite sizes.
	S_CHECK(plain.worldtile > field.worldtile);
	S_CHECK(plain.wt_cols * plain.wt_rows < field.wt_cols * field.wt_rows);

	// Held whole means a slot for every WorldTile and a slot for every atlas -- so nothing
	// is ever evicted, and the streamer's eviction path never runs.
	S_CHECK_EQ(plain.slot_count, plain.wt_cols * plain.wt_rows);
	S_CHECK_EQ(plain.atlas_slots, plain.atlas_count);

	// Whole means whole, before anything asks: a map that fits its pool is filled by
	// pnx_map_load, so the streaming path never runs for it. Without that the comparison
	// would be dishonest -- `plain` would still be reading flash as the player walked, and
	// "held whole" would only describe the allocation.
	S_CHECK_EQ(pnx_map_resident(&plain), plain.wt_cols * plain.wt_rows);
	S_CHECK_EQ(pnx_map_stream_now(&plain, 0, 0, 200, 228), 0);
	const size_t plain_bytes = scene.used;

	// Batched: holding 144 WorldTiles took one read per BANK, not one per tile. On device a
	// read costs by how far into the resource it starts, so the read COUNT is the number
	// this change has to be judged on -- and asserting it is the only way a later
	// refactor that quietly goes back to one-read-per-tile gets caught.
	//
	// The budget: 18 banks of payload, 18 bank headers checked at load, one map preamble,
	// three atlases. Comfortably under 60; a per-tile loader would be over 140.
	S_CHECK(load_reads > 0 && load_reads < 60);
	printf("  ... held 144 WorldTiles in %u reads, not 144\n", (unsigned)load_reads);

	// The headline. Streaming has to buy back most of the world to be worth its ~2.4 KB of
	// engine, so this is asserted as a ratio rather than reported as a number -- a change
	// that quietly halved the saving would otherwise pass.
	S_CHECK(plain_bytes > field_bytes * 3);
	printf("  ... 192x192 world: %u B streamed, %u B held whole (%ux)\n", (unsigned)field_bytes,
		   (unsigned)plain_bytes, (unsigned)(plain_bytes / (field_bytes ? field_bytes : 1)));

	// Walking the held-whole world reads NOTHING further: every cell was already there.
	// This is the other half of the trade stated plainly -- more RAM, no flash traffic.
	const uint32_t before = pnx_assets_bytes_loaded();
	walk_everything(&plain, &cam, 1, &holes, &worst_missing);
	S_CHECK_EQ(holes, 0);
	S_CHECK_EQ(worst_missing, 0);
	S_CHECK_EQ(pnx_assets_bytes_loaded(), before);
	S_CHECK_EQ(scene.used, plain_bytes);

	pnx_arena_destroy(&scene);
	pnx_arena_destroy(&persistent);
}
