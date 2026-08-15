// Blitting and the camera.
//
// Everything draws through here, and everything here is 4bpp indexed: two pixels per
// byte, high nibble first, index 0 transparent in every palette. That last convention is
// load-bearing in the inner loop -- a transparent pixel is rejected on the index, before
// the palette is read and before the destination is touched.
//
// Clipping is done per row against the target's reported span rather than assumed, since
// the device's framebuffer can report a narrower valid range than its width.

#pragma once

#include "../pnx_config.h"
#include "../assets/pnx_assets.h"
#include "../core/pnx_fx.h"
#include "../platform/pnx_platform.h"

#include <stdint.h>
#include <stdbool.h>

// -------------------------------------------------------------------- 1-bit output
//
// Shared with pnx_text.c: a glyph blit and an indexed blit are different ways of deciding
// WHICH pixels to touch, but on a 1-bit target they write the SAME way once that decision
// is made, so both funnel through one rule rather than keeping two.
#if PNX_DISPLAY_BW
bool pnx_bw_is_ink(uint8_t colour);
void pnx_bw_set_pixel(uint8_t* row_data, int32_t x, bool ink);
#endif

// ------------------------------------------------------------------------ camera
//
// World coordinates are pixels, not tiles. Tile-sized steps make scrolling jerk at the
// tile boundary; pixels let the camera move smoothly and the tilemap simply starts its
// first column part-way off screen.

typedef struct
{
	int32_t x, y; // top-left of the view, in world pixels
	int16_t view_w, view_h;
} PnxCamera;

void pnx_camera_init(PnxCamera* c, int16_t view_w, int16_t view_h);

// Centres on a world point, then clamps so the view never leaves the world. Clamping
// here rather than at the call site is deliberate: every caller wants it, and forgetting
// it shows up as a strip of garbage at a map edge.
void pnx_camera_center(PnxCamera* c, int32_t wx, int32_t wy, int32_t world_w, int32_t world_h);

// ------------------------------------------------------------------------- blits

void pnx_gfx_clear(PnxTarget* t, uint8_t colour);

// Flip bits for pnx_blit_4bpp. X is 1 so an old `true` still means a horizontal flip.
#define PNX_FLIP_NONE 0
#define PNX_FLIP_X	  1
#define PNX_FLIP_Y	  2

// Blits a 4bpp image at a screen position. `mirror` flips horizontally, which is how a
// character faces left with no extra art -- the measured sprite sheets contain a walk
// cycle in one direction only.
//
// `src` is 4bpp indexed on a colour build and a ~bw resource (pack_unit_2bpp,
// tools/pnx_assets.py) on a 1-bit one -- one format per build (PNX_DISPLAY_BW), not a
// per-call choice, so the signature does not carry it. `palette` is read on a colour
// build and ignored on a 1-bit one, kept only so both builds share one call shape.
void pnx_blit_4bpp(PnxTarget* t, const uint8_t* src, const PnxPalette* palette, int32_t x,
				   int32_t y, int16_t w, int16_t h, uint8_t flip);

// Draws one metatiled tile: four 8x8 quadrants composed into a 16x16.
//
// Done as a single 16-row walk rather than four 8x8 blits. Four blits would double the
// per-row cost -- 32 row lookups and clip computations instead of 16 -- and row overhead
// is what dominates here: the measured frame cost fell 7,400 -> 5,100 us purely by
// reducing per-pixel and per-row work, not by changing what is drawn.
void pnx_blit_metatile(PnxTarget* t, const PnxAtlas* atlas, uint8_t tile, int32_t x, int32_t y);

// Same, with the palette supplied rather than looked up -- used for palette-swapped
// draws, and by tests that build an atlas without a loaded palette table.
void pnx_blit_metatile_with(PnxTarget* t, const PnxAtlas* atlas, uint8_t tile,
							const PnxPalette* palette, int32_t x, int32_t y);

// Filled rectangle in screen space, clipped. For dialog boxes and HUD panels.
void pnx_gfx_fill_rect(PnxTarget* t, int32_t x, int32_t y, int16_t w, int16_t h,
					   uint8_t colour);
