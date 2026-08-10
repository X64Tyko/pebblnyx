#include "pnx_audio.h"

#if PNX_USE_AUDIO

#include "../core/pnx_diag.h"

#include <string.h>

#ifndef PNX_AUDIO_VOICES
#define PNX_AUDIO_VOICES 8
#endif

// How far ahead of playback to keep the stream. The spike measured 120 ms as sufficient
// at 8 kHz; kept here because the thing it absorbs is a late frame, and frames get very
// late (~0.4fps) whenever a modal covers the app.
#ifndef PNX_AUDIO_LEAD_MS
#define PNX_AUDIO_LEAD_MS 120
#endif

// Scratch samples per feed. One frame at 16 kHz needs ~600, so this covers a frame plus
// catch-up without a large buffer.
#ifndef PNX_AUDIO_CHUNK
#define PNX_AUDIO_CHUNK 1024
#endif

typedef struct {
  const int8_t *pcm;
  uint32_t samples;
  uint32_t loop_start;     // PNX_AUDIO_NO_LOOP to stop at the end
  uint32_t phase;          // 16.16 index into pcm
  uint32_t step;           // 16.16 advance per output sample
  uint8_t volume;          // 0..255
  bool active;
} Voice;

static Voice s_voices[PNX_AUDIO_VOICES];
static int8_t s_scratch[PNX_AUDIO_CHUNK];
static PnxAudioStats s_stats;

static bool s_on;
static PnxAudioFormat s_format;
static uint32_t s_byte_rate;
static uint32_t s_start_ms;
static bool s_16bit;

bool pnx_audio_init(PnxAudioFormat format, uint8_t volume) {
  if (s_on) return true;
  if (!pnx_platform_audio_open(format, volume)) {
    pnx_log("audio: stream would not open");
    return false;
  }
  memset(s_voices, 0, sizeof(s_voices));
  memset(&s_stats, 0, sizeof(s_stats));
  s_format = format;
  s_byte_rate = pnx_audio_byte_rate(format);
  s_16bit = (format == PNX_AUDIO_16KHZ_16BIT || format == PNX_AUDIO_8KHZ_16BIT);
  s_start_ms = 0;
  s_on = true;
  return true;
}

void pnx_audio_shutdown(void) {
  if (!s_on) return;
  pnx_platform_audio_close();
  s_on = false;
}

static uint32_t output_rate(void) {
  return (s_format == PNX_AUDIO_16KHZ_16BIT || s_format == PNX_AUDIO_16KHZ_8BIT)
         ? 16000u : 8000u;
}

uint8_t pnx_audio_play(const int8_t *pcm, uint32_t samples, uint32_t loop_start,
                       uint32_t sample_hz, uint8_t volume) {
  if (!s_on || !pcm || samples == 0 || sample_hz == 0) return PNX_AUDIO_NO_VOICE;

  int free_slot = -1, quietest = 0;
  for (int i = 0; i < PNX_AUDIO_VOICES; i++) {
    if (!s_voices[i].active) { free_slot = i; break; }
    if (s_voices[i].volume < s_voices[quietest].volume) quietest = i;
  }
  // Steal the quietest rather than the oldest: losing a fading effect is less audible
  // than losing whatever just started, which is usually the important one.
  const int slot = free_slot >= 0 ? free_slot : quietest;

  s_voices[slot] = (Voice){
    .pcm = pcm, .samples = samples,
    .loop_start = loop_start < samples ? loop_start : PNX_AUDIO_NO_LOOP,
    .phase = 0,
    // Resampling is just the phase increment: 16.16 ratio of source to output rate.
    .step = (uint32_t)(((uint64_t)sample_hz << 16) / output_rate()),
    .volume = volume, .active = true,
  };
  return (uint8_t)slot;
}

void pnx_audio_stop(uint8_t voice) {
  if (voice < PNX_AUDIO_VOICES) s_voices[voice].active = false;
}

void pnx_audio_stop_all(void) {
  for (int i = 0; i < PNX_AUDIO_VOICES; i++) s_voices[i].active = false;
}

bool pnx_audio_voice_active(uint8_t voice) {
  return voice < PNX_AUDIO_VOICES && s_voices[voice].active;
}

// Sums active voices into signed 8-bit. Accumulating in int32 and clamping once at the
// end keeps intermediate sums from wrapping, which is what makes several loud voices
// distort rather than invert.
static void mix(int8_t *out, uint32_t count) {
  memset(out, 0, count);
  uint8_t active = 0;

  for (int v = 0; v < PNX_AUDIO_VOICES; v++) {
    Voice *o = &s_voices[v];
    if (!o->active) continue;
    active++;

    for (uint32_t n = 0; n < count; n++) {
      const uint32_t index = o->phase >> 16;
      if (index >= o->samples) {
        if (o->loop_start == PNX_AUDIO_NO_LOOP) { o->active = false; break; }
        o->phase = o->loop_start << 16;
        continue;
      }
      const int32_t s = ((int32_t)o->pcm[index] * o->volume) >> 8;
      const int32_t acc = out[n] + s;
      out[n] = (int8_t)(acc < -128 ? -128 : (acc > 127 ? 127 : acc));
      o->phase += o->step;
    }
  }
  s_stats.active_voices = active;
}

void pnx_audio_update(uint32_t now_ms) {
  if (!s_on) return;
  if (s_start_ms == 0) s_start_ms = now_ms;

  const uint32_t elapsed = now_ms - s_start_ms;

  // No fill-level query exists, so underrun is inferred: if playback has consumed more
  // than we ever wrote, the speaker ran dry. This is the only signal available.
  const uint32_t consumed = elapsed * s_byte_rate / 1000u;
  if (consumed > s_stats.written) {
    const uint32_t deficit = consumed - s_stats.written;
    if (deficit > s_stats.worst_deficit) s_stats.worst_deficit = deficit;
  }

  const uint32_t target = (elapsed + PNX_AUDIO_LEAD_MS) * s_byte_rate / 1000u;
  if (target <= s_stats.written) return;

  uint32_t want_bytes = target - s_stats.written;
  const uint32_t max_bytes = s_16bit ? PNX_AUDIO_CHUNK * 2u : PNX_AUDIO_CHUNK;
  if (want_bytes > max_bytes) want_bytes = max_bytes;

  const uint32_t samples = s_16bit ? want_bytes / 2u : want_bytes;
  if (samples == 0) return;

  mix(s_scratch, samples);

  size_t wrote;
  if (s_16bit) {
    // Widen in place into the caller's view of the buffer. Two bytes per sample, little
    // endian, which is what the device expects.
    static uint8_t wide[PNX_AUDIO_CHUNK * 2];
    for (uint32_t i = 0; i < samples; i++) {
      const int16_t v = (int16_t)(s_scratch[i] << 8);
      wide[i * 2] = (uint8_t)(v & 0xFF);
      wide[i * 2 + 1] = (uint8_t)((v >> 8) & 0xFF);
    }
    wrote = pnx_platform_audio_write(wide, samples * 2u);
  } else {
    wrote = pnx_platform_audio_write(s_scratch, samples);
  }

  if (wrote < (s_16bit ? samples * 2u : samples)) s_stats.short_writes++;
  s_stats.written += (uint32_t)wrote;
  s_stats.feeds++;
}

const PnxAudioStats *pnx_audio_stats(void) { return &s_stats; }

#endif  // PNX_USE_AUDIO
