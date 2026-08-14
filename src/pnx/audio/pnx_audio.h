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

#define PNX_AUDIO_NO_LOOP  0xFFFFFFFFu
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

typedef enum
{
	PNX_WAVE_SQUARE = 0,  // hollow, cuts through a mix; the default lead
	PNX_WAVE_SAW,		  // bright, buzzy; bass and strings
	PNX_WAVE_TRIANGLE,	  // soft, flute-like
	PNX_WAVE_NOISE,		  // percussion
	PNX_WAVE_COUNT
} PnxWaveform;

// Milliseconds, except `sustain` which is a 0..255 level. A note with no release clicks
// off, which is the single most audible difference between "a tone" and "an instrument".
typedef struct
{
	uint16_t attack_ms;
	uint16_t decay_ms;
	uint8_t sustain;
	uint16_t release_ms;
} PnxEnvelope;

// MIDI note numbers: 60 is middle C. Frequency is looked up rather than computed, since
// there is no FPU and a 12-entry table plus an octave shift is exact enough.
uint32_t pnx_note_hz(uint8_t midi_note);

typedef struct
{
	uint32_t written;		 // bytes handed to the stream since init
	uint32_t worst_deficit;	 // worst shortfall against the consume rate, in bytes
	uint32_t short_writes;	 // times the device accepted less than offered
	uint32_t feeds;
	uint32_t capacity;	// bytes accepted before the first short write -- the device's
						// buffer depth, which it offers no way to query
	uint32_t carried;	// bytes currently held over from a short write
	// Samples the output clamp actually had to cut, and the loudest value seen going into
	// it. Two numbers that separate "too hot" from every other cause of harshness -- and the
	// clamp was silent before, so those were indistinguishable from a log.
	uint32_t clipped;
	uint32_t peak;	// pre-clamp magnitude; 127 is full scale

	uint16_t left_playing;	// times the speaker stopped being in Playing state. Each one
							// is playback halting and resuming, which is heard as the
							// sound starting over.
	uint8_t state;			// most recent PnxAudioState
	uint16_t worst_gap_ms;	// longest interval between feeds, all time
	uint16_t gap_ms;		// most recent interval. Reported separately because the all-time
							// maximum is pinned by a single hitch and then hides the steady
							// state -- one 54ms stall made 32ms feeding look like 54ms
							// feeding for the rest of the run.
	uint16_t feed_min;		// smallest and largest bytes mixed in one call. A pulse at the
	uint16_t feed_max;		// frame rate (~27Hz) sounds like a thrum, and an uneven feed
							// is the mechanism that would cause one.
	uint8_t active_voices;	// snapshot from the last mix, not a live count. An update that
							// has already reached its lead returns without mixing and leaves
							// this stale; use pnx_audio_voice_active for the current state.
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
uint8_t pnx_audio_play(const int8_t* pcm, uint32_t samples, uint32_t loop_start,
					   uint32_t sample_hz, uint8_t volume);

// Plays a note on a generated instrument. `priority` decides what may be stolen: a
// higher-priority sound never loses its voice to a lower one, which is what stops a
// footstep silencing the melody.
uint8_t pnx_audio_note(PnxWaveform wave, uint8_t midi_note, uint8_t volume,
					   const PnxEnvelope* env, uint8_t priority);

// Begins the release phase rather than cutting the voice, so a held note ends musically.
void pnx_audio_release(uint8_t voice);

// Releases over a given time. A few milliseconds is enough to avoid a click without
// audibly lengthening the note -- cutting a voice mid-waveform is a step discontinuity,
// which is exactly what a click is.
void pnx_audio_release_in(uint8_t voice, uint16_t ms);

// Full form: a PCM sample with priority and an optional envelope.
uint8_t pnx_audio_play_pri(const int8_t* pcm, uint32_t samples, uint32_t loop_start,
						   uint32_t sample_hz, uint8_t volume, uint8_t priority,
						   const PnxEnvelope* env);

void pnx_audio_stop(uint8_t voice);
void pnx_audio_stop_all(void);
bool pnx_audio_voice_active(uint8_t voice);

const PnxAudioStats* pnx_audio_stats(void);

// How far ahead of playback to keep the stream, in milliseconds.
//
// Adjustable because the device gives no way to ask how deep its buffer is, and the
// consequences of guessing wrong are opposite: too little and it drains between feeds,
// too much and -- if write accepts bytes it cannot hold -- the surplus is silently
// discarded and the shortfall never shows up as a short write. Sweeping this is the only
// way to find out which is happening.
void pnx_audio_set_lead(uint16_t ms);
uint16_t pnx_audio_lead(void);

// Reopens the stream in another format, preserving nothing. For A/B testing on device:
// 8-bit carries exactly the information an 8-bit mixer produces, but that is a claim about
// information, not about how a particular DAC path sounds.
bool pnx_audio_reopen(PnxAudioFormat format, uint8_t volume);

// One-pole low-pass on the mix, in Hz. 0 disables it.
//
// A 64-entry wavetable read at a large step undersamples: at 880Hz on an 8kHz stream that is
// 7 table entries per output sample, and harmonics above Nyquist fold back as inharmonic
// components -- fast ticking rather than tone. Band-limited tables per octave would be the
// thorough fix; this is the cheap one that removes what is audibly too high.
void pnx_audio_set_lowpass(uint16_t cutoff_hz);
uint16_t pnx_audio_lowpass(void);
PnxAudioFormat pnx_audio_format(void);

#endif	// PNX_USE_AUDIO
