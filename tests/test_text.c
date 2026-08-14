// Host tests for fonts: the blob reader, glyph placement, clipping, wrapping.
//
// These build their own font rather than leaning on the example's, because the point is
// to assert exact pixels at exact coordinates and that needs metrics chosen to make the
// arithmetic visible -- a bearing that is not zero, an advance that is not the glyph
// width, and a glyph the font deliberately lacks.
//
// The one thing the host cannot reach is a narrowed row span: pnx_target_row reports
// min_x = 0 and max_x = w - 1 for every row here, where a round display reports less.
// The span writers clip against the row rather than the target width, but only a device
// or a round-target host build proves it.

#include "../src/pnx/gfx/pnx_text.h"
#include "../src/pnx/gfx/pnx_gfx.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define T_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define T_CHECK_EQ(a, b)                                                                     \
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

void test_text(void);

// ------------------------------------------------------------------- the test font
//
// Codepoints 32..35, so the map is four bytes:
//
//   ' '  glyph 0   no ink, advance 2          -- the space path: advance without a blit
//   '!'  glyph 1   2x3 block, bearing (0, 3), advance 3
//   '"'  glyph 2   4x2 block, bearing (1, 5), advance 6   -- non-zero x bearing
//   '#'            absent, so it resolves to the fallback
//
// line_height 8, baseline 6. Advance differs from width on every glyph deliberately:
// a font where they matched would hide an error that swapped them.

#define FONT_FIRST	  32
#define FONT_LAST	  35
#define FONT_GLYPHS	  3
#define FONT_FALLBACK 1 // '!' stands in for anything missing

static uint8_t* build_font_blob(size_t* out_len, uint8_t depth, uint8_t version)
{
	// 2x3 at 1bpp is one byte per row: 0b11xxxxxx. 4x2 likewise: 0b1111xxxx.
	static const uint8_t bitmaps[5] = { 0xC0, 0xC0, 0xC0, 0xF0, 0xF0 };
	const uint16_t bitmap_bytes		= sizeof(bitmaps);

	const size_t index_bytes = FONT_GLYPHS * PNX_FONT_GLYPH_BYTES;
	const size_t map_bytes	 = FONT_LAST - FONT_FIRST + 1;
	const size_t len		 = PNX_BLOB_HEADER_BYTES + 8 + index_bytes + map_bytes + bitmap_bytes;

	uint8_t* b = calloc(1, len);
	size_t o   = 0;

	b[o++] = 'P';
	b[o++] = 'F';
	b[o++] = version;
	b[o++] = depth;
	b[o++] = 8; // line_height
	b[o++] = 6; // baseline
	b[o++] = 0; // advance axis: left to right
	b[o++] = 0; // orientation: buttons right

	b[o++] = FONT_GLYPHS;
	b[o++] = 0;
	b[o++] = (uint8_t)bitmap_bytes;
	b[o++] = 0;
	b[o++] = FONT_FIRST;
	b[o++] = FONT_LAST;
	b[o++] = FONT_FALLBACK;
	b[o++] = 2; // space_advance

	// offset lo, offset hi, w, h, advance, bearing_x, bearing_y, pad
	static const uint8_t index[FONT_GLYPHS][PNX_FONT_GLYPH_BYTES] = {
		{ 0, 0, 0, 0, 2, 0, 0, 0 }, // ' '
		{ 0, 0, 2, 3, 3, 0, 3, 0 }, // '!'
		{ 3, 0, 4, 2, 6, 1, 5, 0 }, // '"'
	};
	memcpy(b + o, index, index_bytes);
	o += index_bytes;

	b[o++] = 0;					// ' '
	b[o++] = 1;					// '!'
	b[o++] = 2;					// '"'
	b[o++] = PNX_FONT_NO_GLYPH; // '#' is not carried

	memcpy(b + o, bitmaps, bitmap_bytes);

	*out_len = len;
	return b;
}

// Writes a blob to disk and points a resource id at it, since the host platform reads
// resources from files.
static void install_blob(uint32_t id, const char* path, const uint8_t* b, size_t len)
{
	FILE* f = fopen(path, "wb");
	if (!f)
	{
		printf("  FAIL cannot write %s\n", path);
		s_failures++;
		return;
	}
	(void)fwrite(b, 1, len, f); // a short write here fails the blob a test loads right
								// after, which is a louder signal than checking it here
	fclose(f);
	pnx_host_register_resource(id, path);
}

static uint8_t px(PnxTarget* t, int16_t x, int16_t y)
{
	PnxRow row = pnx_target_row(t, y);
	return row.data ? row.data[x] : 0;
}

// Counts non-background pixels, so a test can assert that clipping removed exactly the
// pixels it should have and left the rest.
static int ink_count(PnxTarget* t, uint8_t background)
{
	int n = 0;
	for (int16_t y = 0; y < pnx_target_height(t); y++)
	{
		for (int16_t x = 0; x < pnx_target_width(t); x++)
		{
			if (px(t, x, y) != background)
				n++;
		}
	}
	return n;
}

#define INK 0xFF

// ------------------------------------------------------------- rotated fonts (M4c)
//
// A landscape build bakes its glyphs on their side and stamps an advance axis into the
// font, so the pen walks down a column instead of across a row. The claim worth testing
// is not "some pixels appear" -- it is that a rotated font draws the SAME IMAGE, turned:
// every pixel of the portrait render lands where the rotation says it should, including
// the ones alignment and line stacking put there.
//
// So each case here renders twice. Once with the portrait font, into a virtual author
// canvas of ROT_W x ROT_H; once with the rotated font, at the rotated origin. Then every
// pixel in the window is compared through the same mapping the pipeline uses. A sign
// error in a bearing, a line stacking the wrong way, or an off-by-one on a glyph's far
// edge all fail this, and none of them would fail a test that counted ink.
//
// The canvas is a window inside the host's 200x228 target rather than a target of its
// own: only the coordinate mapping matters, and both orientations have to fit somewhere.

#define ROT_W 40 // author canvas: wider than tall, which is the point of landscape
#define ROT_H 30

// The one glyph, deliberately asymmetric under BOTH mirrors and under transposition. A
// solid block -- which is what the font above uses -- cannot tell a clockwise rotation
// from an anticlockwise one, and would pass every arrangement of these tests.
#define ROT_GW 3
#define ROT_GH 4
static const char* const ROT_GLYPH[ROT_GH] = {
	"##.",
	"#..",
	"##.",
	"#.#",
};

#define ROT_ADVANCE		5
#define ROT_BEARING_X	1
#define ROT_BEARING_Y	5
#define ROT_LINE_HEIGHT 9
#define ROT_BASELINE	7

// Author (ax, ay) -> framebuffer (fx, fy), for a canvas of w x h. The C twin of
// rotate_point in tools/pnx_assets.py: if these two ever disagree, content is baked one
// way and drawn the other.
static void rot_point(int ax, int ay, int w, int h, uint8_t axis, int* fx, int* fy)
{
	if (axis == PNX_ADVANCE_Y_POS)
	{
		*fx = h - 1 - ay;
		*fy = ax;
	}
	else if (axis == PNX_ADVANCE_Y_NEG)
	{
		*fx = ay;
		*fy = w - 1 - ax;
	}
	else
	{
		*fx = ax;
		*fy = ay;
	}
}

// Packs the glyph, rotated to match the axis, MSB-first one row at a time -- the same
// layout pack_glyph_rows emits.
static size_t pack_rot_glyph(uint8_t axis, uint8_t* out, uint8_t* out_w, uint8_t* out_h)
{
	const int rotated	= (axis == PNX_ADVANCE_Y_POS || axis == PNX_ADVANCE_Y_NEG);
	const int w			= rotated ? ROT_GH : ROT_GW;
	const int h			= rotated ? ROT_GW : ROT_GH;
	const size_t stride = (size_t)((w + 7) / 8);

	memset(out, 0, stride * (size_t)h);
	for (int gy = 0; gy < ROT_GH; gy++)
	{
		for (int gx = 0; gx < ROT_GW; gx++)
		{
			if (ROT_GLYPH[gy][gx] != '#')
				continue;
			int x = gx, y = gy;
			rot_point(gx, gy, ROT_GW, ROT_GH, axis, &x, &y);
			out[(size_t)y * stride + (size_t)(x >> 3)] |= (uint8_t)(0x80u >> (x & 7));
		}
	}

	*out_w = (uint8_t)w;
	*out_h = (uint8_t)h;
	return stride * (size_t)h;
}

// One glyph at codepoint 'A', plus a space, so wrapping has something to break on.
static uint8_t* build_rot_font(size_t* out_len, uint8_t axis)
{
	uint8_t bits[16];
	uint8_t gw = 0, gh = 0;
	const size_t bitmap_bytes = pack_rot_glyph(axis, bits, &gw, &gh);

	const size_t glyphs		 = 2;
	const size_t index_bytes = glyphs * PNX_FONT_GLYPH_BYTES;
	const size_t map_bytes	 = 'A' - ' ' + 1;
	const size_t len		 = PNX_BLOB_HEADER_BYTES + 8 + index_bytes + map_bytes + bitmap_bytes;

	uint8_t* b = calloc(1, len);
	size_t o   = 0;

	b[o++] = 'P';
	b[o++] = 'F';
	b[o++] = PNX_BLOB_VERSION;
	b[o++] = 1; // depth
	b[o++] = ROT_LINE_HEIGHT;
	b[o++] = ROT_BASELINE;
	b[o++] = axis;
	b[o++] = 0; // orientation: the stamp is checked elsewhere

	b[o++] = (uint8_t)glyphs;
	b[o++] = 0;
	b[o++] = (uint8_t)bitmap_bytes;
	b[o++] = 0;
	b[o++] = ' ';
	b[o++] = 'A';
	b[o++] = 1;			  // fallback: the inked glyph
	b[o++] = ROT_ADVANCE; // space_advance

	const uint8_t index[2][PNX_FONT_GLYPH_BYTES] = {
		{ 0, 0, 0, 0, ROT_ADVANCE, 0, 0, 0 },							// ' '
		{ 0, 0, gw, gh, ROT_ADVANCE, ROT_BEARING_X, ROT_BEARING_Y, 0 }, // 'A'
	};
	memcpy(b + o, index, index_bytes);
	o += index_bytes;

	for (size_t i = 0; i < map_bytes; i++)
		b[o + i] = PNX_FONT_NO_GLYPH;
	b[o]				 = 0; // ' '
	b[o + map_bytes - 1] = 1; // 'A'
	o += map_bytes;

	memcpy(b + o, bits, bitmap_bytes);

	*out_len = len;
	return b;
}

// Every pixel of the author canvas, compared through the rotation. `snapshot` holds the
// portrait render; the target holds the rotated one.
static int windows_match(const uint8_t* snapshot, PnxTarget* t, uint8_t axis)
{
	int mismatches = 0;
	for (int ay = 0; ay < ROT_H; ay++)
	{
		for (int ax = 0; ax < ROT_W; ax++)
		{
			int fx = 0, fy = 0;
			rot_point(ax, ay, ROT_W, ROT_H, axis, &fx, &fy);
			const uint8_t want = snapshot[(size_t)ay * ROT_W + (size_t)ax];
			const uint8_t got  = px(t, (int16_t)fx, (int16_t)fy);
			if (want != got)
			{
				if (mismatches < 4)
				{
					printf("    author (%d,%d) -> fb (%d,%d): want %02X got %02X\n", ax, ay, fx,
						   fy, want, got);
				}
				mismatches++;
			}
		}
	}
	return mismatches;
}

static void snapshot_window(PnxTarget* t, uint8_t* out)
{
	for (int ay = 0; ay < ROT_H; ay++)
	{
		for (int ax = 0; ax < ROT_W; ax++)
		{
			out[(size_t)ay * ROT_W + (size_t)ax] = px(t, (int16_t)ax, (int16_t)ay);
		}
	}
}

static void test_rotated_fonts(void)
{
	static uint32_t resources[4];
	for (uint32_t i = 0; i < 4; i++)
		resources[i] = i + 600;

	PnxArena persistent, scene;
	pnx_arena_init(&persistent, "rot-persistent", 1024, 4);
	pnx_arena_init(&scene, "rot-scene", 8192, 4);
	pnx_assets_init(&persistent, &scene, resources, 4);

	size_t len	  = 0;
	uint8_t* flat = build_rot_font(&len, PNX_ADVANCE_X_POS);
	install_blob(resources[0], "build/test_font_rot_x.bin", flat, len);

	PnxFont portrait;
	T_CHECK(pnx_font_load(&portrait, 0));
	T_CHECK_EQ(portrait.advance, PNX_ADVANCE_X_POS);

	static uint8_t snapshot[ROT_W * ROT_H];
	PnxTarget* t = pnx_host_target();

	// Both landscape orientations. They are not each other's mirror -- one rotates the
	// content clockwise and the other anticlockwise -- so passing one says nothing about
	// the other, which is exactly why both are here.
	const uint8_t axes[2] = { PNX_ADVANCE_Y_POS, PNX_ADVANCE_Y_NEG };
	for (int i = 0; i < 2; i++)
	{
		const uint8_t axis = axes[i];
		char path[64];
		snprintf(path, sizeof(path), "build/test_font_rot_%u.bin", axis);

		size_t rlen	 = 0;
		uint8_t* rot = build_rot_font(&rlen, axis);
		install_blob(resources[1 + i], path, rot, rlen);

		PnxFont turned;
		T_CHECK(pnx_font_load(&turned, (uint16_t)(1 + i)));
		T_CHECK_EQ(turned.advance, axis);

		// The bitmap is the same glyph on its side, so the stored dimensions swap while the
		// metrics beside them -- advance, both bearings -- do not.
		PnxGlyph pg, rg;
		pnx_font_glyph(&portrait, pnx_font_glyph_index(&portrait, 'A'), &pg);
		pnx_font_glyph(&turned, pnx_font_glyph_index(&turned, 'A'), &rg);
		T_CHECK_EQ(rg.w, pg.h);
		T_CHECK_EQ(rg.h, pg.w);
		T_CHECK_EQ(rg.advance, pg.advance);
		T_CHECK_EQ(rg.bearing_x, pg.bearing_x);
		T_CHECK_EQ(rg.bearing_y, pg.bearing_y);

		const int ax0 = 4, base_ay = 12;
		int fx = 0, fy = 0;
		rot_point(ax0, base_ay, ROT_W, ROT_H, axis, &fx, &fy);

		// --- one line
		pnx_host_reset();
		const int16_t flat_adv = pnx_text_draw(t, &portrait, "AA A", ax0, base_ay, INK);
		snapshot_window(t, snapshot);

		pnx_host_reset();
		const int16_t turned_adv = pnx_text_draw(t, &turned, "AA A", fx, fy, INK);
		T_CHECK_EQ(turned_adv, flat_adv); // a length, never negative
		T_CHECK_EQ(windows_match(snapshot, t, axis), 0);

		// --- wrapped and centred, which is where line stacking and alignment show up. Both
		//     run along the baseline, so both have a direction to get wrong.
		pnx_host_reset();
		const int16_t flat_lines = pnx_text_draw_wrapped(t, &portrait, "AA AA", ax0, base_ay,
														 12, 0, INK, PNX_ALIGN_CENTER);
		snapshot_window(t, snapshot);
		T_CHECK_EQ(flat_lines, 2);

		pnx_host_reset();
		const int16_t turned_lines =
			pnx_text_draw_wrapped(t, &turned, "AA AA", fx, fy, 12, 0, INK, PNX_ALIGN_CENTER);
		T_CHECK_EQ(turned_lines, flat_lines);
		T_CHECK_EQ(windows_match(snapshot, t, axis), 0);

		// --- the box bound cuts the same line off whichever way lines stack
		pnx_host_reset();
		T_CHECK_EQ(pnx_text_draw_wrapped(t, &turned, "AA AA", fx, fy, 12, ROT_LINE_HEIGHT, INK,
										 PNX_ALIGN_LEFT),
				   1);

		free(rot);
	}

	// An axis the format does not define is a refusal, not a font drawn sideways by
	// accident: the byte is new, so a blob from an older pipeline could carry anything.
	{
		PnxFont bad;
		flat[6] = PNX_ADVANCE_COUNT;
		install_blob(resources[3], "build/test_font_rot_bad.bin", flat, len);
		T_CHECK(!pnx_font_load(&bad, 3));
		flat[6] = PNX_ADVANCE_X_POS;
	}

	free(flat);
	pnx_arena_destroy(&scene);
	pnx_arena_destroy(&persistent);
}

void test_text(void)
{
	printf("text\n");

	static uint32_t resources[8];
	for (uint32_t i = 0; i < 8; i++)
		resources[i] = i + 500;

	PnxArena persistent, scene;
	pnx_arena_init(&persistent, "text-persistent", 4096, 4);
	pnx_arena_init(&scene, "text-scene", 16384, 4);
	pnx_assets_init(&persistent, &scene, resources, 8);

	size_t len	  = 0;
	uint8_t* blob = build_font_blob(&len, 1, PNX_BLOB_VERSION);
	install_blob(resources[0], "build/test_font.bin", blob, len);

	PnxFont font;
	T_CHECK(pnx_font_load(&font, 0));

	// --- metrics survive the round trip
	T_CHECK_EQ(font.glyph_count, FONT_GLYPHS);
	T_CHECK_EQ(font.depth, 1);
	T_CHECK_EQ(font.line_height, 8);
	T_CHECK_EQ(font.baseline, 6);
	T_CHECK_EQ(font.space_advance, 2);
	T_CHECK_EQ(font.bitmap_bytes, 5);

	// --- lookup, including the two ways a character can be missing
	T_CHECK_EQ(pnx_font_glyph_index(&font, ' '), 0);
	T_CHECK_EQ(pnx_font_glyph_index(&font, '!'), 1);
	T_CHECK_EQ(pnx_font_glyph_index(&font, '"'), 2);
	T_CHECK_EQ(pnx_font_glyph_index(&font, '#'), FONT_FALLBACK);  // in range, not carried
	T_CHECK_EQ(pnx_font_glyph_index(&font, 'z'), FONT_FALLBACK);  // past last_cp
	T_CHECK_EQ(pnx_font_glyph_index(&font, '\t'), FONT_FALLBACK); // before first_cp

	PnxGlyph g;
	pnx_font_glyph(&font, 0, &g);
	T_CHECK(g.bits == NULL); // a space has no bitmap at all
	T_CHECK_EQ(g.advance, 2);

	pnx_font_glyph(&font, 2, &g);
	T_CHECK(g.bits != NULL);
	T_CHECK_EQ(g.w, 4);
	T_CHECK_EQ(g.h, 2);
	T_CHECK_EQ(g.bearing_x, 1);
	T_CHECK_EQ(g.bearing_y, 5);
	T_CHECK_EQ(g.advance, 6);

	T_CHECK_EQ(pnx_font_row_bytes(&font, 2), 1);
	T_CHECK_EQ(pnx_font_row_bytes(&font, 8), 1);
	T_CHECK_EQ(pnx_font_row_bytes(&font, 9), 2);

	// --- measuring
	T_CHECK_EQ(pnx_text_width(&font, ""), 0);
	T_CHECK_EQ(pnx_text_width(&font, " "), 2);
	T_CHECK_EQ(pnx_text_width(&font, "!"), 3);
	T_CHECK_EQ(pnx_text_width(&font, "!\""), 9);
	T_CHECK_EQ(pnx_text_width(&font, "#"), 3);		   // fallback's advance, not zero
	T_CHECK_EQ(pnx_text_width(&font, "!\n\"\"\""), 3); // stops at the newline

	pnx_host_reset();
	PnxTarget* t = pnx_host_target();

	// --- placement: bearing_y is measured UP from the baseline, bearing_x right from
	// the pen. '!' is 2x3 with bearing (0, 3), so at pen 10 / baseline 20 it occupies
	// x 10..11, y 17..19.
	T_CHECK_EQ(pnx_text_draw(t, &font, "!", 10, 20, INK), 3);
	T_CHECK_EQ(px(t, 10, 17), INK);
	T_CHECK_EQ(px(t, 11, 17), INK);
	T_CHECK_EQ(px(t, 10, 19), INK);
	T_CHECK_EQ(px(t, 11, 19), INK);
	T_CHECK_EQ(px(t, 12, 18), 0);	// one past the right edge
	T_CHECK_EQ(px(t, 9, 18), 0);	// one before the left
	T_CHECK_EQ(px(t, 10, 16), 0);	// one above the top
	T_CHECK_EQ(px(t, 10, 20), 0);	// the baseline row itself is below this glyph
	T_CHECK_EQ(ink_count(t, 0), 6); // 2x3 and nothing else

	// A non-zero x bearing must offset the bitmap without changing the advance.
	pnx_host_reset();
	T_CHECK_EQ(pnx_text_draw(t, &font, "\"", 10, 20, INK), 6);
	T_CHECK_EQ(px(t, 10, 15), 0); // bearing_x = 1, so the pen column stays clear
	T_CHECK_EQ(px(t, 11, 15), INK);
	T_CHECK_EQ(px(t, 14, 15), INK);
	T_CHECK_EQ(px(t, 15, 15), 0);
	T_CHECK_EQ(ink_count(t, 0), 8); // 4x2

	// --- the advance a draw consumes must equal what measuring promised, or every
	// centred label sits slightly wrong.
	pnx_host_reset();
	T_CHECK_EQ(pnx_text_draw(t, &font, "!\" !", 0, 20, INK), pnx_text_width(&font, "!\" !"));

	// A space draws nothing but still advances.
	pnx_host_reset();
	T_CHECK_EQ(pnx_text_draw(t, &font, " ", 10, 20, INK), 2);
	T_CHECK_EQ(ink_count(t, 0), 0);

	// --- clipping at each edge. The glyph is 2x3, so a partial overlap leaves a
	// countable number of pixels rather than all or nothing.
	pnx_host_reset();
	pnx_text_draw(t, &font, "!", -1, 20, INK); // one column off the left
	T_CHECK_EQ(ink_count(t, 0), 3);
	T_CHECK_EQ(px(t, 0, 18), INK);

	pnx_host_reset();
	pnx_text_draw(t, &font, "!", 199, 20, INK); // one column on screen at the right
	T_CHECK_EQ(ink_count(t, 0), 3);
	T_CHECK_EQ(px(t, 199, 18), INK);

	pnx_host_reset();
	pnx_text_draw(t, &font, "!", 10, 2, INK); // baseline 2: rows -1..1, one clipped
	T_CHECK_EQ(ink_count(t, 0), 4);
	T_CHECK_EQ(px(t, 10, 0), INK);
	T_CHECK_EQ(px(t, 10, 1), INK);

	pnx_host_reset();
	pnx_text_draw(t, &font, "!", 10, 229, INK); // rows 226..228, last one off-screen
	T_CHECK_EQ(ink_count(t, 0), 4);
	T_CHECK_EQ(px(t, 10, 227), INK);

	// Entirely off-screen in each direction must draw nothing and not crash.
	pnx_host_reset();
	pnx_text_draw(t, &font, "!", -50, 20, INK);
	pnx_text_draw(t, &font, "!", 400, 20, INK);
	pnx_text_draw(t, &font, "!", 10, -50, INK);
	pnx_text_draw(t, &font, "!", 10, 500, INK);
	T_CHECK_EQ(ink_count(t, 0), 0);

	// --- wrapping. Advances are 3 for '!' and 2 for ' ', so "! ! !" is 3+2+3+2+3 = 13.
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "! ! !", 13), 1); // exactly fits
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "! ! !", 12), 2); // last '!' pushed over
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "! ! !", 8), 2);
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "! ! !", 3), 3); // one glyph per line
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "", 100), 0);

	// An explicit newline breaks regardless of remaining width.
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "!\n!", 100), 2);
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "!\n\n!", 100), 3); // blank line preserved

	// A word wider than the box hard-breaks rather than running out of it. "!!!!" is 12
	// wide; at width 7 that is two glyphs, then two more.
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "!!!!", 7), 2);

	// Even a box narrower than a single glyph must terminate, one character per line.
	T_CHECK_EQ(pnx_text_lines_wrapped(&font, "!!!", 1), 3);

	T_CHECK_EQ(pnx_text_height_wrapped(&font, "! ! !", 8), 16); // 2 lines x 8
	T_CHECK_EQ(pnx_text_height_wrapped(&font, "", 8), 0);

	// --- wrapped drawing places successive baselines one line_height apart
	pnx_host_reset();
	T_CHECK_EQ(pnx_text_draw_wrapped(t, &font, "! !", 10, 20, 3, 0, INK, PNX_ALIGN_LEFT), 2);
	T_CHECK_EQ(px(t, 10, 17), INK);	 // first line, baseline 20
	T_CHECK_EQ(px(t, 10, 25), INK);	 // second, baseline 28
	T_CHECK_EQ(ink_count(t, 0), 12); // two 2x3 glyphs, the space dropped

	// `h` bounds the box: at one line's worth only the first line is drawn.
	pnx_host_reset();
	T_CHECK_EQ(pnx_text_draw_wrapped(t, &font, "! !", 10, 20, 3, 8, INK, PNX_ALIGN_LEFT), 1);
	T_CHECK_EQ(ink_count(t, 0), 6);

	// Centring uses the same width the draw consumes. "!" is 3 wide in a box of 13, so
	// it starts at (13 - 3) / 2 = 5 past x.
	pnx_host_reset();
	pnx_text_draw_wrapped(t, &font, "!", 10, 20, 13, 0, INK, PNX_ALIGN_CENTER);
	T_CHECK_EQ(px(t, 15, 18), INK);
	T_CHECK_EQ(px(t, 10, 18), 0);

	pnx_host_reset();
	pnx_text_draw_wrapped(t, &font, "!", 10, 20, 13, 0, INK, PNX_ALIGN_RIGHT);
	// Alignment works in ADVANCE width, not ink width: the pen lands at 10 + 13 - 3 = 20
	// and the 2px-wide bitmap covers 20..21, leaving the advance's third column clear.
	T_CHECK_EQ(px(t, 19, 18), 0);
	T_CHECK_EQ(px(t, 20, 18), INK);
	T_CHECK_EQ(px(t, 21, 18), INK);
	T_CHECK_EQ(px(t, 22, 18), 0);

	// --- depth 2: coverage levels blend against what is already on screen.
	//
	// The glyph is one row of four pixels at levels 0, 1, 2, 3. Checked against the
	// formula rather than against the table in pnx_text.c, so a typo in the table is a
	// failure here rather than a colour that is merely slightly wrong.
	free(blob);
	blob = build_font_blob(&len, 2, PNX_BLOB_VERSION);
	// Overwrite glyph 1 with a 4x1 run of levels 0..3: at 2bpp that is one byte,
	// 0b00_01_10_11.
	{
		const size_t idx = PNX_BLOB_HEADER_BYTES + 8;
		uint8_t* e		 = blob + idx + PNX_FONT_GLYPH_BYTES; // glyph 1
		e[0]			 = 0;
		e[1]			 = 0; // offset 0
		e[2]			 = 4;
		e[3]			 = 1; // 4x1
		e[4]			 = 5; // advance
		e[5]			 = 0;
		e[6]			 = 1;	 // bearings
		blob[len - 5]	 = 0x1B; // first byte of the bitmap block
	}
	install_blob(resources[1], "build/test_font2.bin", blob, len);

	PnxFont aa;
	T_CHECK(pnx_font_load(&aa, 1));
	T_CHECK_EQ(aa.depth, 2);

	// A destination of 0x55 is ARGB2222 (1,1,1) with alpha 1; ink 0xFF is (3,3,3) opaque.
	pnx_host_reset();
	pnx_gfx_fill_rect(t, 0, 0, 200, 228, 0x55);
	pnx_text_draw(t, &aa, "!", 10, 20, 0xFF);

	const uint8_t dst_ch = 1, ink_ch = 3;
	const uint8_t want1 = (uint8_t)((ink_ch * 1 + dst_ch * 2 + 1) / 3);
	const uint8_t want2 = (uint8_t)((ink_ch * 2 + dst_ch * 1 + 1) / 3);
	const uint8_t px1	= (uint8_t)(0xC0 | (want1 << 4) | (want1 << 2) | want1);
	const uint8_t px2	= (uint8_t)(0xC0 | (want2 << 4) | (want2 << 2) | want2);

	T_CHECK_EQ(px(t, 10, 19), 0x55); // level 0: untouched
	T_CHECK_EQ(px(t, 11, 19), px1);	 // level 1: one third ink
	T_CHECK_EQ(px(t, 12, 19), px2);	 // level 2: two thirds
	T_CHECK_EQ(px(t, 13, 19), 0xFF); // level 3: straight ink, no read

	// --- the loader must refuse anything it cannot trust, because the blitter does no
	// checking of its own.
	free(blob);
	blob = build_font_blob(&len, 1, PNX_BLOB_VERSION);
	PnxFont bad;

	install_blob(resources[2], "build/test_font_short.bin", blob, len - 3);
	T_CHECK(!pnx_font_load(&bad, 2)); // truncated: sizes disagree

	blob[2] = PNX_BLOB_VERSION - 1;
	install_blob(resources[3], "build/test_font_old.bin", blob, len);
	T_CHECK(!pnx_font_load(&bad, 3)); // a stale build
	blob[2] = PNX_BLOB_VERSION;

	blob[0] = 'P';
	blob[1] = 'A';
	install_blob(resources[4], "build/test_font_magic.bin", blob, len);
	T_CHECK(!pnx_font_load(&bad, 4)); // an atlas is not a font
	blob[0] = 'P';
	blob[1] = 'F';

	blob[3] = 3;
	install_blob(resources[5], "build/test_font_depth.bin", blob, len);
	T_CHECK(!pnx_font_load(&bad, 5)); // only 1 and 2 exist
	blob[3] = 1;

	// A bitmap offset past the block would have the blitter reading arena memory as
	// pixels -- the failure this validation exists to prevent.
	{
		uint8_t* e			= blob + PNX_BLOB_HEADER_BYTES + 8 + PNX_FONT_GLYPH_BYTES;
		const uint8_t saved = e[0];
		e[0]				= 200;
		install_blob(resources[6], "build/test_font_oob.bin", blob, len);
		T_CHECK(!pnx_font_load(&bad, 6));
		e[0] = saved;
	}

	// A codepoint mapping to a glyph that does not exist, likewise.
	{
		uint8_t* map		= blob + PNX_BLOB_HEADER_BYTES + 8 + FONT_GLYPHS * PNX_FONT_GLYPH_BYTES;
		const uint8_t saved = map[0];
		map[0]				= 9;
		install_blob(resources[7], "build/test_font_badmap.bin", blob, len);
		T_CHECK(!pnx_font_load(&bad, 7));
		map[0] = saved;
	}

	free(blob);
	pnx_arena_destroy(&scene);
	pnx_arena_destroy(&persistent);

	test_rotated_fonts();
}
