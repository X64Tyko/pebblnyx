// Diagnostics: deferred logging and frame statistics.
//
// The deferral is not a nicety. `pebble install --logs` attaches its stream AFTER the
// app has started, so everything logged during init is silently discarded -- no error,
// no partial output. That cost time three separate times before it was understood, and
// it presents as a log-level problem, which sends you the wrong way.
//
// So pnx_log() buffers into a ring until pnx_diag_flush() is called, which a game does
// from a button handler or a few seconds into the first scene. Messages logged early
// survive instead of vanishing.
//
// Compiled out entirely when PNX_USE_DIAGNOSTICS is 0.

#pragma once

#include "../pnx_config.h"

#include <stdint.h>
#include <stdbool.h>

#if PNX_USE_DIAGNOSTICS

// Buffers a message. Safe before the log stream exists.
void pnx_log(const char* fmt, ...);

// Emits everything buffered, then switches to pass-through so later calls go straight
// out. Call it from a button handler, or on a timer a few seconds after launch.
void pnx_diag_flush(void);

// True once flushed; useful for a "press UP for diagnostics" hint.
bool pnx_diag_flushed(void);

// Per-frame accounting. Call once per frame with the time spent inside the frame
// callback; the rest of the period is idle wait we do not control.
void pnx_diag_frame(uint32_t frame_ms, uint32_t work_ms);

typedef struct
{
	uint32_t fps_x10;	// frames per second, times ten
	uint32_t frame_us;	// mean whole-frame period
	uint32_t work_us;	// mean time inside our callback
	uint32_t worst_us;	// worst callback in the current window
	uint32_t frames;	// frames in the completed window
} PnxFrameStats;

const PnxFrameStats* pnx_diag_stats(void);

#else

// Zero-cost when disabled: the calls vanish and the format strings never reach .rodata.
#define pnx_log(...)		 ((void)0)
#define pnx_diag_flush()	 ((void)0)
#define pnx_diag_flushed()	 (false)
#define pnx_diag_frame(a, b) ((void)0)
#define pnx_diag_stats()	 (NULL)

#endif	// PNX_USE_DIAGNOSTICS