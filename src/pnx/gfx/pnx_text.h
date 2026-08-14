// Text: glyph blitting and line layout.
//
// This replaces the SDK text hook in the platform layer, which cost ~4.3 ms per draw --
// 12% of a 37.33 ms frame -- and could only run AFTER the framebuffer was released, so
// text could never have anything drawn over it. A glyph blit is an ordinary blit: it
// happens during the frame, in order, like a sprite.
//
// Coordinates are screen pixels and **y is the BASELINE, not the top of the line box**.
// That is where the metrics are measured from, and it is what keeps a HUD number and a
// label on the same visual line when they come from different fonts. To position from a
// box top instead, add the font's `baseline`:
//
//     pnx_text_draw(t, font, "HP", x, box_y + font->baseline, ink);
//
// **A rotated font swaps which coordinate is which.** A landscape build bakes its glyphs
// on their side and the pen walks down a column, so for a PNX_ADVANCE_Y_* face the pen
// starts at `y` and `x` is the baseline. (x, y) is still simply the point on the baseline
// where the text starts, and every call below still takes it that way -- but a game that
// positions text by adding `font->baseline` adds it to x rather than to y.
//
// Widths, wrap boxes and the return value are lengths along the baseline, whichever
// direction that runs. `w` in the wrapped call is therefore measured down the screen in
// landscape, and `h` across it.
//
// Colour is a GColor8 value, the same as every other draw takes. There is no palette:
// text is one colour, and the four bits an atlas spends naming a palette entry would be
// waste per pixel. See PnxFont in assets/pnx_assets.h for the storage format.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_TEXT

#include "../assets/pnx_assets.h"
#include "../platform/pnx_platform.h"

#include <stdint.h>

typedef enum
{
	PNX_ALIGN_LEFT = 0,
	PNX_ALIGN_CENTER,
	PNX_ALIGN_RIGHT,
} PnxTextAlign;

// ---------------------------------------------------------------------- measuring
//
// Measuring and drawing walk the same code, so a width can never disagree with what a
// draw actually puts on screen -- which is the bug that makes a centred label sit
// slightly off, and the reason these are not two independent implementations.

// Advance width of `s` on one line, ignoring any newline in it.
int16_t pnx_text_width(const PnxFont* f, const char* s);

// Height `s` needs when wrapped into `w` pixels: line_height per line, no trailing gap.
int16_t pnx_text_height_wrapped(const PnxFont* f, const char* s, int16_t w);

// Lines `s` wraps to at width `w`. Useful for paging dialogue before drawing it.
int16_t pnx_text_lines_wrapped(const PnxFont* f, const char* s, int16_t w);

// ------------------------------------------------------------------------ drawing

// One line, no wrapping, clipped. Returns the advance width actually consumed, so
// successive draws can be chained -- a label followed by a value in another colour.
int16_t pnx_text_draw(PnxTarget* t, const PnxFont* f, const char* s, int32_t x, int32_t y,
					  uint8_t colour);

// Word-wrapped into a box. `x, y` is the baseline of the FIRST line; `w` is the wrap
// width. `h` bounds the drawing -- lines whose baseline would fall past `y + h` are not
// drawn -- and `h == 0` means unbounded.
//
// Breaks on spaces, and hard-breaks a word too long to fit on a line of its own rather
// than letting it run out of the box. Honours '\n' as a forced break.
//
// Returns the number of lines drawn.
int16_t pnx_text_draw_wrapped(PnxTarget* t, const PnxFont* f, const char* s, int32_t x,
							  int32_t y, int16_t w, int16_t h, uint8_t colour,
							  PnxTextAlign align);

#endif // PNX_USE_TEXT
