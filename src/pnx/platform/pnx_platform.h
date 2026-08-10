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

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

// ------------------------------------------------------------------------- time

// Milliseconds since an arbitrary epoch. Resolution is 1ms on device; anything shorter
// must be measured by repetition, not by a single call.
uint32_t pnx_platform_now_ms(void);

// ------------------------------------------------------------------------ output

void pnx_platform_log(const char *message);

// ---------------------------------------------------------------- render target
//
// Row-based rather than a flat pointer, because the device's framebuffer is accessed
// per row and may report a narrower valid span on some displays. A flat abstraction
// would be a lie on hardware and would break on a round screen.

typedef struct PnxTarget PnxTarget;

typedef struct {
  uint8_t *data;      // byte at column 0 of this row; index it with x directly
  int16_t min_x;      // first valid column
  int16_t max_x;      // last valid column, inclusive
} PnxRow;

int16_t pnx_target_width(const PnxTarget *t);
int16_t pnx_target_height(const PnxTarget *t);
PnxRow pnx_target_row(PnxTarget *t, int16_t y);

// ---------------------------------------------------------------------- resources
//
// Packed asset blobs live in Pebble resources. Reads cost ~29 us per CALL plus about
// 33 MB/s of transfer, and locality does not matter -- a scattered read is no worse
// than a sequential one. The consequence shapes the whole asset layer: batch into few
// large reads and hold them resident, never stream per tile. Reading one 16x16 tile at
// a time would cost ~6.7 ms/frame to save memory that is not scarce.

// False if the id is unknown. Size is the whole resource.
bool pnx_platform_resource_size(uint32_t resource_id, size_t *out_size);

// Reads a byte range into dst, returning bytes actually read (0 on failure). One call
// per asset blob is the intended usage.
size_t pnx_platform_resource_read(uint32_t resource_id, size_t offset,
                                  void *dst, size_t bytes);

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

typedef enum {
  PNX_AUDIO_16KHZ_16BIT = 0,   // 32 KB/s; the default, enough headroom to mix into
  PNX_AUDIO_16KHZ_8BIT,        // 16 KB/s; half the writes, audibly noisier
  PNX_AUDIO_8KHZ_16BIT,
  PNX_AUDIO_8KHZ_8BIT,
} PnxAudioFormat;

bool pnx_platform_audio_open(PnxAudioFormat format, uint8_t volume);
size_t pnx_platform_audio_write(const void *data, size_t bytes);
void pnx_platform_audio_close(void);
bool pnx_platform_audio_is_open(void);

// Bytes per second for a format, so the mixer can size a frame's worth of samples.
static inline uint32_t pnx_audio_byte_rate(PnxAudioFormat f) {
  const uint32_t rate = (f == PNX_AUDIO_16KHZ_16BIT || f == PNX_AUDIO_16KHZ_8BIT)
                        ? 16000u : 8000u;
  const uint32_t width = (f == PNX_AUDIO_16KHZ_16BIT || f == PNX_AUDIO_8KHZ_16BIT)
                         ? 2u : 1u;
  return rate * width;
}

// -------------------------------------------------------------------------- text
//
// Text is the one thing worth borrowing from the SDK rather than reimplementing: it has
// the font data and the layout engine, and a bitmap-font pipeline would cost resource
// budget to reproduce what is already there. The hook is deliberately narrow -- a string
// and a box -- so that replacing it later with our own glyph atlas changes one file.
//
// Measured at ~4.3 ms per draw, 12% of the frame budget, so it belongs in dialog and
// menus rather than anywhere per-entity.

typedef enum {
  PNX_TEXT_SMALL = 0,
  PNX_TEXT_MEDIUM,
  PNX_TEXT_LARGE,
} PnxTextSize;

// Valid only inside a frame callback: on device it needs the graphics context that only
// exists during the layer update, which is the same reason the target does.
void pnx_platform_text_draw(const char *text, PnxTextSize size, uint8_t colour,
                            int32_t x, int32_t y, int16_t w, int16_t h);

// ------------------------------------------------------------------------- input

typedef enum {
  PNX_BUTTON_BACK = 0,
  PNX_BUTTON_UP,
  PNX_BUTTON_SELECT,
  PNX_BUTTON_DOWN,
  PNX_BUTTON_COUNT
} PnxButton;

typedef enum {
  PNX_EVENT_NONE = 0,
  PNX_EVENT_BUTTON_DOWN,
  PNX_EVENT_BUTTON_UP,
  PNX_EVENT_TOUCH_DOWN,
  PNX_EVENT_TOUCH_MOVE,
  PNX_EVENT_TOUCH_UP,
  PNX_EVENT_FOCUS_LOST,     // fires ~297ms before the app is fully covered
  PNX_EVENT_FOCUS_GAINED,
} PnxEventType;

typedef struct {
  PnxEventType type;
  uint32_t time_ms;   // stamped at delivery: the earliest observable moment, and what
                      // any timing judgement must use
  int16_t x, y;       // touch only
  uint8_t button;     // button only
} PnxEvent;

// Pops the next queued event. False when empty. Events are queued rather than
// delivered as callbacks so the game reads input at a defined point in the frame
// rather than re-entrantly.
bool pnx_platform_poll_event(PnxEvent *out);

bool pnx_platform_has_touch(void);

// -------------------------------------------------------------------- frame loop

// Called once per rendered frame. `elapsed_ms` is measured, not assumed: the display
// paces at ~37.33ms but jitters, and while covered the app is throttled to ~0.4fps, so
// a frame can arrive carrying seconds. Clamp before feeding a fixed-timestep sim.
typedef void (*PnxFrameFn)(void *ctx, uint32_t elapsed_ms, PnxTarget *target);

// Called after the frame's pixels, once the framebuffer has been released -- the SDK
// refuses to draw text while it is captured. Optional.
typedef void (*PnxTextFn)(void *ctx);
void pnx_platform_set_text_fn(PnxTextFn fn);

// Sets up the window and runs until the app exits. Returns on exit.
void pnx_platform_run(PnxFrameFn frame, void *ctx);

// Requests exit at the next opportunity.
void pnx_platform_quit(void);