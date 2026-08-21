// Host tests for pnx_hud_window.c: anchor resolution, show/hide animation, and the
// blob-parsing half of pnx_hud_window_load.
//
// The anchor/animation/draw-dispatch tests build a PnxHudWindow/PnxHudElement directly
// (test_sprite.c's own "hand-built fixture, not loaded from a blob" approach) rather than
// going through the asset pipeline -- pnx_hud_window_draw never cares how its elements
// got there. Only test_load exercises the real byte-parsing path, with a blob hand-built
// the way test_text.c's build_font_blob/install_blob already do for fonts, kept to a
// single `bar` element so it needs no nested nine_slice/sprite/font blob of its own.

#include "../src/pnx/gfx/pnx_hud_vars.h"
#include "../src/pnx/gfx/pnx_hud_window.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define W_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define W_CHECK_EQ(a, b)                                                                     \
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

static uint8_t px(PnxTarget* t, int16_t x, int16_t y)
{
	PnxRow row = pnx_target_row(t, y);
	return row.data ? row.data[x] : 0;
}

// A single bar element, at rest (no slide -- from == to), at `anchor`/`offset`.
static PnxHudWindow bar_window(PnxHudElement* storage, uint8_t anchor, int16_t offx,
							   int16_t offy, uint8_t border)
{
	storage[0] = (PnxHudElement){
		.kind	  = PNX_HUD_ELEMENT_BAR,
		.anchor	  = anchor,
		.offset_x = offx,
		.offset_y = offy,
		.hud_var  = PNX_HUD_VAR_NONE,
		.as.bar	  = { .w = 10, .h = 6, .border = border, .track = 0, .fill = 0, .max = 100 },
	};
	PnxHudWindow win = { 0 };
	win.elements	 = storage;
	win.count		 = 1;
	// duration 0, from == to: pnx_tween_value returns this constant regardless of now_ms.
	pnx_tween_start(&win.slide_x, 0, 0, 0, pnx_ease_linear, 0);
	pnx_tween_start(&win.slide_y, 0, 0, 0, pnx_ease_linear, 0);
	return win;
}

// The bar's top-left (its border pixel, pnx_hud.c's pnx_hud_bar_draw) lands exactly on
// the anchor point at TOP_LEFT with a zero offset -- the one anchor size-aware
// correction never touches, so this alone pins anchor_point's own raw math.
static void test_anchor_top_left(void)
{
	PnxHudElement storage[1];
	PnxTarget* t = pnx_host_target();
	pnx_gfx_clear(t, 0);
	PnxHudWindow win = bar_window(storage, PNX_HUD_ANCHOR_TOP_LEFT, 0, 0, 0xFF);
	pnx_hud_window_draw(&win, t, NULL, 0);
	W_CHECK_EQ(px(t, 0, 0), 0xFF);
}

// Size-aware anchor correction (pnx_hud_window.c's anchor_side_h/anchor_side_v): a
// RIGHT/BOTTOM/CENTER anchor moves the element's OWN far edge or centre onto the anchor
// point, not its top-left corner -- the fix for a right-anchored element otherwise
// running straight off the right edge, which is exactly backwards from what naming a
// RIGHT anchor is for. Checked two ways per anchor: the corrected top-left pixel is
// where the math says it should be, AND (the real regression case) the whole 10x6 bar
// stays fully on a 0-offset screen edge instead of running past it.
static void test_anchor_size_correction(void)
{
	PnxHudElement storage[1];
	PnxTarget* t	= pnx_host_target();
	const int16_t w = PNX_DISPLAY_WIDTH, h = PNX_DISPLAY_HEIGHT;
	const int16_t bw = 10, bh = 6;

	struct
	{
		uint8_t anchor;
		int8_t sh, sv; // -1 near/top, 0 centre, 1 far/bottom -- must match anchor_side_h/_v
	} cases[] = {
		{ PNX_HUD_ANCHOR_TOP_LEFT, -1, -1 },
		{ PNX_HUD_ANCHOR_TOP, 0, -1 },
		{ PNX_HUD_ANCHOR_TOP_RIGHT, 1, -1 },
		{ PNX_HUD_ANCHOR_LEFT, -1, 0 },
		{ PNX_HUD_ANCHOR_CENTER, 0, 0 },
		{ PNX_HUD_ANCHOR_RIGHT, 1, 0 },
		{ PNX_HUD_ANCHOR_BOTTOM_LEFT, -1, 1 },
		{ PNX_HUD_ANCHOR_BOTTOM, 0, 1 },
		{ PNX_HUD_ANCHOR_BOTTOM_RIGHT, 1, 1 },
	};

	for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
	{
		pnx_gfx_clear(t, 0);
		PnxHudWindow win = bar_window(storage, cases[i].anchor, 0, 0, 0xFF);
		pnx_hud_window_draw(&win, t, NULL, 0);

		int16_t ax, ay; // the raw anchor point, computed the same way anchor_point does
		switch (cases[i].sh)
		{
			case -1: ax = 0; break;
			case 0: ax = (int16_t)(w / 2); break;
			default: ax = w; break;
		}
		switch (cases[i].sv)
		{
			case -1: ay = 0; break;
			case 0: ay = (int16_t)(h / 2); break;
			default: ay = h; break;
		}
		const int16_t ex = (int16_t)(ax - (cases[i].sh == 0 ? bw / 2 : cases[i].sh == 1 ? bw
																						: 0));
		const int16_t ey = (int16_t)(ay - (cases[i].sv == 0 ? bh / 2 : cases[i].sv == 1 ? bh
																						: 0));
		W_CHECK_EQ(px(t, ex, ey), 0xFF);

		// The real regression case: every pixel of the bar's own box is on screen.
		W_CHECK(ex >= 0 && ex + bw <= w && ey >= 0 && ey + bh <= h);
	}
}

// pnx_hud_var-bound bar value reaches the draw call -- proves the binding, not just the
// placement: a filled pixel just inside the border only appears once `value` is nonzero.
static void test_bar_reads_hud_var(void)
{
	int32_t ints[1] = { 0 };
	pnx_hud_vars_init(ints, NULL, 1);

	PnxHudElement storage[1] = { {
		.kind	  = PNX_HUD_ELEMENT_BAR,
		.anchor	  = PNX_HUD_ANCHOR_TOP_LEFT,
		.offset_x = 0,
		.offset_y = 0,
		.hud_var  = 0,
		.as.bar	  = { .w = 10, .h = 6, .border = 0x01, .track = 0x00, .fill = 0xFF, .max = 100 },
	} };
	PnxHudWindow win		 = { 0 };
	win.elements			 = storage;
	win.count				 = 1;
	pnx_tween_start(&win.slide_x, 0, 0, 0, pnx_ease_linear, 0);
	pnx_tween_start(&win.slide_y, 0, 0, 0, pnx_ease_linear, 0);

	PnxTarget* t = pnx_host_target();
	pnx_gfx_clear(t, 0);
	pnx_hud_window_draw(&win, t, NULL, 0);
	W_CHECK_EQ(px(t, 1, 1), 0x00); // value 0: track only, no fill yet

	pnx_hud_var_set_i32(0, 100); // full
	pnx_gfx_clear(t, 0);
	pnx_hud_window_draw(&win, t, NULL, 0);
	W_CHECK_EQ(px(t, 1, 1), 0xFF); // now filled
}

// pnx_hud_window_show/_hide retarget the two slide tweens; interrupting one with the
// other reverses smoothly from wherever the slide currently sits (this function's own
// doc comment in pnx_hud_window.h), not from the configured offscreen/onscreen extreme.
static void test_show_hide(void)
{
	PnxHudElement storage[1];
	PnxHudWindow win = bar_window(storage, PNX_HUD_ANCHOR_TOP_LEFT, 0, 0, 0xFF);
	win.slide_dx	 = 40;
	win.slide_dy	 = 0;
	win.show_ms		 = 100;
	win.hide_ms		 = 100;
	// Starts hidden: sitting at the offscreen displacement, forever, until shown.
	pnx_tween_start(&win.slide_x, win.slide_dx, win.slide_dx, 0, pnx_ease_linear, 0);
	pnx_tween_start(&win.slide_y, win.slide_dy, win.slide_dy, 0, pnx_ease_linear, 0);

	W_CHECK_EQ(pnx_tween_value(&win.slide_x, 0), 40);

	pnx_hud_window_show(&win, 0);
	W_CHECK_EQ(pnx_tween_value(&win.slide_x, 0), 40);  // just started: still at 40
	W_CHECK_EQ(pnx_tween_value(&win.slide_x, 100), 0); // fully shown at show_ms

	// Interrupt a hide partway through with a show: must continue from the CURRENT
	// value, not snap back to 40 first.
	pnx_hud_window_hide(&win, 100);							// from 0 toward 40, over [100,200]
	const int32_t mid = pnx_tween_value(&win.slide_x, 150); // halfway: ~20
	W_CHECK(mid > 5 && mid < 35);
	pnx_hud_window_show(&win, 150);						 // reverse from `mid`, not from 40
	W_CHECK_EQ(pnx_tween_value(&win.slide_x, 150), mid); // no jump at the interrupt instant
	W_CHECK_EQ(pnx_tween_value(&win.slide_x, 250), 0);	 // and reaches 0 (shown) by 150+100
}

// A window before its first show/hide call reads as fully hidden -- pnx_hud_window_load's
// own initial state (this function's doc comment) -- so a window a game never shows
// draws nothing a viewer would call visible, with no separate "have I shown this" flag.
static void test_hidden_until_shown(void)
{
	PnxHudElement storage[1] = { {
		.kind	  = PNX_HUD_ELEMENT_BAR,
		.anchor	  = PNX_HUD_ANCHOR_TOP_LEFT,
		.offset_x = 0,
		.offset_y = 0,
		.hud_var  = PNX_HUD_VAR_NONE,
		.as.bar	  = { .w = 10, .h = 6, .border = 0xFF, .track = 0, .fill = 0, .max = 100 },
	} };
	PnxHudWindow win		 = { 0 };
	win.elements			 = storage;
	win.count				 = 1;
	win.slide_dx			 = 250; // off the 200px-wide host display entirely
	win.slide_dy			 = 0;
	pnx_tween_start(&win.slide_x, win.slide_dx, win.slide_dx, 0, pnx_ease_linear, 0);
	pnx_tween_start(&win.slide_y, 0, 0, 0, pnx_ease_linear, 0);

	PnxTarget* t = pnx_host_target();
	pnx_gfx_clear(t, 0);
	pnx_hud_window_draw(&win, t, NULL, 0);
	W_CHECK_EQ(px(t, 0, 0), 0); // nothing drawn at the resting anchor: the bar sat at x=250
}

// --------------------------------------------------------------------------- load path

static void install_blob(uint32_t id, const char* path, const uint8_t* b, size_t len)
{
	FILE* f = fopen(path, "wb");
	if (!f)
	{
		printf("  FAIL cannot write %s\n", path);
		s_failures++;
		return;
	}
	(void)fwrite(b, 1, len, f);
	fclose(f);
	pnx_host_register_resource(id, path);
}

// One `bar` element -- the only kind needing no nested nine_slice/sprite/font blob of its
// own, so this proves the header/element byte layout without a cascading fixture.
static uint8_t* build_window_blob(size_t* out_len, uint16_t show_ms, uint16_t hide_ms,
								  int16_t slide_x, int16_t slide_y, uint8_t ease)
{
	const size_t payload = 8 + PNX_HUD_ELEMENT_BYTES;
	const size_t len	 = PNX_BLOB_HEADER_BYTES + payload;
	uint8_t* b			 = calloc(1, len);
	size_t o			 = 0;

	b[o++] = 'H';
	b[o++] = 'W';
	b[o++] = PNX_BLOB_VERSION;
	b[o++] = 1; // element count
	b[o++] = ease;
	b[o++] = 0; // reserved
	b[o++] = 0; // reserved
	b[o++] = 0; // orientation

	b[o++] = (uint8_t)(show_ms & 0xFF);
	b[o++] = (uint8_t)(show_ms >> 8);
	b[o++] = (uint8_t)(hide_ms & 0xFF);
	b[o++] = (uint8_t)(hide_ms >> 8);
	b[o++] = (uint8_t)((uint16_t)slide_x & 0xFF);
	b[o++] = (uint8_t)((uint16_t)slide_x >> 8);
	b[o++] = (uint8_t)((uint16_t)slide_y & 0xFF);
	b[o++] = (uint8_t)((uint16_t)slide_y >> 8);

	// kind, anchor, offset_x(2), offset_y(2), asset_id(2, unused by `bar`), hud_var,
	// p0=border, p1=track, p2=fill, w(2), h(2), bar_max(2).
	b[o++] = PNX_HUD_ELEMENT_BAR;
	b[o++] = PNX_HUD_ANCHOR_BOTTOM_RIGHT;
	b[o++] = (uint8_t)(3 & 0xFF); // offset_x = 3
	b[o++] = 0;
	b[o++] = (uint8_t)((int16_t)-3 & 0xFF); // offset_y = -3
	b[o++] = (uint8_t)(((int16_t)-3 >> 8) & 0xFF);
	b[o++] = 0xFF; // asset_id lo (unused)
	b[o++] = 0xFF; // asset_id hi
	b[o++] = 0;	   // hud_var: 0
	b[o++] = 0x11; // border
	b[o++] = 0x22; // track
	b[o++] = 0x33; // fill
	b[o++] = 20;   // w lo
	b[o++] = 0;	   // w hi
	b[o++] = 8;	   // h lo
	b[o++] = 0;	   // h hi
	b[o++] = 200;  // bar_max lo (200)
	b[o++] = 0;	   // bar_max hi

	*out_len = len;
	return b;
}

static void test_load(void)
{
	static uint32_t resources[3];
	for (uint32_t i = 0; i < 3; i++)
		resources[i] = i + 700; // arbitrary, distinct from every other suite's ids

	static PnxArena persistent, scene;
	pnx_arena_init(&persistent, "hud-window-persistent", 1024, 4);
	pnx_arena_init(&scene, "hud-window-scene", 1024, 4);
	pnx_assets_init(&persistent, &scene, resources, 3);

	size_t len;
	uint8_t* blob = build_window_blob(&len, 250, 200, 40, 0, /*ease=*/5 /* out_cubic */);
	install_blob(resources[0], "build/test_hud_window.bin", blob, len);
	free(blob);

	PnxHudElement storage[2];
	PnxHudWindow win;
	W_CHECK(pnx_hud_window_load(&win, 0, storage, 2));
	W_CHECK_EQ(win.count, 1);
	W_CHECK_EQ(win.show_ms, 250);
	W_CHECK_EQ(win.hide_ms, 200);
	W_CHECK_EQ(win.slide_dx, 40);
	W_CHECK_EQ(win.slide_dy, 0);
	// Starts hidden, sitting at the offscreen displacement (this module's own contract).
	W_CHECK_EQ(pnx_tween_value(&win.slide_x, 0), 40);

	W_CHECK_EQ(win.elements[0].kind, PNX_HUD_ELEMENT_BAR);
	W_CHECK_EQ(win.elements[0].anchor, PNX_HUD_ANCHOR_BOTTOM_RIGHT);
	W_CHECK_EQ(win.elements[0].offset_x, 3);
	W_CHECK_EQ(win.elements[0].offset_y, -3);
	W_CHECK_EQ(win.elements[0].as.bar.border, 0x11);
	W_CHECK_EQ(win.elements[0].as.bar.track, 0x22);
	W_CHECK_EQ(win.elements[0].as.bar.fill, 0x33);
	W_CHECK_EQ(win.elements[0].as.bar.w, 20);
	W_CHECK_EQ(win.elements[0].as.bar.h, 8);
	W_CHECK_EQ(win.elements[0].as.bar.max, 200);

	// Storage smaller than the blob's own element count is refused, not truncated.
	PnxHudElement tiny[1];
	// This blob only has 1 element, so re-encode with count=2 in the header to exercise
	// the refusal without a second nested-blob element to satisfy.
	blob	= build_window_blob(&len, 250, 200, 40, 0, 5);
	blob[3] = 2; // claim 2 elements while the payload only holds 1 element's worth
	install_blob(resources[1], "build/test_hud_window_bad_count.bin", blob, len);
	free(blob);
	W_CHECK(!pnx_hud_window_load(&win, 1, tiny, 1));

	// An out-of-range ease id is refused.
	blob = build_window_blob(&len, 250, 200, 40, 0, /*ease=*/99);
	install_blob(resources[2], "build/test_hud_window_bad_ease.bin", blob, len);
	free(blob);
	W_CHECK(!pnx_hud_window_load(&win, 2, storage, 2));
}

void test_hud_window(void)
{
	printf("hud_window\n");

	test_anchor_top_left();
	test_anchor_size_correction();
	test_bar_reads_hud_var();
	test_show_hide();
	test_hidden_until_shown();
	test_load();
}
