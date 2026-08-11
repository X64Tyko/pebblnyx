// Software mixer over a continuously open PCM stream.
//
// Three measured facts shape this entirely (docs/MEASUREMENTS.md):
//
//   1. The batch API costs ~94 ms per submission and refuses a second submission while
//      one plays, so music and effects cannot coexist through it. Streaming is the only
//      option, and mixing is therefore ours to do.
//   2. **There is no way to ask how full the buffer is.** Underrun can only be inferred:
//      compare bytes written against what elapsed time says has been consumed. The spike
//      in pebble-tile-probe established this and the lead-based feed that follows from it.
//   3. There is ~30 ms of idle CPU per frame, so per-sample mixing is affordable --
//      600 samples x 8 voices is nothing.
//
// Feeding is therefore *lead-based*, not "write whatever fits": each frame tops the
// stream up to PNX_AUDIO_LEAD_MS ahead of playback. A fixed lead absorbs a late frame,
// which is the failure this has to survive -- the app is throttled to ~0.4fps whenever a
// notification covers it.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_AUDIO

#include "../platform/pnx_platform.h"

#include <stdint.h>
#include <stdbool.h>

#define PNX_AUDIO_NO_LOOP 0xFFFFFFFFu
#define PNX_AUDIO_NO_VOICE 0xFF

// ------------------------------------------------------------------- instruments
//
// An instrument is one cycle of a waveform, generated into the arena at init and looped.
// This costs **nothing in the 256KB resource budget**, which matters more here than
// anywhere: audio is the one category with no measured ceiling and ~70KB left after art.
// A recorded sample would sound better and could eat the remainder on its own.
//
// Pitch falls out of the existing resampling: playing an L-sample cycle at
// `note_hz * L` advances exactly note_hz cycles per second. No new mixing code.

typedef enum {
  PNX_WAVE_SQUARE = 0,   // hollow, cuts through a mix; the default lead
  PNX_WAVE_SAW,          // bright, buzzy; bass and strings
  PNX_WAVE_TRIANGLE,     // soft, flute-like
  PNX_WAVE_NOISE,        // percussion
  PNX_WAVE_COUNT
} PnxWaveform;

// Milliseconds, except `sustain` which is a 0..255 level. A note with no release clicks
// off, which is the single most audible difference between "a tone" and "an instrument".
typedef struct {
  uint16_t attack_ms;
  uint16_t decay_ms;
  uint8_t sustain;
  uint16_t release_ms;
} PnxEnvelope;

// MIDI note numbers: 60 is middle C. Frequency is looked up rather than computed, since
// there is no FPU and a 12-entry table plus an octave shift is exact enough.
uint32_t pnx_note_hz(uint8_t midi_note);

typedef struct {
  uint32_t written;        // bytes handed to the stream since init
  uint32_t worst_deficit;  // worst shortfall against the consume rate, in bytes
  uint32_t short_writes;   // times the device accepted less than offered
  uint32_t feeds;
  uint32_t capacity;       // bytes accepted before the first short write -- the device's
                           // buffer depth, which it offers no way to query
  uint32_t carried;        // bytes currently held over from a short write
  uint8_t active_voices;
} PnxAudioStats;

// `volume` is 0..100, matching the platform.
bool pnx_audio_init(PnxAudioFormat format, uint8_t volume);
void pnx_audio_shutdown(void);

// Call once per frame with a monotonic clock. Everything else is bookkeeping.
void pnx_audio_update(uint32_t now_ms);

// Starts a sample on a free voice, or steals the quietest if none is free. Returns the
// voice, or PNX_AUDIO_NO_VOICE if audio is not running.
//
// `sample_hz` is the rate the data was recorded at; resampling to the output rate is a
// phase-step calculation, so a sample can be pitched by lying about it.
uint8_t pnx_audio_play(const int8_t *pcm, uint32_t samples, uint32_t loop_start,
                       uint32_t sample_hz, uint8_t volume);

// Plays a note on a generated instrument. `priority` decides what may be stolen: a
// higher-priority sound never loses its voice to a lower one, which is what stops a
// footstep silencing the melody.
uint8_t pnx_audio_note(PnxWaveform wave, uint8_t midi_note, uint8_t volume,
                       const PnxEnvelope *env, uint8_t priority);

// Begins the release phase rather than cutting the voice, so a held note ends musically.
void pnx_audio_release(uint8_t voice);

// Full form: a PCM sample with priority and an optional envelope.
uint8_t pnx_audio_play_pri(const int8_t *pcm, uint32_t samples, uint32_t loop_start,
                           uint32_t sample_hz, uint8_t volume, uint8_t priority,
                           const PnxEnvelope *env);

void pnx_audio_stop(uint8_t voice);
void pnx_audio_stop_all(void);
bool pnx_audio_voice_active(uint8_t voice);

const PnxAudioStats *pnx_audio_stats(void);

#endif  // PNX_USE_AUDIO
