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

// clock_gettime is POSIX, not ISO C, and -std=c11 hides it without this. The name and
// leading underscore are POSIX's, not a choice available here -- NOLINT
#define _POSIX_C_SOURCE 200809L	 // NOLINT

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

struct PnxTarget
{
	uint8_t* pixels;
	int16_t w, h;
};

static uint8_t s_pixels[PNX_HOST_WIDTH * PNX_HOST_HEIGHT];
static PnxTarget s_target = { s_pixels, PNX_HOST_WIDTH, PNX_HOST_HEIGHT };

static PnxEvent s_queued[32];
static int s_queued_count;
static int s_queued_read;
static bool s_quit;

uint32_t pnx_platform_now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

void pnx_platform_log(const char* message)
{
	printf("%s\n", message);
}

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
	if (!t || y < 0 || y >= t->h)
		return row;
	row.data = t->pixels + (size_t)y * t->w;
	row.min_x = 0;
	row.max_x = (int16_t)(t->w - 1);
	return row;
}

// --------------------------------------------------------------------- resources
//
// File-backed, so host tests exercise the SAME parsing code against the SAME blobs the
// device loads. A mock returning synthetic bytes would test the mock.

// Generous, and host-only. Registration past the end is silently dropped, which presents
// as a scene failing to load with nothing wrong in it -- and a project's asset count is no
// longer roughly its content count now that a map's WorldTile banks are resources of their
// own. `examples/worldtiles` alone declares 51.
#define MAX_RESOURCES 256

typedef struct
{
	uint32_t id;
	char path[256];
} HostResource;

static HostResource s_resources[MAX_RESOURCES];
static int s_resource_count;

void pnx_host_register_resource(uint32_t resource_id, const char* path)
{
	if (s_resource_count >= MAX_RESOURCES || !path)
		return;
	HostResource* r = &s_resources[s_resource_count++];
	r->id = resource_id;
	strncpy(r->path, path, sizeof(r->path) - 1);
	r->path[sizeof(r->path) - 1] = '\0';
}

static const char* resource_path(uint32_t resource_id)
{
	for (int i = 0; i < s_resource_count; i++)
	{
		if (s_resources[i].id == resource_id)
			return s_resources[i].path;
	}
	return NULL;
}

bool pnx_platform_resource_size(uint32_t resource_id, size_t* out_size)
{
	const char* path = resource_path(resource_id);
	if (!path)
		return false;

	FILE* f = fopen(path, "rb");
	if (!f)
		return false;

	if (fseek(f, 0, SEEK_END) != 0)
	{
		fclose(f);
		return false;
	}
	const long size = ftell(f);
	fclose(f);

	if (size <= 0)
		return false;
	if (out_size)
		*out_size = (size_t)size;
	return true;
}

// Counted because the DEVICE's cost is per call, not per byte -- a ranged read there
// streams from the start of the resource, so a change that halves the reads is worth far
// more than one that halves the bytes. The host cannot reproduce that cost, but it can
// count the thing that causes it.
static uint32_t s_resource_reads;

uint32_t pnx_host_resource_reads(void)
{
	return s_resource_reads;
}

size_t pnx_platform_resource_read(uint32_t resource_id, size_t offset, void* dst, size_t bytes)
{
	const char* path = resource_path(resource_id);
	if (!path || !dst)
		return 0;

	s_resource_reads++;
	FILE* f = fopen(path, "rb");
	if (!f)
		return 0;

	if (fseek(f, (long)offset, SEEK_SET) != 0)
	{
		fclose(f);
		return 0;
	}
	const size_t got = fread(dst, 1, bytes, f);
	fclose(f);
	return got;
}

// ------------------------------------------------------------------------ persist
//
// An in-memory table, not a file -- a save's correctness has to survive a process that
// never restarts (the host harness and the test binary both are), so "does it round-trip
// within one run" is the whole of what this needs to prove. Call counts are tracked for
// the same reason resource reads are: on device a persist WRITE costs ~7ms per call
// regardless of size (docs/MEASUREMENTS.md), so "how many calls" is what a save's design
// has to be judged on, and it is not observable any other way on a host.

#define MAX_PERSIST_KEYS 64

typedef struct
{
	uint32_t key;
	uint8_t bytes[PNX_PERSIST_KEY_BYTES];
	uint16_t len;
	bool used;
} HostPersistSlot;

static HostPersistSlot s_persist[MAX_PERSIST_KEYS];
static uint32_t s_persist_writes, s_persist_reads, s_persist_deletes;

static HostPersistSlot* persist_find(uint32_t key, bool create)
{
	int free_slot = -1;
	for (int i = 0; i < MAX_PERSIST_KEYS; i++)
	{
		if (s_persist[i].used && s_persist[i].key == key)
			return &s_persist[i];
		if (free_slot < 0 && !s_persist[i].used)
			free_slot = i;
	}
	if (!create || free_slot < 0)
		return NULL;
	s_persist[free_slot].used = true;
	s_persist[free_slot].key = key;
	s_persist[free_slot].len = 0;
	return &s_persist[free_slot];
}

bool pnx_platform_persist_read(uint32_t key, void* dst, size_t bytes, size_t* out_bytes)
{
	if (!dst)
		return false;
	HostPersistSlot* s = persist_find(key, false);
	if (!s)
		return false;
	s_persist_reads++;
	const size_t got = s->len < bytes ? s->len : bytes;
	memcpy(dst, s->bytes, got);
	if (out_bytes)
		*out_bytes = got;
	return true;
}

bool pnx_platform_persist_write(uint32_t key, const void* data, size_t bytes)
{
	if (!data)
		return false;
	if (bytes > PNX_PERSIST_KEY_BYTES)
		bytes = PNX_PERSIST_KEY_BYTES;
	HostPersistSlot* s = persist_find(key, true);
	if (!s)
		return false;
	s_persist_writes++;
	memcpy(s->bytes, data, bytes);
	s->len = (uint16_t)bytes;
	return true;
}

bool pnx_platform_persist_delete(uint32_t key)
{
	HostPersistSlot* s = persist_find(key, false);
	if (!s)
		return false;
	s_persist_deletes++;
	s->used = false;
	s->len = 0;
	return true;
}

bool pnx_platform_persist_exists(uint32_t key)
{
	return persist_find(key, false) != NULL;
}

uint32_t pnx_host_persist_writes(void)
{
	return s_persist_writes;
}
uint32_t pnx_host_persist_reads(void)
{
	return s_persist_reads;
}
uint32_t pnx_host_persist_deletes(void)
{
	return s_persist_deletes;
}

// ------------------------------------------------------------------------- input

bool pnx_platform_poll_event(PnxEvent* out)
{
	if (s_queued_read >= s_queued_count)
	{
		// Drained: rewind, so the next frame's events start at the front of the array.
		//
		// Without this the queue is a one-shot buffer of 32 events for the whole process, and
		// the 33rd is dropped by pnx_host_queue_event with no error -- fine for a test that
		// queues a handful, silently wrong for anything that drives a game over hundreds of
		// frames. A harness hit it as "the button stops working after a while", which is the
		// worst possible presentation of a full buffer.
		s_queued_read = s_queued_count = 0;
		return false;
	}
	*out = s_queued[s_queued_read++];
	return true;
}

bool pnx_platform_has_touch(void)
{
	return true;
}

void pnx_platform_run(PnxFrameFn frame, void* ctx)
{
	// Runs a bounded number of frames rather than forever, so a test that forgets to
	// quit fails fast instead of hanging a CI job.
	s_quit = false;
	uint32_t last = pnx_platform_now_ms();

	for (int i = 0; i < 1000 && !s_quit; i++)
	{
		const uint32_t now = pnx_platform_now_ms();
		const uint32_t elapsed = now - last;
		last = now;
		if (frame)
			frame(ctx, elapsed ? elapsed : 1, &s_target);
	}
}

void pnx_platform_quit(void)
{
	s_quit = true;
}

// The host has no BACK button to swallow and no backlight to hold, so the lock is only a
// flag here. It is still worth having: a game's own logic decides when to raise it, and
// that logic is testable even though its effects are not.
static bool s_screen_locked;

void pnx_platform_set_screen_lock(bool locked)
{
	s_screen_locked = locked;
}
bool pnx_platform_screen_locked(void)
{
	return s_screen_locked;
}

#if PNX_USE_SDK_TEXT

// No text rendering on the host: it exists to test logic, and the device's font metrics
// are not reproducible here anyway. Recorded so a test can assert a call happened.
static char s_last_text[128];

void pnx_platform_text_draw(const char* text, PnxTextSize size, uint8_t colour, int32_t x,
							int32_t y, int16_t w, int16_t h)
{
	if (!text)
		return;
	strncpy(s_last_text, text, sizeof(s_last_text) - 1);
	s_last_text[sizeof(s_last_text) - 1] = '\0';
}

const char* pnx_host_last_text(void)
{
	return s_last_text;
}

#endif	// PNX_USE_SDK_TEXT

void pnx_platform_set_post_frame_fn(PnxPostFrameFn fn)
{
	(void)fn;
}

// ------------------------------------------------------------------------- audio
//
// Accepts everything and keeps the last buffer, so mixer output can be asserted on
// without a speaker. A host that dropped samples would hide exactly the underruns the
// mixer exists to avoid.

static bool s_audio_open;
static uint8_t s_audio_last[4096];
static size_t s_audio_last_bytes;
static uint32_t s_audio_total;

bool pnx_platform_audio_open(PnxAudioFormat format, uint8_t volume)
{
	(void)format;
	(void)volume;
	s_audio_open = true;
	s_audio_total = 0;
	return true;
}

size_t pnx_platform_audio_write(const void* data, size_t bytes)
{
	if (!s_audio_open)
		return 0;
	const size_t keep = bytes < sizeof(s_audio_last) ? bytes : sizeof(s_audio_last);
	memcpy(s_audio_last, data, keep);
	s_audio_last_bytes = keep;
	s_audio_total += (uint32_t)bytes;
	return bytes;
}

void pnx_platform_audio_close(void)
{
	s_audio_open = false;
}
bool pnx_platform_audio_is_open(void)
{
	return s_audio_open;
}

// No timers on the host: tests drive pnx_audio_update directly, which is more controllable
// than a real clock and is the point of the host platform.
void pnx_platform_set_audio_timer(PnxTickFn fn, void* ctx, uint16_t interval_ms)
{
	(void)fn;
	(void)ctx;
	(void)interval_ms;
}

PnxAudioState pnx_platform_audio_state(void)
{
	return s_audio_open ? PNX_AUDIO_PLAYING : PNX_AUDIO_IDLE;
}

const void* pnx_host_audio_last(size_t* bytes)
{
	if (bytes)
		*bytes = s_audio_last_bytes;
	return s_audio_last;
}
uint32_t pnx_host_audio_total(void)
{
	return s_audio_total;
}

// ------------------------------------------------------- test-only entry points

void pnx_host_queue_event(PnxEvent ev)
{
	if (s_queued_count < (int)(sizeof(s_queued) / sizeof(s_queued[0])))
	{
		s_queued[s_queued_count++] = ev;
	}
}

void pnx_host_reset(void)
{
	s_resource_count = 0;
	s_resource_reads = 0;
	s_queued_count = 0;
	s_queued_read = 0;
	s_quit = false;
	memset(s_pixels, 0, sizeof(s_pixels));
	memset(s_persist, 0, sizeof(s_persist));
	s_persist_writes = s_persist_reads = s_persist_deletes = 0;
}

PnxTarget* pnx_host_target(void)
{
	return &s_target;
}

#endif	// PNX_PLATFORM_HOST
