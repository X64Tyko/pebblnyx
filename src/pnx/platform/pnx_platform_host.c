// Host implementation of the platform seam.
//
// Exists so core, gfx, audio and save can be compiled and tested with a normal
// compiler and a normal debugger, instead of only on a watch behind a Bluetooth log
// stream. It is not an emulator and makes no attempt to be: timing here is real
// wall-clock, not the device's 37.33ms pace, and nothing about performance measured on
// a host means anything for the device.
//
// The render target is a flat buffer, so blitter code can be tested by asserting on
// pixels rather than by looking at a watch.

// Compiles to nothing unless the build asks for the host platform. Without this the
// device build's source glob picks the file up and it collides with the Pebble
// implementation, which defines the same symbols.
#ifdef PNX_PLATFORM_HOST

// clock_gettime is POSIX, not ISO C, and -std=c11 hides it without this.
#define _POSIX_C_SOURCE 200809L

#include "pnx_platform.h"
#include "pnx_platform_host.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifndef PNX_HOST_WIDTH
#define PNX_HOST_WIDTH 200
#endif
#ifndef PNX_HOST_HEIGHT
#define PNX_HOST_HEIGHT 228
#endif

struct PnxTarget {
  uint8_t *pixels;
  int16_t w, h;
};

static uint8_t s_pixels[PNX_HOST_WIDTH * PNX_HOST_HEIGHT];
static PnxTarget s_target = { s_pixels, PNX_HOST_WIDTH, PNX_HOST_HEIGHT };

static PnxEvent s_queued[32];
static int s_queued_count;
static int s_queued_read;
static bool s_quit;

uint32_t pnx_platform_now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

void pnx_platform_log(const char *message) {
  printf("%s\n", message);
}

int16_t pnx_target_width(const PnxTarget *t) { return t ? t->w : 0; }
int16_t pnx_target_height(const PnxTarget *t) { return t ? t->h : 0; }

PnxRow pnx_target_row(PnxTarget *t, int16_t y) {
  PnxRow row = {0};
  if (!t || y < 0 || y >= t->h) return row;
  row.data = t->pixels + (size_t)y * t->w;
  row.min_x = 0;
  row.max_x = (int16_t)(t->w - 1);
  return row;
}

// --------------------------------------------------------------------- resources
//
// File-backed, so host tests exercise the SAME parsing code against the SAME blobs the
// device loads. A mock returning synthetic bytes would test the mock.

#define MAX_RESOURCES 32

typedef struct {
  uint32_t id;
  char path[256];
} HostResource;

static HostResource s_resources[MAX_RESOURCES];
static int s_resource_count;

void pnx_host_register_resource(uint32_t resource_id, const char *path) {
  if (s_resource_count >= MAX_RESOURCES || !path) return;
  HostResource *r = &s_resources[s_resource_count++];
  r->id = resource_id;
  strncpy(r->path, path, sizeof(r->path) - 1);
  r->path[sizeof(r->path) - 1] = '\0';
}

static const char *resource_path(uint32_t resource_id) {
  for (int i = 0; i < s_resource_count; i++) {
    if (s_resources[i].id == resource_id) return s_resources[i].path;
  }
  return NULL;
}

bool pnx_platform_resource_size(uint32_t resource_id, size_t *out_size) {
  const char *path = resource_path(resource_id);
  if (!path) return false;

  FILE *f = fopen(path, "rb");
  if (!f) return false;

  fseek(f, 0, SEEK_END);
  const long size = ftell(f);
  fclose(f);

  if (size <= 0) return false;
  if (out_size) *out_size = (size_t)size;
  return true;
}

size_t pnx_platform_resource_read(uint32_t resource_id, size_t offset,
                                  void *dst, size_t bytes) {
  const char *path = resource_path(resource_id);
  if (!path || !dst) return 0;

  FILE *f = fopen(path, "rb");
  if (!f) return 0;

  if (fseek(f, (long)offset, SEEK_SET) != 0) { fclose(f); return 0; }
  const size_t got = fread(dst, 1, bytes, f);
  fclose(f);
  return got;
}

// ------------------------------------------------------------------------- input

bool pnx_platform_poll_event(PnxEvent *out) {
  if (s_queued_read >= s_queued_count) return false;
  *out = s_queued[s_queued_read++];
  return true;
}

bool pnx_platform_has_touch(void) { return true; }

void pnx_platform_run(PnxFrameFn frame, void *ctx) {
  // Runs a bounded number of frames rather than forever, so a test that forgets to
  // quit fails fast instead of hanging a CI job.
  s_quit = false;
  uint32_t last = pnx_platform_now_ms();

  for (int i = 0; i < 1000 && !s_quit; i++) {
    const uint32_t now = pnx_platform_now_ms();
    const uint32_t elapsed = now - last;
    last = now;
    if (frame) frame(ctx, elapsed ? elapsed : 1, &s_target);
  }
}

void pnx_platform_quit(void) { s_quit = true; }

// No text rendering on the host: it exists to test logic, and the device's font metrics
// are not reproducible here anyway. Recorded so a test can assert a call happened.
static char s_last_text[128];

void pnx_platform_text_draw(const char *text, PnxTextSize size, uint8_t colour,
                            int32_t x, int32_t y, int16_t w, int16_t h) {
  if (!text) return;
  strncpy(s_last_text, text, sizeof(s_last_text) - 1);
  s_last_text[sizeof(s_last_text) - 1] = '\0';
}

const char *pnx_host_last_text(void) { return s_last_text; }

void pnx_platform_set_post_frame_fn(PnxPostFrameFn fn) { (void)fn; }

// ------------------------------------------------------------------------- audio
//
// Accepts everything and keeps the last buffer, so mixer output can be asserted on
// without a speaker. A host that dropped samples would hide exactly the underruns the
// mixer exists to avoid.

static bool s_audio_open;
static uint8_t s_audio_last[4096];
static size_t s_audio_last_bytes;
static uint32_t s_audio_total;

bool pnx_platform_audio_open(PnxAudioFormat format, uint8_t volume) {
  (void)format; (void)volume;
  s_audio_open = true;
  s_audio_total = 0;
  return true;
}

size_t pnx_platform_audio_write(const void *data, size_t bytes) {
  if (!s_audio_open) return 0;
  const size_t keep = bytes < sizeof(s_audio_last) ? bytes : sizeof(s_audio_last);
  memcpy(s_audio_last, data, keep);
  s_audio_last_bytes = keep;
  s_audio_total += (uint32_t)bytes;
  return bytes;
}

void pnx_platform_audio_close(void) { s_audio_open = false; }
bool pnx_platform_audio_is_open(void) { return s_audio_open; }

const void *pnx_host_audio_last(size_t *bytes) {
  if (bytes) *bytes = s_audio_last_bytes;
  return s_audio_last;
}
uint32_t pnx_host_audio_total(void) { return s_audio_total; }

// ------------------------------------------------------- test-only entry points

void pnx_host_queue_event(PnxEvent ev) {
  if (s_queued_count < (int)(sizeof(s_queued) / sizeof(s_queued[0]))) {
    s_queued[s_queued_count++] = ev;
  }
}

void pnx_host_reset(void) {
  s_resource_count = 0;
  s_queued_count = 0;
  s_queued_read = 0;
  s_quit = false;
  memset(s_pixels, 0, sizeof(s_pixels));
}

PnxTarget *pnx_host_target(void) { return &s_target; }

#endif  // PNX_PLATFORM_HOST
