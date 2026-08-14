// The platform seam.
//
// This header declares everything the framework needs from the outside world, in its
// own types. It must never include <pebble.h>, and nothing above this layer may either.
// That is what allows core, gfx, audio and save to compile and be tested on a host, and
// it is the only reason the framework could ever target something else.
//
// Two implementations:
//   pnx_platform_pebble.c   the real device
//   pnx_platform_host.c     enough to run tests natively
//
// Deliberately narrow. Each subsystem adds only what it needs, when it needs it, rather
// than mirroring the whole SDK up front.

#pragma once

// The config, because this header is now conditional on part of it. Without it a
// PNX_USE_* guard here reads as 0 while the same guard in a .c that DID include the config
// reads as 1 -- so a declaration vanishes and its definition does not, and the error names
// a missing type rather than a missing setting.
#include "../pnx_config.h"

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

// ------------------------------------------------------------------------- time

// Milliseconds since an arbitrary epoch. Resolution is 1ms on device; anything shorter
// must be measured by repetition, not by a single call.
uint32_t pnx_platform_now_ms(void);

// ------------------------------------------------------------------------ output

void pnx_platform_log(const char* message);

// ---------------------------------------------------------------- render target
//
// Row-based rather than a flat pointer, because the device's framebuffer is accessed
// per row and may report a narrower valid span on some displays. A flat abstraction
// would be a lie on hardware and would break on a round screen.

typedef struct PnxTarget PnxTarget;

typedef struct
{
	uint8_t* data;	// byte at column 0 of this row; index it with x directly
	int16_t min_x;	// first valid column
	int16_t max_x;	// last valid column, inclusive
} PnxRow;

int16_t pnx_target_width(const PnxTarget* t);
int16_t pnx_target_height(const PnxTarget* t);
PnxRow pnx_target_row(PnxTarget* t, int16_t y);

// ---------------------------------------------------------------------- resources
//
// Packed asset blobs live in Pebble resources. Reads cost ~29 us per CALL plus about
// 33 MB/s of transfer, and locality does not matter -- a scattered read is no worse
// than a sequential one. The consequence shapes the whole asset layer: batch into few
// large reads and hold them resident, never stream per tile. Reading one 16x16 tile at
// a time would cost ~6.7 ms/frame to save memory that is not scarce.

// False if the id is unknown. Size is the whole resource.
bool pnx_platform_resource_size(uint32_t resource_id, size_t* out_size);

// Reads a byte range into dst, returning bytes actually read (0 on failure). One call
// per asset blob is the intended usage.
size_t pnx_platform_resource_read(uint32_t resource_id, size_t offset, void* dst, size_t bytes);

// ------------------------------------------------------------------------- audio
//
// A continuously open PCM stream, fed a little every frame. This is the only shape that
// works: the batch API (speaker_play_tracks) costs ~94 ms per submission and refuses a
// second submission while one is playing, so music and effects cannot coexist through
// it. See docs/MEASUREMENTS.md.
//
// pnx_platform_audio_write returns how many bytes the device ACCEPTED, which is the
// flow-control signal the mixer needs -- write until it stops accepting, and the buffer
// depth takes care of itself without us modelling it.

// Order is arbitrary and must NOT be relied on: map to the platform's own constants by
// name, never by position. A positional table here mapped every format to the wrong one.
//
// 8-bit is the right choice with an 8-bit mixer -- a 16-bit stream would be the same
// samples shifted left eight, carrying no extra information for twice the bandwidth.
typedef enum
{
	PNX_AUDIO_16KHZ_16BIT = 0,	// 32 KB/s
	PNX_AUDIO_16KHZ_8BIT,		// 16 KB/s
	PNX_AUDIO_8KHZ_16BIT,		// 16 KB/s
	PNX_AUDIO_8KHZ_8BIT,		//  8 KB/s; the default
} PnxAudioFormat;

bool pnx_platform_audio_open(PnxAudioFormat format, uint8_t volume);
size_t pnx_platform_audio_write(const void* data, size_t bytes);
void pnx_platform_audio_close(void);
bool pnx_platform_audio_is_open(void);

// What the speaker itself thinks it is doing. Worth asking, because "we supplied enough
// bytes in aggregate" and "the buffer never emptied" are different claims, and only the
// second one keeps playback continuous. A stream that drains transitions out of Playing
// and restarts on the next write, which sounds like the note beginning again.
typedef enum
{
	PNX_AUDIO_IDLE = 0,
	PNX_AUDIO_PLAYING,
	PNX_AUDIO_DRAINING,
	PNX_AUDIO_UNKNOWN,
} PnxAudioState;

PnxAudioState pnx_platform_audio_state(void);

// Runs `fn` every `interval_ms`, independently of rendering.
//
// Audio was being fed once per rendered frame, which ties it to a display capped at
// 26.8fps: feeds arrive ~37ms apart at best and measured 140ms apart in practice, forcing
// a 400ms lead to stay continuous -- and 400ms of lead is 400ms before an effect is heard.
//
// The render cap is a property of the display, not of the speaker. A timer feeding every
// few milliseconds keeps the buffer full with a fraction of the lead, which fixes the
// stutter and the latency together.
typedef void (*PnxTickFn)(void* ctx);
void pnx_platform_set_audio_timer(PnxTickFn fn, void* ctx, uint16_t interval_ms);

// Bytes per second for a format, so the mixer can size a frame's worth of samples.
static inline uint32_t pnx_audio_byte_rate(PnxAudioFormat f)
{
	const uint32_t rate =
		(f == PNX_AUDIO_16KHZ_16BIT || f == PNX_AUDIO_16KHZ_8BIT) ? 16000u : 8000u;
	const uint32_t width = (f == PNX_AUDIO_16KHZ_16BIT || f == PNX_AUDIO_8KHZ_16BIT) ? 2u : 1u;
	return rate * width;
}

// ------------------------------------------------------------------------ persist
//
// Key-value flash storage, keyed by a small integer. Costed in docs/MEASUREMENTS.md and
// the two numbers there ARE the save module's design:
//
//   - a write costs ~7ms PER CALL, almost flat regardless of how many bytes are in it
//     (a 4-byte int is 3.5ms; 256 bytes is 5-7ms) -- so the thing to minimise is CALLS,
//     not bytes, and packing several small fields into one key is worth far more than
//     shrinking any one of them.
//   - each key caps at PNX_PERSIST_KEY_BYTES. A save larger than one key has no choice
//     but to spend more calls, which is where pnx_save's chunking and its one-call-per-
//     frame writer both come from -- see pnx_save.h.
//
// Reads are ~100x cheaper (~70us) and are not spread across frames.

#define PNX_PERSIST_KEY_BYTES 256

// Reads up to `bytes` into `dst`. `*out_bytes` is what was actually stored under the key
// (which may be less than `bytes`, or the call fails and leaves it at 0). False if the key
// has never been written.
bool pnx_platform_persist_read(uint32_t key, void* dst, size_t bytes, size_t* out_bytes);

// Overwrites the key wholesale -- there is no partial or appending write. `bytes` must not
// exceed PNX_PERSIST_KEY_BYTES; the platform truncates rather than failing, because a save
// format that stays inside the limit should never learn from a runtime error that it did
// not.
bool pnx_platform_persist_write(uint32_t key, const void* data, size_t bytes);

bool pnx_platform_persist_delete(uint32_t key);
bool pnx_platform_persist_exists(uint32_t key);

// -------------------------------------------------------------------------- text
//
// Text is the one thing worth borrowing from the SDK rather than reimplementing: it has
// the font data and the layout engine, and a bitmap-font pipeline would cost resource
// budget to reproduce what is already there. The hook is deliberately narrow -- a string
// and a box -- so that replacing it later with our own glyph atlas changes one file.
//
// Measured at ~4.3 ms per draw, 12% of the frame budget, so it belongs in dialog and
// menus rather than anywhere per-entity.

#if PNX_USE_SDK_TEXT

typedef enum
{
	PNX_TEXT_SMALL = 0,
	PNX_TEXT_MEDIUM,
	PNX_TEXT_LARGE,
} PnxTextSize;

// Valid only inside a frame callback: on device it needs the graphics context that only
// exists during the layer update, which is the same reason the target does.
//
// The ESCAPE HATCH, not the text path -- see PNX_USE_SDK_TEXT in pnx_config.h. Games with
// fonts should build with it off, which removes this declaration and turns any remaining
// call into a compile error rather than a silently expensive draw.
void pnx_platform_text_draw(const char* text, PnxTextSize size, uint8_t colour, int32_t x,
							int32_t y, int16_t w, int16_t h);

#endif	// PNX_USE_SDK_TEXT

// ------------------------------------------------------------------------- input

typedef enum
{
	PNX_BUTTON_BACK = 0,
	PNX_BUTTON_UP,
	PNX_BUTTON_SELECT,
	PNX_BUTTON_DOWN,
	PNX_BUTTON_COUNT
} PnxButton;

typedef enum
{
	PNX_EVENT_NONE = 0,
	PNX_EVENT_BUTTON_DOWN,
	PNX_EVENT_BUTTON_UP,
	PNX_EVENT_TOUCH_DOWN,
	PNX_EVENT_TOUCH_MOVE,
	PNX_EVENT_TOUCH_UP,
	PNX_EVENT_FOCUS_LOST,  // fires ~297ms before the app is fully covered
	PNX_EVENT_FOCUS_GAINED,
} PnxEventType;

typedef struct
{
	PnxEventType type;
	uint32_t time_ms;  // stamped at delivery: the earliest observable moment, and what
					   // any timing judgement must use
	int16_t x, y;	   // touch only
	uint8_t button;	   // button only
} PnxEvent;

// Pops the next queued event. False when empty. Events are queued rather than
// delivered as callbacks so the game reads input at a defined point in the frame
// rather than re-entrantly.
bool pnx_platform_poll_event(PnxEvent* out);

bool pnx_platform_has_touch(void);

// ------------------------------------------------------------------- screen lock
//
// Two things, and a game in the middle of a session wants both:
//
//   BACK stops dismissing the app. The system's default is to exit, which off the wrist
//   is one misplaced finger between a player and their session. While locked, BACK still
//   arrives as an ordinary button event -- a game can use it -- but it no longer ends the
//   app. Unlocked, it exits exactly as it always did.
//
//   The backlight is held on. A dim room and a screen that goes dark mid-turn reads as a
//   crash, and the usual remedy -- flick your wrist -- is not available to hands holding
//   the watch like a gamepad.
//
// Left off by default: an app that never unlocks is an app the player has to force-quit,
// and that is a decision for the game, at the moments it knows it is mid-play.
void pnx_platform_set_screen_lock(bool locked);
bool pnx_platform_screen_locked(void);

// -------------------------------------------------------------------- frame loop

// Called once per rendered frame. `elapsed_ms` is measured, not assumed: the display
// paces at ~37.33ms but jitters, and while covered the app is throttled to ~0.4fps, so
// a frame can arrive carrying seconds. Clamp before feeding a fixed-timestep sim.
typedef void (*PnxFrameFn)(void* ctx, uint32_t elapsed_ms, PnxTarget* target);

// Called after the frame's pixels, once the framebuffer has been RELEASED.
//
// Holding the framebuffer captured blocks the compositor, and the SDK will not draw text
// while it is held. Audio belongs here for the same reason: feeding the speaker stream
// from inside the capture window was measurably audible as periodic blips on a single
// sustained tone, with the mixer's own output proven clean and no underrun reported.
// Anything that talks to the system rather than to pixels goes here.
typedef void (*PnxPostFrameFn)(void* ctx);
void pnx_platform_set_post_frame_fn(PnxPostFrameFn fn);

// Sets up the window and runs until the app exits. Returns on exit.
void pnx_platform_run(PnxFrameFn frame, void* ctx);

// Requests exit at the next opportunity.
void pnx_platform_quit(void);