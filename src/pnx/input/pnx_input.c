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

void pnx_input_init(uint8_t orientation)
{
	s_orientation = orientation < PNX_ORIENT_COUNT ? orientation : PNX_ORIENT_BUTTONS_RIGHT;
	memset(s_held, 0, sizeof(s_held));
	memset(s_pressed, 0, sizeof(s_pressed));
	memset(s_released, 0, sizeof(s_released));
	memset(s_since, 0, sizeof(s_since));
}

void pnx_input_frame(void)
{
	memset(s_pressed, 0, sizeof(s_pressed));
	memset(s_released, 0, sizeof(s_released));
}

void pnx_input_event(const PnxEvent* ev)
{
	if (!ev || ev->button >= PNX_BUTTON_COUNT)
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

#endif // PNX_USE_INPUT
