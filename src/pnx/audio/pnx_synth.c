#include "pnx_synth.h"

#if PNX_USE_SYNTH

#include "../core/pnx_diag.h"
#include "../core/pnx_fx.h"
#include "../platform/pnx_platform.h"

#include <stdlib.h>
#include <string.h>

// ------------------------------------------------------------------ wavetables
//
// One cycle per waveform, as the mixer already does. Pitch then needs no new code: an
// L-entry cycle stepped at note_hz * L / rate advances exactly note_hz cycles a second.
//
// 64 entries, matching the mixer. Small enough that the whole table stays in cache and
// large enough that linear interpolation between entries is not audibly stepped.
#define CYCLE 64

// BAND-LIMITED, one table per waveform per octave.
//
// A 64-entry table carries harmonics 1..32. Played at C6 that puts 25 of them above the
// 8 kHz Nyquist, where they fold back as inharmonic noise -- and three detuned saws is the
// worst case there is for it. Identified by elimination on device: no clipping (counted),
// no underruns (counted), still harsh. That leaves aliasing, and this is the standard fix.
//
// Each octave gets a table built by additive synthesis carrying only the harmonics that
// stay under Nyquist at the TOP of that octave. Low notes keep all 31 and stay bright;
// high notes get four and stay clean. The cost is 2 KB and a table selection per pitch
// update -- not per sample -- so the inner loop is unchanged.
#define MIPS 8

// Harmonics that fit under Nyquist at the top of each octave, at 16 kHz.
static const uint8_t MIP_HARMONICS[MIPS] = { 31, 31, 31, 31, 31, 16, 8, 4 };

// One cycle of sine in Q12, for the additive build. A table rather than sinf() because
// pulling libm into a 64 KB budget for eight table builds is a poor trade -- and the
// harmonics of a 64-point fundamental land exactly on this grid, so it is not even an
// approximation.
static const int16_t SINE_Q12[CYCLE] = {
	0,
	401,
	799,
	1189,
	1567,
	1931,
	2276,
	2598,
	2896,
	3166,
	3406,
	3612,
	3784,
	3920,
	4017,
	4076,
	4096,
	4076,
	4017,
	3920,
	3784,
	3612,
	3406,
	3166,
	2896,
	2598,
	2276,
	1931,
	1567,
	1189,
	799,
	401,
	0,
	-401,
	-799,
	-1189,
	-1567,
	-1931,
	-2276,
	-2598,
	-2896,
	-3166,
	-3406,
	-3612,
	-3784,
	-3920,
	-4017,
	-4076,
	-4096,
	-4076,
	-4017,
	-3920,
	-3784,
	-3612,
	-3406,
	-3166,
	-2896,
	-2598,
	-2276,
	-1931,
	-1567,
	-1189,
	-799,
	-401,
};

// [waveform][octave][phase].
static int8_t (*s_wave)[MIPS][CYCLE];

static uint32_t s_rate = 16000u;
static bool s_ready;

// ----------------------------------------------------------------------- effects
//
// Freeverb's comb and allpass lengths, scaled from 44.1 kHz to ours. Prime-ish and
// mutually coprime so the combs do not line up and ring at one pitch.
//
// These are the RAM cost of the whole feature and the reason effects are GLOBAL SENDS
// rather than per-instrument: at 16 kHz these lines come to ~5 KB, which is comparable
// to the entire original mixer buffer set (7,168 B) that MEASUREMENTS.md called out as
// the expensive habit on a 64 KB static ceiling. Four instruments each with their own
// reverb would be 20 KB and would not fit.
#define COMBS	4
#define ALLPASS 2
static const uint16_t COMB_LEN[COMBS]	   = { 405, 431, 463, 492 };
static const uint16_t ALLPASS_LEN[ALLPASS] = { 202, 160 };
#define CHORUS_LEN 480 // 30 ms at 16 kHz

typedef struct
{
	int16_t* buf;
	uint16_t len, at;
} Line;

// Voice-major block size. See pnx_synth_render for why this exists and why it is small.
#define BLOCK 64

// Per-block accumulators: the dry sum, and one send per global effect. The effects are
// global, so every voice's contribution has to be summed before they run -- which is the
// entire reason these buffers exist. 768 bytes at BLOCK 64; 9 KB if the block were the
// full chunk, on a platform where a buffer per stage is already the recorded mistake.
static int32_t* s_dry;
static int32_t* s_rev_send;
static int32_t* s_cho_send;

static Line s_comb[COMBS];
static Line s_allpass[ALLPASS];
static Line s_chorus;
static int16_t s_comb_store[COMBS]; // one-pole damping inside each comb
static uint32_t s_chorus_phase;
static uint32_t s_effect_bytes;
static uint32_t s_total_bytes;
static uint8_t* s_block_base; // start of the single allocation, for free()

// ------------------------------------------------------------------------ voices

typedef enum
{
	ENV_OFF = 0,
	ENV_ATTACK,
	ENV_DECAY,
	ENV_SUSTAIN,
	ENV_RELEASE
} EnvStage;

typedef struct
{
	uint32_t phase, step;
	uint8_t mip; // which band-limited table this oscillator reads
} Osc;

// Field order groups by what the comments explain (identity, oscillators, amp envelope,
// filter envelope, pitch, filter state), not by size -- byte-optimal reordering would
// save 11 padding bytes per voice (PNX_SYNTH_VOICES of them; see VOICES_BYTES in
// pnx_audio.c for the real total) at the cost of scattering that grouping. Left as a
// known, considered tradeoff rather than either silently applied or silently ignored.
// NOLINTNEXTLINE(clang-analyzer-optin.performance.Padding)
typedef struct
{
	bool active;
	uint8_t note, velocity;
	PnxInstrument inst; // a COPY: the slot's instrument may be replaced while this
						// note is still sounding, and a note must finish with the
						// instrument it started on.
	Osc osc[PNX_SYNTH_OSCILLATORS];

	int32_t amp_level, amp_rate;
	uint8_t amp_stage;
	int32_t cut_level, cut_rate;
	uint8_t cut_stage;

	uint32_t lfo_phase;
	int32_t pitch_env; // cents, decaying to zero
	uint8_t mod_count; // samples until the next pitch recompute; see PITCH_EVERY
	bool pitch_moves;  // does anything modulate this voice's pitch at all

	// State-variable filter state. Chamberlin's form: two integrators, one multiply each,
	// and it gives lowpass, highpass and bandpass from the same three lines -- which is why
	// the filter mode costs nothing extra over having one fixed type.
	int32_t flt_low, flt_band;
} Voice;

// Folded into pnx_synth_init's own single malloc'd block (below) rather than resident
// arrays -- PNX_USE_SYNTH is already opt-in (off by default), but a project that DOES
// turn it on used to pay this 1,288 bytes of permanent .bss whether or not the synth was
// ever actually running, on top of the malloc'd block it was already paying for the
// effect tail/wavetables. One allocation, one lifetime, same as everything else this
// module already owns.
static Voice* s_voice;
static PnxInstrument* s_slot;

// How fast a displaced note fades. Long enough to have no edge, short enough not to muddy
// the note replacing it -- the same few milliseconds the plain mixer uses for the same job.
#define STEAL_MS 4
static PnxSynthConfig s_cfg;
static int32_t s_osc_recip = 65536; // 65536 / oscillator count
// One bit, and this default has been wrong in both directions already, so the reasoning
// is recorded rather than the conclusion.
//
// Zero looked right: the mixer applies MIX_HEADROOM_SHIFT before its own clamp, so a bit
// here as well should attenuate twice. It is wrong because that shift is sized for the
// MIXER's voices -- eight of them at +-127, relying on the clamp for the loud cases -- and
// the synth arrives on top of that with four voices of its own plus wet effects. Measured
// on the real song: peak 239 against 127, with 4.5% of all output samples clipped. That is
// heard as static, and static is indistinguishable by ear from aliasing or an underrun.
//
// Three bits was the first guess and was wrong the other way: peak 12 of 127 trades
// clipping for quantisation noise on an output that only has eight bits to spend.
static uint8_t s_headroom = 1;

// Blocks of effect tail still to process after the last voice ends. See TAIL_BLOCKS.
static uint16_t s_tail;

// --------------------------------------------------------------------- helpers

// MIDI note to Hz in Q16, without floating point. 440 * 2^((n-69)/12), computed as a
// table of the twelve semitones in an octave and a shift for the octave.
static const uint32_t SEMITONE_Q16[12] = {
	// 2^(n/12) in Q16, n = 0..11
	65536, 69433, 73562, 77936, 82570, 87480, 92682, 98195, 104037, 110228, 116788, 123741
};

static uint32_t note_hz_q16(int32_t midi_note, int32_t cents)
{
	// Cents fold into the note as a fractional semitone. A 1200-cent range is one octave,
	// which is all detune and pitch envelopes need.
	int32_t total = midi_note * 100 + cents;
	if (total < 0)
		total = 0;
	int32_t semis	   = total / 100;
	int32_t frac_cents = total - semis * 100;

	int32_t octave = semis / 12 - 5; // relative to MIDI 60 = C4
	int32_t within = semis % 12;

	// 261.63 Hz (C4) in Q16.
	uint32_t hz = (uint32_t)((261630u * 65536u) / 1000u);
	hz			= (uint32_t)(((uint64_t)hz * SEMITONE_Q16[within]) >> 16);
	if (octave > 0)
		hz <<= octave;
	else if (octave < 0)
		hz >>= -octave;

	// Interpolate the last hundredth of a semitone linearly. The error against the true
	// exponential is under 0.03 cents, which is inaudible and costs one multiply.
	if (frac_cents)
	{
		uint32_t next = (uint32_t)(((uint64_t)hz * SEMITONE_Q16[1]) >> 16);
		// Divided BEFORE the multiply, and in 32 bits. The other order overflows a uint32 at
		// the top of the MIDI range (a semitone gap up there is ~49M in Q16, times 99), and
		// doing it in 64 bits to avoid that put another division in the per-sample path. A
		// constant divisor the compiler strength-reduces to a multiply-high is the cheap
		// shape; the precision lost is under a millionth of the frequency.
		hz += ((next - hz) / 100u) * (uint32_t)frac_cents;
	}
	return hz;
}

// (CYCLE << 32) / rate, computed once at init. See step_for.
static uint32_t s_phase_recip;

static uint32_t step_for(uint32_t hz_q16)
{
	// advance = hz * CYCLE / rate, as a multiply by a precomputed reciprocal.
	//
	// This was `((uint64_t)hz_q16 * CYCLE) / s_rate` -- a 64-bit division by a VARIABLE
	// divisor, called once per oscillator per sample. On a 32-bit core that is a call to
	// __aeabi_uldivmod in the innermost loop of the whole synth, and it is why the first
	// measurement blamed the oscillators for 39% of the cost. A reciprocal multiply is
	// UMULL plus taking the high word, which is one instruction and no call.
	//
	// Accuracy: 0.006% on a middle C step, which is under a thousandth of a cent.
	return (uint32_t)(((uint64_t)hz_q16 * s_phase_recip) >> 32);
}

// Envelope timing is in MILLISECONDS and converted against the sample rate, exactly as
// the mixer does it -- level is Q16, sustain is the 0..255 field shifted up by 8. Kept
// identical rather than reinvented so an ADSR means the same thing whether a note is
// played through the synth or as a plain tone; two envelope shapes in one engine would
// be a bug nobody could hear the cause of.
static int32_t env_ramp(uint16_t ms, int32_t span, uint32_t rate)
{
	if (!ms)
		return 0;
	return span / (int32_t)((ms * rate) / 1000u + 1u);
}

static void voice_steps(Voice* v, int32_t lfo)
{
	for (int oi = 0; oi < PNX_SYNTH_OSCILLATORS; oi++)
	{
		const PnxOscillator* o = &v->inst.osc[oi];
		int32_t cents		   = o->detune + (int32_t)o->octave * 1200 + v->pitch_env;
		if (v->inst.lfo_target == PNX_LFO_PITCH)
			cents += lfo >> 1;
		v->osc[oi].step = step_for(note_hz_q16(v->note, cents));

		// Which band-limited table this oscillator reads. Per OSCILLATOR, not per voice,
		// because `octave` shifts an oscillator away from the played note -- a pad with a
		// triangle an octave up must not read the octave-down table and alias.
		//
		// Chosen here, on the pitch update, so the inner loop just indexes a pointer.
		int32_t sounding = (int32_t)v->note + (int32_t)o->octave * 12 + (cents / 100);
		int32_t mip		 = sounding / 12;
		if (mip < 0)
			mip = 0;
		if (mip >= MIPS)
			mip = MIPS - 1;
		v->osc[oi].mip = (uint8_t)mip;
	}
}

// Stepped through a MACRO, not a function, and deliberately.
//
// The voice-major loop hoists envelope state into locals so it can live in registers --
// and then the first version passed `&amp_level` to a function, which forces that local
// to the stack and undoes the hoist for the state touched most. Measured: the filter,
// whose state is never addressed, dropped 647 ns; the per-voice cost, whose envelope was,
// moved 58. A macro touches the variables directly and cannot take their address.
//
#define ENV_STEP(level, rate, stage, e, sr)                                  \
	do                                                                       \
	{                                                                        \
		if ((stage) != ENV_OFF)                                              \
		{                                                                    \
			(level) += (rate);                                               \
			const int32_t _t = (int32_t)(e).sustain << 8;                    \
			if ((stage) == ENV_ATTACK)                                       \
			{                                                                \
				if ((level) >= (1 << 16))                                    \
				{                                                            \
					(level) = 1 << 16;                                       \
					(stage) = ENV_DECAY;                                     \
					(rate)	= -env_ramp((e).decay_ms, (1 << 16) - _t, (sr)); \
					if (!(e).decay_ms)                                       \
					{                                                        \
						(level) = _t;                                        \
						(rate)	= 0;                                         \
						(stage) = ENV_SUSTAIN;                               \
					}                                                        \
				}                                                            \
			}                                                                \
			else if ((stage) == ENV_DECAY)                                   \
			{                                                                \
				if ((level) <= _t)                                           \
				{                                                            \
					(level) = _t;                                            \
					(rate)	= 0;                                             \
					(stage) = ENV_SUSTAIN;                                   \
				}                                                            \
			}                                                                \
			else if ((stage) == ENV_RELEASE)                                 \
			{                                                                \
				if ((level) <= 0)                                            \
				{                                                            \
					(level) = 0;                                             \
					(rate)	= 0;                                             \
					(stage) = ENV_OFF;                                       \
				}                                                            \
			}                                                                \
		}                                                                    \
	} while (0)

// How often a modulated pitch is recomputed, in samples.
//
// This is THE optimisation the spike found. Recomputing the step every sample per
// oscillator measured 14,573 ns/sample on device -- 23% of a core, ~242 cycles per
// oscillator, roughly ten times what an oscillator should cost. note_hz_q16 is a table
// lookup, two widening multiplies, three constant divisions and a conditional shift, and
// it was running twelve times a sample to support an LFO that moves at 20 Hz.
//
// At 16 kHz, 32 samples is a 500 Hz update rate. Vibrato tops out around 20 Hz, so that
// is 25 updates per cycle -- smooth well past anything anyone can hear. A voice whose
// pitch nothing modulates skips it entirely and computes its steps once, at note-on.
#define PITCH_EVERY 32

// ------------------------------------------------------------------------- api

PnxSynthConfig pnx_synth_worst_case(void)
{
	PnxSynthConfig c;
	c.oscillators = PNX_SYNTH_OSCILLATORS;
	c.filter	  = true;
	c.resonance	  = true;
	c.lfo		  = true;
	c.pitch_env	  = true;
	c.reverb	  = true;
	c.chorus	  = true;
	return c;
}

static bool alloc_line(Line* l, uint16_t len, uint8_t** p)
{
	l->buf = (int16_t*)*p;
	l->len = len;
	l->at  = 0;
	memset(l->buf, 0, (size_t)len * sizeof(int16_t));
	*p += (size_t)len * sizeof(int16_t);
	return true;
}

bool pnx_synth_init(uint32_t sample_rate)
{
	const uint32_t want = sample_rate ? sample_rate : 16000u;
	if (s_ready)
	{
		// Already up at this rate: nothing to do, and notes keep sounding across a reopen.
		if (want == s_rate)
			return true;
		// A DIFFERENT rate, which happens when the mixer reopens in another format. Every
		// derived quantity depends on it -- the phase reciprocal, the envelope ramps, and the
		// band limit of every wavetable -- so returning early here would leave the synth
		// playing at the wrong pitch with tables band-limited for a Nyquist it no longer has.
		pnx_synth_shutdown();
	}
	s_rate		  = want;
	s_phase_recip = (uint32_t)(((uint64_t)CYCLE << 32) / s_rate);

	size_t wave_bytes	= (size_t)PNX_WAVE_COUNT * MIPS * CYCLE;
	size_t line_samples = 0;
	for (int i = 0; i < COMBS; i++)
		line_samples += COMB_LEN[i];
	for (int i = 0; i < ALLPASS; i++)
		line_samples += ALLPASS_LEN[i];
	line_samples += CHORUS_LEN;
	size_t line_bytes		 = line_samples * sizeof(int16_t);
	const size_t block_bytes = sizeof(int32_t) * BLOCK * 3u;
	const size_t voice_bytes = sizeof(Voice) * PNX_SYNTH_VOICES;
	const size_t slot_bytes	 = sizeof(PnxInstrument) * PNX_SYNTH_SLOTS;

	// One allocation, carved by decreasing alignment -- int32 accumulators and voice/slot
	// state (both need at most 4-byte alignment), then int16 delay lines, then the int8
	// wavetables -- so nothing needs padding between them.
	uint8_t* p = (uint8_t*)malloc(block_bytes + voice_bytes + slot_bytes + line_bytes + wave_bytes);
	if (!p)
	{
		pnx_log("synth: %u bytes refused",
				(unsigned)(block_bytes + voice_bytes + slot_bytes + line_bytes + wave_bytes));
		return false;
	}
	s_effect_bytes = (uint32_t)line_bytes;
	s_total_bytes  = (uint32_t)(block_bytes + voice_bytes + slot_bytes + line_bytes + wave_bytes);

	s_dry = (int32_t*)p;
	p += sizeof(int32_t) * BLOCK;
	s_rev_send = (int32_t*)p;
	p += sizeof(int32_t) * BLOCK;
	s_cho_send = (int32_t*)p;
	p += sizeof(int32_t) * BLOCK;
	s_block_base = (uint8_t*)s_dry;

	s_voice = (Voice*)p;
	p += voice_bytes;
	s_slot = (PnxInstrument*)p;
	p += slot_bytes;
	for (int i = 0; i < COMBS; i++)
		alloc_line(&s_comb[i], COMB_LEN[i], &p);
	for (int i = 0; i < ALLPASS; i++)
		alloc_line(&s_allpass[i], ALLPASS_LEN[i], &p);
	alloc_line(&s_chorus, CHORUS_LEN, &p);
	s_wave = (int8_t (*)[MIPS][CYCLE])p;

	// Wavetables, built additively per octave so each holds only the harmonics that stay
	// under Nyquist at the top of its range. This is the aliasing fix; see MIPS.
	for (int m = 0; m < MIPS; m++)
	{
		// Halved for an 8 kHz build, whose Nyquist is half as high. Without this a low-rate
		// build would be band-limited for a rate it is not running at, which is the same
		// aliasing with more code.
		int32_t h = MIP_HARMONICS[m];
		if (s_rate < 16000u)
			h = (h > 2) ? h / 2 : 1;
		if (h > CYCLE / 2 - 1)
			h = CYCLE / 2 - 1;

		for (int i = 0; i < CYCLE; i++)
		{
			int32_t saw = 0, sq = 0, tri = 0;
			for (int k = 1; k <= h; k++)
			{
				const int32_t s = SINE_Q12[(k * i) & (CYCLE - 1)];
				saw += ((k & 1) ? s : -s) / k; // every harmonic, 1/k
				if (k & 1)
				{
					sq += s / k;									 // odd harmonics, 1/k
					tri += ((((k - 1) / 2) & 1) ? -s : s) / (k * k); // odd, 1/k^2, alternating
				}
			}
			// Normalised so a saw, a square and a triangle come out at comparable loudness in
			// the +-100 the rest of the synth works in.
			s_wave[PNX_WAVE_SAW][m][i]		= (int8_t)((saw * 100) / 6400);
			s_wave[PNX_WAVE_SQUARE][m][i]	= (int8_t)((sq * 100) / 5200);
			s_wave[PNX_WAVE_TRIANGLE][m][i] = (int8_t)((tri * 100) / 4200);
		}

		// Noise is deliberately NOT band-limited -- it is broadband by definition. From an
		// LFSR rather than rand(): reproducible across runs, which a benchmark needs, and it
		// is what the hardware this style of music came from used.
		uint32_t lfsr = 0xACE1u;
		for (int i = 0; i < CYCLE; i++)
		{
			lfsr						 = (lfsr >> 1) ^ (uint32_t)((-(int32_t)(lfsr & 1u)) & 0xB400u);
			s_wave[PNX_WAVE_NOISE][m][i] = (int8_t)((int8_t)((lfsr >> 4) & 0xFF) / 2);
		}
	}

	s_tail = 0;
	// Voices/instrument slots need to start zeroed, the same guarantee a static array's
	// implicit BSS zero-init used to give for free -- now that they're carved from
	// malloc'd memory (above), that guarantee has to be explicit. voice_bytes/slot_bytes,
	// not sizeof(s_voice)/sizeof(s_slot): those are pointers now, not arrays.
	memset(s_voice, 0, voice_bytes); // clears filter state for a genuinely fresh voice
	memset(s_slot, 0, slot_bytes);
	memset(s_comb_store, 0, sizeof(s_comb_store));
	s_cfg	= pnx_synth_worst_case();
	s_ready = true;
	return true;
}

void pnx_synth_shutdown(void)
{
	if (!s_ready)
		return;
	free(s_block_base);
	s_block_base  = NULL;
	s_wave		  = NULL;
	s_ready		  = false;
	s_total_bytes = s_effect_bytes = 0;
}

uint32_t pnx_synth_bytes(void)
{
	return s_total_bytes;
}
uint32_t pnx_synth_effect_bytes(void)
{
	return s_effect_bytes;
}

void pnx_synth_set_config(const PnxSynthConfig* cfg)
{
	if (cfg)
		s_cfg = *cfg;
	if (s_cfg.oscillators < 1)
		s_cfg.oscillators = 1;
	if (s_cfg.oscillators > PNX_SYNTH_OSCILLATORS)
		s_cfg.oscillators = PNX_SYNTH_OSCILLATORS;
	s_osc_recip = 65536 / (int32_t)s_cfg.oscillators;
}

const PnxSynthConfig* pnx_synth_config(void)
{
	return &s_cfg;
}

void pnx_synth_set_headroom(uint8_t shift)
{
	s_headroom = shift > 6 ? 6 : shift;
}
uint8_t pnx_synth_headroom(void)
{
	return s_headroom;
}

void pnx_synth_set_instrument(uint8_t slot, const PnxInstrument* inst)
{
	if (slot >= PNX_SYNTH_SLOTS || !inst)
		return;
	s_slot[slot] = *inst;
}

void pnx_synth_note_on(uint8_t slot, uint8_t midi_note, uint8_t velocity)
{
	if (slot >= PNX_SYNTH_SLOTS || !s_ready)
		return;

	// Pick the free voice of the pair, or the quieter one when both are sounding. The
	// displaced note is faded rather than cut -- cutting it is the click this pairing
	// exists to remove.
	Voice* a = &s_voice[slot * 2];
	Voice* b = &s_voice[slot * 2 + 1];
	Voice* v;
	if (!a->active)
		v = a;
	else if (!b->active)
		v = b;
	else
		v = (a->amp_level <= b->amp_level) ? a : b;

	Voice* old = (v == a) ? b : a;
	if (old->active && old->amp_stage != ENV_OFF)
	{
		old->amp_stage = ENV_RELEASE;
		old->amp_rate  = -env_ramp(STEAL_MS, old->amp_level, s_rate);
		if (!old->amp_rate)
			old->amp_rate = -(1 << 16);
	}
	if (v->active && v->amp_stage != ENV_OFF)
	{
		// Both were sounding, so this one is being taken over. Fade it in the same way rather
		// than stepping: the join matters more than which voice makes the sound.
		v->amp_stage = ENV_RELEASE;
		v->amp_rate	 = -env_ramp(STEAL_MS, v->amp_level, s_rate);
	}

	// The instrument is copied at note-on. That is what makes "push an instrument into a
	// slot mid-song" safe: the change lands on the next note rather than mutating the one
	// being played, which would step the oscillator count under a running voice.
	// RETRIGGER, not restart.
	//
	// A slot that is already sounding is at some arbitrary amplitude and phase, and a
	// sequencer retriggers it on every row. Snapping the level to zero and the phases to
	// fixed values is a step discontinuity in the middle of a waveform -- a click per note.
	//
	// That was the crackle, and it took a waveform dump to see: 41 discontinuities across
	// 8 seconds, every one on a note boundary. It is invisible to the clip and underrun
	// counters because nothing clips and nothing runs dry -- the samples are all in range,
	// they just do not join up.
	//
	// The plain mixer path never had this problem because it releases the old voice and
	// allocates a NEW one, so the two overlap. A synth slot is the same voice, so the join
	// has to be made here: the attack ramps from wherever the level already is, and the
	// oscillators keep running.
	v->inst = s_slot[slot];
	if (v->inst.osc_count < 1)
		v->inst.osc_count = 1;
	v->note		= midi_note;
	v->velocity = velocity;
	v->active	= true;

	for (int i = 0; i < PNX_SYNTH_OSCILLATORS; i++)
	{
		// Staggered rather than zeroed together. Three oscillators starting in phase sum to
		// one loud transient and then drift apart, which is its own click.
		v->osc[i].phase = (uint32_t)i * (CYCLE << 16) / PNX_SYNTH_OSCILLATORS;
		v->osc[i].step	= 0;
	}
	v->amp_level = 0;
	v->amp_stage = ENV_ATTACK;
	v->amp_rate =
		v->inst.amp.attack_ms ? env_ramp(v->inst.amp.attack_ms, 1 << 16, s_rate) : (1 << 16);
	v->cut_level = 0;
	v->cut_stage = ENV_ATTACK;
	v->cut_rate	 = v->inst.cutoff.attack_ms ? env_ramp(v->inst.cutoff.attack_ms, 1 << 16, s_rate)
											: (1 << 16);
	v->lfo_phase = 0;
	v->pitch_env = v->inst.pitch_env_amount;
	// Filter state is deliberately NOT cleared. A filter is a continuous system and zeroing
	// its integrators mid-sound is a step discontinuity -- a click on every retrigger. Real
	// synths leave it running; only a fresh voice starts from silence.
	v->pitch_moves = (v->inst.lfo_target == PNX_LFO_PITCH && v->inst.lfo_depth) ||
		(v->inst.pitch_env_amount != 0);
	v->mod_count = 0;
	voice_steps(v, 0);
}

void pnx_synth_note_off(uint8_t slot)
{
	if (slot >= PNX_SYNTH_SLOTS)
		return;
	// Both voices of the pair: one may be sounding and the other still fading.
	for (int k = 0; k < 2; k++)
	{
		Voice* v = &s_voice[slot * 2 + k];
		if (!v->active || v->amp_stage == ENV_RELEASE || v->amp_stage == ENV_OFF)
			continue;
		v->amp_stage = ENV_RELEASE;
		v->amp_rate	 = -env_ramp(v->inst.amp.release_ms, v->amp_level, s_rate);
		if (!v->amp_rate)
			v->amp_rate = -(1 << 16);
		v->cut_stage = ENV_RELEASE;
		v->cut_rate	 = -env_ramp(v->inst.cutoff.release_ms, v->cut_level, s_rate);
	}
}

void pnx_synth_all_off(void)
{
	for (int i = 0; i < PNX_SYNTH_VOICES; i++)
		s_voice[i].active = false;
}

uint8_t pnx_synth_active_voices(void)
{
	uint8_t n = 0;
	for (int i = 0; i < PNX_SYNTH_VOICES; i++)
		if (s_voice[i].active)
			n++;
	return n;
}

// --------------------------------------------------------------------- rendering

static inline int32_t line_read(Line* l, uint16_t back)
{
	uint16_t at = (uint16_t)((l->at + l->len - back) % l->len);
	return l->buf[at];
}

static inline void line_write(Line* l, int32_t v)
{
	if (v > 32767)
		v = 32767;
	if (v < -32768)
		v = -32768;
	l->buf[l->at] = (int16_t)v;
	l->at		  = (uint16_t)((l->at + 1u) % l->len);
}

// One sample of the global reverb. A Schroeder/Freeverb arrangement: four damped combs in
// parallel, then two allpasses in series. Fixed point throughout.
static int32_t reverb_sample(int32_t in)
{
	int32_t acc = 0;
	for (int i = 0; i < COMBS; i++)
	{
		Line* l	  = &s_comb[i];
		int32_t y = l->buf[l->at];
		// One-pole damping in the feedback path is what stops the tail sounding metallic.
		s_comb_store[i] += ((y - s_comb_store[i]) * 80) >> 8;
		line_write(l, in + ((s_comb_store[i] * 200) >> 8));
		acc += y;
	}
	acc >>= 2;
	for (int i = 0; i < ALLPASS; i++)
	{
		Line* l		= &s_allpass[i];
		int32_t y	= l->buf[l->at];
		int32_t out = y - acc;
		line_write(l, acc + ((y * 128) >> 8));
		acc = out;
	}
	return acc;
}

// One sample of chorus: a delay line read at a slowly modulated offset. Cheaper than the
// reverb by an order of magnitude, and it is the effect that makes one oscillator sound
// like several without paying for a second oscillator.
static int32_t chorus_sample(int32_t in)
{
	s_chorus_phase += 137u; // ~0.5 Hz at 16 kHz
	int32_t lfo	   = s_wave[PNX_WAVE_TRIANGLE][0][(s_chorus_phase >> 16) & (CYCLE - 1)];
	uint16_t depth = (uint16_t)(CHORUS_LEN / 2 + ((lfo * (CHORUS_LEN / 3)) >> 7));
	if (depth >= s_chorus.len)
		depth = (uint16_t)(s_chorus.len - 1u);
	int32_t y = line_read(&s_chorus, depth);
	line_write(&s_chorus, in);
	return y;
}

// Rendered VOICE-MAJOR, in fixed sub-blocks.
//
// The loop used to be sample-major -- `for each sample: for each voice` -- which touched
// all four voices' state on every single sample. Measured on device, four bare voices
// cost 4,086 ns/sample, 38% of the whole synth and its largest single item, at ~204 cycles
// for one oscillator and one envelope. That is not what an oscillator costs; it is what
// reloading a hundred-odd bytes of voice state 16,000 times a second costs.
//
// Inverted, a voice's phase, envelope and filter state live in LOCALS for a whole block,
// so the compiler can keep them in registers and the inner loop is arithmetic.
//
// The block is small on purpose. The effects are global -- every voice sends into one
// reverb and one chorus -- so the sends have to be accumulated across voices before the
// effects run, which needs buffers. At 64 samples that is 768 bytes; at the full 768-sample
// chunk it would be 9 KB, and this is a platform where MEASUREMENTS.md already records that
// a buffer per pipeline stage is the expensive habit. 64 samples is ample to amortise the
// state load and store.
// Blocks of effect tail to keep processing after the last voice ends.
//
// The reverb is four combs feeding back at ~0.78, so it decays to inaudible in well under
// a second. Beyond that the effects pass is running the delay lines over silence, which
// costs ~2,000 ns a sample for no sound at all -- and a game whose music has stopped
// should pay nothing. 512 blocks of 64 at 16 kHz is ~2 s, comfortably past the tail.
#define TAIL_BLOCKS 512

void pnx_synth_render(int16_t* out, uint32_t count)
{
	if (!s_ready)
		return;

	uint8_t sounding = 0;
	for (int i = 0; i < PNX_SYNTH_VOICES; i++)
		if (s_voice[i].active)
			sounding++;
	if (sounding)
	{
		s_tail = TAIL_BLOCKS;
	}
	else if (s_tail == 0)
	{
		// Nothing sounding and the tail has run out. Returning here is the difference between
		// a synth that costs 3% of a core while silent and one that costs nothing.
		return;
	}

	const uint8_t osc_n = s_cfg.oscillators;

	for (uint32_t base = 0; base < count; base += BLOCK)
	{
		const uint32_t n_block = (count - base < BLOCK) ? (count - base) : BLOCK;

		// No memset. The FIRST active voice writes the accumulators and the rest add to them,
		// which is three fewer buffer passes per block -- 768 bytes of clearing per 64 samples
		// is 12 bytes a sample of pure overhead for something that is about to be overwritten.
		bool first = true;

		for (int vi = 0; vi < PNX_SYNTH_VOICES; vi++)
		{
			Voice* v = &s_voice[vi];
			if (!v->active)
				continue;

			// --- hoisted. Everything the inner loop touches, in locals.
			const PnxInstrument* in = &v->inst;
			int32_t amp_level = v->amp_level, amp_rate = v->amp_rate;
			uint8_t amp_stage = v->amp_stage;
			int32_t cut_level = v->cut_level, cut_rate = v->cut_rate;
			uint8_t cut_stage  = v->cut_stage;
			uint32_t lfo_phase = v->lfo_phase;
			int32_t pitch_env  = v->pitch_env;
			int32_t flt_low = v->flt_low, flt_band = v->flt_band;
			uint8_t mod_count = v->mod_count;
			uint32_t ph[PNX_SYNTH_OSCILLATORS], st[PNX_SYNTH_OSCILLATORS];
			uint8_t mp[PNX_SYNTH_OSCILLATORS];
			for (int oi = 0; oi < PNX_SYNTH_OSCILLATORS; oi++)
			{
				ph[oi] = v->osc[oi].phase;
				st[oi] = v->osc[oi].step;
				mp[oi] = v->osc[oi].mip;
			}
			const int32_t velocity	 = v->velocity;
			const uint8_t lfo_target = in->lfo_target;
			const bool do_filter	 = s_cfg.filter && in->filter_mode != PNX_FILTER_OFF;
			const int32_t rev_send	 = s_cfg.reverb ? in->reverb_send : 0;
			const int32_t cho_send	 = s_cfg.chorus ? in->chorus_send : 0;

			uint32_t n = 0;
			for (; n < n_block; n++)
			{
				ENV_STEP(amp_level, amp_rate, amp_stage, in->amp, s_rate);
				if (amp_stage == ENV_OFF)
					break;
				if (s_cfg.filter)
					ENV_STEP(cut_level, cut_rate, cut_stage, in->cutoff, s_rate);

				int32_t lfo = 0;
				if (s_cfg.lfo && lfo_target != PNX_LFO_OFF)
				{
					lfo_phase += (uint32_t)in->lfo_rate * 24u;
					lfo = s_wave[PNX_WAVE_TRIANGLE][0][(lfo_phase >> 16) & (CYCLE - 1)];
					lfo = (lfo * in->lfo_depth) >> 8;
				}

				if (s_cfg.pitch_env && pitch_env)
				{
					// Decays toward zero, so the note settles on its true pitch. This is the whole
					// drum story: a kick is this sweep on a sine, a snare is it on noise.
					int32_t d = (pitch_env * (int32_t)in->pitch_env_decay) >> 12;
					pitch_env -= d ? d : (pitch_env > 0 ? 1 : -1);
				}

				// --- pitch, on a slow clock. See PITCH_EVERY.
				if (v->pitch_moves && mod_count-- == 0)
				{
					mod_count	 = PITCH_EVERY - 1;
					v->pitch_env = pitch_env;
					voice_steps(v, s_cfg.lfo ? lfo : 0);
					for (int oi = 0; oi < PNX_SYNTH_OSCILLATORS; oi++)
					{
						st[oi] = v->osc[oi].step;
						mp[oi] = v->osc[oi].mip;
					}
				}

				// --- oscillators
				int32_t mixed = 0;
				for (int oi = 0; oi < osc_n; oi++)
				{
					const PnxOscillator* o = &in->osc[oi];
					uint32_t idx		   = (ph[oi] >> 16) & (CYCLE - 1);
					int32_t s;
					uint8_t duty = o->duty;
					if (o->wave == PNX_WAVE_SQUARE && s_cfg.lfo && lfo_target == PNX_LFO_DUTY)
					{
						int32_t d = (int32_t)duty + (lfo >> 1);
						duty	  = (uint8_t)(d < 16 ? 16 : (d > 240 ? 240 : d));
					}
					// A 50% square reads the band-limited table like everything else. Only a MOVED
					// duty falls back to the threshold comparison, because pulse width cannot be
					// tabled without a table per duty -- so PWM keeps its hard edges, which is the
					// chiptune sound and is wanted, while a plain square stops aliasing.
					if (o->wave == PNX_WAVE_SQUARE && duty != 128)
					{
						s = (idx < (((uint32_t)duty * CYCLE) >> 8)) ? 100 : -100;
					}
					else
					{
						const int8_t* tbl = s_wave[o->wave][mp[oi]];
						int32_t a0		  = tbl[idx];
						int32_t a1		  = tbl[(idx + 1u) & (CYCLE - 1)];
						s				  = a0 + (((a1 - a0) * (int32_t)(ph[oi] & 0xFFFF)) >> 16);
					}
					mixed += (s * o->volume) >> 8;
					ph[oi] += st[oi];
				}
				mixed = (mixed * s_osc_recip) >> 16;

				// --- filter
				if (do_filter)
				{
					int32_t cut = (int32_t)in->cutoff_base +
						(((cut_level >> 8) * in->cutoff_env_amount) >> 8);
					if (s_cfg.lfo && lfo_target == PNX_LFO_CUTOFF)
						cut += lfo;
					if (cut < 4)
						cut = 4;
					if (cut > 250)
						cut = 250;

					int32_t f = (cut * 220) >> 4;
					int32_t q = s_cfg.resonance
						? ((65536 - ((int32_t)in->resonance * 240)) >> 4)
						: 4096;
					if (q < 256)
						q = 256;

					int32_t high = (mixed << 8) - flt_low - ((q * flt_band) >> 12);
					flt_band += (f * high) >> 12;
					flt_low += (f * flt_band) >> 12;

					// The STATE is clamped, not just the output. A resonant filter rings well above
					// its input, and an unbounded integrator is what turns "loud" into a sign flip.
					if (flt_band > 262144)
						flt_band = 262144;
					if (flt_band < -262144)
						flt_band = -262144;
					if (flt_low > 262144)
						flt_low = 262144;
					if (flt_low < -262144)
						flt_low = -262144;

					int32_t fout = (in->filter_mode == PNX_FILTER_LOWPASS) ? flt_low
						: (in->filter_mode == PNX_FILTER_HIGHPASS)		   ? high
																		   : flt_band;
					if (fout > (127 << 8))
						fout = 127 << 8;
					if (fout < -(127 << 8))
						fout = -(127 << 8);
					mixed = fout >> 8;
				}

				int32_t amp = amp_level >> 8;
				if (s_cfg.lfo && lfo_target == PNX_LFO_VOLUME)
					amp = (amp * (128 + (lfo >> 1))) >> 7;
				int32_t sample = (mixed * amp) >> 8;
				sample		   = (sample * velocity) >> 8;

				if (first)
				{
					s_dry[n] = sample;
					if (s_cfg.reverb)
						s_rev_send[n] = (sample * rev_send) >> 8;
					if (s_cfg.chorus)
						s_cho_send[n] = (sample * cho_send) >> 8;
				}
				else
				{
					s_dry[n] += sample;
					if (rev_send)
						s_rev_send[n] += (sample * rev_send) >> 8;
					if (cho_send)
						s_cho_send[n] += (sample * cho_send) >> 8;
				}
			}

			// A voice whose envelope ended part-way through the block still has to have written
			// the whole block, or the tail would be whatever the last block left behind.
			if (first)
			{
				for (uint32_t k = n; k < n_block; k++)
				{
					s_dry[k] = 0;
					if (s_cfg.reverb)
						s_rev_send[k] = 0;
					if (s_cfg.chorus)
						s_cho_send[k] = 0;
				}
				first = false;
			}

			// --- written back once per block, not once per sample
			v->amp_level = amp_level;
			v->amp_rate	 = amp_rate;
			v->amp_stage = amp_stage;
			v->cut_level = cut_level;
			v->cut_rate	 = cut_rate;
			v->cut_stage = cut_stage;
			v->lfo_phase = lfo_phase;
			v->pitch_env = pitch_env;
			v->flt_low	 = flt_low;
			v->flt_band	 = flt_band;
			v->mod_count = mod_count;
			for (int oi = 0; oi < PNX_SYNTH_OSCILLATORS; oi++)
			{
				v->osc[oi].phase = ph[oi];
				v->osc[oi].step	 = st[oi];
			}
			if (amp_stage == ENV_OFF)
				v->active = false;
		}

		// Nothing sounded, so nothing wrote the accumulators -- and they still hold the
		// previous block. Silence has to be silence, not a repeat.
		if (first)
		{
			memset(s_dry, 0, sizeof(int32_t) * n_block);
			if (s_cfg.reverb)
				memset(s_rev_send, 0, sizeof(int32_t) * n_block);
			if (s_cfg.chorus)
				memset(s_cho_send, 0, sizeof(int32_t) * n_block);
		}

		if (!sounding && s_tail)
			s_tail--;

		// --- effects, once across the block
		//
		// The delay lines are global, so they must be advanced exactly once per sample no
		// matter how many voices sent into them. That is the constraint that decides this
		// pass exists at all.
		for (uint32_t n = 0; n < n_block; n++)
		{
			int32_t wet = 0;
			if (s_cfg.reverb)
				wet += reverb_sample(s_rev_send[n]);
			if (s_cfg.chorus)
				wet += chorus_sample(s_cho_send[n]);

			// Headroom. Four voices at full velocity plus wet do not fit the 8-bit domain the
			// mixer clamps to -- the device measured a peak of 163 against 127, heard as
			// roughness. One bit lands it near 85. See pnx_synth_set_headroom.
			int32_t v = (s_dry[n] + wet) >> s_headroom;
			if (v > 32767)
				v = 32767;
			if (v < -32768)
				v = -32768;
			out[base + n] += (int16_t)v;
		}
	}
}

// ------------------------------------------------------------------- measurement

void pnx_synth_bench(uint32_t chunks, uint32_t count, PnxSynthBench* out)
{
	if (!out)
		return;
	memset(out, 0, sizeof(*out));
	if (!s_ready || !chunks || !count)
		return;

	// A scratch buffer of the caller's chunk size. Allocated rather than static so the
	// benchmark's own footprint does not land in .bss and skew the RAM figure it exists to
	// report alongside.
	int16_t* buf = (int16_t*)malloc(sizeof(int16_t) * count);
	if (!buf)
		return;

	const uint32_t t0 = pnx_platform_now_ms();
	int32_t sum		  = 0;
	for (uint32_t c = 0; c < chunks; c++)
	{
		memset(buf, 0, sizeof(int16_t) * count);
		pnx_synth_render(buf, count);
		// The compiler deletes a write-only buffer -- this project has already lost a 16 KB
		// array that way -- so the result is read and carried out through `checksum`.
		sum += buf[c % count] + buf[(count - 1u) - (c % count)];
	}
	const uint32_t elapsed = pnx_platform_now_ms() - t0;

	free(buf);

	out->elapsed_ms = elapsed;
	out->samples	= chunks * count;
	out->checksum	= sum;
	if (out->samples)
	{
		// Nanoseconds, because microseconds per sample rounds to zero here and the whole
		// point is a figure that survives being small. Derived from a millisecond total over
		// many samples -- repetition, not a multiplied millisecond clock.
		out->ns_per_sample = (uint32_t)(((uint64_t)elapsed * 1000000u) / out->samples);
		// What fraction of one core this would take at the output rate, in hundredths of a
		// percent. This is the number that decides the feature set.
		out->pct_of_realtime = (uint32_t)(((uint64_t)out->ns_per_sample * s_rate) / 100000u);
	}
}

#endif // PNX_USE_SYNTH
