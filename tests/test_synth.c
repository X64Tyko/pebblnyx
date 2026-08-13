// Synth spike: correctness on the host, and a per-feature cost breakdown.
//
// The device is the authority on timing -- a desktop CPU says nothing about a Cortex-M33
// and this file does not pretend otherwise. What the host CAN settle is everything that
// would make a device number meaningless: that the voice produces signal at all, that
// envelopes reach their stages and end, that a resonant filter driven hard does not wrap
// to full scale, that swapping an instrument mid-note does not change the note being
// played, and that turning a feature off actually removes its work.
//
// The relative costs printed here are host figures and are labelled as such. They are
// useful for one thing only: ordering the features by expense, which does carry across.

#include "../src/pnx/audio/pnx_music.h"
#include "../src/pnx/audio/pnx_synth.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#if !PNX_USE_SYNTH

void test_synth(void) {}

#else

#define SY_CHECK(label, cond) do {                                          \
    s_checks++;                                                             \
    if (!(cond)) {                                                          \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, (label));            \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

#define note(...) do { printf(__VA_ARGS__); printf("\n"); } while (0)

#define RATE  16000u
#define CHUNK 768u

static PnxInstrument lead_instrument(void) {
  PnxInstrument in;
  memset(&in, 0, sizeof(in));
  in.osc_count = 3;
  // Three saws a few cents apart. Detune is what makes a lead sound thick, and it is also
  // the case that stresses headroom hardest, because slightly detuned oscillators drift
  // in and out of phase and their sum peaks well above any one of them.
  for (int i = 0; i < 3; i++) {
    in.osc[i].wave = PNX_WAVE_SAW;
    in.osc[i].volume = 200;
    in.osc[i].duty = 128;
    in.osc[i].octave = 0;
  }
  in.osc[0].detune = 0;
  in.osc[1].detune = 7;
  in.osc[2].detune = -9;

  in.amp.attack_ms = 5;
  in.amp.decay_ms = 80;
  in.amp.sustain = 180;
  in.amp.release_ms = 120;

  in.cutoff.attack_ms = 2;
  in.cutoff.decay_ms = 200;
  in.cutoff.sustain = 60;
  in.cutoff.release_ms = 150;

  in.filter_mode = PNX_FILTER_LOWPASS;
  in.cutoff_base = 40;
  in.resonance = 200;
  in.cutoff_env_amount = 200;

  in.lfo_target = PNX_LFO_PITCH;
  in.lfo_rate = 40;
  in.lfo_depth = 30;

  in.reverb_send = 90;
  in.chorus_send = 70;
  return in;
}

// A kick: the case that proves the pitch envelope earns its place. Nothing else in the
// instrument makes this sound, and doing it as PCM instead costs 16,000 bytes a second
// against ~160 for a whole song.
static PnxInstrument kick_instrument(void) {
  PnxInstrument in;
  memset(&in, 0, sizeof(in));
  in.osc_count = 1;
  in.osc[0].wave = PNX_WAVE_TRIANGLE;
  in.osc[0].volume = 255;
  in.amp.attack_ms = 1;
  in.amp.decay_ms = 90;
  in.amp.sustain = 0;
  in.amp.release_ms = 40;
  in.pitch_env_amount = 900;      // starts ~9 semitones up
  in.pitch_env_decay = 200;
  return in;
}

static int32_t peak_of(const int16_t *buf, uint32_t n) {
  int32_t peak = 0;
  for (uint32_t i = 0; i < n; i++) {
    int32_t v = buf[i] < 0 ? -buf[i] : buf[i];
    if (v > peak) peak = v;
  }
  return peak;
}

static uint32_t nonzero_of(const int16_t *buf, uint32_t n) {
  uint32_t c = 0;
  for (uint32_t i = 0; i < n; i++) if (buf[i]) c++;
  return c;
}

static void render_into(int16_t *buf, uint32_t n) {
  memset(buf, 0, sizeof(int16_t) * n);
  pnx_synth_render(buf, n);
}

// Cost of one configuration, in nanoseconds per sample on THIS machine. Only the ordering
// between configurations is meaningful off-device.
static uint32_t cost_of(const PnxSynthConfig *cfg, uint32_t chunks) {
  pnx_synth_set_config(cfg);
  for (int i = 0; i < PNX_SYNTH_SLOTS; i++) pnx_synth_note_on((uint8_t)i, 48 + i * 5, 200);
  PnxSynthBench b;
  pnx_synth_bench(chunks, CHUNK, &b);
  // Read the checksum, or the whole render is dead code and the compiler is entitled to
  // delete it -- which has already cost this project a 16 KB array once.
  if (b.checksum == 0x7FFFFFFF) note("  (impossible checksum, kept for the optimiser)");
  return b.ns_per_sample;
}

// The packed record and the C decoder are two hand-written descriptions of one layout,
// in two languages. Nothing makes the compiler compare them, so they are compared here: a
// record built byte by byte the way pack_synth_instrument builds it, run through the
// decoder, every field asserted. Drift between them yields an instrument that loads and
// sounds wrong -- the worst kind of mismatch, because nothing errors.
static void test_record_roundtrip(void) {
  uint8_t rec[PNX_SYNTH_RECORD_BYTES];
  memset(rec, 0, sizeof(rec));

  rec[0] = 3;                       // osc_count
  rec[1] = PNX_FILTER_BANDPASS;
  rec[2] = 60;  rec[3] = 190; rec[4] = 180;
  rec[5] = PNX_LFO_DUTY; rec[6] = 40; rec[7] = 25;
  rec[8] = 0x84; rec[9] = 0x03;     // pitch_env_amount = 900
  rec[10] = 200; rec[11] = 90; rec[12] = 70;

  rec[14] = 5;   rec[16] = 200; rec[18] = 190; rec[20] = 150;
  rec[22] = 2;   rec[24] = 44; rec[25] = 1;    // cutoff.decay_ms = 300
  rec[26] = 80;  rec[28] = 200;

  // The third oscillator carries a NEGATIVE detune and octave, because a sign error in a
  // hand-written little-endian decode is exactly what this exists to catch.
  const uint8_t waves[3] = { PNX_WAVE_SAW, PNX_WAVE_SQUARE, PNX_WAVE_TRIANGLE };
  const int16_t detunes[3] = { 0, 7, -9 };
  const int8_t octaves[3] = { 0, 1, -2 };
  for (int i = 0; i < 3; i++) {
    uint8_t *o = rec + 30 + i * 6;
    o[0] = waves[i];
    o[1] = (uint8_t)(200 - i * 10);
    o[2] = (uint8_t)(detunes[i] & 0xFF);
    o[3] = (uint8_t)((detunes[i] >> 8) & 0xFF);
    o[4] = (uint8_t)octaves[i];
    o[5] = (uint8_t)(128 - i * 32);
  }

  PnxSong song;
  memset(&song, 0, sizeof(song));
  song.synth = rec;
  song.synth_count = 1;
  song.synth_stride = PNX_SYNTH_RECORD_BYTES;

  PnxInstrument in;
  pnx_music_decode_instrument(&song, 0, &in);

  SY_CHECK("record: osc_count", in.osc_count == 3);
  SY_CHECK("record: filter mode", in.filter_mode == PNX_FILTER_BANDPASS);
  SY_CHECK("record: cutoff base/resonance/env",
           in.cutoff_base == 60 && in.resonance == 190 &&
           in.cutoff_env_amount == 180);
  SY_CHECK("record: lfo", in.lfo_target == PNX_LFO_DUTY &&
           in.lfo_rate == 40 && in.lfo_depth == 25);
  SY_CHECK("record: pitch envelope",
           in.pitch_env_amount == 900 && in.pitch_env_decay == 200);
  SY_CHECK("record: sends", in.reverb_send == 90 && in.chorus_send == 70);
  SY_CHECK("record: amp envelope",
           in.amp.attack_ms == 5 && in.amp.decay_ms == 200 &&
           in.amp.sustain == 190 && in.amp.release_ms == 150);
  SY_CHECK("record: cutoff envelope",
           in.cutoff.attack_ms == 2 && in.cutoff.decay_ms == 300 &&
           in.cutoff.sustain == 80 && in.cutoff.release_ms == 200);

  bool oscs_ok = true;
  for (int i = 0; i < 3; i++) {
    if (in.osc[i].wave != waves[i]) oscs_ok = false;
    if (in.osc[i].volume != (uint8_t)(200 - i * 10)) oscs_ok = false;
    if (in.osc[i].detune != detunes[i]) oscs_ok = false;
    if (in.osc[i].octave != octaves[i]) oscs_ok = false;
    if (in.osc[i].duty != (uint8_t)(128 - i * 32)) oscs_ok = false;
  }
  SY_CHECK("record: every oscillator, signs included", oscs_ok);
  SY_CHECK("record: the width both sides encode", PNX_SYNTH_RECORD_BYTES == 48);

  // A decoded instrument has to actually play, or the round trip proves only that bytes
  // moved. This is the join between the two halves of the work.
  pnx_synth_all_off();
  pnx_synth_set_instrument(0, &in);
  pnx_synth_note_on(0, 60, 200);
  int16_t probe[128];
  memset(probe, 0, sizeof(probe));
  pnx_synth_render(probe, 128);
  uint32_t nz = 0;
  for (int i = 0; i < 128; i++) if (probe[i]) nz++;
  SY_CHECK("a decoded instrument sounds", nz > 32);
  pnx_synth_all_off();
}

// Wavetables must be BAND-LIMITED per octave.
//
// A 64-entry table carries harmonics 1..32; at C6 that puts 25 of them above the 8 kHz
// Nyquist, where they fold back as inharmonic noise. Three detuned saws is the worst case
// there is for it, and it is what the example lead uses.
//
// Found by elimination on device -- clipping counted at zero, underruns counted at zero,
// still harsh -- so this asserts the property directly rather than leaving it to be heard.
// A crude energy ratio, not a spectrum analyser: everything above the highest harmonic a
// table is supposed to carry is aliasing by definition, and that is measurable by summing
// differences between neighbouring samples, which rises sharply with high-frequency
// content.
static void test_bandlimited_tables(void) {
  PnxSynthConfig c = pnx_synth_worst_case();
  c.oscillators = 1; c.filter = false; c.lfo = false;
  c.pitch_env = false; c.reverb = false; c.chorus = false;
  pnx_synth_set_config(&c);

  PnxInstrument in;
  memset(&in, 0, sizeof(in));
  in.osc_count = 1;
  in.osc[0].wave = PNX_WAVE_SAW;
  in.osc[0].volume = 255;
  in.osc[0].duty = 128;
  in.amp.attack_ms = 1;
  in.amp.decay_ms = 2000;
  in.amp.sustain = 255;
  in.amp.release_ms = 100;

  // Slew per sample, normalised by amplitude. A band-limited high note is a smooth curve;
  // an aliased one jumps around, because the folded harmonics are high-frequency by
  // construction. The HIGH note must not be rougher than the low one.
  int32_t rough[2] = { 0, 0 };
  const uint8_t notes[2] = { 48, 84 };        // C3 and C6
  for (int w = 0; w < 2; w++) {
    pnx_synth_all_off();
    pnx_synth_set_instrument(0, &in);
    pnx_synth_note_on(0, notes[w], 255);
    int16_t probe[512];
    memset(probe, 0, sizeof(probe));
    pnx_synth_render(probe, 512);
    int32_t slew = 0, peak = 1;
    for (int i = 1; i < 512; i++) {
      int32_t d = probe[i] - probe[i - 1];
      slew += d < 0 ? -d : d;
      int32_t m = probe[i] < 0 ? -probe[i] : probe[i];
      if (m > peak) peak = m;
    }
    rough[w] = slew / peak;                    // slew per unit amplitude
  }

  // Slew per sample scales with PITCH -- a C6 saw legitimately moves eight times as far
  // between samples as a C3 saw, because its fundamental is eight times faster. So the
  // assertion is not "the high note is smooth", it is "the high note is no rougher than
  // its pitch alone accounts for". C3 to C6 is three octaves, a factor of eight; the bound
  // allows half again on top for the coarser table.
  //
  // An unfiltered table fails this badly: at C6 it steps four entries per sample and
  // adjacent samples land anywhere in the cycle, so the ratio goes far past twelve.
  SY_CHECK("a high note is no rougher than its pitch accounts for",
           rough[1] <= rough[0] * 12);
  note("  saw slew/amplitude: C3 %d, C6 %d (pitch alone accounts for 8x)",
       (int)rough[0], (int)rough[1]);
  pnx_synth_all_off();
}

void test_synth(void) {
  int16_t *buf = (int16_t *)malloc(sizeof(int16_t) * CHUNK);

  SY_CHECK("the synth initialises", pnx_synth_init(RATE));
  note("  heap: %u B total, %u B of that effect delay lines",
       (unsigned)pnx_synth_bytes(), (unsigned)pnx_synth_effect_bytes());

  PnxSynthConfig worst = pnx_synth_worst_case();
  pnx_synth_set_config(&worst);
  // Set explicitly, because the default is 0 -- the normal path renders into the mixer,
  // which supplies its own bit before its clamp. These tests render the synth ALONE, so
  // they own the headroom the mixer would otherwise apply, and the peak figure below is
  // only interpretable against a stated setting.
  pnx_synth_set_headroom(1);

  PnxInstrument lead = lead_instrument();
  for (int i = 0; i < PNX_SYNTH_SLOTS; i++) pnx_synth_set_instrument((uint8_t)i, &lead);

  // --- it makes a sound at all
  pnx_synth_note_on(0, 60, 200);
  render_into(buf, CHUNK);
  SY_CHECK("a voice produces signal", nonzero_of(buf, CHUNK) > CHUNK / 2);
  SY_CHECK("and stays inside int16", peak_of(buf, CHUNK) <= 32767);

  // --- four voices at once, which is the case that has to fit
  for (int i = 0; i < PNX_SYNTH_SLOTS; i++) pnx_synth_note_on((uint8_t)i, 48 + i * 5, 220);
  SY_CHECK("every slot sounds together",
           pnx_synth_active_voices() >= PNX_SYNTH_SLOTS);
  render_into(buf, CHUNK);
  int32_t four_peak = peak_of(buf, CHUNK);
  SY_CHECK("four detuned voices do not wrap", four_peak <= 32767);
  // Judged against 127, not 32767. The mixer clamps its accumulator to the 8-bit range
  // before output regardless of whether the device is 8- or 16-bit, so that -- not int16
  // full scale -- is the ceiling this signal actually has to live under. Reporting it
  // against 32767 would show 0% and hide the headroom problem entirely.
  note("  four-voice peak: %d of the mixer's 127 (%d%% of the domain it clamps to)",
       (int)four_peak, (int)(four_peak * 100 / 127));
  SY_CHECK("four voices leave headroom in the mixer's 8-bit domain", four_peak <= 127);

  // --- envelopes reach their end
  pnx_synth_all_off();
  pnx_synth_note_on(0, 60, 255);
  for (int i = 0; i < 40; i++) render_into(buf, CHUNK);   // ~1.9 s
  SY_CHECK("a held note is still sounding at sustain", pnx_synth_active_voices() == 1);
  pnx_synth_note_off(0);
  for (int i = 0; i < 20; i++) render_into(buf, CHUNK);
  SY_CHECK("and release ends the voice", pnx_synth_active_voices() == 0);

  // --- the resonant filter does not blow up
  //
  // A resonant SVF driven hard genuinely can self-oscillate, and on integers that is a
  // wrap to full scale rather than a graceful overload -- it would be heard as a burst of
  // noise, not as a loud filter.
  PnxInstrument screaming = lead;
  screaming.resonance = 255;
  screaming.cutoff_base = 250;
  screaming.cutoff_env_amount = 255;
  pnx_synth_set_instrument(0, &screaming);
  pnx_synth_all_off();
  pnx_synth_note_on(0, 84, 255);
  bool wrapped = false;
  for (int i = 0; i < 60; i++) {
    render_into(buf, CHUNK);
    // A wrap shows up as adjacent samples at opposite extremes.
    for (uint32_t n = 1; n < CHUNK; n++)
      if ((buf[n - 1] > 30000 && buf[n] < -30000) ||
          (buf[n - 1] < -30000 && buf[n] > 30000)) wrapped = true;
  }
  SY_CHECK("maximum resonance does not wrap to full scale", !wrapped);

  // --- a pitch envelope actually moves the pitch
  //
  // Measured as zero crossings early against late: a kick starts high and falls, so the
  // first window must cross zero more often than the last. Without this the drum story
  // has no test at all.
  PnxInstrument kick = kick_instrument();
  pnx_synth_set_instrument(1, &kick);
  pnx_synth_all_off();
  // A long sustain, so the window being compared is still SOUNDING. The first version of
  // this used a percussive envelope and measured 58 crossings against 0 -- which passes,
  // and proves only that the note ended. It would have passed with no pitch envelope at
  // all.
  kick.amp.decay_ms = 400;
  kick.amp.sustain = 200;
  pnx_synth_set_instrument(1, &kick);
  pnx_synth_note_on(1, 36, 255);
  render_into(buf, CHUNK);
  uint32_t early = 0;
  for (uint32_t n = 1; n < CHUNK; n++)
    if ((buf[n - 1] < 0) != (buf[n] < 0)) early++;
  for (int i = 0; i < 3; i++) render_into(buf, CHUNK);
  uint32_t late = 0;
  for (uint32_t n = 1; n < CHUNK; n++)
    if ((buf[n - 1] < 0) != (buf[n] < 0)) late++;
  SY_CHECK("a pitch envelope falls over the note", early > late);
  SY_CHECK("and the note is still sounding when the fall is measured", late > 0);
  note("  kick zero crossings: %u early -> %u late (still sounding)",
       (unsigned)early, (unsigned)late);

  // --- swapping an instrument mid-note does not disturb the note
  //
  // This is the whole "push an instrument into a slot mid-song" requirement. The note
  // must finish on the instrument it started with; changing oscillator count under a
  // running voice is a click in the middle of a held note.
  pnx_synth_all_off();
  pnx_synth_set_instrument(0, &lead);
  pnx_synth_note_on(0, 60, 200);
  render_into(buf, CHUNK);
  int32_t before = peak_of(buf, CHUNK);
  PnxInstrument other = kick_instrument();
  pnx_synth_set_instrument(0, &other);
  render_into(buf, CHUNK);
  int32_t after = peak_of(buf, CHUNK);
  SY_CHECK("swapping a slot leaves the sounding note alone",
        after > 0 && before > 0 && (after * 4 > before));
  pnx_synth_all_off();
  pnx_synth_note_on(0, 60, 200);
  render_into(buf, CHUNK);
  SY_CHECK("and the next note uses the new instrument", nonzero_of(buf, CHUNK) > 0);

  // --- turning a feature off removes its work
  //
  // The point of the whole spike: attribute the cost rather than measure it in aggregate.
  // Host numbers, so only the ORDER is meaningful -- but the order is what decides which
  // feature gets cut if the device says no.
  pnx_synth_all_off();
  pnx_synth_set_instrument(0, &lead);
  for (int i = 0; i < PNX_SYNTH_SLOTS; i++) pnx_synth_set_instrument((uint8_t)i, &lead);

  // Enough repetitions that a feature worth a nanosecond a sample still moves the
  // millisecond clock. At 200 chunks the cheap features all reported exactly 0, which is
  // the resolution failing, not the feature being free -- and "free" is precisely the
  // wrong conclusion to hand someone deciding what to cut.
  const uint32_t CH = 2000;                   // ~96 s of audio per configuration
  uint32_t full = cost_of(&worst, CH);

  PnxSynthConfig c;
  c = worst; c.reverb = false;      uint32_t no_reverb = cost_of(&c, CH);
  c = worst; c.chorus = false;      uint32_t no_chorus = cost_of(&c, CH);
  c = worst; c.filter = false;      uint32_t no_filter = cost_of(&c, CH);
  c = worst; c.resonance = false;   uint32_t no_res    = cost_of(&c, CH);
  c = worst; c.lfo = false;         uint32_t no_lfo    = cost_of(&c, CH);
  c = worst; c.pitch_env = false;   uint32_t no_pitch  = cost_of(&c, CH);
  c = worst; c.oscillators = 2;     uint32_t two_osc   = cost_of(&c, CH);
  c = worst; c.oscillators = 1;     uint32_t one_osc   = cost_of(&c, CH);

  note("  host ns/sample, 4 voices (ORDERING only -- the device is the authority):");
  note("    everything on        %5u", (unsigned)full);
  note("    third oscillator     %5d", (int)full - (int)two_osc);
  note("    second oscillator    %5d", (int)two_osc - (int)one_osc);
  note("    filter + envelope    %5d", (int)full - (int)no_filter);
  note("    resonance            %5d", (int)full - (int)no_res);
  note("    reverb               %5d", (int)full - (int)no_reverb);
  note("    chorus               %5d", (int)full - (int)no_chorus);
  note("    lfo                  %5d", (int)full - (int)no_lfo);
  note("    pitch envelope       %5d", (int)full - (int)no_pitch);

  SY_CHECK("everything on costs more than one oscillator alone", full > one_osc);
  SY_CHECK("dropping the filter is cheaper than keeping it", no_filter < full);
  SY_CHECK("dropping reverb is cheaper than keeping it", no_reverb < full);

  // Realtime fraction on THIS machine, purely as a sanity floor: if a desktop cannot do
  // it in a small fraction of realtime, the watch has no chance and the design is wrong
  // before anyone flashes anything.
  pnx_synth_set_config(&worst);
  for (int i = 0; i < PNX_SYNTH_SLOTS; i++) pnx_synth_note_on((uint8_t)i, 48 + i * 5, 200);
  PnxSynthBench b;
  pnx_synth_bench(CH, CHUNK, &b);
  note("  host realtime fraction: %u.%02u%% of one core at %u Hz",
       (unsigned)(b.pct_of_realtime / 100), (unsigned)(b.pct_of_realtime % 100),
       (unsigned)RATE);
  SY_CHECK("the host renders faster than realtime", b.pct_of_realtime < 10000);

  test_bandlimited_tables();
  test_record_roundtrip();

  pnx_synth_shutdown();
  SY_CHECK("shutdown releases the heap", pnx_synth_bytes() == 0);
  free(buf);
}

#endif  // PNX_USE_SYNTH
