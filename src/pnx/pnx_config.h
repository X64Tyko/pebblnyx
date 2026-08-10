// Compile-time module selection.
//
// A game copies this file, edits it, and puts it on the include path ahead of the
// framework's default. Every module is opt-in, and a disabled module must contribute
// ZERO bytes -- its whole translation unit is wrapped in the matching #if, so it
// compiles to an empty object.
//
// This exists because `.text + .data + .bss` is capped at 65,535 bytes for the
// framework AND the game together (see docs/MEASUREMENTS.md). A module that cannot be
// compiled out taxes every game that does not use it. Run tools/size_report.py after a
// build to see what each one actually costs.

#pragma once

// Asset registry: packed blobs, handle-based lookup, bulk residency.
#ifndef PNX_USE_ASSETS
#define PNX_USE_ASSETS 1
#endif

#ifndef PNX_USE_TILEMAP
#define PNX_USE_TILEMAP 1
#endif

#ifndef PNX_USE_SPRITES
#define PNX_USE_SPRITES 1
#endif

#ifndef PNX_USE_TEXT
#define PNX_USE_TEXT 1
#endif

#ifndef PNX_USE_AUDIO
#define PNX_USE_AUDIO 1
#endif

// The sequencer is the music half of audio. A game with sound effects but no music
// wants PNX_USE_AUDIO=1 and this 0.
#ifndef PNX_USE_SEQUENCER
#define PNX_USE_SEQUENCER 1
#endif

#ifndef PNX_USE_SAVE
#define PNX_USE_SAVE 1
#endif

#ifndef PNX_USE_DIALOG
#define PNX_USE_DIALOG 1
#endif

// Timing-window judging for Additions and similar. Cheap, but pointless in a game
// with no timed input. See docs/GAME.md for the measured parameters.
#ifndef PNX_USE_TIMING
#define PNX_USE_TIMING 1
#endif

// On-screen performance overlay and deferred logging. Should be 0 in a release build:
// a text draw costs ~4.3ms, 12% of a frame.
#ifndef PNX_USE_DIAGNOSTICS
#define PNX_USE_DIAGNOSTICS 1
#endif

// Deferred log ring. LINES * LINE_LEN bytes of bss, and at the default 24 x 96 that is
// 2,304 bytes -- the largest single static allocation in an empty app, roughly a third
// of its footprint. Worth shrinking in anything that logs little, and it costs nothing
// at all with PNX_USE_DIAGNOSTICS=0.
#ifndef PNX_LOG_LINES
#define PNX_LOG_LINES 24
#endif

#ifndef PNX_LOG_LINE_LEN
#define PNX_LOG_LINE_LEN 96
#endif

// Palette table. Slots are claimed by atlas loads and by explicit variant loads, and
// all are released together when a scene resets the arena. Bounded deliberately: a
// framework cannot assume how much content a project has, and an unbounded table would
// fail as a wrong-colours art bug rather than as an error. Costs SLOTS * 16 bytes of
// arena. The pipeline reports palette count per scene against this number.
#ifndef PNX_PALETTE_SLOTS
#define PNX_PALETTE_SLOTS 32
#endif

// ---------------------------------------------------------------------- tuning

// Simulation tick. 25Hz sits just under the 26.8fps display ceiling, so the sim never
// starves waiting for a frame that cannot arrive.
#ifndef PNX_TICK_MS
#define PNX_TICK_MS 40
#endif

// Maximum ticks consumed in one frame. NOT defensive padding: while covered by a modal
// the app is throttled to ~0.4fps, so a frame can arrive carrying several seconds of
// elapsed time. Without this clamp the sim fast-forwards on every notification.
#ifndef PNX_MAX_CATCHUP_TICKS
#define PNX_MAX_CATCHUP_TICKS 4
#endif