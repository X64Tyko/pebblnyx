// Orientation: where the button cluster sits when the device is held to play.
//
// Its own header because three unrelated modules need the same values -- assets stamps
// it into every blob, text turns it into a pen direction, input turns it into what UP
// means -- and none of them should have to include the others to say it.
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

	// Cluster along the left edge -- the watch turned a half-turn from buttons_right, face
	// still toward the player. The mirror of the native hold: a menu under the OTHER
	// thumb, for a left-handed grip, or a shooter's shoulder-trigger pair the other way
	// round. Content is rotated 180 degrees; unlike the two above, width and height do not
	// swap.
	PNX_ORIENT_BUTTONS_LEFT,

	PNX_ORIENT_COUNT
} PnxOrientation;

// Only possible at all because these games are played OFF the wrist, in two hands -- a
// watch strapped to an arm cannot be turned sideways and still read. See docs/PLATFORM.md.
//
// buttons_left is NOT landscape: it is portrait turned upside down, not sideways, so
// width and height stay the display's own -- see rotate_dims in tools/pnx_assets.py.
static inline bool pnx_orient_is_landscape(uint8_t o)
{
	return o == PNX_ORIENT_BUTTONS_TOP || o == PNX_ORIENT_BUTTONS_BOTTOM;
}
