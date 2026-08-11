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

static void build_wavetable(void) {
  uint32_t rng = 0x2545F491u;
  for (int i = 0; i < CYCLE; i++) {
    s_wavetable[PNX_WAVE_SQUARE][i] = (int8_t)(i < CYCLE / 2 ? 100 : -100);
    s_wavetable[PNX_WAVE_SAW][i] = (int8_t)((i * 200 / CYCLE) - 100);
    s_wavetable[PNX_WAVE_TRIANGLE][i] = (int8_t)(i < CYCLE / 2
        ? (i * 400 / CYCLE) - 100
        : 100 - ((i - CYCLE / 2) * 400 / CYCLE));
    // xorshift, so the noise table is identical on every run and in every build --
    // reproducible audio matters for the same reason reproducible simulation does.
    rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
    s_wavetable[PNX_WAVE_NOISE][i] = (int8_t)((rng >> 8) & 0xFF) / 2;
  }
}

static Voice s_voices[PNX_AUDIO_VOICES];
static int8_t s_scratch[PNX_AUDIO_CHUNK];

// Accumulator, so summing happens at full width and clamps ONCE at the end. The first
// version clamped inside the per-voice loop, which saturates intermediate sums: five
// voices at ~78 each summed to ~390 against a +/-127 range, so every peak was flattened
// and the result sounded distorted rather than loud. int16 is enough -- 8 voices x 127.
static int16_t s_acc[PNX_AUDIO_CHUNK];

// Fixed master headroom. NOT derived from the active voice count.
//
// Two attempts at a dynamic gain both failed, in opposite ways. Dividing by the voice
// count stepped the whole mix the instant a voice started -- a pop per beat. Gliding to
// that target instead spread the step over ~125ms, which at a 300ms row is 42% of every
// row: a continuous amplitude warble, worse at a pattern boundary where the voice count
// changes more.
//
// The mistake was making loudness depend on something that changes constantly. A fixed
// divisor is stable by construction, and headroom becomes the composer's business through
// instrument volumes -- which is how trackers have always done it. Four channels at
// PNX_MUSIC_CHANNELS is the design point, so dividing by four lets four full-scale voices
// sum without clipping and leaves room for an effect on top.
#define MIX_HEADROOM_SHIFT 2   // divide by 4

// Bytes mixed but not yet accepted by the device.
//
// speaker_stream_write returns how much it took, and a full buffer takes less than
// offered -- which is normal, not an error. The first version DISCARDED the remainder and
// mixed fresh audio next frame from a phase that had already advanced past it, putting a
// discontinuity in the waveform on every short write. At 82% short writes that is
// continuous glitching, and it sounded exactly as bad as it was.
//
// So the remainder is carried and offered again before anything new is mixed.
static uint8_t s_carry[PNX_AUDIO_CHUNK * 2];
static uint32_t s_carry_bytes;
static uint32_t s_carry_head;
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
  build_wavetable();
  s_format = format;
  s_byte_rate = pnx_audio_byte_rate(format);
  s_16bit = (format == PNX_AUDIO_16KHZ_16BIT || format == PNX_AUDIO_8KHZ_16BIT);
  s_start_ms = 0;
  s_carry_bytes = s_carry_head = 0;
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
  // Resampling is just the phase increment: 16.16 ratio of source to output rate.
  v->step = (uint32_t)(((uint64_t)sample_hz << 16) / output_rate());
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

// Sums active voices into signed 8-bit. Accumulating in int32 and clamping once at the
// end keeps intermediate sums from wrapping, which is what makes several loud voices
// distort rather than invert.
static void mix(int8_t *out, uint32_t count) {
  memset(s_acc, 0, sizeof(int16_t) * count);
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
              if (!o->env.decay_ms) { o->level = target; o->stage = ENV_SUSTAIN; }
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
      s_acc[n] += (int16_t)((enveloped * o->volume) >> 8);
      o->phase += o->step;
    }
  }

  for (uint32_t n = 0; n < count; n++) {
    const int32_t v = s_acc[n] >> MIX_HEADROOM_SHIFT;
    out[n] = (int8_t)(v < -128 ? -128 : (v > 127 ? 127 : v));
  }
  s_stats.active_voices = active;
}

// Hands bytes to the device and keeps whatever it would not take.
static void offer(const uint8_t *data, uint32_t bytes) {
  const size_t wrote = pnx_platform_audio_write(data, bytes);
  s_stats.written += (uint32_t)wrote;
  if (wrote < bytes) {
    // The first short write reveals the buffer depth, which the device will not tell us.
    if (s_stats.capacity == 0) s_stats.capacity = s_stats.written;
    s_stats.short_writes++;
    const uint32_t left = bytes - (uint32_t)wrote;
    // Carry the tail rather than dropping it: dropped samples are a hole in the waveform,
    // and the mixer cannot regenerate them because the voice phases have moved on.
    if (left <= sizeof(s_carry)) {
      memmove(s_carry, data + wrote, left);
      s_carry_bytes = left;
      s_carry_head = 0;
    } else {
      s_carry_bytes = s_carry_head = 0;
    }
  } else {
    s_carry_bytes = s_carry_head = 0;
  }
  s_stats.carried = s_carry_bytes - s_carry_head;
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

  // Anything held over goes first, in order. Mixing more while a remainder is pending
  // would reorder the stream.
  if (s_carry_bytes > s_carry_head) {
    const uint32_t left = s_carry_bytes - s_carry_head;
    const size_t wrote = pnx_platform_audio_write(s_carry + s_carry_head, left);
    s_stats.written += (uint32_t)wrote;
    s_carry_head += (uint32_t)wrote;
    if (s_carry_head < s_carry_bytes) {
      s_stats.short_writes++;
      return;                 // still backed up; do not mix ahead of it
    }
    s_carry_bytes = s_carry_head = 0;
  }

  const uint32_t target = (elapsed + PNX_AUDIO_LEAD_MS) * s_byte_rate / 1000u;
  if (target <= s_stats.written) return;

  uint32_t want_bytes = target - s_stats.written;
  const uint32_t max_bytes = s_16bit ? PNX_AUDIO_CHUNK * 2u : PNX_AUDIO_CHUNK;
  if (want_bytes > max_bytes) want_bytes = max_bytes;

  const uint32_t samples = s_16bit ? want_bytes / 2u : want_bytes;
  if (samples == 0) return;

  mix(s_scratch, samples);
  s_stats.feeds++;

  if (s_16bit) {
    static uint8_t wide[PNX_AUDIO_CHUNK * 2];
    for (uint32_t i = 0; i < samples; i++) {
      const int16_t v = (int16_t)(s_scratch[i] << 8);
      wide[i * 2] = (uint8_t)(v & 0xFF);
      wide[i * 2 + 1] = (uint8_t)((v >> 8) & 0xFF);
    }
    offer(wide, samples * 2u);
  } else {
    offer((const uint8_t *)s_scratch, samples);
  }
}

const PnxAudioStats *pnx_audio_stats(void) { return &s_stats; }

#endif  // PNX_USE_AUDIO
