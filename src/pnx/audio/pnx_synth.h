// Subtractive synth voice -- a MEASUREMENT SPIKE, not a shipped feature yet.
//
// The question this exists to answer: can a voice with three detuned oscillators, a
// resonant filter with its own envelope, a volume envelope, an LFO, and sends into a
// global reverb and chorus run four at a time at 16 kHz on the watch, and what does it
// cost in RAM.
//
// It is a spike because MEASUREMENTS.md line 618 names audio as "the category with no
// measured cost yet and the one most able to blow the budget", and because the existing
// mixer's per-voice cost is roughly a dozen operations while this is closer to two
// hundred per sample across four channels. That is probably affordable against ~35 ms of
// idle CPU per frame. "Probably" is the word this project's measurement discipline exists
// to refuse.
//
// So the point is not a single number. It is a per-feature BREAKDOWN: `PnxSynthConfig`
// turns each expensive thing off independently, so the answer is not "200 ops" but "the
// third oscillator costs X, resonance costs Y, reverb costs Z" -- which is what actually
// decides whether this ships as a three-oscillator synth or a two-oscillator one.
//
// Defaults to compiled out. Nothing pays for it until the numbers say it is worth having.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_SYNTH

#include "pnx_audio.h"

#include <stdbool.h>
#include <stdint.h>

#define PNX_SYNTH_OSCILLATORS 3

// Four channels, TWO voices each.
//
// A sequencer retriggers a channel on every row, and a single voice per channel cannot
// join the notes: whatever it does at the boundary -- snap the level to zero, or keep the
// old amplitude and change pitch under it -- is a step in the middle of a waveform, heard
// as a click on every note. Measured on a waveform dump: 41 discontinuities across 8
// seconds, one per note, invisible to the clip and underrun counters because nothing
// clips and nothing runs dry. The samples are all in range, they just do not join up.
//
// The plain mixer never had this because it releases the old voice and allocates a NEW
// one, so the two overlap for a few milliseconds. This is that, for synth slots: the new
// note takes the free voice of the pair and the displaced one gets a fast release.
//
// The overlap is brief, so average cost is barely changed -- only the moment of a note
// change runs both.
#define PNX_SYNTH_SLOTS       4     // the sequencer's four channels
#define PNX_SYNTH_VOICES      (PNX_SYNTH_SLOTS * 2)

// Where an LFO's output goes. One LFO with a target covers vibrato, tremolo, pulse-width
// modulation and a filter wobble -- four recognisable effects for one implementation,
// which is why it is a routing enum rather than four separate features.
typedef enum {
  PNX_LFO_OFF = 0,
  PNX_LFO_PITCH,      // vibrato
  PNX_LFO_VOLUME,     // tremolo
  PNX_LFO_DUTY,       // PWM -- the most recognisable chiptune texture there is
  PNX_LFO_CUTOFF,     // filter wobble
} PnxLfoTarget;

typedef enum {
  PNX_FILTER_OFF = 0,
  PNX_FILTER_LOWPASS,
  PNX_FILTER_HIGHPASS,
  PNX_FILTER_BANDPASS,
} PnxFilterMode;

// One oscillator of an instrument. `detune` is in CENTS relative to the played note, not
// an absolute pitch: two oscillators a few cents apart is what makes a lead sound thick,
// and expressing that as absolute frequency would make an instrument only work at one
// note.
typedef struct {
  uint8_t wave;        // PnxWaveform
  uint8_t volume;      // 0..255, mixed within the voice before the filter
  int16_t detune;      // cents, -1200..+1200
  int8_t octave;       // relative to the note; the first oscillator is the reference
  uint8_t duty;        // 0..255, square only. 128 is a 50% square.
} PnxOscillator;

// An instrument: what a slot holds. Deliberately a plain struct with no pointers, so the
// packed on-disk form is this laid out flat and the sequencer can index instruments by
// number without a scan.
typedef struct {
  PnxOscillator osc[PNX_SYNTH_OSCILLATORS];
  uint8_t osc_count;              // 1..PNX_SYNTH_OSCILLATORS

  PnxEnvelope amp;                // volume ADSR
  PnxEnvelope cutoff;             // filter ADSR -- swept independently of volume

  uint8_t filter_mode;            // PnxFilterMode
  uint8_t cutoff_base;            // 0..255 mapped across the audible range
  uint8_t resonance;              // 0..255. Without this a cutoff sweep is just quieter.
  uint8_t cutoff_env_amount;      // how far the cutoff envelope moves the cutoff

  uint8_t lfo_target;             // PnxLfoTarget
  uint8_t lfo_rate;               // 0..255 -> roughly 0.1..20 Hz
  uint8_t lfo_depth;

  // Pitch envelope. Without one there are no drums: a kick is a fast downward sweep and
  // a snare is noise plus a sweep, and doing them as PCM instead costs 16,000 bytes a
  // second against ~160 for a whole song.
  int16_t pitch_env_amount;       // cents at the start of the note
  uint8_t pitch_env_decay;        // how fast it falls back to the played pitch

  // Effects are SENDS into one global instance each, not per-instrument instances. A
  // reverb is four comb filters and two allpasses; per instrument that is N sets of
  // delay lines, and MEASUREMENTS.md is explicit that on a 64 KB static ceiling "a buffer
  // per pipeline stage is the expensive habit, not the code".
  uint8_t reverb_send;
  uint8_t chorus_send;
} PnxInstrument;

// Which expensive things are switched on. The whole point of the spike: each of these is
// measured by turning it off and re-running, so the cost is attributed rather than
// guessed at in aggregate.
typedef struct {
  uint8_t oscillators;   // 1..PNX_SYNTH_OSCILLATORS
  bool filter;
  bool resonance;
  bool lfo;
  bool pitch_env;
  bool reverb;
  bool chorus;
} PnxSynthConfig;

// Everything on, which is the case that has to fit.
PnxSynthConfig pnx_synth_worst_case(void);

// Allocates the wavetables and the effect delay lines. Returns false if the allocation
// fails, which on this platform is a real outcome rather than a formality.
bool pnx_synth_init(uint32_t sample_rate);
void pnx_synth_shutdown(void);

// Heap held by the synth, so the RAM half of the answer is a number and not an estimate.
uint32_t pnx_synth_bytes(void);
uint32_t pnx_synth_effect_bytes(void);

// Output attenuation, as a right shift. Four voices plus reverb genuinely do not fit the
// 8-bit domain the mixer clamps to -- the device measured 163 against 127 -- so this is
// the knob that trades loudness for not clipping. 1 by default, matching the mixer's own
// MIX_HEADROOM_SHIFT. Too much attenuation is not "safe": every bit given up here is a bit
// of an 8-bit output spent on silence, and that is quantisation noise rather than clipping.
// Anything using the synth seriously wants a 16-bit output format instead.
void pnx_synth_set_headroom(uint8_t shift);
uint8_t pnx_synth_headroom(void);

void pnx_synth_set_config(const PnxSynthConfig *cfg);
const PnxSynthConfig *pnx_synth_config(void);

// Loads an instrument into one of the four slots. Takes effect on the NEXT note rather
// than immediately: a note already sounding keeps the instrument it started with, because
// swapping oscillator counts and filter state under a running voice is how you get a
// click in the middle of a held note.
void pnx_synth_set_instrument(uint8_t slot, const PnxInstrument *inst);

void pnx_synth_note_on(uint8_t slot, uint8_t midi_note, uint8_t velocity);
void pnx_synth_note_off(uint8_t slot);
void pnx_synth_all_off(void);
uint8_t pnx_synth_active_voices(void);

// Renders `count` samples, summing into `out`. Additive so the spike can share the
// existing mixer's accumulator rather than needing one of its own.
void pnx_synth_render(int16_t *out, uint32_t count);

// The measurement itself.
//
// Timed by REPETITION, because `time_ms()` has 1 ms resolution and MEASUREMENTS.md is
// explicit that sub-millisecond work must never be timed by multiplying a millisecond
// clock. Renders `chunks` chunks of `count` samples with every voice sounding, and
// returns nanoseconds per sample so the figure survives being small.
//
// `checksum` is written with a value derived from the rendered audio and must be read by
// the caller: a benchmark kernel with no observable side effect gets deleted, which has
// already cost this project a 16 KB array.
typedef struct {
  uint32_t elapsed_ms;
  uint32_t samples;
  uint32_t ns_per_sample;
  uint32_t pct_of_realtime;   // hundredths of a percent of one core at the sample rate
  int32_t checksum;
} PnxSynthBench;

void pnx_synth_bench(uint32_t chunks, uint32_t count, PnxSynthBench *out);

#endif  // PNX_USE_SYNTH
