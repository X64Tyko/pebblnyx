// Orientation: where the button cluster sits when the device is held to play.
//
// Its own header because three unrelated modules need the same three values -- assets
// stamps it into every blob, text turns it into a pen direction, input turns it into
// what UP means -- and none of them should have to include the others to say it.
//
// **The framebuffer never rotates.** Content is rotated at BUILD time: the pipeline emits
// atlases, sprites, maps and glyphs already turned, so the engine's ordinary portrait
// blit puts a correct landscape image on the screen. Rotating at render time would have
// meant a stride multiply per pixel across four span writers and strided framebuffer
// writes whose cost was never measured; pre-rotation removes the question instead of
// answering it, and works on a round display for free. See docs/ROADMAP.md, M4c.
//
// So nothing below this line is a rendering mode. Game code always works in the
// framebuffer's frame, whatever the orientation -- the values here exist for the three
// things that genuinely cannot be baked: which blob belongs to which build, which way a
// pen walks, and which physical button means "up" to a player holding the watch sideways.
//
// Named for the cluster because that is what the author is choosing, and because
// "landscape_left" starts an argument about whether the device or the image turned.

#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum
{
	// Portrait, the display's native orientation. The cluster falls under one thumb, which
	// reads as a menu column: an RPG, anything with a cursor.
	PNX_ORIENT_BUTTONS_RIGHT,

	// Held so the cluster runs along the top edge, under both index fingers: shoulder
	// triggers, which is what makes a shooter playable. Content is rotated clockwise into
	// the framebuffer.
	PNX_ORIENT_BUTTONS_TOP,

	// Cluster along the bottom edge, under both thumbs: flippers, for pinball. Content is
	// rotated anticlockwise.
	PNX_ORIENT_BUTTONS_BOTTOM,

	PNX_ORIENT_COUNT
} PnxOrientation;

// Only possible at all because these games are played OFF the wrist, in two hands -- a
// watch strapped to an arm cannot be turned sideways and still read. See docs/PLATFORM.md.
static inline bool pnx_orient_is_landscape(uint8_t o)
{
	return o == PNX_ORIENT_BUTTONS_TOP || o == PNX_ORIENT_BUTTONS_BOTTOM;
}
