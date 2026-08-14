// Host tests for the software mixer.
//
// The host platform accepts every write and records the total, which makes the lead
// arithmetic testable without a speaker: feed at a simulated clock and assert the stream
// stays ahead of what playback would have consumed. An underrun on device is silent --
// the audio simply gaps -- so this is the only place it can be caught cheaply.

#include "../src/pnx/audio/pnx_audio.h"
#include "../src/pnx/audio/pnx_synth.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define AU_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define AU_CHECK_EQ(a, b)                                                                    \
	do                                                                                       \
	{                                                                                        \
		s_checks++;                                                                          \
		const long _a = (long)(a), _b = (long)(b);                                           \
		if (_a != _b)                                                                        \
		{                                                                                    \
			printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n", __FILE__, __LINE__, #a, #b, _a, \
				   _b);                                                                      \
			s_failures++;                                                                    \
		}                                                                                    \
	} while (0)

void test_audio(void);

#if PNX_USE_SYNTH
// The synth renders into the mixer's accumulator, so opening the mixer has to open it too.
//
// This exists because the opposite shipped: every synth entry point begins
// `if (!s_ready) return`, so a game that called pnx_audio_init and not pnx_synth_init
// routed its music into functions that silently did nothing. Sound effects kept working
// -- they go through the mixer's own voices -- so the symptom was "music is gone" with a
// clean log and a passing build.
static void test_synth_shares_the_mixer_lifecycle(void)
{
	pnx_audio_shutdown();
	AU_CHECK(pnx_synth_bytes() == 0);
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 200));
	AU_CHECK(pnx_synth_bytes() > 0);  // opening the mixer opened the synth

	// And a note routed through it actually reaches the output, which is the property the
	// shipped bug broke -- the allocation alone would not have caught it.
	PnxInstrument in;
	memset(&in, 0, sizeof(in));
	in.osc_count = 1;
	in.osc[0].wave = PNX_WAVE_SAW;
	in.osc[0].volume = 255;
	in.amp.attack_ms = 1;
	in.amp.decay_ms = 500;
	in.amp.sustain = 255;
	in.amp.release_ms = 100;
	pnx_synth_all_off();
	pnx_synth_set_instrument(0, &in);
	pnx_synth_note_on(0, 60, 255);

	const uint32_t before = pnx_host_audio_total();
	for (int i = 0; i < 8; i++)
		pnx_audio_update(100u + (uint32_t)i * 40u);
	AU_CHECK(pnx_host_audio_total() > before);

	size_t bytes = 0;
	const int8_t* last = (const int8_t*)pnx_host_audio_last(&bytes);
	bool sounded = false;
	for (size_t i = 0; i < bytes; i++)
		if (last[i])
			sounded = true;
	AU_CHECK(sounded);	// the synth is IN the stream, not just alive

	pnx_synth_all_off();
	pnx_audio_shutdown();
	AU_CHECK(pnx_synth_bytes() == 0);  // and closing it closes the synth
}

// Four synth voices plus effects must not overdrive the mixer's output clamp.
//
// This shipped broken and the symptom was "it sounds staticy", which is the same thing
// aliasing, an underrun and a bad sample all sound like -- so it cost device round trips
// to identify. Measured on the real song it was peak 239 against 127 with 4.5% of samples
// clipped. The clamp is silent by design, so nothing but a counter can tell the difference.
static void test_synth_does_not_overdrive_the_mixer(void)
{
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 200));

	PnxInstrument in;
	memset(&in, 0, sizeof(in));
	in.osc_count = 3;
	for (int i = 0; i < 3; i++)
	{
		in.osc[i].wave = PNX_WAVE_SAW;
		in.osc[i].volume = 200;
		in.osc[i].duty = 128;
	}
	in.osc[1].detune = 7;
	in.osc[2].detune = -9;
	in.amp.attack_ms = 5;
	in.amp.decay_ms = 200;
	in.amp.sustain = 200;
	in.amp.release_ms = 150;
	in.filter_mode = PNX_FILTER_LOWPASS;
	in.cutoff_base = 60;
	in.resonance = 190;
	in.cutoff_env_amount = 180;
	in.cutoff.attack_ms = 2;
	in.cutoff.decay_ms = 300;
	in.cutoff.sustain = 80;
	in.cutoff.release_ms = 200;
	in.reverb_send = 90;
	in.chorus_send = 70;

	// Four voices at the velocity a real song uses. Detuned oscillators drift in and out of
	// phase and their sum peaks well above any one of them, so this is already the awkward
	// case -- the example song measures 120 of 127 here.
	pnx_synth_all_off();
	for (int v = 0; v < PNX_SYNTH_SLOTS; v++)
	{
		pnx_synth_set_instrument((uint8_t)v, &in);
		pnx_synth_note_on((uint8_t)v, (uint8_t)(48 + v * 5), 200);
	}
	for (uint32_t ms = 0; ms < 4000; ms += 10)
		pnx_audio_update(ms);

	const PnxAudioStats* st = pnx_audio_stats();
	AU_CHECK(st->peak <= 127);
	AU_CHECK(st->clipped == 0);
	// And not so quiet that the headroom was bought with dynamic range: an 8-bit output has
	// none to spare, and over-attenuating trades clipping for quantisation noise.
	AU_CHECK(st->peak > 48);
	printf("  ... synth into mixer: peak %u/127, %u clipped\n", (unsigned)st->peak,
		   (unsigned)st->clipped);
	pnx_synth_all_off();
	pnx_audio_shutdown();

	// And the LIMIT, asserted rather than left to be discovered.
	//
	// Four detuned voices at velocity 255 with both sends up genuinely exceed an 8-bit
	// output -- measured at peak 153. More attenuation is not the fix: every bit of headroom
	// is a bit of an eight-bit output spent on silence. This is the case MEASUREMENTS.md
	// names as the argument for a 16-bit format, and it is pinned here so that "the synth
	// can clip" stays a known, counted property rather than a mystery someone re-derives
	// from a description of static.
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 200));
	pnx_synth_all_off();
	for (int v = 0; v < PNX_SYNTH_SLOTS; v++)
	{
		pnx_synth_set_instrument((uint8_t)v, &in);
		pnx_synth_note_on((uint8_t)v, (uint8_t)(48 + v * 5), 255);
	}
	for (uint32_t ms = 0; ms < 4000; ms += 10)
		pnx_audio_update(ms);
	const PnxAudioStats* hot = pnx_audio_stats();
	AU_CHECK(hot->clipped > 0);	 // the ceiling is real
	AU_CHECK(hot->peak > 127);	 // and the counter sees it
	printf("  ... at full velocity: peak %u/127, %u clipped -- the 8-bit ceiling\n",
		   (unsigned)hot->peak, (unsigned)hot->clipped);

	pnx_synth_all_off();
	pnx_audio_shutdown();

	// Reopening in another FORMAT must carry the synth's sample rate with it. Everything
	// the synth derives -- phase increment, envelope ramps, the band limit of every
	// wavetable -- is a function of the rate, so a stale one is wrong pitch and wrong
	// aliasing at once, with nothing to indicate it.
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 200));
	const uint32_t at16 = pnx_synth_bytes();
	AU_CHECK(at16 > 0);
	AU_CHECK(pnx_audio_reopen(PNX_AUDIO_8KHZ_8BIT, 200));
	AU_CHECK(pnx_synth_bytes() > 0);  // still up after the reopen
	AU_CHECK(pnx_audio_reopen(PNX_AUDIO_16KHZ_8BIT, 200));
	AU_CHECK(pnx_synth_bytes() == at16);
	pnx_audio_shutdown();
}
#endif

void test_audio(void)
{
	printf("audio\n");

	static int8_t tone[64];
	for (int i = 0; i < 64; i++)
		tone[i] = (int8_t)(i < 32 ? 100 : -100);

	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_16BIT, 80));
	AU_CHECK(pnx_platform_audio_is_open());

	// Nothing playing yet, but the stream must still be fed -- silence is data, and a gap
	// in it underruns exactly like a missing note would.
	pnx_audio_update(0);
	AU_CHECK(pnx_audio_stats()->written > 0);

	// --- voices
	const uint8_t v = pnx_audio_play(tone, 64, PNX_AUDIO_NO_LOOP, 16000, 255);
	AU_CHECK(v != PNX_AUDIO_NO_VOICE);
	AU_CHECK(pnx_audio_voice_active(v));

	// Counted on a LOOPING voice. The one-shot above is 64 samples at 16kHz -- 4ms -- so
	// whether it survives a given update depends on how many samples that feed happened to
	// mix, which follows from the lead and chunk size. A test should not be coupled to
	// those: lowering the lead from 80ms to 60ms broke this assertion without anything
	// being wrong.
	// Asserted on the voice itself, not on stats->active_voices. That field is a snapshot
	// taken inside mix(), so an update which reaches its lead and returns without mixing
	// leaves it stale -- it reports what was sounding at the last mix, not what is sounding
	// now. Voice state is the thing being tested here.
	const uint8_t sustained = pnx_audio_play(tone, 64, 0, 16000, 200);
	pnx_audio_update(40);
	AU_CHECK(pnx_audio_voice_active(sustained));
	pnx_audio_update(80);
	AU_CHECK(pnx_audio_voice_active(sustained));  // looping: never retires on its own
	pnx_audio_stop(sustained);
	AU_CHECK(!pnx_audio_voice_active(sustained));

	// A non-looping sample must retire itself; 64 samples at 16kHz is 4ms, long gone.
	pnx_audio_update(200);
	AU_CHECK(!pnx_audio_voice_active(v));

	// A looping sample must not.
	const uint8_t lv = pnx_audio_play(tone, 64, 0, 16000, 200);
	pnx_audio_update(400);
	pnx_audio_update(800);
	AU_CHECK(pnx_audio_voice_active(lv));
	pnx_audio_stop(lv);
	AU_CHECK(!pnx_audio_voice_active(lv));

	// --- the lead is what prevents underrun, so walk a clock and assert it holds
	pnx_audio_shutdown();
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_16BIT, 80));
	pnx_audio_play(tone, 64, 0, 16000, 255);

	// 10ms cadence, matching the audio timer the platform now runs. Audio is deliberately
	// NOT fed from the render loop: that is capped at 26.8fps, so feeds arrived 37ms apart at
	// best and 140ms apart in practice on device, which forced a lead so deep that effects
	// were audibly late.
	for (uint32_t t = 0; t <= 2000; t += 10)
		pnx_audio_update(t);
	const PnxAudioStats* st = pnx_audio_stats();

	// Writes are deferred until a whole quantum is available, so `written` lags by up to one
	// quantum and the deficit measure sees that deferral. It is not starvation: the lead is
	// far larger than the quantum, so the buffer still holds plenty. Tolerating exactly one
	// quantum keeps the assertion meaningful while not failing on the alignment.
	AU_CHECK(st->worst_deficit <= 512);
	AU_CHECK(st->feeds > 10);

	// The gap between feeds is the number that decides continuity, not the aggregate rate.
	AU_CHECK(st->worst_gap_ms <= 10);

	// Written must be ahead of consumed by roughly the configured lead, not miles beyond:
	// over-feeding would mean unbounded latency on every sound effect.
	const uint32_t consumed = 2000u * 32000u / 1000u;
	AU_CHECK(st->written > consumed);
	AU_CHECK(st->written < consumed + 32000u / 2u);	 // under half a second ahead

	// A cadence slower than the lead starves the stream, which is the whole reason audio
	// moved off the render loop. Asserted so the reason cannot be quietly lost: feeding every
	// 37ms against an 80ms lead is fine, but the render loop measured 140ms on device.
	pnx_audio_shutdown();
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_16BIT, 80));
	pnx_audio_play(tone, 64, 0, 16000, 255);
	for (uint32_t t = 0; t <= 2000; t += 200)
		pnx_audio_update(t);
	AU_CHECK(pnx_audio_stats()->worst_deficit > 0);	 // 200ms cadence cannot hold an 80ms lead
	AU_CHECK(pnx_audio_stats()->worst_gap_ms >= 200);

	// --- a late frame must be survivable: the app is throttled to ~0.4fps when covered
	pnx_audio_shutdown();
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_16BIT, 80));
	// The queue actually achieved is smaller than the lead requested, and that gap is what a
	// stall has to fit inside. Measured on the host: an 80ms lead settles at 52-58ms queued,
	// because quantum alignment defers each write until 256 bytes have accrued and the feed is
	// capped at a chunk. So ask for more lead than the stall you intend to survive -- roughly
	// 1.5x. A 40ms stall inside an 80ms lead holds at every priming length tried; 60ms does
	// not, which is why this is asserted rather than left to be rediscovered.
	for (uint32_t t = 0; t <= 30; t++)
		pnx_audio_update(t);
	pnx_audio_update(70);  // 40ms of silence, inside the real queue
	AU_CHECK_EQ(pnx_audio_stats()->worst_deficit, 0);

	// A 2-second stall is beyond any lead; the deficit must be REPORTED rather than hidden,
	// because that is the only way a game learns its audio gapped.
	pnx_audio_update(2137);
	AU_CHECK(pnx_audio_stats()->worst_deficit > 0);

	pnx_audio_stop_all();
	pnx_audio_shutdown();
	AU_CHECK(!pnx_platform_audio_is_open());

	// --- the waveform itself, across feed boundaries
	//
	// A click is a step discontinuity, and the seam between one feed and the next is where
	// buffer bugs put one. Every earlier click was found by ear on device; this catches the
	// same class in a second. Checked on a sustained triangle because it has a known maximum
	// slope -- 440Hz at 16kHz is 36 samples a cycle, so ~5.6 per step. Percussion is noise and
	// has no bound worth asserting.
	pnx_audio_shutdown();
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 60));
	const PnxEnvelope flat = { .attack_ms = 1, .decay_ms = 1, .sustain = 255, .release_ms = 1 };
	pnx_audio_note(PNX_WAVE_TRIANGLE, 69, 255, &flat, 0);  // A4

	// The host keeps only the most recent block, so the running total is what says whether
	// there is new data and whether any was missed. Without that, an update which skips its
	// write leaves the same block in place and gets compared against itself -- a fake seam.
	int32_t worst_delta = 0, prev = 0;
	uint32_t sampled = 0, boundaries = 0;
	bool have_prev = false;
	uint32_t seen_total = pnx_host_audio_total();
	for (uint32_t t = 0; t <= 3000; t += 10)
	{
		pnx_audio_update(t);
		const uint32_t total = pnx_host_audio_total();
		if (total == seen_total)
			continue;  // nothing written this update

		size_t bytes = 0;
		const int8_t* block = (const int8_t*)pnx_host_audio_last(&bytes);
		const bool contiguous = (total - seen_total) == bytes;	// no earlier write was missed
		seen_total = total;
		if (!block || bytes == 0)
			continue;
		if (contiguous)
			boundaries++;
		else
			have_prev = false;

		for (size_t i = 0; i < bytes; i++)
		{
			if (have_prev)
			{
				const int32_t d = block[i] > prev ? block[i] - prev : prev - block[i];
				if (d > worst_delta)
					worst_delta = d;
			}
			prev = block[i];
			have_prev = true;
			sampled++;
		}
	}
	AU_CHECK(boundaries > 50);	// the seams were actually exercised
	AU_CHECK(sampled > 20000);
	AU_CHECK(worst_delta <= 12);
	if (worst_delta > 12)
		printf("    worst sample-to-sample delta %d over %u samples\n", (int)worst_delta,
			   sampled);

	// The in-place conversion must leave 16-bit output as the 8-bit waveform shifted left 8.
	// Reading an entry after writing the byte that shares it would corrupt exactly this.
	pnx_audio_shutdown();
	AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_16BIT, 60));
	pnx_audio_note(PNX_WAVE_TRIANGLE, 69, 255, &flat, 0);
	pnx_audio_update(0);
	size_t wide_bytes = 0;
	const int16_t* wide = (const int16_t*)pnx_host_audio_last(&wide_bytes);
	AU_CHECK(wide != NULL && wide_bytes >= 64);
	if (wide && wide_bytes >= 64)
	{
		bool clean = true;
		for (size_t i = 0; i < wide_bytes / 2; i++)
		{
			if ((wide[i] & 0xFF) != 0)
				clean = false;	// low byte must be zero: c << 8
		}
		AU_CHECK(clean);
	}

	pnx_audio_stop_all();
	pnx_audio_shutdown();
#if PNX_USE_SYNTH
	test_synth_shares_the_mixer_lifecycle();
	test_synth_does_not_overdrive_the_mixer();
#endif
}
