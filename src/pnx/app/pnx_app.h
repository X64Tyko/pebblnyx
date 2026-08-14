// App lifecycle: a state stack, and the fixed-timestep loop every pnx game otherwise
// hand-rolls.
//
// Called a STATE rather than a scene deliberately -- assets/pnx_assets.h already owns
// pnx_scene_*, and there a scene is a loaded content chunk. A state here is a mode of
// play: the title screen, the field, a menu overlay. The two are orthogonal (a state may
// span several asset scenes, or a scene may be visited by several states) and reusing the
// name would have made every sentence about either one ambiguous.
//
// Two things this module owns:
//
//   THE STACK. Push a state and it becomes current; the one beneath is left in place but
//   stops being driven -- exactly "nothing simulates behind the menu", as a property of
//   the stack instead of something every mode's tick function has to remember on its own.
//   Pop and the state beneath resumes. Depth is fixed and small (PNX_APP_MAX_DEPTH) -- a
//   dynamically growing stack is a poor fit for a game with a handful of modes and a hard
//   RAM ceiling.
//
//   THE FRAME LOOP. pnx_app_frame is a PnxFrameFn: hand it to pnx_platform_run and it
//   drives input/tick/draw on whatever is on top, owns the clamped fixed-timestep
//   accumulator so every game does not reimplement it, and turns OS focus events into
//   suspend/resume on the top state -- which is also where throttle-aware pausing lives:
//   between FOCUS_LOST and FOCUS_GAINED the accumulator is not fed at all, so a covered
//   app does not accrue ticks to spend in a burst on return. See PNX_MAX_CATCHUP_TICKS in
//   pnx_config.h for the belt-and-braces clamp that still applies to ordinary jitter.
//
// suspend/resume fire for BOTH reasons a state can stop or start being the one in charge:
// another state pushed on top of it, or the OS taking focus away from the whole app. A
// state that just wants to know "am I the one running right now" does not need to tell
// the two apart; one that does can ask pnx_app_covered().

#pragma once

#include "../pnx_config.h"

#if PNX_USE_APP

#include "../platform/pnx_platform.h"

#include <stdint.h>
#include <stdbool.h>

// Every hook is optional -- a state that has nothing to do on, say, suspend leaves it
// NULL. `frame` is the one exception to "nothing simulates while covered": it runs every
// RENDERED frame regardless, for the one thing pnx_app has found that must not stop when
// the app is covered -- resonant's sequencer advances against the wall clock rather than
// the tick specifically so a notification does not slow the music down with it. Most
// states leave `frame` NULL.
typedef struct
{
	void (*enter)(void* ctx);	 // just pushed
	void (*exit)(void* ctx);	 // just popped, permanently
	void (*suspend)(void* ctx);	 // covered -- by a push, or by the OS
	void (*resume)(void* ctx);	 // uncovered -- by a pop, or by the OS
	void (*input)(void* ctx);	 // once per rendered frame, before tick
	void (*tick)(void* ctx);	 // fixed PNX_TICK_MS steps; skipped while covered
	void (*frame)(void* ctx);	 // once per rendered frame, even while covered
	void (*draw)(void* ctx, PnxTarget* target);
} PnxAppOps;

#ifndef PNX_APP_MAX_DEPTH
#define PNX_APP_MAX_DEPTH 4
#endif

// `ctx` is passed to every hook unchanged -- one game-global struct, the same way every
// other pnx module that takes a callback works. Call once, before the first push.
void pnx_app_init(void* ctx);

// Suspends the current top (if any), pushes `ops`, and calls its enter. Silently refused
// past PNX_APP_MAX_DEPTH -- logged, not asserted, because a game that overflows this in a
// release build should keep running the state it already has rather than crash on a mode
// nobody will ever notice was skipped.
void pnx_app_push(const PnxAppOps* ops);

// Exits the current top and pops it, then resumes whatever is now on top, if anything.
// A no-op on an empty stack.
void pnx_app_pop(void);

// Exits the current top (NOT suspend -- it is not coming back) and pushes `ops` in its
// place, without a suspend/resume pair for whatever, if anything, is beneath. Pushes
// plainly if the stack was empty. For a transition that replaces the current mode rather
// than layering over it, e.g. returning from a menu straight to a different top-level
// screen.
void pnx_app_replace(const PnxAppOps* ops);

uint8_t pnx_app_depth(void);

// True between a FOCUS_LOST and the matching FOCUS_GAINED. Ticks are not running.
bool pnx_app_covered(void);

// PnxFrameFn-shaped: hand this straight to pnx_platform_run. Pumps events (lifecycle ones
// are consumed here; everything else is forwarded, see pnx_app_poll_event), then drives
// the top state's input, its tick loop, its per-frame hook, and its draw, in that order.
// Also calls pnx_diag_frame() itself, once, on every call including the early returns on
// an empty stack -- a game does not need to call it too.
void pnx_app_frame(void* unused_ctx, uint32_t elapsed_ms, PnxTarget* target);

// Events left over after pnx_app_frame has consumed FOCUS_LOST/FOCUS_GAINED for itself. A
// state's own input() calls this instead of pnx_platform_poll_event directly.
bool pnx_app_poll_event(PnxEvent* out);

#endif	// PNX_USE_APP
