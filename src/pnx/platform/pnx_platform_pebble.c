// Pebble implementation of the platform seam.
//
// This is the ONLY file in the framework permitted to include <pebble.h>.
//
// Inert on a host build, mirroring the guard in pnx_platform_host.c: exactly one
// implementation is live in any given build.

#ifndef PNX_PLATFORM_HOST

#include "pnx_platform.h"
#include "../pnx_config.h"

#include <pebble.h>
#include <string.h>

// The frame loop is a self-rearming 1ms timer that marks the canvas dirty, which is the
// same shape the probes used. It cannot beat the display: PebbleOS gates rendering on
// framebuffer_render_pending, so the real pace is ~37.33ms no matter what is requested.
// Asking for 1ms simply means "as soon as allowed".
#define FRAME_TIMER_MS 1

#define EVENT_QUEUE_LEN 16

struct PnxTarget {
  GBitmap *fb;
  int16_t w, h;
};

static Window *s_window;
static GContext *s_ctx;   // valid only during update_proc

static Layer *s_canvas;
static AppTimer *s_timer;

static PnxFrameFn s_frame_fn;
static PnxPostFrameFn s_post_fn;
static void *s_frame_ctx;
static uint32_t s_last_frame_ms;
static bool s_quit;

static PnxEvent s_events[EVENT_QUEUE_LEN];
static uint8_t s_ev_head, s_ev_count;

// ------------------------------------------------------------------------- time

uint32_t pnx_platform_now_ms(void) {
  time_t seconds = 0;
  uint16_t ms = time_ms(&seconds, NULL);
  return (uint32_t)seconds * 1000u + (uint32_t)ms;
}

void pnx_platform_log(const char *message) {
  // WARNING rather than INFO: this device filters INFO-level app logs out entirely,
  // so an INFO message is indistinguishable from no message.
  APP_LOG(APP_LOG_LEVEL_WARNING, "%s", message);
}

// ------------------------------------------------------------------------ target

int16_t pnx_target_width(const PnxTarget *t) { return t ? t->w : 0; }
int16_t pnx_target_height(const PnxTarget *t) { return t ? t->h : 0; }

PnxRow pnx_target_row(PnxTarget *t, int16_t y) {
  PnxRow row = {0};
  if (!t || !t->fb || y < 0 || y >= t->h) return row;

  const GBitmapDataRowInfo info = gbitmap_get_data_row_info(t->fb, (uint16_t)y);
  row.data = info.data;
  row.min_x = info.min_x;
  row.max_x = info.max_x;
  return row;
}

// --------------------------------------------------------------------- resources

bool pnx_platform_resource_size(uint32_t resource_id, size_t *out_size) {
  ResHandle handle = resource_get_handle(resource_id);
  if (!handle) return false;
  const size_t size = resource_size(handle);
  if (out_size) *out_size = size;
  return size > 0;
}

size_t pnx_platform_resource_read(uint32_t resource_id, size_t offset,
                                  void *dst, size_t bytes) {
  ResHandle handle = resource_get_handle(resource_id);
  if (!handle || !dst) return 0;
  return resource_load_byte_range(handle, (uint32_t)offset, (uint8_t *)dst, bytes);
}

// ------------------------------------------------------------------------- audio

static bool s_audio_open;

bool pnx_platform_audio_open(PnxAudioFormat format, uint8_t volume) {
  static const SpeakerPcmFormat map[] = {
    SpeakerPcmFormat_16kHz_8bit, SpeakerPcmFormat_16kHz_16bit,
    SpeakerPcmFormat_8kHz_8bit,  SpeakerPcmFormat_8kHz_16bit,
  };
  if (s_audio_open) return true;
  s_audio_open = speaker_stream_open(map[format], volume);
  return s_audio_open;
}

size_t pnx_platform_audio_write(const void *data, size_t bytes) {
  if (!s_audio_open) return 0;
  return speaker_stream_write(data, (uint32_t)bytes);
}

void pnx_platform_audio_close(void) {
  if (!s_audio_open) return;
  speaker_stream_close();
  s_audio_open = false;
}

bool pnx_platform_audio_is_open(void) { return s_audio_open; }

PnxAudioState pnx_platform_audio_state(void) {
  if (!s_audio_open) return PNX_AUDIO_IDLE;
  switch (speaker_get_status()) {
    case SpeakerStatusIdle:     return PNX_AUDIO_IDLE;
    case SpeakerStatusPlaying:  return PNX_AUDIO_PLAYING;
    case SpeakerStatusDraining: return PNX_AUDIO_DRAINING;
    default:                    return PNX_AUDIO_UNKNOWN;
  }
}

// -------------------------------------------------------------------------- text

void pnx_platform_text_draw(const char *text, PnxTextSize size, uint8_t colour,
                            int32_t x, int32_t y, int16_t w, int16_t h) {
  // The framebuffer must be released before the SDK will draw into it, so text is
  // deferred by the caller to after the frame's own blitting -- see the note in
  // update_proc.
  if (!s_ctx || !text) return;

  const char *font_key = FONT_KEY_GOTHIC_14;
  if (size == PNX_TEXT_MEDIUM) font_key = FONT_KEY_GOTHIC_18;
  else if (size == PNX_TEXT_LARGE) font_key = FONT_KEY_GOTHIC_24_BOLD;

  graphics_context_set_text_color(s_ctx, (GColor8){ .argb = colour });
  graphics_draw_text(s_ctx, text, fonts_get_system_font(font_key),
                     GRect((int16_t)x, (int16_t)y, w, h),
                     GTextOverflowModeWordWrap, GTextAlignmentLeft, NULL);
}

// ------------------------------------------------------------------------- input

// Dropping the newest event when full is deliberate: the queue only overflows if the
// game stopped polling, and in that case the oldest events are the ones that still
// describe a coherent gesture.
static void push_event(PnxEventType type, int16_t x, int16_t y, uint8_t button) {
  if (s_ev_count >= EVENT_QUEUE_LEN) return;

  const uint8_t slot = (uint8_t)((s_ev_head + s_ev_count) % EVENT_QUEUE_LEN);
  s_events[slot] = (PnxEvent){
    .type = type,
    .time_ms = pnx_platform_now_ms(),
    .x = x, .y = y,
    .button = button,
  };
  s_ev_count++;
}

bool pnx_platform_poll_event(PnxEvent *out) {
  if (s_ev_count == 0) return false;
  *out = s_events[s_ev_head];
  s_ev_head = (uint8_t)((s_ev_head + 1) % EVENT_QUEUE_LEN);
  s_ev_count--;
  return true;
}

bool pnx_platform_has_touch(void) {
  return touch_service_is_enabled();
}

static uint8_t map_button(ButtonId id) {
  switch (id) {
    case BUTTON_ID_UP:     return PNX_BUTTON_UP;
    case BUTTON_ID_SELECT: return PNX_BUTTON_SELECT;
    case BUTTON_ID_DOWN:   return PNX_BUTTON_DOWN;
    default:               return PNX_BUTTON_BACK;
  }
}

static void raw_down(ClickRecognizerRef r, void *c) {
  push_event(PNX_EVENT_BUTTON_DOWN, 0, 0, map_button(click_recognizer_get_button_id(r)));
}

static void raw_up(ClickRecognizerRef r, void *c) {
  push_event(PNX_EVENT_BUTTON_UP, 0, 0, map_button(click_recognizer_get_button_id(r)));
}

static void click_config(void *context) {
  // Raw press/release only. Interpretation -- clicks, holds, double-taps -- belongs to
  // the input module, not here, so a game can define its own gestures.
  window_raw_click_subscribe(BUTTON_ID_UP, raw_down, raw_up, NULL);
  window_raw_click_subscribe(BUTTON_ID_SELECT, raw_down, raw_up, NULL);
  window_raw_click_subscribe(BUTTON_ID_DOWN, raw_down, raw_up, NULL);
}

static void touch_handler(const TouchEvent *event, void *context) {
  switch (event->type) {
    case TouchEvent_Touchdown:
      push_event(PNX_EVENT_TOUCH_DOWN, event->x, event->y, 0);
      break;
    case TouchEvent_PositionUpdate:
      push_event(PNX_EVENT_TOUCH_MOVE, event->x, event->y, 0);
      break;
    case TouchEvent_Liftoff:
    default:
      push_event(PNX_EVENT_TOUCH_UP, event->x, event->y, 0);
      break;
  }
}

// will_focus fires ~297ms before the app is fully covered, which is enough to persist a
// 4KB save (measured at 106ms). That warning is the reason save-on-blur works.
static void will_focus(bool in_focus) {
  push_event(in_focus ? PNX_EVENT_FOCUS_GAINED : PNX_EVENT_FOCUS_LOST, 0, 0, 0);
}

// -------------------------------------------------------------------- frame loop

static void frame_timer(void *ctx);

static void update_proc(Layer *layer, GContext *ctx) {
  s_ctx = ctx;
  const uint32_t now = pnx_platform_now_ms();
  const uint32_t elapsed = s_last_frame_ms ? (now - s_last_frame_ms) : PNX_TICK_MS;
  s_last_frame_ms = now;

  GBitmap *fb = graphics_capture_frame_buffer(ctx);
  if (fb) {
    const GRect bounds = layer_get_bounds(layer);
    PnxTarget target = { .fb = fb, .w = bounds.size.w, .h = bounds.size.h };

    if (s_frame_fn) s_frame_fn(s_frame_ctx, elapsed, &target);

    graphics_release_frame_buffer(ctx, fb);

    // Everything that talks to the system rather than to pixels happens here, after the
    // release: text, because the SDK will not draw while the framebuffer is captured, and
    // audio, because feeding the speaker inside the capture window is audible.
    if (s_post_fn) s_post_fn(s_frame_ctx);
  }

  if (s_quit) {
    window_stack_pop_all(false);
    return;
  }

  // Re-armed after drawing, not before: the next frame should only be scheduled once
  // this one has actually been rendered.
  s_timer = app_timer_register(FRAME_TIMER_MS, frame_timer, NULL);
}

static void frame_timer(void *ctx) {
  s_timer = NULL;
  if (s_canvas) layer_mark_dirty(s_canvas);
}

static void window_load(Window *window) {
  Layer *root = window_get_root_layer(window);
  s_canvas = layer_create(layer_get_bounds(root));
  layer_set_update_proc(s_canvas, update_proc);
  layer_add_child(root, s_canvas);

  s_last_frame_ms = 0;
  s_timer = app_timer_register(FRAME_TIMER_MS, frame_timer, NULL);
}

static void window_unload(Window *window) {
  if (s_timer) { app_timer_cancel(s_timer); s_timer = NULL; }
  layer_destroy(s_canvas);
  s_canvas = NULL;
}

void pnx_platform_set_post_frame_fn(PnxPostFrameFn fn) { s_post_fn = fn; }

void pnx_platform_run(PnxFrameFn frame, void *ctx) {
  s_frame_fn = frame;
  s_frame_ctx = ctx;
  s_quit = false;

  s_window = window_create();
  window_set_background_color(s_window, GColorBlack);
  window_set_click_config_provider(s_window, click_config);
  window_set_window_handlers(s_window, (WindowHandlers){
    .load = window_load, .unload = window_unload,
  });

  if (touch_service_is_enabled()) {
    touch_service_subscribe(touch_handler, NULL);
  }
  app_focus_service_subscribe(will_focus);

  window_stack_push(s_window, true);
  app_event_loop();

  app_focus_service_unsubscribe();
  touch_service_unsubscribe();
  window_destroy(s_window);
  s_window = NULL;
}

void pnx_platform_quit(void) {
  s_quit = true;
}

#endif  // !PNX_PLATFORM_HOST
