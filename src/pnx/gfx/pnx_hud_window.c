#include "pnx_hud_window.h"

#if PNX_USE_HUD

#include "../core/pnx_diag.h"
#include "pnx_hud.h"
#include "pnx_hud_vars.h"

// The fixed ease table a window's one `ease` byte indexes into -- order matches
// tools/pnx_assets.py's own EASE_NAMES exactly, since that order IS the id.
static const PnxEaseFn EASE_TABLE[] = {
	pnx_ease_linear,
	pnx_ease_in_quad,
	pnx_ease_out_quad,
	pnx_ease_in_out_quad,
	pnx_ease_in_cubic,
	pnx_ease_out_cubic,
	pnx_ease_in_out_cubic,
};
#define EASE_COUNT (sizeof(EASE_TABLE) / sizeof(EASE_TABLE[0]))

// Payload layout after the shared 8-byte blob header: show_ms/hide_ms/slide_x/slide_y (2
// bytes each, little-endian), then `count` PNX_HUD_ELEMENT_BYTES element records.
#define PAYLOAD_HEADER_BYTES 8

static uint16_t rd_u16(const uint8_t* p)
{
	return (uint16_t)(p[0] | (p[1] << 8));
}
static int16_t rd_i16(const uint8_t* p)
{
	return (int16_t)rd_u16(p);
}

bool pnx_hud_window_load(PnxHudWindow* win, uint16_t asset_id, PnxHudElement* storage,
						 uint8_t capacity)
{
	uint8_t count = 0, ease = 0, reserved_c = 0, reserved_d = 0;
	size_t payload = 0;
	const uint8_t* data =
		pnx_blob_load(asset_id, "HW", &count, &ease, &reserved_c, &reserved_d, &payload);
	if (!data)
		return false;

	if (count > capacity)
	{
		pnx_log("hud_window %u: %u elements, storage holds %u", asset_id, count, capacity);
		return false;
	}
	if (ease >= EASE_COUNT)
	{
		pnx_log("hud_window %u: ease %u out of range", asset_id, ease);
		return false;
	}
	const size_t needed = PAYLOAD_HEADER_BYTES + (size_t)count * PNX_HUD_ELEMENT_BYTES;
	if (payload < needed)
	{
		pnx_log("hud_window %u: truncated (need %u bytes, have %u)", asset_id,
				(unsigned)needed, (unsigned)payload);
		return false;
	}

	const uint16_t show_ms = rd_u16(data + 0);
	const uint16_t hide_ms = rd_u16(data + 2);
	const int16_t slide_x  = rd_i16(data + 4);
	const int16_t slide_y  = rd_i16(data + 6);

	win->elements = storage;
	win->count	  = count;
	win->show_ms  = show_ms;
	win->hide_ms  = hide_ms;
	win->slide_dx = slide_x;
	win->slide_dy = slide_y;
	// Sits at the offscreen displacement, permanently (duration 0), until the first
	// pnx_hud_window_show -- see this function's own doc comment in the header.
	pnx_tween_start(&win->slide_x, slide_x, slide_x, 0, EASE_TABLE[ease], 0);
	pnx_tween_start(&win->slide_y, slide_y, slide_y, 0, EASE_TABLE[ease], 0);

	const uint8_t* rec = data + PAYLOAD_HEADER_BYTES;
	for (uint8_t i = 0; i < count; i++, rec += PNX_HUD_ELEMENT_BYTES)
	{
		PnxHudElement* e		 = &storage[i];
		e->kind					 = rec[0];
		e->anchor				 = rec[1];
		e->offset_x				 = rd_i16(rec + 2);
		e->offset_y				 = rd_i16(rec + 4);
		const uint16_t asset_ref = rd_u16(rec + 6);
		e->hud_var				 = rec[8];
		const uint8_t p0 = rec[9], p1 = rec[10], p2 = rec[11];
		const uint16_t w = rd_u16(rec + 12), h = rd_u16(rec + 14);
		const int16_t bar_max = rd_i16(rec + 16);

		bool ok = true;
		switch (e->kind)
		{
			case PNX_HUD_ELEMENT_PANEL:
				ok			  = pnx_nineslice_load(&e->as.panel.ns, asset_ref);
				e->as.panel.w = w;
				e->as.panel.h = h;
				break;
			case PNX_HUD_ELEMENT_SPRITE:
				ok				   = pnx_sprite_load(&e->as.sprite.sp, asset_ref);
				e->as.sprite.frame = p0;
				break;
			case PNX_HUD_ELEMENT_BAR:
				e->as.bar.w		 = w;
				e->as.bar.h		 = h;
				e->as.bar.border = p0;
				e->as.bar.track	 = p1;
				e->as.bar.fill	 = p2;
				e->as.bar.max	 = bar_max;
				break;
			case PNX_HUD_ELEMENT_TEXT:
				ok				  = pnx_font_load(&e->as.text.font, asset_ref);
				e->as.text.colour = p0;
				break;
			default:
				pnx_log("hud_window %u: element %u has unknown kind %u", asset_id, i, e->kind);
				ok = false;
				break;
		}
		if (!ok)
		{
			pnx_log("hud_window %u: element %u (kind %u) failed to load its asset", asset_id,
					i, e->kind);
			return false;
		}
	}

	return true;
}

void pnx_hud_window_show(PnxHudWindow* win, uint32_t now_ms)
{
	const int32_t cur_x = pnx_tween_value(&win->slide_x, now_ms);
	const int32_t cur_y = pnx_tween_value(&win->slide_y, now_ms);
	pnx_tween_start(&win->slide_x, cur_x, 0, win->show_ms, win->slide_x.ease, now_ms);
	pnx_tween_start(&win->slide_y, cur_y, 0, win->show_ms, win->slide_y.ease, now_ms);
}

void pnx_hud_window_hide(PnxHudWindow* win, uint32_t now_ms)
{
	const int32_t cur_x = pnx_tween_value(&win->slide_x, now_ms);
	const int32_t cur_y = pnx_tween_value(&win->slide_y, now_ms);
	pnx_tween_start(&win->slide_x, cur_x, win->slide_dx, win->hide_ms, win->slide_x.ease,
					now_ms);
	pnx_tween_start(&win->slide_y, cur_y, win->slide_dy, win->hide_ms, win->slide_y.ease,
					now_ms);
}

// The 9 anchor points in the CURRENT orientation's own frame (PNX_DISPLAY_WIDTH/HEIGHT
// are already the rotated dimensions -- pnx_platform.h) -- no per-orientation authoring,
// same reasoning the manifest format's own comment gives.
static void anchor_point(uint8_t anchor, int16_t* ax, int16_t* ay)
{
	const int16_t w = PNX_DISPLAY_WIDTH, h = PNX_DISPLAY_HEIGHT;
	switch (anchor)
	{
		case PNX_HUD_ANCHOR_TOP_LEFT:
			*ax = 0;
			*ay = 0;
			break;
		case PNX_HUD_ANCHOR_TOP:
			*ax = (int16_t)(w / 2);
			*ay = 0;
			break;
		case PNX_HUD_ANCHOR_TOP_RIGHT:
			*ax = w;
			*ay = 0;
			break;
		case PNX_HUD_ANCHOR_LEFT:
			*ax = 0;
			*ay = (int16_t)(h / 2);
			break;
		case PNX_HUD_ANCHOR_CENTER:
			*ax = (int16_t)(w / 2);
			*ay = (int16_t)(h / 2);
			break;
		case PNX_HUD_ANCHOR_RIGHT:
			*ax = w;
			*ay = (int16_t)(h / 2);
			break;
		case PNX_HUD_ANCHOR_BOTTOM_LEFT:
			*ax = 0;
			*ay = h;
			break;
		case PNX_HUD_ANCHOR_BOTTOM:
			*ax = (int16_t)(w / 2);
			*ay = h;
			break;
		case PNX_HUD_ANCHOR_BOTTOM_RIGHT:
		default:
			*ax = w;
			*ay = h;
			break;
	}
}

// Which side of the ANCHOR POINT an element's own edge lands on -- -1 (the element's
// near edge sits at the point: LEFT/TOP), 0 (the point is the element's CENTRE), or 1
// (the element's FAR edge sits at the point: RIGHT/BOTTOM). Without this, every element
// draws with (x, y) as its own top-left regardless of anchor, so a `top_right` element
// wider than its offset margin runs straight off the right edge instead of hugging it --
// exactly backwards from what naming a RIGHT anchor is for.
static int8_t anchor_side_h(uint8_t anchor)
{
	switch (anchor)
	{
		case PNX_HUD_ANCHOR_TOP_RIGHT:
		case PNX_HUD_ANCHOR_RIGHT:
		case PNX_HUD_ANCHOR_BOTTOM_RIGHT:
			return 1;
		case PNX_HUD_ANCHOR_TOP:
		case PNX_HUD_ANCHOR_CENTER:
		case PNX_HUD_ANCHOR_BOTTOM:
			return 0;
		default:
			return -1;
	}
}
static int8_t anchor_side_v(uint8_t anchor)
{
	switch (anchor)
	{
		case PNX_HUD_ANCHOR_BOTTOM_LEFT:
		case PNX_HUD_ANCHOR_BOTTOM:
		case PNX_HUD_ANCHOR_BOTTOM_RIGHT:
			return 1;
		case PNX_HUD_ANCHOR_LEFT:
		case PNX_HUD_ANCHOR_CENTER:
		case PNX_HUD_ANCHOR_RIGHT:
			return 0;
		default:
			return -1;
	}
}

void pnx_hud_window_draw(const PnxHudWindow* win, PnxTarget* t, const PnxPalette* palette,
						 uint32_t now_ms)
{
	if (!win || !win->elements)
		return;

	const int32_t slide_x = pnx_tween_value(&win->slide_x, now_ms);
	const int32_t slide_y = pnx_tween_value(&win->slide_y, now_ms);
	// A HUD window draws in SCREEN space, not world space -- an identity camera (origin
	// at 0,0) turns pnx_sprite_draw's usual "world minus camera" math into a no-op
	// subtraction, so an element's own x/y IS its screen position.
	static const PnxCamera identity = { 0, 0, PNX_DISPLAY_WIDTH, PNX_DISPLAY_HEIGHT };

	for (uint8_t i = 0; i < win->count; i++)
	{
		const PnxHudElement* e = &win->elements[i];
		int16_t ax, ay;
		anchor_point(e->anchor, &ax, &ay);
		int32_t x = ax + e->offset_x + slide_x;
		int32_t y = ay + e->offset_y + slide_y;

		// Size-aware correction, x for every kind and y for every kind but TEXT: a
		// text element's y is the glyph BASELINE (pnx_text.h's own documented
		// coordinate), not a box top, and there is no single "text height" to
		// subtract the way a box's own h is unambiguous for the other three kinds --
		// see this file's own PNX_HUD_ELEMENT_TEXT comment below for what that means
		// for a bottom/centre vertical anchor on text.
		int32_t ew = 0, eh = 0;
		switch (e->kind)
		{
			case PNX_HUD_ELEMENT_PANEL:
				ew = e->as.panel.w;
				eh = e->as.panel.h;
				break;
			case PNX_HUD_ELEMENT_BAR:
				ew = e->as.bar.w;
				eh = e->as.bar.h;
				break;
			case PNX_HUD_ELEMENT_SPRITE:
				{
					PnxSpriteFrame f;
					pnx_sprite_frame_get(&e->as.sprite.sp, e->as.sprite.frame, &f);
					ew = f.w;
					eh = f.h;
					break;
				}
			case PNX_HUD_ELEMENT_TEXT:
				{
					const char* s =
						e->hud_var != PNX_HUD_VAR_NONE ? pnx_hud_var_get_text(e->hud_var) : "";
					ew = pnx_text_width(&e->as.text.font, s);
					break;
				}
			default:
				break;
		}
		const int8_t sh = anchor_side_h(e->anchor);
		x -= sh == 0 ? ew / 2 : sh == 1 ? ew
										: 0;
		if (e->kind != PNX_HUD_ELEMENT_TEXT)
		{
			const int8_t sv = anchor_side_v(e->anchor);
			y -= sv == 0 ? eh / 2 : sv == 1 ? eh
											: 0;
		}

		switch (e->kind)
		{
			case PNX_HUD_ELEMENT_PANEL:
				pnx_hud_panel_draw(t, &e->as.panel.ns, palette, x, y, (int16_t)e->as.panel.w,
								   (int16_t)e->as.panel.h);
				break;
			case PNX_HUD_ELEMENT_SPRITE:
				pnx_sprite_draw(&e->as.sprite.sp, t, &identity, x, y, e->as.sprite.frame, palette,
								false);
				break;
			case PNX_HUD_ELEMENT_BAR:
				{
					const int16_t value = e->hud_var != PNX_HUD_VAR_NONE
						? (int16_t)pnx_hud_var_get_i32(e->hud_var)
						: 0;
					pnx_hud_bar_draw(t, (int16_t)x, (int16_t)y, (int16_t)e->as.bar.w,
									 (int16_t)e->as.bar.h, value, e->as.bar.max, e->as.bar.border,
									 e->as.bar.track, e->as.bar.fill);
					break;
				}
			case PNX_HUD_ELEMENT_TEXT:
				{
					const char* s =
						e->hud_var != PNX_HUD_VAR_NONE ? pnx_hud_var_get_text(e->hud_var) : "";
					pnx_text_draw(t, &e->as.text.font, s, x, y, e->as.text.colour);
					break;
				}
			default:
				break;
		}
	}
}

#endif // PNX_USE_HUD
