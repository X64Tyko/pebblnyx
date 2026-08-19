#include "pnx_nineslice.h"

#if PNX_USE_NINESLICE

// Repeats an `rw` x `rh` source window, itself read via pnx_blit_4bpp_region, to fill a
// `dst_w` x `dst_h` destination area starting at `(x, y)`. The last tile in each axis is
// handed a smaller region than a full repeat rather than the whole thing, so the fill
// stops exactly at the destination edge -- nothing else in this engine has a scissor rect
// to fall back on, so getting this right here is what keeps a panel's edge tiling from
// spilling onto whatever the panel was drawn over.
//
// A corner is the degenerate case where `rw == dst_w` and `rh == dst_h`: the loop below
// runs exactly once, with no special-casing needed to tell a corner from a tiled edge.
static void blit_tiled(PnxTarget* t, const uint8_t* src, int16_t src_w, const PnxPalette* pal,
					   int32_t x, int32_t y, int16_t sx, int16_t sy, int16_t rw, int16_t rh,
					   int16_t dst_w, int16_t dst_h)
{
	if (rw <= 0 || rh <= 0)
		return;

	for (int32_t oy = 0; oy < dst_h; oy += rh)
	{
		const int16_t th = (int16_t)((dst_h - oy < rh) ? (dst_h - oy) : rh);
		for (int32_t ox = 0; ox < dst_w; ox += rw)
		{
			const int16_t tw = (int16_t)((dst_w - ox < rw) ? (dst_w - ox) : rw);
			pnx_blit_4bpp_region(t, src, src_w, pal, x + ox, y + oy, sx, sy, tw, th);
		}
	}
}

void pnx_gfx_draw_nine_slice(PnxTarget* t, const PnxNineSlice* ns, const PnxPalette* palette,
							 int32_t x, int32_t y, int16_t w, int16_t h)
{
	if (!ns || !ns->pixels || w <= 0 || h <= 0)
		return;

	uint8_t bl = ns->border_l, bt = ns->border_t, br = ns->border_r, bb = ns->border_b;

	// A box narrower/shorter than its own borders: split what is there evenly rather than
	// draw past the box or double the corners over each other. pnx_nineslice_load already
	// guarantees border_l+border_r <= ns->w (and the vertical pair likewise), so the
	// smaller values this produces never read past the source image either.
	if ((int16_t)(bl + br) > w)
	{
		bl = (uint8_t)(w / 2);
		br = (uint8_t)(w - bl);
	}
	if ((int16_t)(bt + bb) > h)
	{
		bt = (uint8_t)(h / 2);
		bb = (uint8_t)(h - bt);
	}

	const int16_t cw	 = (int16_t)(ns->w - ns->border_l - ns->border_r); // source centre span
	const int16_t ch	 = (int16_t)(ns->h - ns->border_t - ns->border_b);
	const int16_t dst_cw = (int16_t)(w - bl - br); // destination centre span to fill
	const int16_t dst_ch = (int16_t)(h - bt - bb);
	const int16_t src_r	 = (int16_t)(ns->w - ns->border_r); // right/bottom edge source origin
	const int16_t src_b	 = (int16_t)(ns->h - ns->border_b);
	const int32_t dst_r	 = x + w - br; // right/bottom edge destination origin
	const int32_t dst_b	 = y + h - bb;

	// Corners: exact size, no repeat.
	if (bl && bt)
		blit_tiled(t, ns->pixels, ns->w, palette, x, y, 0, 0, bl, bt, bl, bt);
	if (br && bt)
		blit_tiled(t, ns->pixels, ns->w, palette, dst_r, y, src_r, 0, br, bt, br, bt);
	if (bl && bb)
		blit_tiled(t, ns->pixels, ns->w, palette, x, dst_b, 0, src_b, bl, bb, bl, bb);
	if (br && bb)
		blit_tiled(t, ns->pixels, ns->w, palette, dst_r, dst_b, src_r, src_b, br, bb, br, bb);

	// Edges: tiled along their own axis only.
	if (bt && cw > 0 && dst_cw > 0)
		blit_tiled(t, ns->pixels, ns->w, palette, x + bl, y, ns->border_l, 0, cw, bt, dst_cw, bt);
	if (bb && cw > 0 && dst_cw > 0)
		blit_tiled(t, ns->pixels, ns->w, palette, x + bl, dst_b, ns->border_l, src_b, cw, bb,
				   dst_cw, bb);
	if (bl && ch > 0 && dst_ch > 0)
		blit_tiled(t, ns->pixels, ns->w, palette, x, y + bt, 0, ns->border_t, bl, ch, bl, dst_ch);
	if (br && ch > 0 && dst_ch > 0)
		blit_tiled(t, ns->pixels, ns->w, palette, dst_r, y + bt, src_r, ns->border_t, br, ch, br,
				   dst_ch);

	// Centre: tiled in both axes.
	if (cw > 0 && ch > 0 && dst_cw > 0 && dst_ch > 0)
		blit_tiled(t, ns->pixels, ns->w, palette, x + bl, y + bt, ns->border_l, ns->border_t, cw,
				   ch, dst_cw, dst_ch);
}

#endif // PNX_USE_NINESLICE
