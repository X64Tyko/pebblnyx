// HUD windows: placed elements, anchored per orientation, bound to hud_var and animated
// with a show/hide slide.
//
// Where pnx_hud.h draws one widget at a position a game hands it, this draws a whole
// DECLARED layout -- a panel with a gauge and a label on it, say -- from a manifest
// [[hud_window]] (tools/pnx_assets.py), read back through pnx_hud_var_get_i32/get_text
// (pnx_hud_vars.h) each frame rather than through arguments a game re-passes every draw.
//
// A window is not scene-declared. pnx_scene_sprite/pnx_scene_nine_slice
// (assets/pnx_assets.h) hand back a SCENE-LOCAL index -- position in that one scene's own
// asset list -- which is the wrong fit for something meant to draw the same way from many
// scenes. Instead a window's sprite/nine_slice/font references resolve at BUILD time to
// plain global PNX_ASSET_* ids baked into its own blob, and pnx_hud_window_load pulls
// each one in by that id via the ordinary pnx_sprite_load/pnx_nineslice_load/
// pnx_font_load (already scene-independent) -- the caller brackets the call with
// pnx_assets_persistent(true) exactly as pnx_assets.h's own worked example
// (pnx_music_load) does, so a window's resources load once, for the app's life,
// independent of whatever scene is current.
//
// Depends on PNX_USE_NINESLICE, PNX_USE_TEXT, PNX_USE_SPRITES and PNX_USE_TWEEN, all of
// which default on -- the same soft-dependency posture pnx_config.h already documents for
// PNX_USE_HUD's own dependency on nine-slice and text. Gated on PNX_USE_HUD itself, not a
// flag of its own: meaningless without it, the same call Phase 2 made for pnx_hud_vars.h.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_HUD

#include "../assets/pnx_assets.h"
#include "../core/pnx_tween.h"
#include "pnx_gfx.h"
#include "pnx_nineslice.h"
#include "pnx_sprite.h"
#include "pnx_text.h"

#include <stdbool.h>
#include <stdint.h>

// Element kinds, and the manifest `kind` strings they come from (tools/pnx_assets.py).
#define PNX_HUD_ELEMENT_PANEL  0 // a nine_slice box
#define PNX_HUD_ELEMENT_SPRITE 1 // one frame of a declared sprite
#define PNX_HUD_ELEMENT_BAR	   2 // a pnx_hud_bar_draw-shaped gauge, value from a hud_var
#define PNX_HUD_ELEMENT_TEXT   3 // a hud_var's value, drawn with pnx_text_draw

// 9-way screen anchor an element's offset is relative to, matching the manifest's own
// `anchor` strings in the same order. Size-aware: a RIGHT/BOTTOM/CENTER anchor moves the
// element's own FAR edge (or centre) onto the anchor point, not its top-left corner --
// pnx_hud_window.c's own anchor_side_h/anchor_side_v -- so a `top_right` panel's right
// edge hugs the screen's right edge the way naming it that way implies, rather than
// running past it. The one exception is a `text` element's Y axis: `y` is the glyph
// BASELINE (pnx_text.h's own coordinate), not a box top, and there is no single "text
// height" the way a box's own h is unambiguous for the other three kinds -- text's X
// axis still corrects by its own measured width, just not its Y.
#define PNX_HUD_ANCHOR_TOP_LEFT		0
#define PNX_HUD_ANCHOR_TOP			1
#define PNX_HUD_ANCHOR_TOP_RIGHT	2
#define PNX_HUD_ANCHOR_LEFT			3
#define PNX_HUD_ANCHOR_CENTER		4
#define PNX_HUD_ANCHOR_RIGHT		5
#define PNX_HUD_ANCHOR_BOTTOM_LEFT	6
#define PNX_HUD_ANCHOR_BOTTOM		7
#define PNX_HUD_ANCHOR_BOTTOM_RIGHT 8

// No hud_var bound -- a panel or sprite element, neither of which reads one.
#define PNX_HUD_VAR_NONE 0xFF

// One placed, already-loaded element. A tagged union rather than one flat struct with
// every kind's fields: PnxSprite/PnxNineSlice/PnxFont are each a handful of pointers, and
// a window holds only a few elements, so the union's own footprint is not worth avoiding
// the way PnxSpriteInstance's flatness is (that struct is held by the hundred; this is
// not).
typedef struct
{
	uint8_t kind;
	uint8_t anchor;
	int16_t offset_x, offset_y;
	uint8_t hud_var; // PNX_HUD_VAR_NONE if this element does not bind one

	union
	{
		struct
		{
			PnxNineSlice ns;
			uint16_t w, h;
		} panel;
		struct
		{
			PnxSprite sp;
			uint8_t frame;
		} sprite;
		struct
		{
			uint16_t w, h;
			uint8_t border, track, fill;
			int16_t max;
		} bar;
		struct
		{
			PnxFont font;
			uint8_t colour; // GColor8, authored per element (pnx_hud_row_draw's own
							// "colour decisions stay explicit" posture, applied here too)
		} text;
	} as;
} PnxHudElement;

// Bytes per PACKED element record in a window's own blob -- kind, anchor, offset_x/y,
// asset_id, hud_var, three kind-specific bytes, w, h, bar_max. Unpacked field by field
// (pnx_sprite_frame_get's own style), not cast onto as a struct: portability, and the
// packed layout is not this struct's own layout anyway (PnxHudElement holds LOADED
// PnxSprite/PnxNineSlice/PnxFont, which the blob only ever names by id).
#define PNX_HUD_ELEMENT_BYTES 18

// A named group of elements that show/hide together. `elements`/`capacity` are storage
// the GAME declares, sized by its own generated PNX_HUD_WINDOW_{NAME}_ELEMENTS -- this
// module has no visibility into a specific project's element count, the same reason
// pnx_hud_vars_init (pnx_hud_vars.h) takes its storage as parameters rather than owning
// it.
typedef struct
{
	PnxHudElement* elements;
	uint8_t count;
	// Slide-in/out, both axes tweened together so a panel and its label move as one
	// unit (see this module's own top comment) rather than drifting apart mid-animation.
	PnxTween slide_x, slide_y;
	// Authored config, kept so pnx_hud_window_show/_hide can retarget a tween already
	// mid-flight (interrupting a hide with a show reverses smoothly from wherever the
	// slide currently sits, rather than snapping back to the offscreen extreme first).
	int16_t slide_dx, slide_dy;
	uint16_t show_ms, hide_ms;
} PnxHudWindow;

// Loads the window's own header (element count, show/hide duration, ease, slide offset)
// plus every element -- for panel/sprite/text elements, the referenced nine_slice/
// sprite/font is loaded too, via its baked global asset id, into whatever arena is
// currently active. Bracket with pnx_assets_persistent(true) (assets/pnx_assets.h) so a
// window survives scene changes:
//
//     const bool was = pnx_assets_persistent(true);
//     pnx_hud_window_load(&win, PNX_ASSET_HUD_WINDOW_SPEED_HUD, storage, capacity);
//     pnx_assets_persistent(was);
//
// `capacity` must be at least the blob's own element count (PNX_HUD_WINDOW_*_ELEMENTS,
// generated); a smaller capacity is refused rather than silently truncated.
bool pnx_hud_window_load(PnxHudWindow* win, uint16_t asset_id, PnxHudElement* storage,
						 uint8_t capacity);

// Starts (or restarts) the slide animation toward shown/hidden from `now_ms`. Drawing
// before either has ever been called reads as fully hidden (PnxTween's own "before
// start_ms reads as `from`" rule, pnx_tween.h) -- a window a game never shows never
// draws anything visible, without the caller having to gate the draw call itself on a
// "have I shown this yet" flag.
void pnx_hud_window_show(PnxHudWindow* win, uint32_t now_ms);
void pnx_hud_window_hide(PnxHudWindow* win, uint32_t now_ms);

// Draws every element at its anchor+offset, plus the current slide displacement, reading
// a bar/text element's live value from pnx_hud_var_get_i32/get_text (pnx_hud_vars.h) each
// call. Unconditional -- whether it is worth calling right now (a window fully hidden and
// done animating draws nothing a viewer can see, but still walks its element list) is the
// caller's decision, the same posture every pnx_hud_* draw call already takes.
void pnx_hud_window_draw(const PnxHudWindow* win, PnxTarget* t, const PnxPalette* palette,
						 uint32_t now_ms);

#endif // PNX_USE_HUD
