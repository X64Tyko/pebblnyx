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

// The frame loop is a self-rearming timer that marks the canvas dirty, on a FIXED grid --
// PNX_FRAME_MS apart, measured from the start of the previous frame, not from when it
// finished. It used to just ask again as soon as possible, which sounds free and is not:
// PebbleOS gates rendering on framebuffer_render_pending at a real pace of ~37.33ms no
// matter what is requested, so "ask immediately" converges on exactly that ceiling with
// nothing held back -- and a frame that runs a millisecond long then arrives a millisecond
// late, one for one, because there was no slack to absorb it into.
//
// PNX_FRAME_MS (40, 25fps) is deliberately looser than the ~37.33ms ceiling. The ~2.67ms
// between them is banked as slack on every frame, so the frame that runs long can still
// land on schedule. See PNX_FRAME_MS in pnx_config.h and docs/MEASUREMENTS.md.
//
// It used to also flood the loop at 1ms between marks, on the reasoning that "as soon as
// allowed" costs nothing. It is not free: at 1ms this timer starves every other timer in
// the app's event loop. A 10ms audio timer measured at 54-59ms on device for exactly that
// reason, which held the stream lead high and made effects late -- part of why the audio
// timer runs on its own app_timer now rather than off this one.
#define FRAME_PERIOD_MS PNX_FRAME_MS

#define EVENT_QUEUE_LEN 16

struct PnxTarget
{
	GBitmap* fb;
	int16_t w, h;
};

static Window* s_window;
static GContext* s_ctx; // valid only during update_proc

static Layer* s_canvas;
static AppTimer* s_timer;

static PnxFrameFn s_frame_fn;
static PnxPostFrameFn s_post_fn;
static void* s_frame_ctx;
static uint32_t s_last_frame_ms;
static uint32_t s_frame_due_ms; // wall clock the NEXT frame is scheduled for; the grid
static bool s_quit;
static bool s_screen_locked;

static PnxEvent s_events[EVENT_QUEUE_LEN];
static uint8_t s_ev_head, s_ev_count;

// ------------------------------------------------------------------------- time

uint32_t pnx_platform_now_ms(void)
{
	time_t seconds = 0;
	uint16_t ms	   = time_ms(&seconds, NULL);
	return (uint32_t)seconds * 1000u + (uint32_t)ms;
}

void pnx_platform_log(const char* message)
{
	// WARNING rather than INFO: this device filters INFO-level app logs out entirely,
	// so an INFO message is indistinguishable from no message.
	APP_LOG(APP_LOG_LEVEL_WARNING, "%s", message);
}

// ------------------------------------------------------------------------ target

int16_t pnx_target_width(const PnxTarget* t)
{
	return t ? t->w : 0;
}
int16_t pnx_target_height(const PnxTarget* t)
{
	return t ? t->h : 0;
}

PnxRow pnx_target_row(PnxTarget* t, int16_t y)
{
	PnxRow row = { 0 };
	if (!t || !t->fb || y < 0 || y >= t->h)
		return row;

	const GBitmapDataRowInfo info = gbitmap_get_data_row_info(t->fb, (uint16_t)y);
	row.data					  = info.data;
	row.min_x					  = info.min_x;
	row.max_x					  = info.max_x;
	return row;
}

// --------------------------------------------------------------------- resources

bool pnx_platform_resource_size(uint32_t resource_id, size_t* out_size)
{
	ResHandle handle = resource_get_handle(resource_id);
	if (!handle)
		return false;
	const size_t size = resource_size(handle);
	if (out_size)
		*out_size = size;
	return size > 0;
}

size_t pnx_platform_resource_read(uint32_t resource_id, size_t offset, void* dst, size_t bytes)
{
	ResHandle handle = resource_get_handle(resource_id);
	if (!handle || !dst)
		return 0;
	return resource_load_byte_range(handle, (uint32_t)offset, (uint8_t*)dst, bytes);
}

// ------------------------------------------------------------------------ persist

bool pnx_platform_persist_read(uint32_t key, void* dst, size_t bytes, size_t* out_bytes)
{
	if (!dst)
		return false;
	if (!persist_exists(key))
		return false;
	const int got = persist_read_data(key, dst, bytes);
	if (got < 0)
		return false;
	if (out_bytes)
		*out_bytes = (size_t)got;
	return true;
}

bool pnx_platform_persist_write(uint32_t key, const void* data, size_t bytes)
{
	if (!data)
		return false;
	if (bytes > PNX_PERSIST_KEY_BYTES)
		bytes = PNX_PERSIST_KEY_BYTES;
	return persist_write_data(key, data, bytes) >= 0;
}

bool pnx_platform_persist_delete(uint32_t key)
{
	return persist_delete(key) == S_SUCCESS;
}

bool pnx_platform_persist_exists(uint32_t key)
{
	return persist_exists(key);
}

// ------------------------------------------------------------------------- audio

static bool s_audio_open;

bool pnx_platform_audio_open(PnxAudioFormat format, uint8_t volume)
{
	// Designated initialisers, keyed by our own enum. A positional array silently mapped
	// every format to the wrong one when the enum was reordered in comment but not in code:
	// PNX_AUDIO_8KHZ_8BIT opened the device as 8kHz **16-bit**, so it read our 8-bit samples
	// as 16-bit little-endian and turned every pair into one wrong value. That was the static
	// and the thrum, and no amount of mixer work could have touched it.
	static const SpeakerPcmFormat map[] = {
		[PNX_AUDIO_16KHZ_16BIT] = SpeakerPcmFormat_16kHz_16bit,
		[PNX_AUDIO_16KHZ_8BIT]	= SpeakerPcmFormat_16kHz_8bit,
		[PNX_AUDIO_8KHZ_16BIT]	= SpeakerPcmFormat_8kHz_16bit,
		[PNX_AUDIO_8KHZ_8BIT]	= SpeakerPcmFormat_8kHz_8bit,
	};
	_Static_assert(sizeof(map) / sizeof(map[0]) == PNX_AUDIO_8KHZ_8BIT + 1,
				   "format map must cover every PnxAudioFormat");
	if (s_audio_open)
		return true;
	s_audio_open = speaker_stream_open(map[format], volume);
	return s_audio_open;
}

size_t pnx_platform_audio_write(const void* data, size_t bytes)
{
	if (!s_audio_open)
		return 0;
	return speaker_stream_write(data, (uint32_t)bytes);
}

void pnx_platform_audio_close(void)
{
	if (!s_audio_open)
		return;
	speaker_stream_close();
	s_audio_open = false;
}

bool pnx_platform_audio_is_open(void)
{
	return s_audio_open;
}

static AppTimer* s_audio_timer;
static PnxTickFn s_audio_fn;
static void* s_audio_ctx;
static uint16_t s_audio_interval = 10;

static void audio_tick(void* unused)
{
	s_audio_timer = NULL;
	if (s_audio_fn)
		s_audio_fn(s_audio_ctx);
	// Re-armed after the work, not before, so a slow feed cannot queue timers behind itself.
	s_audio_timer = app_timer_register(s_audio_interval, audio_tick, NULL);
}

void pnx_platform_set_audio_timer(PnxTickFn fn, void* ctx, uint16_t interval_ms)
{
	s_audio_fn		 = fn;
	s_audio_ctx		 = ctx;
	s_audio_interval = interval_ms ? interval_ms : 1;
	if (!s_audio_timer && fn)
	{
		s_audio_timer = app_timer_register(s_audio_interval, audio_tick, NULL);
	}
}

PnxAudioState pnx_platform_audio_state(void)
{
	if (!s_audio_open)
		return PNX_AUDIO_IDLE;
	switch (speaker_get_status())
	{
		case SpeakerStatusIdle:
			return PNX_AUDIO_IDLE;
		case SpeakerStatusPlaying:
			return PNX_AUDIO_PLAYING;
		case SpeakerStatusDraining:
			return PNX_AUDIO_DRAINING;
		default:
			return PNX_AUDIO_UNKNOWN;
	}
}

// -------------------------------------------------------------------------- text

#if PNX_USE_SDK_TEXT

void pnx_platform_text_draw(const char* text, PnxTextSize size, uint8_t colour, int32_t x,
							int32_t y, int16_t w, int16_t h)
{
	// The framebuffer must be released before the SDK will draw into it, so text is
	// deferred by the caller to after the frame's own blitting -- see the note in
	// update_proc.
	if (!s_ctx || !text)
		return;

	const char* font_key = FONT_KEY_GOTHIC_14;
	if (size == PNX_TEXT_MEDIUM)
		font_key = FONT_KEY_GOTHIC_18;
	else if (size == PNX_TEXT_LARGE)
		font_key = FONT_KEY_GOTHIC_24_BOLD;

	graphics_context_set_text_color(s_ctx, (GColor8){ .argb = colour });
	graphics_draw_text(s_ctx, text, fonts_get_system_font(font_key),
					   GRect((int16_t)x, (int16_t)y, w, h), GTextOverflowModeWordWrap,
					   GTextAlignmentLeft, NULL);
}

#endif // PNX_USE_SDK_TEXT

// ------------------------------------------------------------------------- input

// Dropping the newest event when full is deliberate: the queue only overflows if the
// game stopped polling, and in that case the oldest events are the ones that still
// describe a coherent gesture.
static void push_event(PnxEventType type, int16_t x, int16_t y, uint8_t button)
{
	if (s_ev_count >= EVENT_QUEUE_LEN)
		return;

	const uint8_t slot = (uint8_t)((s_ev_head + s_ev_count) % EVENT_QUEUE_LEN);
	s_events[slot]	   = (PnxEvent){
		.type	 = type,
		.time_ms = pnx_platform_now_ms(),
		.x		 = x,
		.y		 = y,
		.button	 = button,
	};
	s_ev_count++;
}

bool pnx_platform_poll_event(PnxEvent* out)
{
	if (s_ev_count == 0)
		return false;
	*out	  = s_events[s_ev_head];
	s_ev_head = (uint8_t)((s_ev_head + 1) % EVENT_QUEUE_LEN);
	s_ev_count--;
	return true;
}

bool pnx_platform_has_touch(void)
{
	return touch_service_is_enabled();
}

static uint8_t map_button(ButtonId id)
{
	switch (id)
	{
		case BUTTON_ID_UP:
			return PNX_BUTTON_UP;
		case BUTTON_ID_SELECT:
			return PNX_BUTTON_SELECT;
		case BUTTON_ID_DOWN:
			return PNX_BUTTON_DOWN;
		default:
			return PNX_BUTTON_BACK;
	}
}

static void raw_down(ClickRecognizerRef r, void* c)
{
	push_event(PNX_EVENT_BUTTON_DOWN, 0, 0, map_button(click_recognizer_get_button_id(r)));
}

static void raw_up(ClickRecognizerRef r, void* c)
{
	push_event(PNX_EVENT_BUTTON_UP, 0, 0, map_button(click_recognizer_get_button_id(r)));
}

// BACK, which the system would otherwise handle for us by dismissing the app.
//
// Subscribing it at all takes that behaviour away, so this handler owes the default back:
// unlocked, a release exits, exactly as it did before. Locked, it does not -- and the
// event still reaches the queue either way, so a game can use BACK as a gesture of its
// own while it holds the lock.
//
// The press is where nothing happens deliberately. Exiting on the release means a game
// that wants to grab BACK on the way down still can.
static void back_up(ClickRecognizerRef r, void* c)
{
	push_event(PNX_EVENT_BUTTON_UP, 0, 0, PNX_BUTTON_BACK);
	if (!s_screen_locked)
		pnx_platform_quit();
}

static void click_config(void* context)
{
	// Raw press/release only. Interpretation -- clicks, holds, double-taps -- belongs to
	// the input module, not here, so a game can define its own gestures.
	window_raw_click_subscribe(BUTTON_ID_UP, raw_down, raw_up, NULL);
	window_raw_click_subscribe(BUTTON_ID_SELECT, raw_down, raw_up, NULL);
	window_raw_click_subscribe(BUTTON_ID_DOWN, raw_down, raw_up, NULL);
	window_raw_click_subscribe(BUTTON_ID_BACK, raw_down, back_up, NULL);
}

static void touch_handler(const TouchEvent* event, void* context)
{
	switch (event->type)
	{
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
static void will_focus(bool in_focus)
{
	push_event(in_focus ? PNX_EVENT_FOCUS_GAINED : PNX_EVENT_FOCUS_LOST, 0, 0, 0);
}

// -------------------------------------------------------------------- frame loop

static void frame_timer(void* ctx);

static void update_proc(Layer* layer, GContext* ctx)
{
	s_ctx				   = ctx;
	const uint32_t now	   = pnx_platform_now_ms();
	const uint32_t elapsed = s_last_frame_ms ? (now - s_last_frame_ms) : PNX_TICK_MS;
	s_last_frame_ms		   = now;

	GBitmap* fb = graphics_capture_frame_buffer(ctx);
	if (fb)
	{
		const GRect bounds = layer_get_bounds(layer);
		PnxTarget target   = { .fb = fb, .w = bounds.size.w, .h = bounds.size.h };

		if (s_frame_fn)
			s_frame_fn(s_frame_ctx, elapsed, &target);

		graphics_release_frame_buffer(ctx, fb);

		// Everything that talks to the system rather than to pixels happens here, after the
		// release: text, because the SDK will not draw while the framebuffer is captured, and
		// audio, because feeding the speaker inside the capture window is audible.
		if (s_post_fn)
			s_post_fn(s_frame_ctx);
	}

	if (s_quit)
	{
		window_stack_pop_all(false);
		return;
	}

	// Re-armed after drawing, not before: the next frame should only be scheduled once
	// this one has actually been rendered. The DELAY targets a fixed grid -- s_frame_due_ms
	// -- rather than a flat offset from "now", so a frame that ran a little long is followed
	// by a slightly shorter wait rather than the schedule permanently drifting by however
	// much that one frame overran.
	s_frame_due_ms += FRAME_PERIOD_MS;
	const uint32_t after = pnx_platform_now_ms();
	uint32_t delay;
	if (s_frame_due_ms > after)
	{
		delay = s_frame_due_ms - after;
	}
	else
	{
		// Behind the grid -- covered by a notification, or a frame ran long enough to eat its
		// own slack. Snap the grid to now rather than firing a burst of make-up frames the
		// display would refuse anyway; the next frame just starts a fresh 40ms count from here.
		s_frame_due_ms = after;
		delay		   = 1;
	}
	s_timer = app_timer_register(delay, frame_timer, NULL);
}

static void frame_timer(void* ctx)
{
	s_timer = NULL;
	if (s_canvas)
		layer_mark_dirty(s_canvas);
}

static void window_load(Window* window)
{
	Layer* root = window_get_root_layer(window);
	s_canvas	= layer_create(layer_get_bounds(root));
	layer_set_update_proc(s_canvas, update_proc);
	layer_add_child(root, s_canvas);

	s_last_frame_ms = 0;
	s_frame_due_ms	= pnx_platform_now_ms();
	s_timer			= app_timer_register(FRAME_PERIOD_MS, frame_timer, NULL);
}

static void window_unload(Window* window)
{
	if (s_timer)
	{
		app_timer_cancel(s_timer);
		s_timer = NULL;
	}
	layer_destroy(s_canvas);
	s_canvas = NULL;
}

void pnx_platform_set_post_frame_fn(PnxPostFrameFn fn)
{
	s_post_fn = fn;
}

void pnx_platform_run(PnxFrameFn frame, void* ctx)
{
	s_frame_fn	= frame;
	s_frame_ctx = ctx;
	s_quit		= false;

	s_window = window_create();
	window_set_background_color(s_window, GColorBlack);
	window_set_click_config_provider(s_window, click_config);
	window_set_window_handlers(s_window, (WindowHandlers){
											 .load	 = window_load,
											 .unload = window_unload,
										 });

	if (touch_service_is_enabled())
	{
		touch_service_subscribe(touch_handler, NULL);
	}
	app_focus_service_subscribe(will_focus);

	window_stack_push(s_window, true);
	app_event_loop();

	if (s_audio_timer)
	{
		app_timer_cancel(s_audio_timer);
		s_audio_timer = NULL;
	}
	s_audio_fn = NULL;
	app_focus_service_unsubscribe();
	touch_service_unsubscribe();
	window_destroy(s_window);
	s_window = NULL;
}

void pnx_platform_quit(void)
{
	s_quit = true;
}

void pnx_platform_set_screen_lock(bool locked)
{
	s_screen_locked = locked;
	// The backlight half. light_enable(true) holds it on until it is released, rather than
	// the timed flash light_enable_interaction gives -- a game played off the wrist in a dim
	// room going dark mid-turn is indistinguishable from a crash, and the player's hands are
	// on the buttons, not shaking the watch.
	light_enable(locked);
}

bool pnx_platform_screen_locked(void)
{
	return s_screen_locked;
}

#endif // !PNX_PLATFORM_HOST
