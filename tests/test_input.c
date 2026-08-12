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

#define I_CHECK(cond) do {                                                  \
    s_checks++;                                                             \
    if (!(cond)) {                                                          \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);              \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

#define I_CHECK_EQ(a, b) do {                                               \
    s_checks++;                                                             \
    const long _a = (long)(a), _b = (long)(b);                              \
    if (_a != _b) {                                                         \
      printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n",                      \
             __FILE__, __LINE__, #a, #b, _a, _b);                           \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

void test_input(void);

static void feed(PnxEventType type, uint8_t button, uint32_t time_ms) {
  PnxEvent ev = { type, time_ms, 0, 0, button };
  pnx_input_event(&ev);
}

void test_input(void) {
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
  I_CHECK(!pnx_input_pressed(PNX_BUTTON_SELECT));   // the edge was last frame's
  I_CHECK(pnx_input_held(PNX_BUTTON_SELECT));       // the hold is not

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

  // --- the cluster, per orientation
  //
  // SELECT is the middle in all three. The ENDS are the claim: turning the watch the
  // other way puts the physically-DOWN button under the player's left hand.
  static const struct {
    uint8_t orientation;
    uint8_t pos0, pos2;
  } clusters[] = {
    { PNX_ORIENT_BUTTONS_RIGHT,  PNX_BUTTON_UP,   PNX_BUTTON_DOWN },
    { PNX_ORIENT_BUTTONS_TOP,    PNX_BUTTON_UP,   PNX_BUTTON_DOWN },
    { PNX_ORIENT_BUTTONS_BOTTOM, PNX_BUTTON_DOWN, PNX_BUTTON_UP   },
  };

  for (unsigned i = 0; i < sizeof(clusters) / sizeof(clusters[0]); i++) {
    pnx_input_init(clusters[i].orientation);

    I_CHECK_EQ(pnx_input_cluster(0), clusters[i].pos0);
    I_CHECK_EQ(pnx_input_cluster(1), PNX_BUTTON_SELECT);
    I_CHECK_EQ(pnx_input_cluster(2), clusters[i].pos2);
    I_CHECK_EQ(pnx_input_cluster(3), PNX_BUTTON_COUNT);   // out of range, not position 0

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
    I_CHECK_EQ(pnx_input_axis(), 1);            // still held at the far end
    I_CHECK_EQ(pnx_input_axis_pressed(), 0);    // but nothing was pressed this frame

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

  // --- touch needs no transform, which is a claim worth pinning down
  //
  // M4c's roadmap called for rotating touch coordinates in the platform layer. It does
  // not need to: content is rotated at BUILD time, so a landscape game draws in the
  // framebuffer's frame like any other, and the device reports touches in that same
  // frame. Rotating them would land a tap where the pixel is not.
  //
  // Asserted rather than argued: paint a pixel, report a touch at those coordinates, and
  // check the touch names the pixel that was painted.
  pnx_host_reset();
  {
    PnxTarget *t = pnx_host_target();
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
}
