// 9-slice panel drawing.
//
// A panel is one packed image plus four border insets (PnxNineSlice, pnx_assets.h). Draw
// it at any box size and the four corners come out exact while the four edges and the
// centre tile to fill the rest -- a bordered HUD panel that is not just a flat rectangle,
// without anything in this engine ever scaling a pixel.
//
// Tile, not stretch: pnx_blit_4bpp has never scaled anything -- it flips and rotates, and
// pnx_blit_metatile's own comment already explains why repeated blits beat a second,
// resampled buffer here. A stretch mode is a plausible future addition (a fifth region
// shape, not a breaking change to this one) but is not built, so this file does not carry
// a fill-mode parameter for a mode that does not exist yet.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_NINESLICE

#include "pnx_gfx.h"
#include "../assets/pnx_assets.h"

// Degenerate boxes (narrower/shorter than the panel's own borders) are clamped rather
// than refused, so a HUD element that briefly animates through a tiny size still draws
// something coherent instead of nothing.
void pnx_gfx_draw_nine_slice(PnxTarget* t, const PnxNineSlice* ns, const PnxPalette* palette,
							 int32_t x, int32_t y, int16_t w, int16_t h);

#endif // PNX_USE_NINESLICE
