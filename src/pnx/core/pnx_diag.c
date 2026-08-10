#include "pnx_diag.h"

#if PNX_USE_DIAGNOSTICS

#include "../platform/pnx_platform.h"
#include "pnx_fmt.h"

#include <stdarg.h>
#include <string.h>

// Sized so a normal startup fits without wrapping. If a game logs more than this before
// flushing, the oldest lines are dropped and the flush says so -- silently losing them
// would repeat exactly the bug this module exists to prevent.
//
// This ring is the single largest static allocation in an otherwise empty app, so both
// dimensions are overridable in pnx_config.h.
#ifndef PNX_LOG_LINES
#define PNX_LOG_LINES 24
#endif
#ifndef PNX_LOG_LINE_LEN
#define PNX_LOG_LINE_LEN 96
#endif

#define LOG_LINES PNX_LOG_LINES
#define LOG_LINE_LEN PNX_LOG_LINE_LEN

static char s_lines[LOG_LINES][LOG_LINE_LEN];
static uint8_t s_head;
static uint8_t s_count;
static uint32_t s_dropped;
static bool s_flushed;

void pnx_log(const char *fmt, ...) {
  char buf[LOG_LINE_LEN];

  va_list ap;
  va_start(ap, fmt);
  pnx_vformat(buf, sizeof(buf), fmt, ap);
  va_end(ap);

  if (s_flushed) {
    pnx_platform_log(buf);
    return;
  }

  if (s_count == LOG_LINES) {
    s_dropped++;
    s_head = (uint8_t)((s_head + 1) % LOG_LINES);
    s_count--;
  }
  const uint8_t slot = (uint8_t)((s_head + s_count) % LOG_LINES);
  memcpy(s_lines[slot], buf, sizeof(buf));
  s_count++;
}

void pnx_diag_flush(void) {
  if (s_flushed) return;

  pnx_platform_log("--- buffered startup log ---");
  for (uint8_t i = 0; i < s_count; i++) {
    pnx_platform_log(s_lines[(s_head + i) % LOG_LINES]);
  }
  if (s_dropped) {
    char note[64];
    pnx_format(note, sizeof(note), "--- %u earlier lines dropped ---",
               (unsigned)s_dropped);
    pnx_platform_log(note);
  }
  pnx_platform_log("--- end buffered log ---");

  s_count = 0;
  s_head = 0;
  s_flushed = true;
}

bool pnx_diag_flushed(void) {
  return s_flushed;
}

// ------------------------------------------------------------------ frame stats

#define STATS_WINDOW_MS 1000

static PnxFrameStats s_stats;
static uint32_t s_window_ms;
static uint32_t s_window_frames;
static uint32_t s_window_work_ms;
static uint32_t s_window_worst_ms;

void pnx_diag_frame(uint32_t frame_ms, uint32_t work_ms) {
  s_window_ms += frame_ms;
  s_window_frames++;
  s_window_work_ms += work_ms;
  if (work_ms > s_window_worst_ms) s_window_worst_ms = work_ms;

  if (s_window_ms < STATS_WINDOW_MS) return;

  // Averaged over a window rather than per frame: the clock has 1ms resolution, so a
  // single frame's work is mostly quantisation noise.
  s_stats.fps_x10 = (s_window_frames * 10000u) / s_window_ms;
  s_stats.frame_us = (s_window_ms * 1000u) / s_window_frames;
  s_stats.work_us = (s_window_work_ms * 1000u) / s_window_frames;
  s_stats.worst_us = s_window_worst_ms * 1000u;
  s_stats.frames = s_window_frames;

  s_window_ms = 0;
  s_window_frames = 0;
  s_window_work_ms = 0;
  s_window_worst_ms = 0;
}

const PnxFrameStats *pnx_diag_stats(void) {
  return &s_stats;
}

#endif  // PNX_USE_DIAGNOSTICS