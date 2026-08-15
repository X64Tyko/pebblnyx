// Verifies the blitter actually honours a round display's per-row bounds, rather than
// merely being designed to (docs/ROADMAP.md's M9: "PnxRow carries min_x/max_x... verify
// the blitter honours them and that nothing assumes a rectangle"). Every OTHER host test
// runs against a rectangular target (PNX_HOST_WIDTH x PNX_HOST_HEIGHT with pnx_target_row
// always reporting the full row), so gabbro and chalk's actual shape -- a per-row span
// that is NOT [0, w-1] -- has never been exercised by anything before this file.
//
// The technique: poison the whole framebuffer to a sentinel no draw call ever produces,
// draw through pnx_host_set_round(true), then walk every pixel and check that ONLY those
// within that row's own [min_x, max_x] (queried fresh per row, the same oracle the
// blitter itself reads) changed. A write that landed one column past the mask is a
// corrupted neighbour on real hardware; here it is a specific (x, y) in a FAIL line.
//
// Not part of the main `make test` binary: that target is 200x228 (PNX_HOST_WIDTH x
// PNX_HOST_HEIGHT), which is not square and so cannot be round at all -- pnx_target_row
// only applies the circular mask when width equals height. Build square instead:
//
//   cc -std=c11 -Wall -Wextra -Wno-unused-parameter -DPNX_PLATFORM_HOST -DPNX_HOST_WIDTH=200 -DPNX_HOST_HEIGHT=200 -DPNX_USE_SYNTH=1 -g -O1 test_round.c ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/gfx/pnx_gfx.c ../src/pnx/platform/pnx_platform_host.c -o /tmp/test_round && /tmp/test_round

#include "../src/pnx/gfx/pnx_gfx.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

static int s_checks, s_failures;

#define CHECK(cond)                                                  \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define SENTINEL 0xEE

// Direct pixel access, bypassing any clipping: pnx_target_row's `data` pointer is column 0
// of the row REGARDLESS of the row's reported [min_x, max_x] (the host target is a flat
// buffer underneath), so this can poison and later read every column, including the ones
// a round mask should have refused to draw into.
static void poke(PnxTarget* t, int16_t x, int16_t y, uint8_t v)
{
	PnxRow row = pnx_target_row(t, y);
	if (row.data)
		row.data[x] = v;
}
static uint8_t peek(PnxTarget* t, int16_t x, int16_t y)
{
	PnxRow row = pnx_target_row(t, y);
	return row.data ? row.data[x] : 0;
}

static void poison(PnxTarget* t, int16_t w, int16_t h)
{
	for (int16_t y = 0; y < h; y++)
		for (int16_t x = 0; x < w; x++)
			poke(t, x, y, SENTINEL);
}

// Walks every pixel and checks it against the mask pnx_target_row itself reports for that
// row -- the same oracle the blitter under test was given. `expect_in` is what an in-mask
// pixel should hold after the draw; every out-of-mask pixel must still be SENTINEL.
static void check_masked(PnxTarget* t, int16_t w, int16_t h, const char* what,
						 uint8_t expect_in)
{
	for (int16_t y = 0; y < h; y++)
	{
		PnxRow row = pnx_target_row(t, y);
		for (int16_t x = 0; x < w; x++)
		{
			const bool in_mask = row.data && x >= row.min_x && x <= row.max_x;
			const uint8_t got  = peek(t, x, y);
			s_checks++;
			if (in_mask && got != expect_in)
			{
				printf("  FAIL %s: (%d,%d) in mask, expected %#x got %#x\n", what, x, y,
					   expect_in, got);
				s_failures++;
			}
			else if (!in_mask && got != SENTINEL)
			{
				printf("  FAIL %s: (%d,%d) OUTSIDE mask [%d,%d] but got written -- %#x\n",
					   what, x, y, row.min_x, row.max_x, got);
				s_failures++;
			}
		}
	}
}

int main(void)
{
#if PNX_DISPLAY_WIDTH != PNX_DISPLAY_HEIGHT
	printf(
		"test_round: build with PNX_HOST_WIDTH == PNX_HOST_HEIGHT, this is a no-op "
		"otherwise\n");
	return 0;
#else
	printf("test_round (%dx%d)\n", PNX_DISPLAY_WIDTH, PNX_DISPLAY_HEIGHT);
	const int16_t w = PNX_DISPLAY_WIDTH, h = PNX_DISPLAY_HEIGHT;
	PnxTarget* t = pnx_host_target();

	// Confirms the mask is genuinely non-rectangular before trusting anything else here --
	// a bug that made round mode a no-op would make every check below pass vacuously.
	pnx_host_set_round(true);
	PnxRow top = pnx_target_row(t, 0), mid = pnx_target_row(t, (int16_t)(h / 2));
	CHECK(top.max_x - top.min_x < mid.max_x - mid.min_x);
	// The row nearest the centre is within a pixel or two of full width, not exactly --
	// an even-diameter circle's true centre sits BETWEEN two integer rows, so no single
	// row ever touches both edges of the continuous circle exactly.
	CHECK(mid.max_x - mid.min_x >= w - 3);

	// pnx_gfx_clear: every row, so this is the widest-coverage check -- if the mask were
	// ignored entirely, this is what would light up as a solid square instead of a disc.
	pnx_host_reset();
	pnx_host_set_round(true);
	poison(t, w, h);
	pnx_gfx_clear(t, 0xC0);
	check_masked(t, w, h, "pnx_gfx_clear", 0xC0);

	// pnx_gfx_fill_rect spanning the full width: the case that most resembles a dialog
	// box or HUD panel author-once code draws without knowing the screen is round.
	poison(t, w, h);
	pnx_gfx_fill_rect(t, 0, 0, w, h, 0xC7);
	check_masked(t, w, h, "pnx_gfx_fill_rect", 0xC7);

	// pnx_blit_4bpp, unmirrored and mirrored, positioned to straddle the mask's edge
	// rather than sit safely in the middle -- an opaque 8x8 block in the top-left corner,
	// where the circular mask is narrowest and clipping actually has work to do.
	static const uint8_t OPAQUE8[32] = {
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
		0x11,
	};
	PnxPalette pal;
	memset(&pal, 0, sizeof(pal));
	pal.entries[1] = 0xFF; // the only non-transparent index OPAQUE8 uses

	poison(t, w, h);
	pnx_blit_4bpp(t, OPAQUE8, &pal, -2, -2, 8, 8, PNX_FLIP_NONE);
	pnx_blit_4bpp(t, OPAQUE8, &pal, (int16_t)(w - 6), -2, 8, 8, PNX_FLIP_X);
	// Not check_masked here: only two small corners were drawn into, not the whole
	// screen, so "everywhere else is still SENTINEL" is the same check but the "in mask"
	// half only applies within the two blit rects. Walk those rects directly instead.
	for (int16_t y = -2; y < 6; y++)
	{
		if (y < 0 || y >= h)
			continue;
		PnxRow row = pnx_target_row(t, y);
		for (int16_t x = -2; x < 6; x++)
		{
			if (x < 0 || x >= w)
				continue;
			const bool in_mask = x >= row.min_x && x <= row.max_x;
			s_checks++;
			if (peek(t, x, y) != (in_mask ? 0xFF : SENTINEL))
			{
				printf("  FAIL pnx_blit_4bpp top-left: (%d,%d) mask[%d,%d] got %#x\n", x, y,
					   row.min_x, row.max_x, peek(t, x, y));
				s_failures++;
			}
		}
	}

	printf("%d checks, %d failures\n", s_checks, s_failures);
	return s_failures ? 1 : 0;
#endif
}
