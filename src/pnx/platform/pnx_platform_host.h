// Test-only hooks into the host platform.
//
// Separate from pnx_platform.h so these cannot be called from framework or game code:
// they exist to drive the host build and have no meaning on device.

#pragma once

#include "pnx_platform.h"

// Queues an event for pnx_platform_poll_event to return.
void pnx_host_queue_event(PnxEvent ev);

// Clears queued events and zeroes the framebuffer.
void pnx_host_reset(void);

// The flat render target, for asserting on pixels directly.
PnxTarget *pnx_host_target(void);

// The most recent block written to the audio stream, and the running total.
const void *pnx_host_audio_last(size_t *bytes);
uint32_t pnx_host_audio_total(void);

// The most recent string passed to pnx_platform_text_draw.
const char *pnx_host_last_text(void);

// Points a resource id at a file on disk, so tests load the same blobs the device does.
void pnx_host_register_resource(uint32_t resource_id, const char *path);
