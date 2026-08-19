#include "pnx_hud.h"

#if PNX_USE_HUD

void pnx_hud_panel_draw(PnxTarget* t, const PnxNineSlice* ns, const PnxPalette* palette,
						int32_t x, int32_t y, int16_t w, int16_t h)
{
	pnx_gfx_draw_nine_slice(t, ns, palette, x, y, w, h);
}

void pnx_hud_bar_draw(PnxTarget* t, int16_t x, int16_t y, int16_t w, int16_t h, int16_t value,
					  int16_t max, uint8_t border, uint8_t track, uint8_t fill)
{
	if (max <= 0)
		return;
	if (value < 0)
		value = 0;
	if (value > max)
		value = max;

	pnx_gfx_fill_rect(t, x, y, w, h, border);
	pnx_gfx_fill_rect(t, (int16_t)(x + 1), (int16_t)(y + 1), (int16_t)(w - 2), (int16_t)(h - 2),
					  track);

	const int16_t filled = (int16_t)(((int32_t)(w - 2) * value) / max);
	if (filled > 0)
		pnx_gfx_fill_rect(t, (int16_t)(x + 1), (int16_t)(y + 1), filled, (int16_t)(h - 2), fill);
}

void pnx_hud_row_draw(PnxTarget* t, const PnxFont* f, int16_t left_x, int16_t right_margin,
					  int16_t y, const char* left, const char* right, bool selected,
					  uint8_t ink)
{
	if (!f)
		return;

	// The cursor bar sits a fixed 8px left of the text it marks -- any closer reads as
	// touching the text rather than pointing at it, on a display this size.
	if (selected)
		pnx_gfx_fill_rect(t, (int16_t)(left_x - 8), (int16_t)(y - f->baseline), 2,
						  f->line_height, ink);

	pnx_text_draw(t, f, left, left_x, y, ink);
	if (right)
	{
		const int16_t w = pnx_target_width(t);
		pnx_text_draw(t, f, right, (int16_t)(w - right_margin - pnx_text_width(f, right)), y,
					  ink);
	}
}

#endif // PNX_USE_HUD
