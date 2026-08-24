// Host tests for input: edges, hold times, and the cluster mapping.
//
// The cluster is the part worth testing hardest. Its whole job is that the buttons do NOT
// rotate with the content, so an assertion that "position 0 is UP" is only interesting
// when it is checked against an orientation where it is not.

#include "../src/pnx/input/pnx_input.h"
#include "../src/pnx/gfx/pnx_gfx.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>

extern int s_failures;
extern int s_checks;

#define I_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define I_CHECK_EQ(a, b)                                                                     \
	do                                                                                       \
	{                                                                                        \
		s_checks++;                                                                          \
		const long _a = (long)(a), _b = (long)(b);                                           \
		if (_a != _b)                                                                        \
		{                                                                                    \
			printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n", __FILE__, __LINE__, #a, #b, _a, \
				   _b);                                                                      \
			s_failures++;                                                                    \
		}                                                                                    \
	} while (0)

void test_input(void);

static void feed(PnxEventType type, uint8_t button, uint32_t time_ms)
{
	PnxEvent ev = { type, time_ms, 0, 0, button };
	pnx_input_event(&ev);
}

static void feed_touch(PnxEventType type, int16_t x, int16_t y, uint32_t time_ms)
{
	PnxEvent ev = { type, time_ms, x, y, 0 };
	pnx_input_event(&ev);
}

void test_input(void)
{
	printf("input\n");

	// --- edges are per frame, held state is not
	pnx_input_init(PNX_ORIENT_BUTTONS_RIGHT);
	pnx_input_frame();
	feed(PNX_EVENT_BUTTON_DOWN, PNX_BUTTON_SELECT, 1000);

	I_CHECK(pnx_input_pressed(PNX_BUTTON_SELECT));
	I_CHECK(pnx_input_held(PNX_BUTTON_SELECT));
	I_CHECK(!pnx_input_released(PNX_BUTTON_SELECT));
	I_CHECK(!pnx_input_pressed(PNX_BUTTON_UP));

	pnx_input_frame();
	I_CHECK(!pnx_input_pressed(PNX_BUTTON_SELECT)); // the edge was last frame's
	I_CHECK(pnx_input_held(PNX_BUTTON_SELECT));		// the hold is not

	// Measured from the event's own stamp, not from the frame that noticed it.
	I_CHECK_EQ(pnx_input_held_ms(PNX_BUTTON_SELECT, 1250), 250);
	I_CHECK_EQ(pnx_input_held_ms(PNX_BUTTON_UP, 1250), 0);

	// A press straddling the wrap of the millisecond counter is a duration, not a jump to
	// four billion. Unsigned arithmetic gives this for free; a signed cast would not.
	pnx_input_init(PNX_ORIENT_BUTTONS_RIGHT);
	pnx_input_frame();
	feed(PNX_EVENT_BUTTON_DOWN, PNX_BUTTON_DOWN, 0xFFFFFF00u);
	I_CHECK_EQ(pnx_input_held_ms(PNX_BUTTON_DOWN, 0x00000100u), 512);

	pnx_input_frame();
	feed(PNX_EVENT_BUTTON_UP, PNX_BUTTON_DOWN, 0xFFFFFF00u + 600);
	I_CHECK(pnx_input_released(PNX_BUTTON_DOWN));
	I_CHECK(!pnx_input_held(PNX_BUTTON_DOWN));
	I_CHECK_EQ(pnx_input_held_ms(PNX_BUTTON_DOWN, 0x00000100u), 0);

	// Events that are not buttons pass through without disturbing anything -- a game feeds
	// the whole stream in rather than sorting it first.
	pnx_input_frame();
	{
		PnxEvent touch = { PNX_EVENT_TOUCH_DOWN, 5, 40, 60, 0 };
		pnx_input_event(&touch);
		PnxEvent focus = { PNX_EVENT_FOCUS_LOST, 6, 0, 0, 0 };
		pnx_input_event(&focus);
	}
	I_CHECK(!pnx_input_pressed(PNX_BUTTON_BACK));
	I_CHECK(!pnx_input_held(PNX_BUTTON_BACK));

	// --- touch: tap vs drag, edge vs level
	//
	// A tap is down-then-up without leaving the dead zone; a drag is a live dominant-axis
	// sign for as long as the touch stays down past it. Never both for the same touch.
	pnx_input_init(PNX_ORIENT_BUTTONS_RIGHT);
	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 40, 60, 100);
	I_CHECK(pnx_input_touch_held());
	I_CHECK(!pnx_input_touch_tapped()); // not resolved until release
	I_CHECK_EQ(pnx_input_drag_dx(), 0);
	I_CHECK_EQ(pnx_input_drag_dy(), 0);

	feed_touch(PNX_EVENT_TOUCH_UP, 42, 61, 120); // 2,1px -- inside the dead zone
	I_CHECK(pnx_input_touch_tapped());
	I_CHECK(!pnx_input_touch_held());
	I_CHECK_EQ(pnx_input_touch_x(), 42);
	I_CHECK_EQ(pnx_input_touch_y(), 61);

	pnx_input_frame(); // the tap edge belongs to the frame it happened in
	I_CHECK(!pnx_input_touch_tapped());

	// A drag past the dead zone reports a live axis and does NOT tap on release.
	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 0, 0, 200);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 20, 2, 210); // horizontal dominant
	I_CHECK_EQ(pnx_input_drag_dx(), 1);
	I_CHECK_EQ(pnx_input_drag_dy(), 0);
	I_CHECK(pnx_input_touch_held());

	feed_touch(PNX_EVENT_TOUCH_UP, 20, 2, 220);
	I_CHECK(!pnx_input_touch_tapped()); // it was a drag, not a tap
	I_CHECK(!pnx_input_touch_held());
	I_CHECK_EQ(pnx_input_drag_dx(), 0); // cleared on release
	I_CHECK_EQ(pnx_input_drag_dy(), 0);

	// The other axis, and the negative direction.
	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 50, 50, 300);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 48, 30, 310); // vertical dominant, moving up
	I_CHECK_EQ(pnx_input_drag_dx(), 0);
	I_CHECK_EQ(pnx_input_drag_dy(), -1);
	feed_touch(PNX_EVENT_TOUCH_UP, 48, 30, 320);

	// A tie between axes favours vertical, matching field.c's own ax > ay comparison
	// before this logic moved into the framework.
	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 0, 0, 400);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 15, 15, 410);
	I_CHECK_EQ(pnx_input_drag_dx(), 0);
	I_CHECK_EQ(pnx_input_drag_dy(), 1);
	feed_touch(PNX_EVENT_TOUCH_UP, 15, 15, 420);

	// A drag that leaves the dead zone and comes back WITHOUT releasing reads 0 again --
	// not latched at its last direction. Real bug, found independently of the
	// orientation one below: a back-and-forth drag that eased toward the touch-down
	// origin kept reporting whichever direction it last had while outside the dead zone,
	// because the dead-zone branch used to just `return` without resetting s_drag_dx/dy.
	// "0 before the dead zone is crossed" (pnx_input_drag_dx/dy's own doc comment) has to
	// hold every time the touch is back inside it, not just the first.
	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 0, 0, 500);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 20, 0, 510); // well past the dead zone, dx = +1
	I_CHECK_EQ(pnx_input_drag_dx(), 1);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 3, 0, 520); // back inside the dead zone, NOT released
	I_CHECK_EQ(pnx_input_drag_dx(), 0);
	I_CHECK_EQ(pnx_input_drag_dy(), 0);
	I_CHECK(pnx_input_touch_held());			   // still down the whole time
	feed_touch(PNX_EVENT_TOUCH_MOVE, -20, 0, 530); // out again, the OTHER direction
	I_CHECK_EQ(pnx_input_drag_dx(), -1);
	feed_touch(PNX_EVENT_TOUCH_UP, -20, 0, 540);

	// --- the cluster, per orientation
	//
	// SELECT is the middle in all four. The ENDS are the claim: turning the watch the
	// other way puts the physically-DOWN button under the player's left hand.
	static const struct
	{
		uint8_t orientation;
		uint8_t pos0, pos2;
	} clusters[] = {
		{ PNX_ORIENT_BUTTONS_RIGHT, PNX_BUTTON_UP, PNX_BUTTON_DOWN },
		{ PNX_ORIENT_BUTTONS_TOP, PNX_BUTTON_UP, PNX_BUTTON_DOWN },
		{ PNX_ORIENT_BUTTONS_BOTTOM, PNX_BUTTON_DOWN, PNX_BUTTON_UP },
		{ PNX_ORIENT_BUTTONS_LEFT, PNX_BUTTON_DOWN, PNX_BUTTON_UP },
	};

	for (unsigned i = 0; i < sizeof(clusters) / sizeof(clusters[0]); i++)
	{
		pnx_input_init(clusters[i].orientation);

		I_CHECK_EQ(pnx_input_cluster(0), clusters[i].pos0);
		I_CHECK_EQ(pnx_input_cluster(1), PNX_BUTTON_SELECT);
		I_CHECK_EQ(pnx_input_cluster(2), clusters[i].pos2);
		I_CHECK_EQ(pnx_input_cluster(3), PNX_BUTTON_COUNT); // out of range, not position 0

		// Holding the end nearest the screen's origin always reads -1, whichever physical
		// button that turns out to be. That is the entire point of the mapping: a menu
		// written once moves the cursor the way the player expects in every orientation.
		pnx_input_frame();
		feed(PNX_EVENT_BUTTON_DOWN, clusters[i].pos0, 10);
		I_CHECK_EQ(pnx_input_axis(), -1);
		I_CHECK_EQ(pnx_input_axis_pressed(), -1);

		// Both ends cancel rather than latching whichever was seen first.
		feed(PNX_EVENT_BUTTON_DOWN, clusters[i].pos2, 20);
		I_CHECK_EQ(pnx_input_axis(), 0);
		I_CHECK_EQ(pnx_input_axis_pressed(), 0);

		pnx_input_frame();
		feed(PNX_EVENT_BUTTON_UP, clusters[i].pos0, 30);
		I_CHECK_EQ(pnx_input_axis(), 1);		 // still held at the far end
		I_CHECK_EQ(pnx_input_axis_pressed(), 0); // but nothing was pressed this frame

		// SELECT is not on the axis. A game that fires with it does not want the cursor
		// moving as well.
		pnx_input_frame();
		feed(PNX_EVENT_BUTTON_UP, clusters[i].pos2, 40);
		feed(PNX_EVENT_BUTTON_DOWN, PNX_BUTTON_SELECT, 41);
		I_CHECK_EQ(pnx_input_axis(), 0);
		I_CHECK_EQ(pnx_input_axis_pressed(), 0);
	}

	// An orientation the enum does not define falls back to portrait rather than indexing
	// off the end of the table.
	pnx_input_init(200);
	I_CHECK_EQ(pnx_input_cluster(0), PNX_BUTTON_UP);

	// --- touch POSITION needs no transform, which is a claim worth pinning down
	//
	// M4c's roadmap called for rotating touch coordinates in the platform layer. It does
	// not need to: content is rotated at BUILD time, so a landscape game draws in the
	// framebuffer's frame like any other, and the device reports touches in that same
	// frame. Rotating them would land a tap where the pixel is not.
	//
	// Asserted rather than argued: paint a pixel, report a touch at those coordinates, and
	// check the touch names the pixel that was painted.
	//
	// This is about ABSOLUTE position (pnx_input_touch_x/y) only -- what a game checks a
	// tap against, which is drawn in the same framebuffer frame the touch is reported in.
	// Drag DIRECTION (below) is a different question with the opposite answer.
	pnx_host_reset();
	{
		PnxTarget* t	 = pnx_host_target();
		const int16_t tx = 37, ty = 91;
		pnx_gfx_fill_rect(t, tx, ty, 1, 1, 0xF3);

		PnxEvent ev = { PNX_EVENT_TOUCH_DOWN, 7, tx, ty, 0 };
		pnx_host_queue_event(ev);

		PnxEvent got;
		I_CHECK(pnx_platform_poll_event(&got));
		I_CHECK_EQ(got.type, PNX_EVENT_TOUCH_DOWN);

		PnxRow row = pnx_target_row(t, got.y);
		I_CHECK(row.data != NULL);
		I_CHECK_EQ(row.data[got.x], 0xF3);
	}

	// --- drag DIRECTION, unlike position, DOES need a transform
	//
	// A game reads pnx_input_drag_dx/dy as an AUTHOR-frame quantity -- it is what steers
	// lane_x in Need4Pebble, which render.c's own fb_point/fb_rect comment spells out as
	// "author/logical frame -> framebuffer". The touch EVENT arrives in the framebuffer's
	// frame (see the position test above), so a raw delta fed straight into the dominant-
	// axis check would report axes rotated 90 degrees from the one the game is asking
	// about under BUTTONS_TOP/BOTTOM. This is exactly what Need4Pebble hit: a horizontal
	// swipe, meant as steering, arrived as mostly-vertical raw motion and never crossed
	// the dead zone on the axis game.c actually reads -- "the car moves at some point"
	// only when a swipe happened to have enough raw-dx to tip the wrong-axis check, "but I
	// can't steer" the rest of the time.
	//
	// Expected values are rotate_point's own inverse (tools/pnx_assets.py), restricted to
	// a delta: BUTTONS_TOP maps a rightward raw drag (physical +x) to author "up"
	// (ady = -1), and a downward raw drag (physical +y) to author "right" (adx = +1).
	pnx_input_init(PNX_ORIENT_BUTTONS_TOP);
	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 0, 0, 500);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 20, 2, 510); // physical horizontal-dominant, rightward
	I_CHECK_EQ(pnx_input_drag_dx(), 0);
	I_CHECK_EQ(pnx_input_drag_dy(), -1);
	feed_touch(PNX_EVENT_TOUCH_UP, 20, 2, 520);

	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 0, 0, 600);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 2, 20, 610); // physical vertical-dominant, downward
	I_CHECK_EQ(pnx_input_drag_dx(), 1);
	I_CHECK_EQ(pnx_input_drag_dy(), 0);
	feed_touch(PNX_EVENT_TOUCH_UP, 2, 20, 620);

	// BUTTONS_BOTTOM rotates the other way: a rightward raw drag is author "down" instead
	// of "up".
	pnx_input_init(PNX_ORIENT_BUTTONS_BOTTOM);
	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 0, 0, 700);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 20, 2, 710);
	I_CHECK_EQ(pnx_input_drag_dx(), 0);
	I_CHECK_EQ(pnx_input_drag_dy(), 1);
	feed_touch(PNX_EVENT_TOUCH_UP, 20, 2, 720);

	// BUTTONS_LEFT is a half-turn: both axes invert, neither swaps.
	pnx_input_init(PNX_ORIENT_BUTTONS_LEFT);
	pnx_input_frame();
	feed_touch(PNX_EVENT_TOUCH_DOWN, 0, 0, 800);
	feed_touch(PNX_EVENT_TOUCH_MOVE, 20, 2, 810);
	I_CHECK_EQ(pnx_input_drag_dx(), -1);
	I_CHECK_EQ(pnx_input_drag_dy(), 0);
	feed_touch(PNX_EVENT_TOUCH_UP, 20, 2, 820);
}
