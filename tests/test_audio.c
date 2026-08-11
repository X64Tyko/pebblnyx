// Host tests for the software mixer.
//
// The host platform accepts every write and records the total, which makes the lead
// arithmetic testable without a speaker: feed at a simulated clock and assert the stream
// stays ahead of what playback would have consumed. An underrun on device is silent --
// the audio simply gaps -- so this is the only place it can be caught cheaply.

#include "../src/pnx/audio/pnx_audio.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define AU_CHECK(cond) do {                                                 \
    s_checks++;                                                             \
    if (!(cond)) {                                                          \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);              \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

#define AU_CHECK_EQ(a, b) do {                                              \
    s_checks++;                                                             \
    const long _a = (long)(a), _b = (long)(b);                              \
    if (_a != _b) {                                                         \
      printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n",                      \
             __FILE__, __LINE__, #a, #b, _a, _b);                           \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

void test_audio(void);

void test_audio(void) {
  printf("audio\n");

  static int8_t tone[64];
  for (int i = 0; i < 64; i++) tone[i] = (int8_t)(i < 32 ? 100 : -100);

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
  AU_CHECK(pnx_audio_voice_active(sustained));   // looping: never retires on its own
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
  for (uint32_t t = 0; t <= 2000; t += 10) pnx_audio_update(t);
  const PnxAudioStats *st = pnx_audio_stats();
  AU_CHECK_EQ(st->worst_deficit, 0);      // never fell behind playback
  AU_CHECK(st->feeds > 40);

  // The gap between feeds is the number that decides continuity, not the aggregate rate.
  AU_CHECK(st->worst_gap_ms <= 10);

  // Written must be ahead of consumed by roughly the configured lead, not miles beyond:
  // over-feeding would mean unbounded latency on every sound effect.
  const uint32_t consumed = 2000u * 32000u / 1000u;
  AU_CHECK(st->written > consumed);
  AU_CHECK(st->written < consumed + 32000u / 2u);   // under half a second ahead

  // A cadence slower than the lead starves the stream, which is the whole reason audio
  // moved off the render loop. Asserted so the reason cannot be quietly lost: feeding every
  // 37ms against an 80ms lead is fine, but the render loop measured 140ms on device.
  pnx_audio_shutdown();
  AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_16BIT, 80));
  pnx_audio_play(tone, 64, 0, 16000, 255);
  for (uint32_t t = 0; t <= 2000; t += 200) pnx_audio_update(t);
  AU_CHECK(pnx_audio_stats()->worst_deficit > 0);   // 200ms cadence cannot hold an 80ms lead
  AU_CHECK(pnx_audio_stats()->worst_gap_ms >= 200);

  // --- a late frame must be survivable: the app is throttled to ~0.4fps when covered
  pnx_audio_shutdown();
  AU_CHECK(pnx_audio_init(PNX_AUDIO_16KHZ_16BIT, 80));
  pnx_audio_update(0);
  pnx_audio_update(10);
  // ...then nothing for 60ms, inside the 80ms lead.
  pnx_audio_update(70);
  AU_CHECK_EQ(pnx_audio_stats()->worst_deficit, 0);

  // A 2-second stall is beyond any lead; the deficit must be REPORTED rather than hidden,
  // because that is the only way a game learns its audio gapped.
  pnx_audio_update(2137);
  AU_CHECK(pnx_audio_stats()->worst_deficit > 0);

  pnx_audio_stop_all();
  pnx_audio_shutdown();
  AU_CHECK(!pnx_platform_audio_is_open());
}
