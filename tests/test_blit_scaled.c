// Host tests for pnx_blit_4bpp_scaled -- the SNES/GBA-style nearest-neighbor scaled blit.
//
// Nearest-neighbor selection is exactly reproducible, so these are precise expected-output
// tests, not "doesn't crash" checks -- the same standard test_gfx.c already holds
// pnx_blit_4bpp to.

#include "../src/pnx/gfx/pnx_gfx.h"
#include "../src/pnx/assets/pnx_assets.h" // PNX_PALETTE_TRANSPARENT
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define BS_CHECK_EQ(a, b)                                                                    \
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

void test_blit_scaled(void);

static uint8_t pixel_at(PnxTarget* t, int16_t x, int16_t y)
{
	PnxRow row = pnx_target_row(t, y);
	return row.data ? row.data[x] : 0;
}

void test_blit_scaled(void)
{
	printf("blit_scaled\n");

	pnx_host_reset();
	PnxTarget* t = pnx_host_target();

	PnxPalette pal;
	memset(&pal, 0, sizeof(pal));
	for (int i = 1; i <= 6; i++)
		pal.entries[i] = (uint8_t)(i * 0x10); // index 0 stays 0x00: transparent

	// --- unscaled dispatch: dst==src must match a direct pnx_blit_4bpp call exactly.
	// Reuses the odd-width fixture (3 wide x 2 tall, values 1..6 packed as flat nibbles,
	// row 1 starting mid-byte) so this also confirms the dispatch shortcut doesn't lose
	// the flat-nibble-stream addressing pnx_blit_4bpp itself relies on.
	{
		static const uint8_t odd_row[3] = { 0x12, 0x34, 0x56 };

		pnx_gfx_clear(t, 0x00);
		pnx_blit_4bpp(t, odd_row, &pal, 20, 20, 3, 2, PNX_FLIP_NONE);
		uint8_t direct[3][2];
		for (int16_t j = 0; j < 2; j++)
			for (int16_t i = 0; i < 3; i++)
				direct[i][j] = pixel_at(t, (int16_t)(20 + i), (int16_t)(20 + j));

		pnx_gfx_clear(t, 0x00);
		pnx_blit_4bpp_scaled(t, odd_row, &pal, 20, 20, 3, 2, 3, 2);
		for (int16_t j = 0; j < 2; j++)
			for (int16_t i = 0; i < 3; i++)
				BS_CHECK_EQ(pixel_at(t, (int16_t)(20 + i), (int16_t)(20 + j)), direct[i][j]);
	}

	// --- height-only scaling on an odd-width source (Need4Pebble's real call shape: the
	// road-chunk sprite scales dst_h, dst_w stays == src_w). odd_row upscaled 2x tall:
	// row_step = (2<<16)/4 = 32768. j=0,1 -> sy=0 (source row 0: 1,2,3); j=2,3 -> sy=1
	// (source row 1: 4,5,6, which starts on the mid-byte low nibble -- exactly what the
	// flat-nibble addressing this function's own header comment calls out has to get
	// right).
	{
		static const uint8_t odd_row[3] = { 0x12, 0x34, 0x56 };

		pnx_gfx_clear(t, 0x00);
		pnx_blit_4bpp_scaled(t, odd_row, &pal, 30, 30, 3, 2, 3, 4);
		BS_CHECK_EQ(pixel_at(t, 30, 30), 0x10); // row 0 of dest: source row 0 (1,2,3)
		BS_CHECK_EQ(pixel_at(t, 31, 30), 0x20);
		BS_CHECK_EQ(pixel_at(t, 32, 30), 0x30);
		BS_CHECK_EQ(pixel_at(t, 30, 31), 0x10); // row 1 of dest: still source row 0
		BS_CHECK_EQ(pixel_at(t, 30, 32), 0x40); // row 2 of dest: source row 1 (4,5,6)
		BS_CHECK_EQ(pixel_at(t, 31, 32), 0x50);
		BS_CHECK_EQ(pixel_at(t, 32, 32), 0x60);
		BS_CHECK_EQ(pixel_at(t, 30, 33), 0x40); // row 3 of dest: still source row 1
	}

	// --- height-only downscale: 2 wide x 4 tall, one marker value per row (1,2,3,4),
	// scaled to dst_h=2. row_step = (4<<16)/2 = 2<<16, so dest row 0 -> source row 0,
	// dest row 1 -> source row 2 (rows 1 and 3 are skipped entirely by a 2x downscale,
	// same nearest-neighbor behaviour a real road-chunk downscale relies on).
	{
		static const uint8_t rows4[4] = { 0x11, 0x22, 0x33, 0x44 }; // row0..3, 2px/row

		pnx_gfx_clear(t, 0x00);
		pnx_blit_4bpp_scaled(t, rows4, &pal, 40, 40, 2, 4, 2, 2);
		BS_CHECK_EQ(pixel_at(t, 40, 40), 0x10); // dest row 0 = source row 0
		BS_CHECK_EQ(pixel_at(t, 41, 40), 0x10);
		BS_CHECK_EQ(pixel_at(t, 40, 41), 0x30); // dest row 1 = source row 2
		BS_CHECK_EQ(pixel_at(t, 41, 41), 0x30);
	}

	// --- width-only scaling, both directions, single row: 4 columns A,B,C,D.
	{
		static const uint8_t cols4[2] = { 0x12, 0x34 }; // 1,2,3,4 across one row

		// Downscale to 2: col_step = (4<<16)/2 = 2<<16 -> columns 0 and 2 (1 and 3 -> 0x30).
		pnx_gfx_clear(t, 0x00);
		pnx_blit_4bpp_scaled(t, cols4, &pal, 50, 50, 4, 1, 2, 1);
		BS_CHECK_EQ(pixel_at(t, 50, 50), 0x10);
		BS_CHECK_EQ(pixel_at(t, 51, 50), 0x30);

		// Upscale 2 -> 4: col_step = (2<<16)/4 = 32768 -> columns 0,0,1,1 (AABB).
		static const uint8_t cols2[1] = { 0x12 }; // 1, 2
		pnx_gfx_clear(t, 0x00);
		pnx_blit_4bpp_scaled(t, cols2, &pal, 60, 60, 2, 1, 4, 1);
		BS_CHECK_EQ(pixel_at(t, 60, 60), 0x10);
		BS_CHECK_EQ(pixel_at(t, 61, 60), 0x10);
		BS_CHECK_EQ(pixel_at(t, 62, 60), 0x20);
		BS_CHECK_EQ(pixel_at(t, 63, 60), 0x20);
	}

	// --- general two-axis case: a single marker pixel (col=1,row=2) in a 4x4 grid,
	// downscaled to 3x3 (col_step=row_step=(4<<16)/3=87381, selecting source
	// columns/rows 0,1,2 for dest 0,1,2 -- column/row 3 never sampled). The marker
	// must land at exactly dest(1,2) and nowhere else, pinning down that the column
	// and row accumulators are independent and both correct at once, not just each
	// axis in isolation the way the width-only/height-only cases above already cover.
	{
		static const uint8_t mark4x4[8] = {
			0x00,
			0x00, // row 0: nothing
			0x00,
			0x00, // row 1: nothing
			0x01,
			0x00, // row 2: col 1 opaque (low nibble of byte 0 -> pixel index 1)
			0x00,
			0x00, // row 3: nothing
		};

		pnx_gfx_clear(t, 0x00);
		pnx_blit_4bpp_scaled(t, mark4x4, &pal, 70, 70, 4, 4, 3, 3);
		int hits = 0;
		for (int16_t j = 0; j < 3; j++)
			for (int16_t i = 0; i < 3; i++)
				if (pixel_at(t, (int16_t)(70 + i), (int16_t)(70 + j)) != 0x00)
					hits++;
		BS_CHECK_EQ(hits, 1);
		BS_CHECK_EQ(pixel_at(t, 71, 72), 0x10); // dest(1,2) only
	}

	// --- transparency survives scaling, on both a sampled-transparent source pixel and
	// an opaque one selected by the same scaled draw.
	{
		static const uint8_t half4[2] = { 0x11, 0x00 }; // cols 0,1 = 1 (opaque), 2,3 = 0
														// (transparent)

		// Unscaled dispatch (dst==src): confirms transparency isn't broken by routing
		// through pnx_blit_4bpp_scaled at all before testing it under real scaling below.
		pnx_gfx_clear(t, 0x55); // distinct background, not 0x00, so "untouched" is visible
		pnx_blit_4bpp_scaled(t, half4, &pal, 80, 80, 4, 1, 4, 1);
		BS_CHECK_EQ(pixel_at(t, 80, 80), 0x10);
		BS_CHECK_EQ(pixel_at(t, 82, 80), 0x55); // transparent index left the bg alone

		// Scaled (upscale 4 -> 6): col_step = (4<<16)/6 = 43690. i=0..2 -> col 0 (opaque,
		// 1x scaled cols 0,0,0 land inside the opaque half); i=3..5 -> col 2,2,3
		// (transparent half). Confirms transparency is still respected once the column
		// accumulator, not a literal index, picks the source pixel.
		pnx_gfx_clear(t, 0x55);
		pnx_blit_4bpp_scaled(t, half4, &pal, 90, 90, 4, 1, 6, 1);
		BS_CHECK_EQ(pixel_at(t, 90, 90), 0x10);
		BS_CHECK_EQ(pixel_at(t, 95, 90), 0x55);
	}

	// --- vertical clip combined with scaling: source rows 0..3 marked 1,2,3,4,
	// upscaled 2x tall (row_step = (4<<16)/8 = 32768) and drawn at y=-4, so only dest
	// rows 4..7 are visible (screen y 0..3). This is the case most likely to break if
	// the source-row lookup were computed relative to the clipped/visible range instead
	// of the true (unclipped) destination row index j.
	{
		static const uint8_t rows4[4] = { 0x11, 0x22, 0x33, 0x44 };

		pnx_gfx_clear(t, 0x00);
		pnx_blit_4bpp_scaled(t, rows4, &pal, 10, -4, 2, 4, 2, 8);
		BS_CHECK_EQ(pixel_at(t, 10, 0), 0x30); // dest row 4 -> source row 2
		BS_CHECK_EQ(pixel_at(t, 10, 1), 0x30); // dest row 5 -> source row 2
		BS_CHECK_EQ(pixel_at(t, 10, 2), 0x40); // dest row 6 -> source row 3
		BS_CHECK_EQ(pixel_at(t, 10, 3), 0x40); // dest row 7 -> source row 3
	}
}
