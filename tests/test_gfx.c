// Host tests for the blitter and camera.
//
// Clipping is the thing worth testing here. A blit that runs one pixel past a row is
// invisible on a watch until it corrupts something else, and the failure surfaces
// somewhere unrelated. On the host it is an assertion.

#include "../src/pnx/gfx/pnx_gfx.h"
#include "../src/pnx/gfx/pnx_nineslice.h"
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
#if PNX_USE_NINESLICE
static void test_nine_slice(void);
#endif

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
	0x11,
	0x00,
	0x11,
	0x00,
	0x11,
	0x00,
	0x11,
	0x00,
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
	pal.entries[0] = 0x00; // transparent, never written
	pal.entries[1] = 0xFF; // opaque white

	pnx_host_reset();
	PnxTarget* t = pnx_host_target();

	// --- transparency: index 0 must leave the destination untouched
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, HALF, &pal, 10, 10, 4, 4, false);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0xFF); // opaque half drawn
	G_CHECK_EQ(pixel_at(t, 11, 10), 0xFF);
	G_CHECK_EQ(pixel_at(t, 12, 10), 0x40); // transparent half preserved
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
	G_CHECK_EQ(pixel_at(t, 10, 10), 0xFF); // row 0 opaque
	G_CHECK_EQ(pixel_at(t, 10, 13), 0x40); // row 3 transparent

	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, topbar, &pal, 10, 10, 4, 4, PNX_FLIP_Y);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0x40); // now row 3's content
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

	// --- rotate: a single off-diagonal marker pixel, not a symmetric quadrant.
	//
	// A whole quadrant filled in (like HALF/topbar above) is the wrong shape to test
	// this with: transpose moves a solid corner block to the SAME place a specific
	// flip combination would, so a test built on one could pass against a blitter
	// that silently treats PNX_FLIP_ROTATE as a plain flip and never transposes
	// anything. One pixel off the main diagonal -- source (col 1, row 0) of a 4x4 --
	// lands at a DIFFERENT destination cell for all eight {rotate, flip_x, flip_y}
	// combinations, which is what actually pins the transpose down.
	//
	// Source: col=1,row=0 opaque, everything else transparent.
	static const uint8_t mark[8] = {
		0x01,
		0x00,
		0x00,
		0x00,
		0x00,
		0x00,
		0x00,
		0x00,
	};

	// Plain rotate (no flip): dest(i,j) = source(sx=j, sy=i) -- solved for
	// sx=1,sy=0 gives i=0,j=1.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, mark, &pal, 10, 10, 4, 4, PNX_FLIP_ROTATE);
	G_CHECK_EQ(pixel_at(t, 10 + 0, 10 + 1), 0xFF);
	G_CHECK_EQ(pixel_at(t, 10 + 1, 10 + 0), 0x40); // where a plain (non-rotated) blit
												   // would have put it -- ruling out
												   // PNX_FLIP_ROTATE being ignored
	{
		int hits = 0;
		for (int16_t j = 0; j < 4; j++)
			for (int16_t i = 0; i < 4; i++)
				if (pixel_at(t, (int16_t)(10 + i), (int16_t)(10 + j)) == 0xFF)
					hits++;
		G_CHECK_EQ(hits, 1); // exactly the one marker pixel, nothing else touched
	}

	// rotate + flip_x: dest(i,j) = source(sx = 3-j, sy = i) -- solved gives i=0,j=2.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, mark, &pal, 10, 10, 4, 4, PNX_FLIP_ROTATE | PNX_FLIP_X);
	G_CHECK_EQ(pixel_at(t, 10 + 0, 10 + 2), 0xFF);

	// rotate + flip_y: dest(i,j) = source(sx = j, sy = 3-i) -- solved gives i=3,j=1.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, mark, &pal, 10, 10, 4, 4, PNX_FLIP_ROTATE | PNX_FLIP_Y);
	G_CHECK_EQ(pixel_at(t, 10 + 3, 10 + 1), 0xFF);

	// rotate + flip_x + flip_y (the anti-diagonal transpose): dest(i,j) =
	// source(sx = 3-j, sy = 3-i) -- solved gives i=3,j=2.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, mark, &pal, 10, 10, 4, 4,
				  PNX_FLIP_ROTATE | PNX_FLIP_X | PNX_FLIP_Y);
	G_CHECK_EQ(pixel_at(t, 10 + 3, 10 + 2), 0xFF);

	// Clipped at the left edge: the plain-rotate case above puts the marker at local
	// column 0, so drawing at x = -1 pushes exactly that column off-screen (columns 1-3
	// land at screen x 0-2, still visible) -- nothing on screen should be opaque, which
	// rules out a rotate path that ignores the horizontal clip the way a naive per-pixel
	// loop could.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, mark, &pal, -1, 10, 4, 4, PNX_FLIP_ROTATE);
	{
		int hits = 0;
		for (int16_t j = 0; j < 4; j++)
			for (int16_t i = 0; i < 3; i++) // screen x 0..2: columns 1-3 of the tile
				if (pixel_at(t, i, (int16_t)(10 + j)) == 0xFF)
					hits++;
		G_CHECK_EQ(hits, 0);
	}

	// --- clipping off every edge must draw nothing outside the target and not crash
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp(t, HALF, &pal, -2, -2, 4, 4, false);
	G_CHECK_EQ(pixel_at(t, 0, 0), 0x40); // the opaque half is off-screen left
	pnx_blit_4bpp(t, HALF, &pal, 198, 226, 4, 4, false);
	G_CHECK_EQ(pixel_at(t, 198, 226), 0xFF); // partially on, bottom-right

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
		meta.pixels		   = bank;
		meta.metatiles	   = defs;
		meta.tile_count	   = 1;
		meta.subtile_count = 4;
		meta.tile_px	   = 16;
		meta.tile_bytes	   = 128;
		meta.sub_bytes	   = 32;

		// pnx_atlas_tile_palette goes through the loaded palette table, so point the atlas
		// at slot 0 and load a table containing our palette.
		static uint8_t slot = 0;
		meta.tile_palette	= &slot;

		G_CHECK(pnx_atlas_is_metatiled(&meta));
		G_CHECK(pnx_atlas_tile(&meta, 0) == NULL); // no contiguous whole tile exists

		pnx_gfx_clear(t, 0x00);
		pnx_blit_metatile_with(t, &meta, 0, &mp, 40, 40);

		G_CHECK_EQ(pixel_at(t, 40, 40), 0xC1); // top-left  -> quadrant 0
		G_CHECK_EQ(pixel_at(t, 52, 40), 0xC2); // top-right -> quadrant 1
		G_CHECK_EQ(pixel_at(t, 40, 52), 0xC3); // bottom-left
		G_CHECK_EQ(pixel_at(t, 52, 52), 0xC4); // bottom-right

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
	G_CHECK_EQ(cam.x, 0); // clamped at the near edge
	G_CHECK_EQ(cam.y, 0);

	pnx_camera_center(&cam, 1000, 1000, 1000, 1000);
	G_CHECK_EQ(cam.x, 1000 - 200); // clamped at the far edge
	G_CHECK_EQ(cam.y, 1000 - 228);

	pnx_camera_center(&cam, 500, 500, 1000, 1000);
	G_CHECK_EQ(cam.x, 400); // centred when it can be
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

#if PNX_USE_NINESLICE
	test_nine_slice();
#endif

	test_tilemap();
}

#if PNX_USE_NINESLICE
// --------------------------------------------------------------------- 9-slice

// A 4x4 source, 2 bytes/row: distinguishes an even-column read (span_4bpp_at's high
// nibble) from an odd one (its low nibble) -- the one path pnx_blit_4bpp never exercises,
// since every region a border produces in test_nine_slice below starts on an even column.
// Row 1: index 5 at col 1, everything else transparent (index 0).
static const uint8_t REGION_SRC[8] = {
	0x00,
	0x00, // row 0
	0x05,
	0x00, // row 1: col 0 = 0 (transparent), col 1 = 5
	0x00,
	0x00, // row 2
	0x00,
	0x00, // row 3
};

// A 6x6 panel, border (2,2,2,2): four 2x2 corners, four 2x2 edge segments, a 2x2 centre --
// each region a distinct palette index (1-9), so a misrouted region reads as the WRONG
// index rather than merely the wrong pixel, which is what actually pins down which of the
// nine blit_tiled calls in pnx_gfx_draw_nine_slice is at fault when one is.
//
// clang-format off
static const uint8_t PANEL_SRC[18] = {
	0x11, 0x55, 0x22, // row 0: TL(1) TL(1) | top(5) top(5) | TR(2) TR(2)
	0x11, 0x55, 0x22, // row 1: same -- border height 2, uniform
	0x77, 0x99, 0x88, // row 2: L(7) L(7) | centre(9) centre(9) | R(8) R(8)
	0x77, 0x99, 0x88, // row 3: same
	0x33, 0x66, 0x44, // row 4: BL(3) BL(3) | bottom(6) bottom(6) | BR(4) BR(4)
	0x33, 0x66, 0x44, // row 5: same
};
// clang-format on

static void test_nine_slice(void)
{
	printf("nine_slice\n");

	PnxPalette pal;
	memset(&pal, 0, sizeof(pal));
	for (int i = 0; i <= 9; i++)
		pal.entries[i] = (uint8_t)(i * 0x10); // entries[k] == 0x10*k, easy to eyeball

	pnx_host_reset();
	PnxTarget* t = pnx_host_target();

	// --- pnx_blit_4bpp_region: a sub-rect out of a LARGER source, odd source column.
	//
	// Window (sx=1, sy=1, sw=1, sh=1) of REGION_SRC is the single opaque pixel; blitted to
	// (20, 20) it must land there and nowhere else, proving the region blit reads the
	// correct nibble at an odd column rather than the byte's other half.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp_region(t, REGION_SRC, 4, &pal, 20, 20, 1, 1, 1, 1);
	G_CHECK_EQ(pixel_at(t, 20, 20), 0x50);
	G_CHECK_EQ(pixel_at(t, 19, 20), 0x40);
	G_CHECK_EQ(pixel_at(t, 21, 20), 0x40);

	// A wider window that includes the transparent column 0 alongside it -- transparency
	// must still be honoured when reading a sub-rect, exactly as pnx_blit_4bpp guarantees
	// for the whole-image case.
	pnx_gfx_clear(t, 0x40);
	pnx_blit_4bpp_region(t, REGION_SRC, 4, &pal, 20, 20, 0, 1, 2, 1);
	G_CHECK_EQ(pixel_at(t, 20, 20), 0x40); // col 0: transparent, preserved
	G_CHECK_EQ(pixel_at(t, 21, 20), 0x50); // col 1: opaque

	const PnxNineSlice ns = {
		.pixels	  = PANEL_SRC,
		.w		  = 6,
		.h		  = 6,
		.border_l = 2,
		.border_t = 2,
		.border_r = 2,
		.border_b = 2,
	};

	// --- exact size: box == panel size, every region drawn once, no tiling at all.
	pnx_gfx_clear(t, 0x40);
	pnx_gfx_draw_nine_slice(t, &ns, &pal, 10, 10, 6, 6);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0x10); // top-left corner
	G_CHECK_EQ(pixel_at(t, 15, 10), 0x20); // top-right corner
	G_CHECK_EQ(pixel_at(t, 10, 15), 0x30); // bottom-left corner
	G_CHECK_EQ(pixel_at(t, 15, 15), 0x40); // bottom-right corner
	G_CHECK_EQ(pixel_at(t, 12, 10), 0x50); // top edge
	G_CHECK_EQ(pixel_at(t, 12, 15), 0x60); // bottom edge
	G_CHECK_EQ(pixel_at(t, 10, 12), 0x70); // left edge
	G_CHECK_EQ(pixel_at(t, 15, 12), 0x80); // right edge
	G_CHECK_EQ(pixel_at(t, 12, 12), 0x90); // centre

	// --- grown box: centre (2x2 source) tiles exactly 3x2 times to fill a 6x4 span.
	// Corners stay put at the box's own corners; edges tile only along their own axis.
	pnx_gfx_clear(t, 0x40);
	pnx_gfx_draw_nine_slice(t, &ns, &pal, 10, 10, 10, 8);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0x10); // top-left corner unmoved
	G_CHECK_EQ(pixel_at(t, 19, 10), 0x20); // top-right corner at the NEW right edge
	G_CHECK_EQ(pixel_at(t, 10, 17), 0x30); // bottom-left corner at the NEW bottom edge
	G_CHECK_EQ(pixel_at(t, 19, 17), 0x40); // bottom-right corner
	G_CHECK_EQ(pixel_at(t, 12, 10), 0x50); // top edge, first repeat
	G_CHECK_EQ(pixel_at(t, 16, 10), 0x50); // top edge, third repeat (2px each: 12,14,16)
	G_CHECK_EQ(pixel_at(t, 12, 12), 0x90); // centre, first tile
	G_CHECK_EQ(pixel_at(t, 16, 14), 0x90); // centre, last full tile (3rd col, 2nd row)

	// --- partial tile: centre span 7 is not a multiple of the 2px source tile, so the
	// last repeat is truncated to 1px rather than overflowing into the right edge.
	pnx_gfx_clear(t, 0x40);
	pnx_gfx_draw_nine_slice(t, &ns, &pal, 10, 10, 11, 6);
	G_CHECK_EQ(pixel_at(t, 18, 12), 0x90); // truncated centre tile's one visible column
	G_CHECK_EQ(pixel_at(t, 19, 12), 0x80); // right edge starts exactly here, untouched by it

	// --- box smaller than its own borders: clamps rather than reading past the source or
	// double-drawing a corner. A 3x6 box splits its 2+2 horizontal border into 1/2 or 2/1,
	// never reads column 2 of a source whose own border is only 2 wide.
	pnx_gfx_clear(t, 0x40);
	pnx_gfx_draw_nine_slice(t, &ns, &pal, 10, 10, 3, 6);
	G_CHECK_EQ(pixel_at(t, 10, 10), 0x10); // still the top-left corner's own colour
}
#endif // PNX_USE_NINESLICE

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
	G_CHECK(ship.layers[0].wt_cols > 1); // there has to BE a boundary to draw across
	G_CHECK_EQ(pnx_map_stream_now(&ship, 0, 0, 200, 228), 0);

	// Note there is no "load it and draw before streaming" case to test here: `deck` fits
	// its pool, so pnx_map_load holds it whole and it is drawable the moment it loads. The
	// draw-before-stream path only exists for a map too large to hold, and it is tested in
	// test_stream.c against one.

	// Park the camera so the first WorldTile boundary falls mid-screen and assert both
	// sides drew: a WorldTile placed at the wrong origin leaves one half blank or doubles
	// the other. Taken from the map's own tiling, since the pipeline chooses the size.
	const int32_t boundary = (int32_t)ship.layers[0].worldtile * ship.tile_px;
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
