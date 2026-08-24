#include "pnx_gfx.h"

#include <string.h>

// -------------------------------------------------------------------- 1-bit output
//
// A 1-bit target (PNX_DISPLAY_BW, pnx_platform.h) never sees a GColor8 byte written into
// it -- the framebuffer packs 8 pixels per byte, and a pixel is either ink (black, bit
// clear) or paper (white, bit set), per the SDK's own "0 = black, 1 = white". Bit order is
// LSB-first (bit x&7 of byte x>>3), read off the classic Pebble "Buffers and Bitmaps"
// guide -- unconfirmed on real flint/diorite/aplite hardware, same caveat PORTING.md
// already carries for the tag resolution these platforms answer to.
//
// Two write sites need this, not one: art (span_2bpp_packed, below) decides ink/paper/
// dither per pixel from a ~bw resource's own bytes and skips transparent pixels; a solid
// fill (pnx_gfx_fill_rect, pnx_gfx_clear) decides ink/paper ONCE for the whole colour and
// writes every pixel, transparency never entering into it.
//
// A build is exclusively 2bpp here, not a choice between two art formats: the ordinary
// 4bpp-indexed decoder this file also has (span_4bpp) never runs under PNX_DISPLAY_BW, and
// there used to be a THIRD path here -- reading 4bpp-indexed source through a palette's
// ink mask -- for the one case that could not be packed 2bpp: a sprite recoloured by
// `variants`, where baking ink/paper into the bytes and sharing one bitmap across
// recolours cannot both be true. That case is resolved differently now (a per-sprite
// canonical variant, tools/pnx_assets.py), which is what let this collapse to one format
// engine-wide instead of two coexisting at once.

#if PNX_DISPLAY_BW

// The same R*3 + G*6 + B*1 weighting tools/pnx_assets.py's pack_unit_2bpp uses to bake
// ink/paper into art at build time, applied here to a raw colour argument instead --
// fill_rect/clear and pnx_text.c never index a palette at all, so a call here is the only
// place either of them decides ink vs paper. Exposed (pnx_gfx.h) for pnx_text.c.
bool pnx_bw_is_ink(uint8_t c)
{
	const uint8_t r = (c >> 4) & 3, g = (c >> 2) & 3, b = c & 3;
	return (3 * r + 6 * g + b) < 15; // half of 30
}

// Exposed for the same reason: pnx_text.c's own glyph span writers set the same bit this
// module does, just from a coverage level instead of a palette index.
void pnx_bw_set_pixel(uint8_t* data, int32_t x, bool ink)
{
	if (ink)
		data[x >> 3] &= (uint8_t)~(1u << (x & 7));
	else
		data[x >> 3] |= (uint8_t)(1u << (x & 7));
}

// Fills [x0, x1] inclusive with a single ink/paper value -- pnx_gfx_fill_rect and
// pnx_gfx_clear's 1-bit path. Byte-at-a-time once the run is byte-aligned, since a dialog
// box or a full-screen clear is exactly the wide, flat fill that is worth it for.
static void bw_fill_span(uint8_t* data, int32_t x0, int32_t x1, bool ink)
{
	if (x1 < x0)
		return;
	const uint8_t fill = ink ? 0x00 : 0xFF;

	while (x0 <= x1 && (x0 & 7))
	{
		pnx_bw_set_pixel(data, x0, ink);
		x0++;
	}
	while (x0 + 7 <= x1)
	{
		data[x0 >> 3] = fill;
		x0 += 8;
	}
	while (x0 <= x1)
	{
		pnx_bw_set_pixel(data, x0, ink);
		x0++;
	}
}

// Reads a ~bw resource directly (docs/PORTING.md; pack_unit_2bpp, tools/pnx_assets.py):
// 2 bits/pixel, MSB first, 0 transparent / 1 paper / 2 ink / 3 dither. No palette lookup
// at all: ink/paper/dither was decided at build time, so the byte itself is the answer.
//
// `dither_y` is the absolute screen row: pack_unit_2bpp does not store the checkerboard
// pattern, it is read back from screen position at blit time, the same reasoning that
// keeps a metatile's palette lookup out of the packed bytes.
static void span_2bpp_packed(uint8_t* row_base, int32_t x, const uint8_t* line, int32_t w,
							 int16_t min_x, int16_t max_x, int32_t dither_y)
{
	int32_t i0 = 0, i1 = w;
	if (x + i0 < min_x)
		i0 = min_x - x;
	if (x + i1 > max_x + 1)
		i1 = max_x + 1 - x;

	for (int32_t i = i0; i < i1; i++)
	{
		const uint8_t byte	= line[i >> 2];
		const uint8_t state = (uint8_t)((byte >> (6 - 2 * (i & 3))) & 3u);
		if (state == 0)
			continue; // transparent
		const bool ink = (state == 3) ? (((x + i + dither_y) & 1) == 0) : (state == 2);
		pnx_bw_set_pixel(row_base, x + i, ink);
	}
}

#endif // PNX_DISPLAY_BW

// ------------------------------------------------------------------------ camera

void pnx_camera_init(PnxCamera* c, int16_t view_w, int16_t view_h)
{
	c->x = c->y = 0;
	c->view_w	= view_w;
	c->view_h	= view_h;
}

void pnx_camera_center(PnxCamera* c, int32_t wx, int32_t wy, int32_t world_w, int32_t world_h)
{
	c->x = wx - c->view_w / 2;
	c->y = wy - c->view_h / 2;

	// A world smaller than the view would otherwise clamp to a negative maximum and
	// scroll backwards; pinning at 0 leaves it centred on the origin instead.
	const int32_t max_x = world_w > c->view_w ? world_w - c->view_w : 0;
	const int32_t max_y = world_h > c->view_h ? world_h - c->view_h : 0;
	c->x				= pnx_clamp_i32(c->x, 0, max_x);
	c->y				= pnx_clamp_i32(c->y, 0, max_y);
}

// ------------------------------------------------------------------------- blits

void pnx_gfx_clear(PnxTarget* t, uint8_t colour)
{
	const int16_t h = pnx_target_height(t);
#if PNX_DISPLAY_BW
	const bool ink = pnx_bw_is_ink(colour);
#endif
	for (int16_t y = 0; y < h; y++)
	{
		PnxRow row = pnx_target_row(t, y);
		if (!row.data)
			continue;
#if PNX_DISPLAY_BW
		bw_fill_span(row.data, row.min_x, row.max_x, ink);
#else
		memset(row.data + row.min_x, colour, (size_t)(row.max_x - row.min_x) + 1);
#endif
	}
}

void pnx_gfx_fill_rect(PnxTarget* t, int32_t x, int32_t y, int16_t w, int16_t h, uint8_t colour)
{
	const int16_t th = pnx_target_height(t);
#if PNX_DISPLAY_BW
	const bool ink = pnx_bw_is_ink(colour);
#endif
	for (int32_t j = 0; j < h; j++)
	{
		const int32_t py = y + j;
		if (py < 0 || py >= th)
			continue;

		PnxRow row = pnx_target_row(t, (int16_t)py);
		if (!row.data)
			continue;

		int32_t x0 = x < row.min_x ? row.min_x : x;
		int32_t x1 = x + w - 1;
		if (x1 > row.max_x)
			x1 = row.max_x;
		if (x1 < x0)
			continue;

#if PNX_DISPLAY_BW
		bw_fill_span(row.data, x0, x1, ink);
#else
		memset(row.data + x0, colour, (size_t)(x1 - x0) + 1);
#endif
	}
}

void pnx_gfx_fill_rect_dither(PnxTarget* t, int32_t x, int32_t y, int16_t w, int16_t h,
							  uint8_t colour_a, uint8_t colour_b)
{
	const int16_t th = pnx_target_height(t);
#if PNX_DISPLAY_BW
	const bool ink_a = pnx_bw_is_ink(colour_a);
	const bool ink_b = pnx_bw_is_ink(colour_b);
#endif
	for (int32_t j = 0; j < h; j++)
	{
		const int32_t py = y + j;
		if (py < 0 || py >= th)
			continue;

		PnxRow row = pnx_target_row(t, (int16_t)py);
		if (!row.data)
			continue;

		int32_t x0 = x < row.min_x ? row.min_x : x;
		int32_t x1 = x + w - 1;
		if (x1 > row.max_x)
			x1 = row.max_x;
		if (x1 < x0)
			continue;

		for (int32_t px = x0; px <= x1; px++)
		{
			const bool b = ((px + py) & 1) != 0;
#if PNX_DISPLAY_BW
			pnx_bw_set_pixel(row.data, px, b ? ink_b : ink_a);
#else
			row.data[px] = b ? colour_b : colour_a;
#endif
		}
	}
}

// One horizontal run of 4bpp pixels into a row, clipped to [min_x, max_x].
//
// Reads each source byte once and unpacks both nibbles. The odd-pixel prologue exists
// because clipping can start the run on a low nibble, where the pair loop cannot begin --
// and, separately, because `nbase` itself can already be odd: tools/pnx_assets.py's
// pack_sprite packs a frame as "two pixels to a byte, with no per-row padding" and only
// requires the frame's total pixel count (w*h) to be even, not its width. A frame whose
// OWN width is odd (legal, and what a landscape rotation produces from a source frame
// whose height was odd -- see pnx_sprite_load's own comment) still starts every other row
// on the low nibble of its first byte, so the parity that matters is `nbase + i`'s, not a
// bare `i` counted from this row's own start.
//
// Colour-target only: a 1-bit build never calls this and never links it in, since a
// build compiles the 4bpp path or the 1-bit one, never both (docs/PORTING.md, "the 1-bit
// path and the 4bpp path never coexist").
#if !PNX_DISPLAY_BW
static void span_4bpp(uint8_t* row_base, int32_t x, const uint8_t* src, int32_t nbase,
					  const uint8_t* pal, int32_t w, int16_t min_x, int16_t max_x)
{
	int32_t i0 = 0, i1 = w;
	if (x + i0 < min_x)
		i0 = min_x - x;
	if (x + i1 > max_x + 1)
		i1 = max_x + 1 - x;
	if (i1 <= i0)
		return;

	uint8_t* dst = row_base + x;
	int32_t i	 = i0;

	if ((nbase + i) & 1)
	{
		const uint8_t v = src[(nbase + i) >> 1] & 0x0F;
		if (v != PNX_PALETTE_TRANSPARENT)
			dst[i] = pal[v];
		i++;
	}
	for (; i + 1 < i1; i += 2)
	{
		const uint8_t packed = src[(nbase + i) >> 1];
		const uint8_t hi	 = (uint8_t)(packed >> 4);
		const uint8_t lo	 = (uint8_t)(packed & 0x0F);
		if (hi != PNX_PALETTE_TRANSPARENT)
			dst[i] = pal[hi];
		if (lo != PNX_PALETTE_TRANSPARENT)
			dst[i + 1] = pal[lo];
	}
	if (i < i1)
	{
		const uint8_t v = (uint8_t)(src[(nbase + i) >> 1] >> 4);
		if (v != PNX_PALETTE_TRANSPARENT)
			dst[i] = pal[v];
	}
}
#endif // !PNX_DISPLAY_BW

// pnx_blit_4bpp_region's per-pixel writer: `col0 + i` is the SOURCE column (offset into a
// row that does not start at 0, unlike span_4bpp's), `x + i` is the destination column.
// Kept separate from span_4bpp rather than generalised into it -- that function is the
// per-frame hot path (every sprite, every tile) and stays exactly as measured; this one
// is not.
#if !PNX_DISPLAY_BW
static void span_4bpp_at(uint8_t* row_base, int32_t x, const uint8_t* line, const uint8_t* pal,
						 int32_t col0, int32_t i0, int32_t i1)
{
	for (int32_t i = i0; i < i1; i++)
	{
		const int32_t col	 = col0 + i;
		const uint8_t packed = line[col >> 1];
		const uint8_t v		 = (col & 1) ? (uint8_t)(packed & 0x0F) : (uint8_t)(packed >> 4);
		if (v != PNX_PALETTE_TRANSPARENT)
			row_base[x + i] = pal[v];
	}
}
#endif // !PNX_DISPLAY_BW

// Same idea as span_4bpp_at, for a ~bw packed source: dithering is still keyed off the
// SCREEN position (x + i, per span_2bpp_packed's own comment), only the bit lookup moves.
#if PNX_DISPLAY_BW
static void span_2bpp_packed_at(uint8_t* row_base, int32_t x, const uint8_t* line, int32_t col0,
								int32_t i0, int32_t i1, int32_t dither_y)
{
	for (int32_t i = i0; i < i1; i++)
	{
		const int32_t col	= col0 + i;
		const uint8_t byte	= line[col >> 2];
		const uint8_t state = (uint8_t)((byte >> (6 - 2 * (col & 3))) & 3u);
		if (state == 0)
			continue;
		const bool ink = (state == 3) ? (((x + i + dither_y) & 1) == 0) : (state == 2);
		pnx_bw_set_pixel(row_base, x + i, ink);
	}
}
#endif // PNX_DISPLAY_BW

void pnx_blit_4bpp_region(PnxTarget* t, const uint8_t* src, int16_t src_w,
						  const PnxPalette* palette, int32_t x, int32_t y, int16_t sx,
						  int16_t sy, int16_t sw, int16_t sh)
{
#if PNX_DISPLAY_BW
	if (!src)
		return;
#else
	if (!src || !palette)
		return;
#endif

	const int16_t th = pnx_target_height(t);
#if PNX_DISPLAY_BW
	const int32_t stride = src_w / 4;
#else
	const int32_t stride = src_w / 2;
#endif

	int32_t j0 = 0, j1 = sh;
	if (y < 0)
		j0 = -y;
	if (y + sh > th)
		j1 = th - y;

	for (int32_t j = j0; j < j1; j++)
	{
		PnxRow row = pnx_target_row(t, (int16_t)(y + j));
		if (!row.data)
			continue;

		int32_t i0 = 0, i1 = sw;
		if (x + i0 < row.min_x)
			i0 = row.min_x - x;
		if (x + i1 > row.max_x + 1)
			i1 = row.max_x + 1 - x;
		if (i1 <= i0)
			continue;

		const uint8_t* line = src + (sy + j) * stride;
#if PNX_DISPLAY_BW
		span_2bpp_packed_at(row.data, x, line, sx, i0, i1, y + j);
#else
		span_4bpp_at(row.data, x, line, palette->entries, sx, i0, i1);
#endif
	}
}

#if !PNX_DISPLAY_BW
void pnx_blit_4bpp_scaled(PnxTarget* t, const uint8_t* src, const PnxPalette* palette,
						  int32_t x, int32_t y, int16_t src_w, int16_t src_h, int16_t dst_w,
						  int16_t dst_h)
{
	if (!src || !palette || src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0)
		return;

	if (dst_w == src_w && dst_h == src_h)
	{
		pnx_blit_4bpp(t, src, palette, x, y, dst_w, dst_h, PNX_FLIP_NONE);
		return;
	}

	const int16_t th = pnx_target_height(t);

	// 16.16 fixed point. Plain int32_t here (not int64_t): src_h/src_w would need to pass
	// 32767 before `<< 16` overflows int32_t, far past any sprite this display's own
	// resource budget could hold -- and a division, unlike the `>> 16` multiplies below,
	// costs real bytes (__aeabi_ldivmod pulled out of libgcc) the moment ANY caller
	// anywhere in the link reaches it, on every platform, not just the ones tight on
	// space. The multiplies below stay widened: j/i can run up to a screen dimension and
	// row_step/col_step can itself be large at extreme scale ratios, so THAT product can
	// realistically exceed int32_t -- but `>> 16` is a shift, not a division, so it never
	// carries this same cost.
	const int32_t row_step = (int32_t)((src_h << 16) / dst_h);
	const int32_t col_step = (int32_t)((src_w << 16) / dst_w);

	int32_t j0 = 0, j1 = dst_h;
	if (y < 0)
		j0 = -y;
	if (y + dst_h > th)
		j1 = th - y;

	for (int32_t j = j0; j < j1; j++)
	{
		PnxRow row = pnx_target_row(t, (int16_t)(y + j));
		if (!row.data)
			continue;

		const int32_t sy	= (int32_t)(((int64_t)j * row_step) >> 16);
		const int32_t nbase = sy * src_w; // flat nibble-stream row start, not a byte stride

		if (dst_w == src_w)
		{
			// Height-only scaling (the one shape that actually reaches hardware today):
			// span_4bpp already does its own horizontal clip, so no i0/i1 here.
			span_4bpp(row.data, x, src, nbase, palette->entries, dst_w, row.min_x, row.max_x);
			continue;
		}

		int32_t i0 = 0, i1 = dst_w;
		if (x + i0 < row.min_x)
			i0 = row.min_x - x;
		if (x + i1 > row.max_x + 1)
			i1 = row.max_x + 1 - x;
		if (i1 <= i0)
			continue;

		// General 2-axis case: per-pixel column accumulator, same no-lockstep shape
		// span_4bpp_at already uses, with a scaled `col` instead of a literal col0+i.
		for (int32_t i = i0; i < i1; i++)
		{
			const int32_t col	 = (int32_t)(((int64_t)i * col_step) >> 16);
			const int32_t n		 = nbase + col;
			const uint8_t packed = src[n >> 1];
			const uint8_t v		 = (n & 1) ? (uint8_t)(packed & 0x0F) : (uint8_t)(packed >> 4);
			if (v != PNX_PALETTE_TRANSPARENT)
				row.data[x + i] = palette->entries[v];
		}
	}
}
#endif // !PNX_DISPLAY_BW

void pnx_blit_metatile(PnxTarget* t, const PnxAtlas* atlas, uint8_t tile, int32_t x, int32_t y)
{
	pnx_blit_metatile_with(t, atlas, tile, pnx_atlas_tile_palette(atlas, tile), x, y);
}

void pnx_blit_metatile_with(PnxTarget* t, const PnxAtlas* atlas, uint8_t tile,
							const PnxPalette* palette, int32_t x, int32_t y)
{
	// `palette` is meaningless on a 1-bit build -- a ~bw quadrant's ink/paper/dither is
	// baked into its bytes, nothing looks a colour up any more -- so the parameter is kept
	// only for one call shape across both builds; on BW it is never read past this line.
#if PNX_DISPLAY_BW
	if (!atlas->metatiles)
		return;
#else
	if (!palette || !atlas->metatiles)
		return;
#endif

	const uint16_t* quads = &atlas->metatiles[(uint32_t)tile * 4];
	const int32_t T		  = atlas->tile_px;
	const int32_t half	  = T / 2;
#if PNX_DISPLAY_BW
	const int32_t sub_stride = half / 4; // bytes per quadrant row, ~bw packed 2bpp
#else
	const int32_t sub_stride = half / 2; // bytes per quadrant row at 4bpp
#endif
	const int16_t th = pnx_target_height(t);

	int32_t j0 = 0, j1 = T;
	if (y < 0)
		j0 = -y;
	if (y + T > th)
		j1 = th - y;

	for (int32_t j = j0; j < j1; j++)
	{
		PnxRow row = pnx_target_row(t, (int16_t)(y + j));
		if (!row.data)
			continue;

		// Top half reads quadrants 0/1, bottom half 2/3, each at its own row within them.
		const uint16_t* pair = quads + ((j < half) ? 0 : 2);
		const int32_t qj	 = (j < half) ? j : j - half;

		for (int32_t k = 0; k < 2; k++)
		{
			const uint8_t* line =
				atlas->pixels + (uint32_t)pair[k] * atlas->sub_bytes + qj * sub_stride;
#if PNX_DISPLAY_BW
			span_2bpp_packed(row.data, x + k * half, line, half, row.min_x, row.max_x, y + j);
#else
			// nbase=0: `line` already points at this row's own start (subtile rows are
			// individually addressed, unlike a sprite frame's flat stream), so the parity
			// span_4bpp checks against nbase+i reduces to plain i, same as before.
			span_4bpp(row.data, x + k * half, line, 0, palette->entries, half, row.min_x,
					  row.max_x);
#endif
		}
	}
}

void pnx_blit_4bpp(PnxTarget* t, const uint8_t* src, const PnxPalette* palette, int32_t x,
				   int32_t y, int16_t w, int16_t h, uint8_t flip)
{
	// `palette` is meaningless on a 1-bit build -- see pnx_blit_metatile_with's own
	// comment -- so the precondition drops it there rather than refusing every call.
#if PNX_DISPLAY_BW
	if (!src)
		return;
#else
	if (!src || !palette)
		return;
#endif

	const int16_t th = pnx_target_height(t);
	// 4 pixels/byte, ~bw packed 2bpp, on a 1-bit build; 2 pixels/byte, 4bpp indexed,
	// otherwise -- unconditional, not a per-call question any more (see atlas_load_into's
	// own comment in pnx_assets.c for why there is no longer a per-blob format to answer).
#if PNX_DISPLAY_BW
	const int32_t stride = w / 4;
#else
	const int32_t stride = w / 2;
#endif

	// Vertical clip up front, so the row loop never runs for invisible rows.
	int32_t j0 = 0, j1 = h;
	if (y < 0)
		j0 = -y;
	if (y + h > th)
		j1 = th - y;

	const bool rotate = (flip & PNX_FLIP_ROTATE) != 0;

	for (int32_t j = j0; j < j1; j++)
	{
		PnxRow row = pnx_target_row(t, (int16_t)(y + j));
		if (!row.data)
			continue;

		// Horizontal clip likewise, against the row's own reported span rather than the
		// target width -- they differ on some displays.
		int32_t i0 = 0, i1 = w;
		if (x + i0 < row.min_x)
			i0 = row.min_x - x;
		if (x + i1 > row.max_x + 1)
			i1 = row.max_x + 1 - x;
		if (i1 <= i0)
			continue;

#if !PNX_DISPLAY_BW
		const uint8_t* pal = palette->entries;
		uint8_t* dst	   = row.data + x;
#endif

		if (rotate)
		{
			// Transpose: swap source row/column before flip_x/flip_y are applied to the
			// result (see PNX_MAP_ROTATE's own comment for why this ordering is what
			// spans all 8 symmetries with three independent bits). Pre-flip, a rotated
			// destination row reads source COLUMN j -- constant across the row, so it is
			// still hoisted here -- but source ROW i, which is the thing that varies
			// PIXEL BY PIXEL as the row is walked. That is what costs the fast path: this
			// branch is always per-pixel, never a span copy, regardless of flip_x/flip_y.
			const int32_t sx_col = (flip & PNX_FLIP_X) ? (w - 1 - j) : j;
			for (int32_t i = i0; i < i1; i++)
			{
				const int32_t sy_row = (flip & PNX_FLIP_Y) ? (h - 1 - i) : i;
				const uint8_t* line	 = src + sy_row * stride;
#if PNX_DISPLAY_BW
				const uint8_t byte	= line[sx_col >> 2];
				const uint8_t state = (uint8_t)((byte >> (6 - 2 * (sx_col & 3))) & 3u);
				if (state == 0)
					continue;
				const bool ink = (state == 3) ? (((x + i + y + j) & 1) == 0) : (state == 2);
				pnx_bw_set_pixel(row.data, x + i, ink);
#else
				const uint8_t packed = line[sx_col >> 1];
				const uint8_t v =
					(sx_col & 1) ? (uint8_t)(packed & 0x0F) : (uint8_t)(packed >> 4);
				if (v != PNX_PALETTE_TRANSPARENT)
					dst[i] = pal[v];
#endif
			}
			continue;
		}

		// Flip Y is free: read the source row from the other end. No second span writer, no
		// per-pixel cost -- only this index changes.
		const int32_t sj = (flip & PNX_FLIP_Y) ? (h - 1 - j) : j;
#if PNX_DISPLAY_BW
		const uint8_t* line = src + sj * stride;
#else
		// span_4bpp's own row start, in the frame's flat nibble stream rather than a
		// byte offset -- a byte-aligned `line` pointer cannot stand in for it once w is
		// odd (see that function's own comment).
		const int32_t nbase = sj * w;
#endif

		// Two paths rather than a `mirror ?` per pixel. The forward one is the shared
		// span; mirrored cannot use it because destination and source walk opposite ways.
		if (!(flip & PNX_FLIP_X))
		{
#if PNX_DISPLAY_BW
			span_2bpp_packed(row.data, x, line, w, row.min_x, row.max_x, y + j);
#else
			span_4bpp(row.data, x, src, nbase, pal, w, row.min_x, row.max_x);
#endif
		}
		else
		{
			// Mirrored: destination walks forward while the source walks back, so the pairing
			// does not line up and each pixel is addressed individually.
			for (int32_t i = i0; i < i1; i++)
			{
				const int32_t si = w - 1 - i;
#if PNX_DISPLAY_BW
				const uint8_t byte	= line[si >> 2];
				const uint8_t state = (uint8_t)((byte >> (6 - 2 * (si & 3))) & 3u);
				if (state == 0)
					continue;
				const bool ink = (state == 3) ? (((x + i + y + j) & 1) == 0) : (state == 2);
				pnx_bw_set_pixel(row.data, x + i, ink);
#else
				// Same flat-nibble-stream addressing as span_4bpp, for the same reason:
				// `si` is a column within this row, not a byte offset, once w is odd.
				const int32_t n		 = nbase + si;
				const uint8_t packed = src[n >> 1];
				const uint8_t v		 = (n & 1) ? (uint8_t)(packed & 0x0F) : (uint8_t)(packed >> 4);
				if (v != PNX_PALETTE_TRANSPARENT)
					dst[i] = pal[v];
#endif
			}
		}
	}
}
