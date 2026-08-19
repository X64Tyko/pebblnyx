// HUD widgets: bars, labelled rows, and bordered panels.
//
// Everything here used to be hand-rolled per game -- resonant's ui.c drew its stat bar and
// its menu rows as flat pnx_gfx_fill_rect calls with the game's own colour logic baked
// into the drawing code, nothing of it reusable by another game. This module is that same
// drawing pulled out: no game-specific colour or layout baked in anywhere here, every
// colour and position an explicit argument, the same way the rest of gfx/ already works.
// A game's own colour DECISIONS (dimmed vs selected vs normal, its own palette indices)
// stay in the game -- resonant's ui_row_draw is exactly that resolution, now calling
// pnx_hud_row_draw with the ink it already picked rather than drawing the row itself.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_HUD

#include "pnx_gfx.h"
#include "pnx_nineslice.h"
#include "pnx_text.h"

#include <stdbool.h>

// A bordered panel at any size -- corners exact, edges/centre tiled. Thin wrapper over
// pnx_gfx_draw_nine_slice: the point of this module is the widget layer above the
// primitive, not a second copy of it.
void pnx_hud_panel_draw(PnxTarget* t, const PnxNineSlice* ns, const PnxPalette* palette,
						int32_t x, int32_t y, int16_t w, int16_t h);

// A bordered, filled gauge: `border` frames the whole w x h box, `track` fills the empty
// remainder, `fill` covers `value` of `max` from the left. Clamps `value` to [0, max]; a
// non-positive `max` draws nothing, since there is no meaningful fraction of it.
void pnx_hud_bar_draw(PnxTarget* t, int16_t x, int16_t y, int16_t w, int16_t h, int16_t value,
					  int16_t max, uint8_t border, uint8_t track, uint8_t fill);

// A menu row: a `left` label, an optional right-aligned `right` value, a 2px cursor bar
// when `selected`. `ink` is the already-resolved colour for the whole row -- see this
// file's own comment for why dimmed/selected/normal stays the caller's decision.
void pnx_hud_row_draw(PnxTarget* t, const PnxFont* f, int16_t left_x, int16_t right_margin,
					  int16_t y, const char* left, const char* right, bool selected,
					  uint8_t ink);

#endif // PNX_USE_HUD
