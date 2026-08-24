#include "pnx_input.h"

#if PNX_USE_INPUT

#include <string.h>

// Where each cluster position lands, per orientation. A table rather than arithmetic
// because there are four cases and they are not a formula -- the middle button is always
// SELECT, and only the ends swap. See the header for why buttons_bottom reverses.
//
// buttons_left reads the same row as buttons_bottom, which is not a copy-paste: the
// button nearest the origin is whichever one ends up nearest the screen's physical
// top-left corner, and a half-turn from buttons_right puts the same corner there that a
// further quarter-turn from buttons_top does. Worked from rotate_point in
// tools/pnx_assets.py, not from a hunch -- see that module's orientation section.
static const uint8_t s_cluster[PNX_ORIENT_COUNT][PNX_CLUSTER_SIZE] = {
	{ PNX_BUTTON_UP, PNX_BUTTON_SELECT, PNX_BUTTON_DOWN }, // buttons_right
	{ PNX_BUTTON_UP, PNX_BUTTON_SELECT, PNX_BUTTON_DOWN }, // buttons_top
	{ PNX_BUTTON_DOWN, PNX_BUTTON_SELECT, PNX_BUTTON_UP }, // buttons_bottom
	{ PNX_BUTTON_DOWN, PNX_BUTTON_SELECT, PNX_BUTTON_UP }, // buttons_left
};

static uint8_t s_orientation;
static bool s_held[PNX_BUTTON_COUNT];
static bool s_pressed[PNX_BUTTON_COUNT];
static bool s_released[PNX_BUTTON_COUNT];
static uint32_t s_since[PNX_BUTTON_COUNT]; // delivery stamp of the press

static bool s_touch_held;
static bool s_touch_tapped;
static int16_t s_touch_x0, s_touch_y0; // where this touch went down
static int16_t s_touch_x, s_touch_y;   // last known position
static int8_t s_drag_dx, s_drag_dy;

void pnx_input_init(uint8_t orientation)
{
	s_orientation = orientation < PNX_ORIENT_COUNT ? orientation : PNX_ORIENT_BUTTONS_RIGHT;
	memset(s_held, 0, sizeof(s_held));
	memset(s_pressed, 0, sizeof(s_pressed));
	memset(s_released, 0, sizeof(s_released));
	memset(s_since, 0, sizeof(s_since));

	s_touch_held   = false;
	s_touch_tapped = false;
	s_touch_x0 = s_touch_y0 = s_touch_x = s_touch_y = 0;
	s_drag_dx = s_drag_dy = 0;
}

void pnx_input_frame(void)
{
	memset(s_pressed, 0, sizeof(s_pressed));
	memset(s_released, 0, sizeof(s_released));
	s_touch_tapped = false;
}

// Dominant-axis sign of how far (dx, dy) has moved from the touch's origin, once that
// exceeds the dead zone -- field.c's own ax/ay comparison before this moved into the
// framework. Ties (a diagonal drag) favour the vertical axis, same as field.c did.
//
// Re-entering the dead zone resets to 0 rather than leaving the last direction latched:
// without this, a drag that crossed the threshold and then eased back toward the origin
// kept reporting whichever direction it last had, even sitting still near where the
// touch went down -- "0 before the dead zone is crossed" (this header's own doc comment
// on pnx_input_drag_dx/dy) is only true the FIRST time; every re-entry after that stayed
// stuck. Confirmed as a real, separate defect from the orientation bug below: a drag
// that goes out and comes back without releasing should read 0 again, and didn't.
//
// dx/dy here must already be in the AUTHOR frame a game draws in, not the raw touch
// frame -- see rotate_touch_delta's own comment below for why.
static void update_drag(int16_t dx, int16_t dy)
{
	const int16_t ax = (int16_t)(dx < 0 ? -dx : dx);
	const int16_t ay = (int16_t)(dy < 0 ? -dy : dy);
	if (ax < PNX_INPUT_DRAG_DEAD && ay < PNX_INPUT_DRAG_DEAD)
	{
		s_drag_dx = s_drag_dy = 0;
		return;
	}

	// Dominant axis only -- the other stays 0. Ties favour vertical (ax > ay, not >=).
	if (ax > ay)
	{
		s_drag_dx = dx > 0 ? 1 : -1;
		s_drag_dy = 0;
	}
	else
	{
		s_drag_dx = 0;
		s_drag_dy = dy > 0 ? 1 : -1;
	}
}

// pnx_orient.h: "the framebuffer never rotates" -- a TouchEvent's (x, y) is the display's
// own physical frame, same as it always is, with no per-orientation adjustment from the
// platform layer. But a game's own drag steering (pnx_input_drag_dx/dy) means left/right
// in the AUTHOR frame it draws and reasons in -- exactly what fb_point/fb_rect (e.g.
// Need4Pebble's render.c) convert INTO from, every frame, via tools/pnx_assets.py's
// rotate_point. Feeding a raw physical delta straight into update_drag skipped that
// conversion, so under BUTTONS_TOP/BOTTOM a player's intentional horizontal swipe showed
// up as mostly VERTICAL raw motion -- landing on the axis the game never reads. This is
// rotate_point's own inverse, restricted to a delta: dropping the width/height terms is
// valid because a pure rotation/reflection of a DIFFERENCE needs no absolute origin,
// unlike rotate_point itself (which maps a position, not a displacement).
static void rotate_touch_delta(int16_t dx, int16_t dy, int16_t* out_dx, int16_t* out_dy)
{
	switch (s_orientation)
	{
		case PNX_ORIENT_BUTTONS_TOP:
			*out_dx = dy;
			*out_dy = (int16_t)-dx;
			break;
		case PNX_ORIENT_BUTTONS_BOTTOM:
			*out_dx = (int16_t)-dy;
			*out_dy = dx;
			break;
		case PNX_ORIENT_BUTTONS_LEFT:
			*out_dx = (int16_t)-dx;
			*out_dy = (int16_t)-dy;
			break;
		default: // PNX_ORIENT_BUTTONS_RIGHT: the native frame, no rotation
			*out_dx = dx;
			*out_dy = dy;
			break;
	}
}

static void touch_event(const PnxEvent* ev)
{
	if (!s_touch_held && ev->type == PNX_EVENT_TOUCH_DOWN)
	{
		s_touch_held = true;
		s_touch_x0 = s_touch_x = ev->x;
		s_touch_y0 = s_touch_y = ev->y;
		s_drag_dx = s_drag_dy = 0;
		return;
	}
	if (ev->type != PNX_EVENT_TOUCH_MOVE && ev->type != PNX_EVENT_TOUCH_UP)
		return;
	if (!s_touch_held)
		return;

	s_touch_x = ev->x;
	s_touch_y = ev->y;
	int16_t adx, ady;
	rotate_touch_delta((int16_t)(s_touch_x - s_touch_x0), (int16_t)(s_touch_y - s_touch_y0),
					   &adx, &ady);
	update_drag(adx, ady);

	if (ev->type == PNX_EVENT_TOUCH_UP)
	{
		// Still within the dead zone at release: a tap, not a drag that happened to end.
		if (s_drag_dx == 0 && s_drag_dy == 0)
			s_touch_tapped = true;
		s_touch_held = false;
		s_drag_dx = s_drag_dy = 0;
	}
}

void pnx_input_event(const PnxEvent* ev)
{
	if (!ev)
		return;

	if (ev->type == PNX_EVENT_TOUCH_DOWN || ev->type == PNX_EVENT_TOUCH_MOVE ||
		ev->type == PNX_EVENT_TOUCH_UP)
	{
		touch_event(ev);
		return;
	}

	if (ev->button >= PNX_BUTTON_COUNT)
		return;

	if (ev->type == PNX_EVENT_BUTTON_DOWN)
	{
		// A repeated down without an up in between keeps the ORIGINAL stamp. The platform
		// subscribes raw press and release so this should not happen, but a hold that
		// silently restarted its clock would be a bug nobody could see -- the button would
		// simply never reach its threshold.
		if (!s_held[ev->button])
			s_since[ev->button] = ev->time_ms;
		s_held[ev->button]	  = true;
		s_pressed[ev->button] = true;
	}
	else if (ev->type == PNX_EVENT_BUTTON_UP)
	{
		s_held[ev->button]	   = false;
		s_released[ev->button] = true;
		s_since[ev->button]	   = 0;
	}
}

bool pnx_input_held(PnxButton button)
{
	return button < PNX_BUTTON_COUNT && s_held[button];
}

bool pnx_input_pressed(PnxButton button)
{
	return button < PNX_BUTTON_COUNT && s_pressed[button];
}

bool pnx_input_released(PnxButton button)
{
	return button < PNX_BUTTON_COUNT && s_released[button];
}

uint32_t pnx_input_held_ms(PnxButton button, uint32_t now_ms)
{
	if (button >= PNX_BUTTON_COUNT || !s_held[button])
		return 0;
	// Unsigned subtraction, so the millisecond counter wrapping mid-hold gives the right
	// elapsed time rather than a number near 2^32.
	return now_ms - s_since[button];
}

PnxButton pnx_input_cluster(uint8_t pos)
{
	if (pos >= PNX_CLUSTER_SIZE)
		return PNX_BUTTON_COUNT;
	return (PnxButton)s_cluster[s_orientation][pos];
}

int8_t pnx_input_axis(void)
{
	const bool low	= pnx_input_held(pnx_input_cluster(0));
	const bool high = pnx_input_held(pnx_input_cluster(2));
	return (int8_t)((high ? 1 : 0) - (low ? 1 : 0));
}

int8_t pnx_input_axis_pressed(void)
{
	const bool low	= pnx_input_pressed(pnx_input_cluster(0));
	const bool high = pnx_input_pressed(pnx_input_cluster(2));
	return (int8_t)((high ? 1 : 0) - (low ? 1 : 0));
}

bool pnx_input_touch_held(void)
{
	return s_touch_held;
}

bool pnx_input_touch_tapped(void)
{
	return s_touch_tapped;
}

int16_t pnx_input_touch_x(void)
{
	return s_touch_x;
}

int16_t pnx_input_touch_y(void)
{
	return s_touch_y;
}

int8_t pnx_input_drag_dx(void)
{
	return s_drag_dx;
}

int8_t pnx_input_drag_dy(void)
{
	return s_drag_dy;
}

#endif // PNX_USE_INPUT
