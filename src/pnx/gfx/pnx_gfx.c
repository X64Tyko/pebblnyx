#include "pnx_gfx.h"

#include <string.h>

// ------------------------------------------------------------------------ camera

void pnx_camera_init(PnxCamera* c, int16_t view_w, int16_t view_h)
{
	c->x = c->y = 0;
	c->view_w = view_w;
	c->view_h = view_h;
}

void pnx_camera_center(PnxCamera* c, int32_t wx, int32_t wy, int32_t world_w, int32_t world_h)
{
	c->x = wx - c->view_w / 2;
	c->y = wy - c->view_h / 2;

	// A world smaller than the view would otherwise clamp to a negative maximum and
	// scroll backwards; pinning at 0 leaves it centred on the origin instead.
	const int32_t max_x = world_w > c->view_w ? world_w - c->view_w : 0;
	const int32_t max_y = world_h > c->view_h ? world_h - c->view_h : 0;
	c->x = pnx_clamp_i32(c->x, 0, max_x);
	c->y = pnx_clamp_i32(c->y, 0, max_y);
}

// ------------------------------------------------------------------------- blits

void pnx_gfx_clear(PnxTarget* t, uint8_t colour)
{
	const int16_t h = pnx_target_height(t);
	for (int16_t y = 0; y < h; y++)
	{
		PnxRow row = pnx_target_row(t, y);
		if (!row.data)
			continue;
		memset(row.data + row.min_x, colour, (size_t)(row.max_x - row.min_x) + 1);
	}
}

void pnx_gfx_fill_rect(PnxTarget* t, int32_t x, int32_t y, int16_t w, int16_t h, uint8_t colour)
{
	const int16_t th = pnx_target_height(t);
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

		memset(row.data + x0, colour, (size_t)(x1 - x0) + 1);
	}
}

// One horizontal run of 4bpp pixels into a row, clipped to [min_x, max_x].
//
// Reads each source byte once and unpacks both nibbles. The odd-pixel prologue exists
// because clipping can start the run on a low nibble, where the pair loop cannot begin.
static void span_4bpp(uint8_t* row_base, int32_t x, const uint8_t* line, const uint8_t* pal,
					  int32_t w, int16_t min_x, int16_t max_x)
{
	int32_t i0 = 0, i1 = w;
	if (x + i0 < min_x)
		i0 = min_x - x;
	if (x + i1 > max_x + 1)
		i1 = max_x + 1 - x;
	if (i1 <= i0)
		return;

	uint8_t* dst = row_base + x;
	int32_t i = i0;

	if (i & 1)
	{
		const uint8_t v = line[i >> 1] & 0x0F;
		if (v != PNX_PALETTE_TRANSPARENT)
			dst[i] = pal[v];
		i++;
	}
	for (; i + 1 < i1; i += 2)
	{
		const uint8_t packed = line[i >> 1];
		const uint8_t hi = (uint8_t)(packed >> 4);
		const uint8_t lo = (uint8_t)(packed & 0x0F);
		if (hi != PNX_PALETTE_TRANSPARENT)
			dst[i] = pal[hi];
		if (lo != PNX_PALETTE_TRANSPARENT)
			dst[i + 1] = pal[lo];
	}
	if (i < i1)
	{
		const uint8_t v = (uint8_t)(line[i >> 1] >> 4);
		if (v != PNX_PALETTE_TRANSPARENT)
			dst[i] = pal[v];
	}
}

void pnx_blit_metatile(PnxTarget* t, const PnxAtlas* atlas, uint8_t tile, int32_t x, int32_t y)
{
	pnx_blit_metatile_with(t, atlas, tile, pnx_atlas_tile_palette(atlas, tile), x, y);
}

void pnx_blit_metatile_with(PnxTarget* t, const PnxAtlas* atlas, uint8_t tile,
							const PnxPalette* palette, int32_t x, int32_t y)
{
	if (!palette || !atlas->metatiles)
		return;

	const uint16_t* quads = &atlas->metatiles[(uint32_t)tile * 4];
	const int32_t T = atlas->tile_px;
	const int32_t half = T / 2;
	const int32_t sub_stride = half / 2;  // bytes per quadrant row at 4bpp
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
		const int32_t qj = (j < half) ? j : j - half;

		for (int32_t k = 0; k < 2; k++)
		{
			const uint8_t* line =
				atlas->pixels + (uint32_t)pair[k] * atlas->sub_bytes + qj * sub_stride;
			span_4bpp(row.data, x + k * half, line, palette->entries, half, row.min_x,
					  row.max_x);
		}
	}
}

void pnx_blit_4bpp(PnxTarget* t, const uint8_t* src, const PnxPalette* palette, int32_t x,
				   int32_t y, int16_t w, int16_t h, uint8_t flip)
{
	if (!src || !palette)
		return;

	const int16_t th = pnx_target_height(t);
	const int32_t stride = w / 2;  // 4bpp: two pixels per byte

	// Vertical clip up front, so the row loop never runs for invisible rows.
	int32_t j0 = 0, j1 = h;
	if (y < 0)
		j0 = -y;
	if (y + h > th)
		j1 = th - y;

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

		// Flip Y is free: read the source row from the other end. No second span writer, no
		// per-pixel cost -- only this index changes.
		const int32_t sj = (flip & PNX_FLIP_Y) ? (h - 1 - j) : j;
		const uint8_t* line = src + sj * stride;
		const uint8_t* pal = palette->entries;
		uint8_t* dst = row.data + x;

		// Two paths rather than a `mirror ?` per pixel. The forward one is the shared
		// span; mirrored cannot use it because destination and source walk opposite ways.
		if (!(flip & PNX_FLIP_X))
		{
			span_4bpp(row.data, x, line, pal, w, row.min_x, row.max_x);
		}
		else
		{
			// Mirrored: destination walks forward while the source walks back, so the pairing
			// does not line up and each pixel is addressed individually.
			for (int32_t i = i0; i < i1; i++)
			{
				const int32_t si = w - 1 - i;
				const uint8_t packed = line[si >> 1];
				const uint8_t v = (si & 1) ? (uint8_t)(packed & 0x0F) : (uint8_t)(packed >> 4);
				if (v != PNX_PALETTE_TRANSPARENT)
					dst[i] = pal[v];
			}
		}
	}
}
