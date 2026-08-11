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
// Measured on device, the interval between feeds reaches 140ms in steady state and 305ms
// during startup while assets load. A lead only survives stalls SHORTER than itself, so
// 120ms was starving the buffer several times a second -- audible as thrumming, and
// invisible to the deficit statistic, which compares aggregate bytes written against
// elapsed time and so cannot see a buffer that empties and refills.
//
// Chosen for LATENCY, not continuity. Swept on device from 20ms to 250ms with no audible
// difference at either 8k/8 or 16k/8, so buffer depth is not what limits quality here --
// it only decides how long after a trigger a sound is heard. 60ms sits comfortably clear
// of the ~32ms feed interval while keeping effects prompt.
#ifndef PNX_AUDIO_LEAD_MS
#define PNX_AUDIO_LEAD_MS 60
#endif

// Samples per feed, and the size of the mix buffer. Feeds measured ~32ms apart, which at
// 16kHz is ~512 samples; 768 keeps margin and stays a multiple of the 256-byte quantum.
#ifndef PNX_AUDIO_CHUNK
#define PNX_AUDIO_CHUNK 768
#endif

// Writes are rounded down to a multiple of this and the remainder is left for next time.
//
// Without it the write size drifts every feed as the lead target moves -- 253 bytes, then
// 259, then 247 -- and a device consuming in fixed blocks has to straddle them. Uniform
// aligned writes cost nothing but a few bytes of latency and remove a per-feed seam as a
// possible source of clicking.
// Divide by two, not four. Four gave four channels perfect headroom but left a SINGLE voice
// at 20% of full scale, which is quiet and coarsely quantised at 8-bit output. Halving puts
// one voice near 40% and clamps four channels only on coincident peaks -- the trade trackers
// make: loud, with occasional limiting. NOT derived from the active voice count: doing that
// steps the mix when a voice starts (a pop per beat) and gliding to it warbles instead.
#define MIX_HEADROOM_SHIFT 1

// 5kHz by default: high enough to leave the waveforms their brightness, low enough to take
// the edge off the top of a lead's range. The filter was written to fix harshness that turned
// out to be undersampling at 8kHz -- at 16kHz the Nyquist limit is 8kHz and the folded
// harmonics are largely not there -- so it is no longer load-bearing, and a cutoff this high
// is a balance rather than a repair. Costs one multiply and one shift per sample.
// pnx_audio_set_lowpass(0) turns it off.
#ifndef PNX_AUDIO_CUTOFF_HZ
#define PNX_AUDIO_CUTOFF_HZ 5000
#endif

#ifndef PNX_AUDIO_QUANTUM
#define PNX_AUDIO_QUANTUM 256
#endif

// One cycle per waveform, generated once at init. 64 samples is enough for the harmonics
// this speaker can reproduce and keeps the tables in cache.
#define CYCLE 64

typedef enum { ENV_ATTACK = 0, ENV_DECAY, ENV_SUSTAIN, ENV_RELEASE, ENV_OFF } EnvStage;

typedef struct {
  const int8_t *pcm;
  uint32_t samples;
  uint32_t loop_start;     // PNX_AUDIO_NO_LOOP to stop at the end
  uint32_t phase;          // 16.16 index into pcm
  uint32_t step;           // 16.16 advance per output sample
  uint8_t volume;          // 0..255
  uint8_t priority;
  bool active;

  // Envelope. Level is 16.16 so the per-sample increment does not quantise to zero on a
  // slow attack; a per-block envelope would zipper audibly at 16 kHz.
  PnxEnvelope env;
  EnvStage stage;
  int32_t level;
  int32_t rate;
} Voice;

static int8_t s_wavetable[PNX_WAVE_COUNT][CYCLE];

// Equal temperament, one octave tabulated and shifted. Values are Hz x 100 for C4..B4 so
// the arithmetic stays integral.
static const uint16_t NOTE_CENTIHZ[12] = {
  26163, 27718, 29366, 31113, 32963, 34923,
  36999, 39200, 41530, 44000, 46616, 49388,
};

// Centihz for a note, shifted by octave. Kept in hundredths so nothing rounds early.
static uint32_t output_rate(void);

static uint32_t note_centihz(uint8_t midi_note) {
  const int octave = (int)(midi_note / 12) - 5;      // 60 -> octave 0 (C4)
  const uint32_t c = NOTE_CENTIHZ[midi_note % 12];
  return octave > 0 ? (c << octave) : (c >> -octave);
}

// The phase step for a note, computed without ever rounding to whole Hz.
//
// Going through integer Hz was the bug: whole-Hz resolution is 11 cents at the bottom of
// the range, and truncating before the octave shift multiplied the error. Rounding after
// the shift did not help either -- a right shift has already lost the bits.
//
// 65536 / (100 * 16000) is exactly 128/3125, so this is exact in 32-bit arithmetic; the
// largest intermediate is 3.24e9 against a 4.29e9 limit. No 64-bit division, which would
// cost 754 bytes of __udivmoddi4. Worst error across MIDI 24-107 is 0.52 cents.
static uint32_t note_step(uint8_t midi_note, uint32_t cycle_len) {
  const uint32_t mul = (output_rate() == 16000u) ? 128u : 256u;
  return note_centihz(midi_note) * cycle_len * mul / 3125u;
}

uint32_t pnx_note_hz(uint8_t midi_note) {
  const int octave = (int)(midi_note / 12) - 5;      // 60 -> octave 0 (C4)

  // Shift in CENTIHZ and round at the end. Truncating to whole Hz first and then shifting
  // multiplies the rounding error by the octave -- measured at 10 cents flat on D3, where
  // rounding after the shift holds every note inside 2 cents.
  uint32_t centi = NOTE_CENTIHZ[midi_note % 12];
  if (octave > 0) centi <<= octave;
  else if (octave < 0) centi >>= -octave;

  const uint32_t hz = (centi + 50u) / 100u;
  return hz ? hz : 1u;
}

// Every table begins at amplitude ZERO, not at a peak.
//
// A new note starts at phase 0, and phase 0 used to be index 0 -- which for the triangle is
// its trough at -100. So every note onset jumped the output to full negative amplitude, and
// the attack envelope is far too short to hide it: 6ms is 48 samples at 8kHz and percussion
// asks for 1ms, which is 8. That step is a click, and with four channels changing notes it
// is a click several times a second.
//
// Starting at a zero crossing means the onset is silent regardless of how fast the envelope
// opens. The square wave is the exception -- it has no zero to start from -- so it begins at
// a transition and relies on its envelope, which is why a square lead clicks more than a
// triangle one.
static void build_wavetable(void) {
  const int q = CYCLE / 4;
  uint32_t rng = 0x2545F491u;

  for (int i = 0; i < CYCLE; i++) {
    // Triangle: 0 -> +100 -> 0 -> -100 -> 0, symmetric, starting and ending at silence.
    int tri;
    if (i < q)            tri =  i * 100 / q;
    else if (i < 3 * q)   tri =  100 - (i - q) * 200 / (2 * q);
    else                  tri = -100 + (i - 3 * q) * 100 / q;
    s_wavetable[PNX_WAVE_TRIANGLE][i] = (int8_t)tri;

    // Saw: 0 -> +100, wrap to -100, -100 -> 0. One inherent discontinuity, placed at the
    // midpoint rather than at the note onset.
    const int saw = (i < CYCLE / 2)
        ? (i * 200 / CYCLE)
        : ((i - CYCLE / 2) * 200 / CYCLE) - 100;
    s_wavetable[PNX_WAVE_SAW][i] = (int8_t)saw;

    // Square: no zero to begin at, so it starts at a transition and depends on its
    // envelope. Kept honest rather than pretending otherwise.
    s_wavetable[PNX_WAVE_SQUARE][i] = (int8_t)(i < CYCLE / 2 ? 100 : -100);

    // xorshift, so the noise table is identical in every build -- reproducible audio for
    // the same reason as reproducible simulation.
    rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
    s_wavetable[PNX_WAVE_NOISE][i] = (int8_t)((int8_t)((rng >> 8) & 0xFF) / 2);
  }
}

static Voice s_voices[PNX_AUDIO_VOICES];

// One buffer for every stage of a feed: accumulate, output, and hold what the device would
// not take. Was four buffers holding the same signal -- 7,168 of the module's 8,007 bytes.
//
// Each stage fits in place because none is wider than the last. Voices sum as int16, so the
// sum clamps once at the end rather than saturating intermediates. Output overwrites the
// accumulator: 16-bit is the same width, 8-bit is half, so writing byte n touches only the
// entry at n/2, already read. A short write leaves the remainder where it lies and
// s_carry_head marks how far the device got -- update() returns early while one is pending,
// so mixing cannot overwrite it.
//
// int16 keeps the accumulator aligned, and an int16 array is little-endian in memory on both
// ARM and the host, which is the byte order the speaker wants -- so 16-bit output needs no
// conversion.
static int16_t s_mix[PNX_AUDIO_CHUNK];

static uint32_t s_carry_bytes;   // bytes in s_mix awaiting the device
static uint32_t s_carry_head;    // how many it has taken; equal means drained

static PnxAudioStats s_stats;

// One-pole low-pass state, 16.16.
//
// A 64-entry wavetable read at a large step undersamples badly: at 880Hz on an 8kHz stream
// the step is 7 table entries per sample, and the waveform's harmonics above Nyquist fold
// back as inharmonic components -- heard as fast ticking rather than as a tone. Raising the
// sample rate helps but does not eliminate it, and band-limited tables per octave would be
// the thorough fix. A one-pole filter costs one multiply and one shift per sample and
// removes the harshness that is genuinely too high to belong there.
static int32_t s_lp;
static int32_t s_lp_a = 0;      // 0 disables the filter entirely
static uint16_t s_cutoff_hz = PNX_AUDIO_CUTOFF_HZ;

static PnxAudioFormat s_format;
static uint32_t s_byte_rate;
static uint32_t s_start_ms;
static bool s_16bit;
static uint16_t s_lead_ms = PNX_AUDIO_LEAD_MS;
static uint32_t s_last_update_ms;
static bool s_on;

bool pnx_audio_init(PnxAudioFormat format, uint8_t volume) {
  if (s_on) return true;
  if (!pnx_platform_audio_open(format, volume)) {
    pnx_log("audio: stream would not open");
    return false;
  }
  memset(s_voices, 0, sizeof(s_voices));
  memset(&s_stats, 0, sizeof(s_stats));
  build_wavetable();
  s_format = format;
  s_byte_rate = pnx_audio_byte_rate(format);
  s_16bit = (format == PNX_AUDIO_16KHZ_16BIT || format == PNX_AUDIO_8KHZ_16BIT);
  s_start_ms = 0;
  // Cleared so the first gap after an open is not measured from before it. Not doing this
  // reported a 2,398ms gap after a format change, which was the measurement and not the
  // device.
  s_last_update_ms = 0;
  s_stats.worst_gap_ms = 0;
  s_carry_bytes = s_carry_head = 0;
  s_lp = 0;
  pnx_audio_set_lowpass(s_cutoff_hz);
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

  return pnx_audio_play_pri(pcm, samples, loop_start, sample_hz, volume, 0, NULL);
}

// Chooses a voice, honouring priority. A sound never displaces something more important
// than itself, which is what stops a footstep silencing the melody.
static int claim_voice(uint8_t priority) {
  int weakest = -1;
  for (int i = 0; i < PNX_AUDIO_VOICES; i++) {
    if (!s_voices[i].active) return i;
    if (s_voices[i].priority > priority) continue;
    // Among candidates, take the quietest: losing a fading effect is less audible than
    // losing whatever just started.
    if (weakest < 0 || s_voices[i].volume < s_voices[weakest].volume) weakest = i;
  }
  return weakest;
}

static void env_begin(Voice *v, const PnxEnvelope *env) {
  if (!env || (env->attack_ms == 0 && env->decay_ms == 0 && env->release_ms == 0)) {
    v->stage = ENV_OFF;             // no envelope: full level, hard stop
    v->level = 1 << 16;
    v->rate = 0;
    return;
  }
  v->env = *env;
  v->stage = ENV_ATTACK;
  v->level = 0;
  const uint32_t rate = output_rate();
  v->rate = env->attack_ms ? (int32_t)((1 << 16) / ((env->attack_ms * rate) / 1000 + 1))
                           : (1 << 16);
}

uint8_t pnx_audio_play_pri(const int8_t *pcm, uint32_t samples, uint32_t loop_start,
                           uint32_t sample_hz, uint8_t volume, uint8_t priority,
                           const PnxEnvelope *env) {
  if (!s_on || !pcm || samples == 0 || sample_hz == 0) return PNX_AUDIO_NO_VOICE;

  const int slot = claim_voice(priority);
  if (slot < 0) return PNX_AUDIO_NO_VOICE;   // everything playing is more important

  Voice *v = &s_voices[slot];
  memset(v, 0, sizeof(*v));
  v->pcm = pcm;
  v->samples = samples;
  v->loop_start = loop_start < samples ? loop_start : PNX_AUDIO_NO_LOOP;
  // 16.16 ratio of source to output rate, without a 64-bit divide -- that pulled in
  // __udivmoddi4 at 754 bytes, which the size report shows as "(unattributed)".
  // Shifting 16 in two stages keeps every intermediate inside uint32 for any sample rate
  // a short effect will use.
  v->step = ((sample_hz << 8) / output_rate()) << 8;
  if (v->step == 0) v->step = 1;
  v->volume = volume;
  v->priority = priority;
  v->active = true;
  env_begin(v, env);
  return (uint8_t)slot;
}

uint8_t pnx_audio_note(PnxWaveform wave, uint8_t midi_note, uint8_t volume,
                       const PnxEnvelope *env, uint8_t priority) {
  if (!s_on || wave >= PNX_WAVE_COUNT) return PNX_AUDIO_NO_VOICE;

  const int slot = claim_voice(priority);
  if (slot < 0) return PNX_AUDIO_NO_VOICE;

  Voice *v = &s_voices[slot];
  memset(v, 0, sizeof(*v));
  v->pcm = s_wavetable[wave];
  v->samples = CYCLE;
  v->loop_start = 0;
  // Set directly rather than derived from an integer frequency, which is where the
  // tuning error came from.
  v->step = note_step(midi_note, CYCLE);
  v->volume = volume;
  v->priority = priority;
  v->active = true;
  env_begin(v, env);
  return (uint8_t)slot;
}

void pnx_audio_release_in(uint8_t voice, uint16_t ms) {
  if (voice >= PNX_AUDIO_VOICES || !s_voices[voice].active) return;
  Voice *v = &s_voices[voice];
  const uint32_t samples = ((uint32_t)ms * output_rate()) / 1000u + 1u;

  // Even a voice with no envelope gets a fade, because the alternative is a step in the
  // waveform. There is no such thing as a silent hard cut.
  if (v->stage == ENV_OFF) v->level = 1 << 16;
  v->stage = ENV_RELEASE;
  v->rate = -(int32_t)(v->level / samples);
  if (v->rate == 0) v->rate = -1;
}

void pnx_audio_release(uint8_t voice) {
  if (voice >= PNX_AUDIO_VOICES || !s_voices[voice].active) return;
  pnx_audio_release_in(voice, s_voices[voice].env.release_ms);
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

// Sums active voices into s_mix, then converts in place to the output format and returns
// the byte count. Clamping once at the end is what makes several loud voices distort rather
// than invert.
static uint32_t mix(uint32_t count) {
  memset(s_mix, 0, sizeof(int16_t) * count);
  uint8_t active = 0;

  for (int v = 0; v < PNX_AUDIO_VOICES; v++) {
    Voice *o = &s_voices[v];
    if (!o->active) continue;
    active++;

    for (uint32_t n = 0; n < count; n++) {
      uint32_t index = o->phase >> 16;

      if (index >= o->samples) {
        if (o->loop_start == PNX_AUDIO_NO_LOOP) { o->active = false; break; }

        // Subtract the loop length rather than assigning the loop point, so the
        // fractional phase survives the wrap. Assigning discarded the overshoot every
        // cycle, which quantised the period to a whole number of samples: a 440Hz note
        // came out at 432Hz, 32 cents flat.
        o->phase -= (o->samples - o->loop_start) << 16;
        index = o->phase >> 16;

        // Re-read and carry on rather than `continue`. Skipping the iteration left this
        // output sample at zero -- a one-sample dropout on every wrap, so 440 of them per
        // second on a 440Hz note. Because the wrap drifts against the sample grid, which
        // samples got zeroed beat against the tone, and that is heard as pulsing. It was
        // audible on a single sustained voice, which is exactly what the control pattern
        // was built to isolate.
        if (index >= o->samples) { o->active = false; break; }   // pathological step
      }

      // Envelope advance. One add and a stage test per sample, which at 600 samples x
      // 8 voices is nothing against ~30 ms of idle CPU.
      if (o->stage != ENV_OFF) {
        o->level += o->rate;
        const uint32_t sr = output_rate();
        switch (o->stage) {
          case ENV_ATTACK:
            if (o->level >= (1 << 16)) {
              o->level = 1 << 16;
              o->stage = ENV_DECAY;
              const int32_t target = (int32_t)o->env.sustain << 8;
              o->rate = o->env.decay_ms
                  ? -(int32_t)(((1 << 16) - target) / ((o->env.decay_ms * sr) / 1000 + 1))
                  : 0;
              // rate must be cleared too, or sustain keeps applying the attack ramp and
              // the note swells indefinitely.
              if (!o->env.decay_ms) {
                o->level = target;
                o->rate = 0;
                o->stage = ENV_SUSTAIN;
              }
            }
            break;
          case ENV_DECAY:
            if (o->level <= ((int32_t)o->env.sustain << 8)) {
              o->level = (int32_t)o->env.sustain << 8;
              o->rate = 0;
              o->stage = ENV_SUSTAIN;
            }
            break;
          case ENV_RELEASE:
            if (o->level <= 0) { o->level = 0; o->active = false; }
            break;
          default: break;
        }
        if (!o->active) break;
      }

      // Linear interpolation between table entries.
      //
      // The step is almost never a whole number -- 440Hz on a 64-entry cycle at 16kHz is
      // 1.76 -- so nearest-neighbour lookup reads an irregular pattern of entries that
      // repeats at its own frequency, adding a tone that is not in the signal. At 440Hz
      // that artefact lands near 640Hz and is plainly audible as roughness.
      //
      // Interpolating costs one multiply and one shift per sample per voice. Against ~30ms
      // of idle CPU per frame that is free, and it is the difference between a tone and a
      // buzz.
      const uint32_t frac = o->phase & 0xFFFF;
      const int32_t a0 = o->pcm[index];
      uint32_t next = index + 1u;
      if (next >= o->samples) {
        // Wrap to the loop point so the seam interpolates too; a one-cycle wavetable is
        // continuous across it, and treating the end as a cliff would tick once per cycle.
        next = (o->loop_start == PNX_AUDIO_NO_LOOP) ? index : o->loop_start;
      }
      const int32_t a1 = o->pcm[next];
      const int32_t sample = a0 + (((a1 - a0) * (int32_t)frac) >> 16);

      const int32_t enveloped = (o->stage == ENV_OFF)
          ? sample
          : ((sample * (o->level >> 8)) >> 8);
      s_mix[n] += (int16_t)((enveloped * o->volume) >> 8);
      o->phase += o->step;
    }
  }

  // Clamped to 8-bit range even for 16-bit output, which then shifts left 8. The mixer has
  // more resolution than that now the accumulator is the output buffer, but widening the
  // range would change the loudness of every existing instrument.
  int8_t *out8 = (int8_t *)s_mix;
  for (uint32_t n = 0; n < count; n++) {
    int32_t v = s_mix[n] >> MIX_HEADROOM_SHIFT;

    if (s_lp_a) {
      // y += (x - y) * a. Percussion still reads as a hit: noise is broadband and its
      // transient survives a gentle slope.
      s_lp += ((v << 8) - s_lp) * s_lp_a >> 16;
      v = s_lp >> 8;
    }

    const int8_t c = (int8_t)(v < -128 ? -128 : (v > 127 ? 127 : v));
    if (s_16bit) s_mix[n] = (int16_t)(c << 8);   // same entry, read above
    else         out8[n]  = c;                   // byte n lives in entry n/2, already read
  }

  s_stats.active_voices = active;
  return s_16bit ? count * 2u : count;
}

// Hands the freshly mixed bytes to the device. A short write is normal -- it means the
// buffer is full -- and the tail must not be dropped: dropped samples are a hole in the
// waveform the mixer cannot regenerate, because the voice phases have moved on. It stays
// in s_mix and s_carry_head records where the device stopped.
static void offer(uint32_t bytes) {
  const size_t wrote = pnx_platform_audio_write(s_mix, bytes);
  s_stats.written += (uint32_t)wrote;

  if (wrote < bytes) {
    // The first short write reveals the buffer depth, which the device will not tell us.
    if (s_stats.capacity == 0) s_stats.capacity = s_stats.written;
    s_stats.short_writes++;
    s_carry_bytes = bytes;
    s_carry_head = (uint32_t)wrote;
  } else {
    s_carry_bytes = s_carry_head = 0;
  }
  s_stats.carried = s_carry_bytes - s_carry_head;
}

void pnx_audio_update(uint32_t now_ms) {
  if (!s_on) return;
  if (s_start_ms == 0) s_start_ms = now_ms;

  // A drained stream leaves Playing and resumes on the next write, which is audible as
  // the sound restarting -- and invisible to the deficit, which only measures aggregate
  // supply. Count the transitions.
  const PnxAudioState st = pnx_platform_audio_state();
  if (s_stats.state == PNX_AUDIO_PLAYING && st != PNX_AUDIO_PLAYING) {
    if (s_stats.left_playing < 0xFFFF) s_stats.left_playing++;
  }
  s_stats.state = (uint8_t)st;

  // Longest gap between feeds. A stall longer than the lead starves the stream no matter
  // how correct the aggregate rate is, and blocking calls -- APP_LOG over Bluetooth being
  // the obvious one -- are exactly how a gap appears intermittently.
  if (s_last_update_ms) {
    const uint32_t gap = now_ms - s_last_update_ms;
    if (gap < 60000u) {
      s_stats.gap_ms = (uint16_t)gap;
      if (gap > s_stats.worst_gap_ms) s_stats.worst_gap_ms = (uint16_t)gap;
    }
  }
  s_last_update_ms = now_ms;

  const uint32_t elapsed = now_ms - s_start_ms;

  // No fill-level query exists, so underrun is inferred: if playback has consumed more
  // than we ever wrote, the speaker ran dry. This is the only signal available.
  const uint32_t consumed = elapsed * s_byte_rate / 1000u;
  if (consumed > s_stats.written) {
    const uint32_t deficit = consumed - s_stats.written;
    if (deficit > s_stats.worst_deficit) s_stats.worst_deficit = deficit;
  }

  // Anything held over goes first, in order. Mixing more while a remainder is pending
  // would reorder the stream.
  if (s_carry_bytes > s_carry_head) {
    const uint32_t left = s_carry_bytes - s_carry_head;
    const size_t wrote = pnx_platform_audio_write((const uint8_t *)s_mix + s_carry_head, left);
    s_stats.written += (uint32_t)wrote;
    s_carry_head += (uint32_t)wrote;
    if (s_carry_head < s_carry_bytes) {
      s_stats.short_writes++;
      return;                 // still backed up; do not mix ahead of it
    }
    s_carry_bytes = s_carry_head = 0;
  }

  const uint32_t target = (elapsed + s_lead_ms) * s_byte_rate / 1000u;
  if (target <= s_stats.written) return;

  uint32_t want_bytes = target - s_stats.written;
  const uint32_t max_bytes = s_16bit ? PNX_AUDIO_CHUNK * 2u : PNX_AUDIO_CHUNK;
  if (want_bytes > max_bytes) want_bytes = max_bytes;

  // Align down, and skip the feed entirely rather than writing a runt. The next call will
  // have a whole quantum to offer, which keeps every write the same shape.
  want_bytes -= want_bytes % PNX_AUDIO_QUANTUM;
  if (want_bytes == 0) return;

  const uint32_t samples = s_16bit ? want_bytes / 2u : want_bytes;
  if (samples == 0) return;

  const uint32_t out_bytes = mix(samples);
  s_stats.feeds++;
  if (samples < s_stats.feed_min || s_stats.feed_min == 0)
    s_stats.feed_min = (uint16_t)samples;
  if (samples > s_stats.feed_max) s_stats.feed_max = (uint16_t)samples;

  offer(out_bytes);
}

// a = 1 - exp(-2*pi*fc/rate), approximated as 2*pi*fc/(rate + 2*pi*fc) which is accurate
// enough well below Nyquist and needs no floating point.
void pnx_audio_set_lowpass(uint16_t cutoff_hz) {
  s_cutoff_hz = cutoff_hz;
  if (!cutoff_hz || !s_on) { s_lp_a = cutoff_hz ? s_lp_a : 0; return; }

  const uint32_t rate = output_rate();
  if (cutoff_hz * 2u >= rate) { s_lp_a = 0; return; }   // above Nyquist: no filtering

  const uint32_t wc = (cutoff_hz * 6283u) / 1000u;      // 2*pi*fc
  // 32-bit throughout: one 64-bit division costs 754 bytes of __udivmoddi4, and the Nyquist
  // check above bounds wc at 50,264, so wc << 16 stays under 3.3e9 and inside a uint32.
  s_lp_a = (int32_t)((wc << 16) / (rate + wc));
}

uint16_t pnx_audio_lowpass(void) { return s_cutoff_hz; }

PnxAudioFormat pnx_audio_format(void) { return s_format; }

bool pnx_audio_reopen(PnxAudioFormat format, uint8_t volume) {
  pnx_audio_stop_all();
  pnx_platform_audio_close();
  s_on = false;
  return pnx_audio_init(format, volume);
}

void pnx_audio_set_lead(uint16_t ms) {
  s_lead_ms = ms ? ms : 1u;
  // Reset the deficit so a sweep is judged on the new setting rather than on the worst
  // value any earlier setting produced.
  s_stats.worst_deficit = 0;
  s_stats.short_writes = 0;
  s_stats.feeds = 0;
  s_stats.feed_min = s_stats.feed_max = 0;
  s_stats.worst_gap_ms = 0;
  s_last_update_ms = 0;
}

uint16_t pnx_audio_lead(void) { return s_lead_ms; }

const PnxAudioStats *pnx_audio_stats(void) { return &s_stats; }

#endif  // PNX_USE_AUDIO
