// Host tests for the blitter and camera.
//
// Clipping is the thing worth testing here. A blit that runs one pixel past a row is
// invisible on a watch until it corrupts something else, and the failure surfaces
// somewhere unrelated. On the host it is an assertion.

#include "../src/pnx/gfx/pnx_gfx.h"
#include "../src/pnx/gfx/pnx_tilemap.h"
#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

// The example's own blobs, so the draw loop is exercised against real content rather than
// against a map written to suit it. Its generated header names the assets.
#define TILEMAP_DIR "../examples/overworld/resources/"
#include "../examples/overworld/src/c/assets_gen.h"

// Straight from the pipeline, in asset-id order: a hand-written list went stale the
// moment the manifest gained an asset, and WorldTile banks are assets.
static const char* s_tilemap_files[] = PNX_ASSET_FILE_TABLE;
static char s_tilemap_paths[PNX_ASSET_COUNT][64];

static void test_tilemap(void);

extern int s_failures;
extern int s_checks;

#define G_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define G_CHECK_EQ(a, b)                                                                     \
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

void test_gfx(void);

// A 4x4 image: left half palette index 1, right half transparent.
static const uint8_t HALF[8] = {
	0x11, 0x00, 0x11, 0x00, 0x11, 0x00, 0x11, 0x00,
};

static uint8_t pixel_at(PnxTarget* t, int16_t x, int16_t y)
{
	PnxRow row = pnx_target_row(t, y);
	return row.data ? row.data[x] : 0;
}

void test_gfx(void)
{
	printf("gfx\n");

	PnxPalette pal;
	memset(&pal, 0, sizeof(pal));
	pal.entries[0] = 0x00;	// transparent, never written
	pal.entries[1] = 0xFF;	// opaque white

	pnx_host_reset();
	PnxTarget* t = pnx_host_target();

	// --- transparency: index 0 must leave the destination untouched
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, HALF, &pal, 10, 10, 4, 4, false);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0xFF);	// opaque half drawn
	G_CHECK_EQ(pixel_at(t, 11, 10), 0xFF);
	G_CHECK_EQ(pixel_at(t, 12, 10), 0x40);	// transparent half preserved
	G_CHECK_EQ(pixel_at(t, 13, 10), 0x40);

	// --- mirroring swaps which half is opaque
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, HALF, &pal, 10, 10, 4, 4, PNX_FLIP_X);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0x40);
	G_CHECK_EQ(pixel_at(t, 13, 10), 0xFF);

	// --- flip Y reads rows from the other end. Checked against a source whose top and
	// bottom halves differ, because HALF is identical on every row and would pass either way.
	static const uint8_t topbar[8] = { 0x11, 0x11, 0x11, 0x11, 0x00, 0x00, 0x00, 0x00 };
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, topbar, &pal, 10, 10, 4, 4, PNX_FLIP_NONE);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0xFF);	// row 0 opaque
	G_CHECK_EQ(pixel_at(t, 10, 13), 0x40);	// row 3 transparent

	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, topbar, &pal, 10, 10, 4, 4, PNX_FLIP_Y);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0x40);	// now row 3's content
	G_CHECK_EQ(pixel_at(t, 10, 13), 0xFF);

	// Clipped at the top edge, which is where flip Y is easy to get wrong: the vertical clip
	// skips destination rows, and the source row has to be counted from the far end AFTER
	// that skip, not before. At y = -2 the two visible rows read source rows 1 and 0 when
	// flipped -- both opaque -- and rows 2 and 3 when not, both transparent. Asserting the
	// pair distinguishes a correct implementation from one that ignores the clip.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, topbar, &pal, 10, -2, 4, 4, PNX_FLIP_Y);
	G_CHECK_EQ(pixel_at(t, 10, 0), 0xFF);
	G_CHECK_EQ(pixel_at(t, 10, 1), 0xFF);

	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, topbar, &pal, 10, -2, 4, 4, PNX_FLIP_NONE);
	G_CHECK_EQ(pixel_at(t, 10, 0), 0x40);
	G_CHECK_EQ(pixel_at(t, 10, 1), 0x40);

	// --- clipping off every edge must draw nothing outside the target and not crash
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, HALF, &pal, -2, -2, 4, 4, false);
	G_CHECK_EQ(pixel_at(t, 0, 0), 0x40);  // the opaque half is off-screen left
	pnx_blit_4bpp(t, HALF, &pal, 198, 226, 4, 4, false);
	G_CHECK_EQ(pixel_at(t, 198, 226), 0xFF);  // partially on, bottom-right

	// Fully off-screen in each direction: the target must be untouched.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, HALF, &pal, -10, 10, 4, 4, false);
	pnx_blit_4bpp(t, HALF, &pal, 500, 10, 4, 4, false);
	pnx_blit_4bpp(t, HALF, &pal, 10, -10, 4, 4, false);
	pnx_blit_4bpp(t, HALF, &pal, 10, 500, 4, 4, false);
	int dirty = 0;
	for (int16_t y = 0; y < 228; y++)
		for (int16_t x = 0; x < 200; x++)
			if (pixel_at(t, x, y) != 0x40)
				dirty++;
	G_CHECK_EQ(dirty, 0);

	// A NULL palette must be a no-op rather than a crash: it is what an unloaded asset
	// hands back, and that should degrade rather than fault.
	pnx_blit_4bpp(t, HALF, NULL, 10, 10, 4, 4, false);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0x40);

	// --- metatile quadrant ordering
	//
	// Wrong ordering here shows up on device as scrambled tiles and nothing else, so it is
	// worth pinning directly: quadrants are top-left, top-right, bottom-left, bottom-right.
	{
		// Four 8x8 quadrants at 4bpp (32 B each), each solid in its own palette index.
		static uint8_t bank[4 * 32];
		for (int qi = 0; qi < 4; qi++)
		{
			const uint8_t idx = (uint8_t)(qi + 1);
			memset(bank + qi * 32, (uint8_t)((idx << 4) | idx), 32);
		}
		static const uint16_t defs[4] = { 0, 1, 2, 3 };

		PnxPalette mp;
		memset(&mp, 0, sizeof(mp));
		for (int i = 1; i <= 4; i++)
			mp.entries[i] = (uint8_t)(0xC0 + i);

		PnxAtlas meta;
		memset(&meta, 0, sizeof(meta));
		meta.pixels = bank;
		meta.metatiles = defs;
		meta.tile_count = 1;
		meta.subtile_count = 4;
		meta.tile_px = 16;
		meta.tile_bytes = 128;
		meta.sub_bytes = 32;

		// pnx_atlas_tile_palette goes through the loaded palette table, so point the atlas
		// at slot 0 and load a table containing our palette.
		static uint8_t slot = 0;
		meta.tile_palette = &slot;

		G_CHECK(pnx_atlas_is_metatiled(&meta));
		G_CHECK(pnx_atlas_tile(&meta, 0) == NULL);	// no contiguous whole tile exists

		pnx_gfx_clear(t, 0x00);
		pnx_blit_metatile_with(t, &meta, 0, &mp, 40, 40);

		G_CHECK_EQ(pixel_at(t, 40, 40), 0xC1);	// top-left  -> quadrant 0
		G_CHECK_EQ(pixel_at(t, 52, 40), 0xC2);	// top-right -> quadrant 1
		G_CHECK_EQ(pixel_at(t, 40, 52), 0xC3);	// bottom-left
		G_CHECK_EQ(pixel_at(t, 52, 52), 0xC4);	// bottom-right

		// Clipped off the left edge: the right quadrants must still land correctly.
		pnx_gfx_clear(t, 0x00);
		pnx_blit_metatile_with(t, &meta, 0, &mp, -4, 40);
		G_CHECK_EQ(pixel_at(t, 0, 40), 0xC1);
		G_CHECK_EQ(pixel_at(t, 8, 40), 0xC2);
	}

	// --- fill_rect clips the same way
	pnx_gfx_clear(t, 0x00);
	pnx_gfx_fill_rect(t, -5, -5, 10, 10, 0x55);
	G_CHECK_EQ(pixel_at(t, 0, 0), 0x55);
	G_CHECK_EQ(pixel_at(t, 5, 5), 0x00);

	// --- camera clamps to the world, never past it
	PnxCamera cam;
	pnx_camera_init(&cam, 200, 228);

	pnx_camera_center(&cam, 0, 0, 1000, 1000);
	G_CHECK_EQ(cam.x, 0);  // clamped at the near edge
	G_CHECK_EQ(cam.y, 0);

	pnx_camera_center(&cam, 1000, 1000, 1000, 1000);
	G_CHECK_EQ(cam.x, 1000 - 200);	// clamped at the far edge
	G_CHECK_EQ(cam.y, 1000 - 228);

	pnx_camera_center(&cam, 500, 500, 1000, 1000);
	G_CHECK_EQ(cam.x, 400);	 // centred when it can be
	G_CHECK_EQ(cam.y, 386);

	// A world smaller than the view must pin at 0, not clamp to a negative maximum and
	// scroll backwards.
	pnx_camera_center(&cam, 50, 50, 100, 100);
	G_CHECK_EQ(cam.x, 0);
	G_CHECK_EQ(cam.y, 0);

	// --- floor division, which is what stops a column vanishing at a map's left edge
	G_CHECK_EQ(pnx_floor_div(-1, 16), -1);
	G_CHECK_EQ(pnx_floor_div(-16, 16), -1);
	G_CHECK_EQ(pnx_floor_div(-17, 16), -2);
	G_CHECK_EQ(pnx_floor_div(0, 16), 0);
	G_CHECK_EQ(pnx_floor_div(15, 16), 0);
	G_CHECK_EQ(pnx_floor_div(16, 16), 1);

	test_tilemap();
}

// ------------------------------------------------------------------------- tilemap
//
// The draw loop walks WorldTiles outer and cells inner, which means a tile's screen
// position is now computed from its WorldTile's origin plus its offset within it rather
// than from the map alone. Get that wrong by one WorldTile and the world draws in blocks
// shifted against each other -- which looks like art, not like a bug, and is exactly the
// kind of thing no amount of reading catches.
//
// So: draw the real example maps and assert on pixels.

static int ink_in(PnxTarget* t, int16_t x0, int16_t y0, int16_t x1, int16_t y1,
				  uint8_t background)
{
	int n = 0;
	for (int16_t y = y0; y <= y1; y++)
	{
		for (int16_t x = x0; x <= x1; x++)
		{
			if (pixel_at(t, x, y) != background)
				n++;
		}
	}
	return n;
}

static void test_tilemap(void)
{
	PnxArena persistent, scene;
	if (!pnx_arena_init(&persistent, "tm-persistent", 4 * 1024, 4) ||
		!pnx_arena_init(&scene, "tm-scene", 128 * 1024, 4))
	{
		return;
	}

	pnx_host_reset();
	static uint32_t resources[PNX_ASSET_COUNT];
	for (uint32_t i = 0; i < PNX_ASSET_COUNT; i++)
	{
		resources[i] = i + 1;
		snprintf(s_tilemap_paths[i], sizeof(s_tilemap_paths[i]), "%s%s", TILEMAP_DIR,
				 s_tilemap_files[i]);
		pnx_host_register_resource(resources[i], s_tilemap_paths[i]);
	}
	if (!pnx_assets_init(&persistent, &scene, resources, PNX_ASSET_COUNT) ||
		!pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES))
	{
		pnx_arena_destroy(&persistent);
		pnx_arena_destroy(&scene);
		return;
	}

	PnxTarget* t = pnx_host_target();
	PnxCamera cam;
	pnx_camera_init(&cam, 200, 228);

	// --- the ship: two atlases, and a WorldTile boundary down the middle of the screen
	PnxMap ship;
	G_CHECK(pnx_map_load(&ship, PNX_ASSET_MAP_DECK));
	G_CHECK(ship.wt_cols > 1);	// there has to BE a boundary to draw across
	G_CHECK_EQ(pnx_map_stream_now(&ship, 0, 0, 200, 228), 0);

	// Note there is no "load it and draw before streaming" case to test here: `deck` fits
	// its pool, so pnx_map_load holds it whole and it is drawable the moment it loads. The
	// draw-before-stream path only exists for a map too large to hold, and it is tested in
	// test_stream.c against one.

	// Park the camera so the first WorldTile boundary falls mid-screen and assert both
	// sides drew: a WorldTile placed at the wrong origin leaves one half blank or doubles
	// the other. Taken from the map's own tiling, since the pipeline chooses the size.
	const int32_t boundary = (int32_t)ship.worldtile * ship.tile_px;
	pnx_camera_center(&cam, boundary, 3 * ship.tile_px, pnx_tilemap_width(&ship),
					  pnx_tilemap_height(&ship));
	G_CHECK_EQ(pnx_map_stream_now(&ship, cam.x, cam.y, cam.view_w, cam.view_h), 0);

	pnx_gfx_clear(t, 0x40);
	pnx_tilemap_draw(&ship, t, &cam);
	const int32_t split = boundary - cam.x;
	G_CHECK(split > 0 && split < 200);
	if (split > 0 && split < 200)
	{
		G_CHECK(ink_in(t, 0, 0, (int16_t)(split - 1), 227, 0x40) > 0);
		G_CHECK(ink_in(t, (int16_t)split, 0, 199, 227, 0x40) > 0);
	}

	// Nothing may be left as background: the ship map is water and deck edge to edge, so
	// any unwritten pixel is a cell the WorldTile walk missed.
	G_CHECK_EQ(ink_in(t, 0, 0, 199, 227, 0x40), 200 * 228);

	// --- the same content drawn at two camera positions one tile apart must differ by
	//     exactly that: a scroll, not a re-layout.
	pnx_gfx_clear(t, 0x40);
	pnx_tilemap_draw(&ship, t, &cam);
	static uint8_t before[200];
	for (int16_t x = 0; x < 200; x++)
		before[x] = pixel_at(t, x, 100);

	cam.x += ship.tile_px;
	G_CHECK_EQ(pnx_map_stream_now(&ship, cam.x, cam.y, cam.view_w, cam.view_h), 0);
	pnx_gfx_clear(t, 0x40);
	pnx_tilemap_draw(&ship, t, &cam);

	int shifted = 0;
	for (int16_t x = 0; x + ship.tile_px < 200; x++)
	{
		if (pixel_at(t, x, 100) == before[x + ship.tile_px])
			shifted++;
	}
	G_CHECK(shifted > (200 - ship.tile_px) * 9 / 10);

	// --- eviction: a camera that leaves a WorldTile behind must free its slot, and coming
	//     back must reload it rather than finding a stale one.
	PnxMap outdoor;
	G_CHECK(pnx_map_load(&outdoor, PNX_ASSET_MAP_OUTDOOR));
	G_CHECK_EQ(pnx_map_stream_now(&outdoor, 0, 0, 200, 228), 0);
	const uint16_t corner = pnx_map_tile(&outdoor, 0, 0);
	G_CHECK(corner != PNX_MAP_NO_CELL);

	const int32_t far_x = pnx_tilemap_width(&outdoor) - 200;
	const int32_t far_y = pnx_tilemap_height(&outdoor) - 228;
	G_CHECK_EQ(pnx_map_stream_now(&outdoor, far_x, far_y, 200, 228), 0);
	G_CHECK_EQ(pnx_map_stream_now(&outdoor, 0, 0, 200, 228), 0);
	G_CHECK_EQ(pnx_map_tile(&outdoor, 0, 0), corner);

	pnx_arena_destroy(&persistent);
	pnx_arena_destroy(&scene);
}
