// Buttons: edges, hold times, and where the cluster sits on the screen.
//
// Two jobs, and the second one is why this module exists at all.
//
// The first is the bookkeeping every game rewrites: the platform delivers raw down and up
// events, and a game wants to ask "was this pressed THIS frame" and "how long has it been
// held". Twenty lines, written badly at least once per project.
//
// The second is orientation. **The buttons do not rotate.** Content does -- the pipeline
// bakes atlases, maps and glyphs already turned, so the renderer never learns a game is in
// landscape (see core/pnx_orient.h). The three-button cluster is a physical object bolted
// to the right-hand edge of the case, and turning the watch sideways moves it to the top
// or the bottom of what the player is looking at. Nothing else in the framework has to
// care; this does, because "the top button" stops meaning "the button at the top".
//
// So the cluster is addressed by POSITION as the player reads it, running from the
// screen's origin:
//
//   portrait          down the right edge     0 = UP    1 = SELECT  2 = DOWN
//   buttons_top       across the top edge     0 = UP    1 = SELECT  2 = DOWN
//   buttons_bottom    across the bottom edge  0 = DOWN  1 = SELECT  2 = UP
//
// buttons_bottom reverses because the watch turned the other way: the end of the cluster
// nearest the player's left hand is the one physically labelled DOWN. A game that reads
// positions rather than buttons needs no branch for any of this, and a game that genuinely
// wants "the physical UP button" still asks for it by name.
//
// BACK is deliberately not in the cluster. It sits alone on the opposite edge, it means
// the same thing however the watch is held, and on a landscape game it is usually
// something the screen lock is swallowing anyway.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_INPUT

#include "../core/pnx_orient.h"
#include "../platform/pnx_platform.h"

#include <stdint.h>
#include <stdbool.h>

// UP, SELECT, DOWN. BACK is not one of them.
#define PNX_CLUSTER_SIZE 3

// `orientation` is a PnxOrientation -- pass the generated header's PNX_ORIENTATION, the
// same value the assets were baked at. Clears all button state.
void pnx_input_init(uint8_t orientation);

// Call once at the top of a frame, BEFORE feeding it events. Clears the edges, so
// pnx_input_pressed answers about this frame rather than about every frame since the
// press.
void pnx_input_frame(void);

// Feed every event the frame polled. Button events are recorded and anything else is
// ignored, so a game can pass the whole stream through without sorting it first:
//
//     pnx_input_frame();
//     PnxEvent ev;
//     while (pnx_platform_poll_event(&ev)) {
//       pnx_input_event(&ev);
//       if (ev.type == PNX_EVENT_TOUCH_DOWN) { ... }
//     }
//
// The game keeps ownership of the queue. An input module that drained it itself would
// swallow touch and focus events, and be wrong for every game that wants them.
void pnx_input_event(const PnxEvent *ev);

// ------------------------------------------------------------------ physical buttons

bool pnx_input_held(PnxButton button);
bool pnx_input_pressed(PnxButton button);    // went down during this frame
bool pnx_input_released(PnxButton button);   // came up during this frame

// How long `button` has been down, in ms, or 0 when it is not. Measured from the event's
// own delivery stamp rather than from the frame it was noticed in: a frame can arrive
// carrying seconds while the app is covered, and a hold judged against frame boundaries
// would be wrong by exactly that much.
uint32_t pnx_input_held_ms(PnxButton button, uint32_t now_ms);

// ------------------------------------------------------------------- cluster position

// The physical button at cluster position `pos` (0 nearest the screen's origin), or
// PNX_BUTTON_COUNT if `pos` is out of range.
PnxButton pnx_input_cluster(uint8_t pos);

// -1, 0 or +1 along the cluster as the player reads it: a cursor axis in portrait, a
// left/right axis in landscape. Level, not edge -- what continuous movement wants. Both
// ends held cancel to 0, which is the only sane answer and beats latching one of them.
int8_t pnx_input_axis(void);

// The same axis as edges: one step per press, which is what a menu wants. A press this
// frame at either end wins over a button merely still held at the other.
int8_t pnx_input_axis_pressed(void);

#endif  // PNX_USE_INPUT
