// Synth spike, on the watch. The device is the authority; everything else is reasoning.
//
// This answers one question with numbers: can a voice with three detuned oscillators, a
// resonant filter with its own envelope, an LFO, and sends into a global reverb and
// chorus run four at a time at 16 kHz on a Cortex-M33, and what does it cost in RAM.
//
// HOW IT REPORTS, AND WHY
//
// Everything is drawn to the SCREEN. `pebble install --logs` attaches after `init()`
// runs, so anything logged during startup is silently discarded -- a trap that has cost
// this project time three separate times. The results also go to the log, for when the
// run is long enough that the attach has happened, but the screen is the primary.
//
// Timing is by REPETITION. `time_ms()` has 1 ms resolution and per-sample cost here is
// nanoseconds, so each configuration renders tens of seconds of audio and the total is
// divided down. Never a multiplied millisecond clock.
//
// The benchmark renders into a scratch buffer rather than through the speaker. That is
// deliberate: mixing into the stream would fold the device's own feed cadence and short
// writes into the number, and what is being measured here is the SYNTH, not the mixer's
// already-measured feed behaviour.
//
// It also PLAYS, continuously, which the first version did not. A synth spike that only
// reports microseconds answers half the question -- the other half is whether the thing
// sounds like anything, and that is not a number. The stream is opened directly rather
// than through pnx_audio, because the mixer has no hook for "add my samples" and the
// spike is deliberately measuring the synth alone. Short writes carry, for the reason
// MEASUREMENTS.md records: dropped samples are a hole in the waveform the voices cannot
// regenerate, because their phase has already moved on.
//
// Benchmarking blocks, so audio WILL glitch while a sweep runs. That is expected and
// says nothing about the synth.
//
// SELECT re-runs. UP and DOWN move through the configurations one at a time, so the cost
// of each feature can be read off the watch rather than inferred from a total. BACK is
// left alone so the app can be exited normally.

#include "pnx/pnx.h"

#include <string.h>

#define PERSIST_BYTES 512
#define SCENE_BYTES   2048
#define RATE          16000u
#define CHUNK         768u

// ~19 s of audio per configuration. Long enough that a feature worth a nanosecond a
// sample still moves a millisecond clock; short enough that a full sweep of nine
// configurations finishes while someone is holding the watch.
#define BENCH_CHUNKS  400u

// Feed size and cadence. 10 ms timer, ~160 samples a feed at 16 kHz, fed to a lead so a
// late frame is absorbed -- the same shape the mixer uses and for the same reason.
#define FEED_SAMPLES  256u
#define FEED_MS       10u
// How far ahead of realtime to stay. The device buffer holds ~512 ms at 16 kHz 8-bit, so
// this is comfortably inside it, and it absorbs a timer that fires at 56 ms instead of 10.
#define LEAD_MS       150u

// A short phrase so there is something to listen to. Four channels, a note each, moving
// slowly enough that the timbre is what you hear rather than the tune.
//
// One note per SLOT. Slots and voices are not the same count and have not been since
// voice-pairing landed: PNX_SYNTH_VOICES is PNX_SYNTH_SLOTS * 2, because each slot owns a
// pair so a new note can overlap the one it displaces instead of cutting it. Every API
// here -- note_on, note_off, set_instrument -- is addressed by SLOT; the pair is the
// synth's business.
//
// The loops below iterated PNX_SYNTH_VOICES and read four notes off the end of every row.
// The synth's own slot bounds check swallowed the extra calls, so it was inaudible, and it
// stayed that way until -Werror=aggressive-loop-optimizations named it.
static const uint8_t PHRASE[8][PNX_SYNTH_SLOTS] = {
  { 45, 57, 64, 69 }, { 45, 57, 64, 72 },
  { 43, 55, 62, 67 }, { 43, 55, 62, 70 },
  { 41, 53, 60, 65 }, { 41, 53, 60, 69 },
  { 40, 52, 59, 64 }, { 40, 52, 59, 67 },
};
#define STEP_MS 500u

// So the table and the loops cannot drift apart again. If the slot count ever changes,
// this fails at compile time naming the actual problem rather than at run time naming a
// loop iteration.
_Static_assert(sizeof(PHRASE[0]) / sizeof(PHRASE[0][0]) == PNX_SYNTH_SLOTS,
               "PHRASE carries one note per synth SLOT, not per voice");

typedef struct {
  const char *name;
  bool silent;          // measured with no voices sounding: the loop's own floor
  PnxSynthConfig cfg;
  uint32_t ns;
  uint32_t pct;
  bool done;
} Case;

typedef struct {
  PnxArena persistent, scene;
  Case cases[12];
  uint8_t case_count;
  uint8_t showing;
  bool ran;
  bool running;
  uint8_t next_case;

  // Live playback.
  bool audio_on;
  int16_t feed[FEED_SAMPLES];
  uint32_t carry_bytes, carry_head;
  uint32_t next_step_ms;
  uint8_t step;
  uint32_t written, short_writes;
  uint32_t audio_start_ms;
  // A stream that drains leaves Playing and restarts on the next write, which is heard as
  // the sound starting over. THAT is the fault worth counting. Short writes are not: the
  // device buffer holds ~512 ms, so once primed it is nearly always full and refusing --
  // MEASUREMENTS.md measured 82% of writes short and called it normal. Displaying the
  // short count next to the byte total made expected behaviour look like a failure.
  uint16_t underruns;
  uint8_t last_state;
  uint32_t heap_total, heap_effects;
  int32_t peak;
  char line[10][40];
} App;

static void add_case(App *a, const char *name, PnxSynthConfig cfg) {
  if (a->case_count >= 12) return;
  a->cases[a->case_count].name = name;
  a->cases[a->case_count].cfg = cfg;
  a->case_count++;
}

static void build_cases(App *a) {
  a->case_count = 0;
  PnxSynthConfig w = pnx_synth_worst_case();
  add_case(a, "all on", w);

  PnxSynthConfig c;
  c = w; c.oscillators = 2;   add_case(a, "2 osc", c);
  c = w; c.oscillators = 1;   add_case(a, "1 osc", c);
  c = w; c.filter = false;    add_case(a, "no filter", c);
  c = w; c.resonance = false; add_case(a, "no reson", c);
  c = w; c.reverb = false;    add_case(a, "no reverb", c);
  c = w; c.chorus = false;    add_case(a, "no chorus", c);
  c = w; c.lfo = false;       add_case(a, "no lfo", c);
  c = w; c.pitch_env = false; add_case(a, "no pitchenv", c);

  // The 4,738 ns the feature ablations do NOT account for -- four voices, one oscillator
  // each, envelope and output -- is now the largest single item, and no case isolates it.
  // These two do: `bare` is the cheapest possible sounding voice, and `silence` is the
  // loop with nothing active, which is the floor everything else is measured against.
  c = w;
  c.oscillators = 1; c.filter = false; c.resonance = false;
  c.lfo = false; c.pitch_env = false; c.reverb = false; c.chorus = false;
  add_case(a, "bare voice", c);
  add_case(a, "silence", c);      // same config; run_case leaves the voices off
  a->cases[a->case_count - 1u].silent = true;
}

// The instrument the spike measures: three saws a few cents apart through a resonant
// lowpass with an envelope on the cutoff, vibrato, and both sends up. This is the
// expensive case on purpose -- a spike that measures a cheap instrument answers nothing.
static PnxInstrument worst_instrument(void) {
  PnxInstrument in;
  memset(&in, 0, sizeof(in));
  in.osc_count = 3;
  for (int i = 0; i < 3; i++) {
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
  in.cutoff.attack_ms = 2;
  in.cutoff.decay_ms = 300;
  in.cutoff.sustain = 80;
  in.cutoff.release_ms = 200;

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

// ------------------------------------------------------------------ live playback
//
// Straight to the platform stream. The synth accumulates in the mixer's 8-bit domain, so
// the conversion is a clamp to int8 -- the same reduction pnx_audio does before output,
// which is what keeps the peak figure on screen meaningful against what is actually
// heard.
// Fed to a LEAD against the wall clock, not a fixed amount per tick.
//
// The first version rendered exactly 256 samples -- 16 ms of audio -- on a 10 ms timer.
// That looks like 1.6x realtime and is fine right up until the timer is late, and
// MEASUREMENTS.md already measured this one: a 10 ms audio timer runs at 54-59 ms under
// load. At 56 ms a fixed 16 ms feed supplies 29% of realtime, the stream drains, the
// speaker leaves Playing and restarts on the next write -- which is heard as the sound
// starting over, roughly every quarter second. That was the popping.
//
// So the target is "be LEAD_MS ahead of where playback must be by now", and the loop
// renders until it gets there. Late ticks catch up instead of falling behind.
static void feed_audio(App *a) {
  if (!a->audio_on) return;
  const uint32_t now = pnx_platform_now_ms();
  if (!a->audio_start_ms) a->audio_start_ms = now;

  // Anything the device refused last time goes first, and is not regenerated. Dropping it
  // would leave a hole the voices cannot fill: their phase has already advanced past it.
  if (a->carry_bytes > a->carry_head) {
    const uint8_t *p = (const uint8_t *)a->feed + a->carry_head;
    const size_t left = a->carry_bytes - a->carry_head;
    const size_t wrote = pnx_platform_audio_write(p, left);
    a->written += (uint32_t)wrote;
    a->carry_head += (uint32_t)wrote;
    if (a->carry_head < a->carry_bytes) return;    // still full; try again next tick
    a->carry_bytes = a->carry_head = 0;
  }

  // One byte per sample at 8-bit, so bytes and samples are the same number here.
  const PnxAudioState st = pnx_platform_audio_state();
  if (a->last_state == PNX_AUDIO_PLAYING && st != PNX_AUDIO_PLAYING &&
      a->underruns < 0xFFFF) {
    a->underruns++;
  }
  a->last_state = (uint8_t)st;

  const uint32_t want = ((now - a->audio_start_ms + LEAD_MS) * 16000u) / 1000u;

  // Bounded, so a long stall cannot turn into an unbounded render inside one tick and
  // trip the watchdog trying to catch up on audio nobody is waiting for any more.
  for (int guard = 0; guard < 6 && a->written < want; guard++) {
    memset(a->feed, 0, sizeof(a->feed));
    pnx_synth_render(a->feed, FEED_SAMPLES);

    // int16 accumulator -> int8 output, in place. The output is half the accumulator's
    // width, so writing byte n only touches samples already read -- the same trick that
    // let the mixer collapse four buffers into one.
    int8_t *out8 = (int8_t *)a->feed;
    for (uint32_t n = 0; n < FEED_SAMPLES; n++) {
      int32_t v = a->feed[n];
      if (v > 127) v = 127;
      if (v < -127) v = -127;
      out8[n] = (int8_t)v;
    }

    const size_t bytes = FEED_SAMPLES;
    const size_t wrote = pnx_platform_audio_write(out8, bytes);
    a->written += (uint32_t)wrote;
    if (wrote < bytes) {
      a->short_writes++;
      a->carry_bytes = (uint32_t)bytes;
      a->carry_head = (uint32_t)wrote;
      break;                      // buffer is full; the lead is met by definition
    }
  }
}

// Advances the phrase so there is something to listen to, and re-triggers every note so
// the envelopes are exercised rather than sitting at sustain forever.
static void advance_phrase(App *a, uint32_t now) {
  if (!a->audio_on || now < a->next_step_ms) return;
  a->next_step_ms = now + STEP_MS;
  a->step = (uint8_t)((a->step + 1u) % 8u);
  for (int v = 0; v < PNX_SYNTH_SLOTS; v++)
    pnx_synth_note_on((uint8_t)v, PHRASE[a->step][v], 200);
}

static void audio_tick(void *ctx) {
  App *a = (App *)ctx;
  advance_phrase(a, pnx_platform_now_ms());
  feed_audio(a);
}

static void run_case(App *a, uint8_t i) {
  if (i >= a->case_count) return;
  PnxInstrument inst = worst_instrument();
  for (int v = 0; v < PNX_SYNTH_SLOTS; v++) pnx_synth_set_instrument((uint8_t)v, &inst);

  pnx_synth_set_config(&a->cases[i].cfg);
  // Re-triggered per configuration so every run measures four sounding voices. A voice
  // whose envelope had ended between cases would silently make that case look cheap.
  pnx_synth_all_off();
  if (!a->cases[i].silent) {
    for (int v = 0; v < PNX_SYNTH_SLOTS; v++)
      pnx_synth_note_on((uint8_t)v, (uint8_t)(48 + v * 5), 220);
  }

  PnxSynthBench b;
  pnx_synth_bench(BENCH_CHUNKS, CHUNK, &b);
  a->cases[i].ns = b.ns_per_sample;
  a->cases[i].pct = b.pct_of_realtime;
  a->cases[i].done = true;
  // The checksum has to be consumed or the render is dead code. A benchmark kernel with
  // no observable side effect gets deleted -- this project has already lost a 16 KB
  // array that way.
  if (b.checksum == 0x7FFFFFFF) pnx_log("impossible");

  // Logged as well as drawn. Startup logging is discarded -- `--logs` attaches after
  // init() -- but this runs on a button press, which is long after, so it survives. The
  // header claimed this and the first version did not do it.
  pnx_log("synth %-11s %4u ns/sample  %u.%02u%% of a core",
          a->cases[i].name, (unsigned)b.ns_per_sample,
          (unsigned)(b.pct_of_realtime / 100), (unsigned)(b.pct_of_realtime % 100));
}

// Peak of one rendered chunk with everything on, against the 127 the mixer clamps its
// accumulator to. Not int16 full scale: the mixer reduces to the 8-bit range before
// output whatever the device format is, so that is the ceiling this signal lives under
// and the number that says whether four voices plus SFX will clip.
static void measure_peak(App *a) {
  int16_t buf[128];
  PnxSynthConfig w = pnx_synth_worst_case();
  pnx_synth_set_config(&w);
  pnx_synth_all_off();
  for (int v = 0; v < PNX_SYNTH_SLOTS; v++)
    pnx_synth_note_on((uint8_t)v, (uint8_t)(48 + v * 5), 220);

  a->peak = 0;
  for (int c = 0; c < 40; c++) {
    memset(buf, 0, sizeof(buf));
    pnx_synth_render(buf, 128);
    for (int n = 0; n < 128; n++) {
      int32_t v = buf[n] < 0 ? -buf[n] : buf[n];
      if (v > a->peak) a->peak = v;
    }
  }
}

static void compose(App *a) {
  const Case *all = &a->cases[0];
  const Case *cur = &a->cases[a->showing];

  pnx_format(a->line[0], sizeof(a->line[0]), "SYNTH SPIKE  %u Hz", (unsigned)RATE);
  pnx_format(a->line[1], sizeof(a->line[1]), "heap %u B (fx %u)",
             (unsigned)a->heap_total, (unsigned)a->heap_effects);
  pnx_format(a->line[2], sizeof(a->line[2]), "peak %d/127 hr>>%u%s",
             (int)a->peak, (unsigned)pnx_synth_headroom(),
             a->peak > 127 ? " CLIP" : "");

  if (a->running) {
    pnx_format(a->line[3], sizeof(a->line[3]), "measuring %u/%u...",
               (unsigned)(a->next_case + 1u), (unsigned)a->case_count);
    pnx_format(a->line[4], sizeof(a->line[4]), "  %s", a->cases[a->next_case].name);
    a->line[5][0] = 0;
    return;
  }
  if (!a->ran) {
    pnx_format(a->line[3], sizeof(a->line[3]), "SELECT to run");
    a->line[4][0] = 0;
    return;
  }

  pnx_format(a->line[3], sizeof(a->line[3]), "ALL ON %u ns/sm", (unsigned)all->ns);
  pnx_format(a->line[4], sizeof(a->line[4]), "  = %u.%02u%% of a core",
             (unsigned)(all->pct / 100), (unsigned)(all->pct % 100));

  pnx_format(a->line[5], sizeof(a->line[5]), "%u/%u  %s",
             (unsigned)(a->showing + 1), (unsigned)a->case_count, cur->name);
  pnx_format(a->line[6], sizeof(a->line[6]), "  %u ns/sm", (unsigned)cur->ns);

  // The cost of what this case turns OFF, which is the number the whole spike exists to
  // produce: not "it costs 114" but "the third oscillator costs 21".
  int32_t delta = (int32_t)all->ns - (int32_t)cur->ns;
  if (a->showing == 0)
    pnx_format(a->line[7], sizeof(a->line[7]), "  (reference)");
  else
    pnx_format(a->line[7], sizeof(a->line[7]), "  saves %d ns/sm", (int)delta);

  if (a->audio_on)
    pnx_format(a->line[8], sizeof(a->line[8]), "%s  dry %u",
               a->underruns ? "GAPPED" : "clean", (unsigned)a->underruns);
  else
    pnx_format(a->line[8], sizeof(a->line[8]), "SPEAKER WOULD NOT OPEN");
  pnx_format(a->line[9], sizeof(a->line[9]), "SEL run  U/D cycle");
}

// Drawn from the post-frame hook, which is where text can be drawn at all -- the SDK
// path runs after the framebuffer is released.
static void post_frame(void *ctx) {
  App *a = (App *)ctx;
  int16_t y = 8;
  for (int i = 0; i < 10; i++) {
    if (!a->line[i][0]) continue;
    pnx_platform_text_draw(a->line[i], PNX_TEXT_SMALL, 0xFF, 6, y, 190, 20);
    y = (int16_t)(y + 20);
  }
}

static void frame(void *ctx, uint32_t elapsed_ms, PnxTarget *target) {
  App *a = (App *)ctx;
  (void)elapsed_ms;
  (void)target;

  // The input module does NOT drain the platform queue itself -- pnx_input.h says so
  // explicitly, because draining it would swallow the touch and focus events a game
  // wants. The app owns the queue and forwards. Leaving this out is why the first
  // version's buttons did nothing at all: pnx_input_frame() cleared the edges every
  // frame and nothing ever set them.
  pnx_input_frame();
  PnxEvent ev;
  while (pnx_platform_poll_event(&ev)) pnx_input_event(&ev);

  if (pnx_input_pressed(PNX_BUTTON_SELECT) && !a->running) {
    // pnx_log BUFFERS into a ring until this is called -- pnx_diag.h says so, because
    // `--logs` attaches after init() and anything logged before then is discarded with no
    // error. Without the flush the results went into the ring and stayed there, which is
    // why the first run reported nothing. A button press is exactly the moment the header
    // recommends calling it.
    pnx_diag_flush();
    a->running = true;
    a->next_case = 0;
  }
  if (pnx_input_pressed(PNX_BUTTON_UP) && a->case_count)
    a->showing = (uint8_t)((a->showing + a->case_count - 1u) % a->case_count);
  if (pnx_input_pressed(PNX_BUTTON_DOWN) && a->case_count)
    a->showing = (uint8_t)((a->showing + 1u) % a->case_count);

  // ONE configuration per frame, never the whole sweep in one press.
  //
  // Nine configurations back to back is seconds of solid compute inside a frame
  // callback. Pebble's watchdog kills an app that does not return to the event loop, so
  // the all-at-once version would either freeze visibly or be killed outright -- and
  // either way the screen cannot update to say what it is doing. Spread across frames the
  // progress is visible and the loop stays alive.
  if (a->running) {
    run_case(a, a->next_case);
    // The bench blocks for seconds. Without this the feed would wake up owing seconds of
    // audio and try to render all of it, which is a stall on top of a stall.
    a->audio_start_ms = 0;
    a->written = 0;
    a->next_case++;
    if (a->next_case >= a->case_count) {
      a->running = false;
      a->ran = true;
      measure_peak(a);
      // Restored to everything-on. The sweep ends on whichever case turned a feature
      // off, and leaving it there would mean the thing you listen to afterwards is
      // quietly missing a feature -- which is exactly the kind of silent difference that
      // makes someone mistrust a measurement.
      PnxSynthConfig w = pnx_synth_worst_case();
      pnx_synth_set_config(&w);
      pnx_log("synth heap %u B (effects %u B)  peak %d/127 at headroom >>%u",
              (unsigned)a->heap_total, (unsigned)a->heap_effects,
              (int)a->peak, (unsigned)pnx_synth_headroom());
    }
  }

  compose(a);
}

int main(void) {
  static App app;
  memset(&app, 0, sizeof(app));

  if (!pnx_arena_init(&app.persistent, "persistent", PERSIST_BYTES, 4) ||
      !pnx_arena_init(&app.scene, "scene", SCENE_BYTES, 4)) {
    pnx_platform_log("arena init failed");
    return 1;
  }
  pnx_input_init(PNX_ORIENT_BUTTONS_RIGHT);

  if (!pnx_synth_init(RATE)) {
    // Allocation refusal is a real outcome on this platform, not a formality, and it is
    // itself an answer: if the delay lines will not fit there is no point timing them.
    pnx_format(app.line[0], sizeof(app.line[0]), "SYNTH ALLOC FAILED");
    app.line[1][0] = 0;
  } else {
    // This spike writes to the stream directly rather than through the mixer, so it owns
    // the headroom the mixer would otherwise supply.
    pnx_synth_set_headroom(1);
    app.heap_total = pnx_synth_bytes();
    app.heap_effects = pnx_synth_effect_bytes();
    build_cases(&app);
    measure_peak(&app);
  }

  // 16 kHz 8-bit: the format the synth renders for, and the one the mixer's own
  // measurements were taken at.
  app.audio_on = pnx_platform_audio_open(PNX_AUDIO_16KHZ_8BIT, 90);
  if (!app.audio_on) pnx_log("synth spike: speaker would not open");
  else {
    PnxInstrument inst = worst_instrument();
    for (int v = 0; v < PNX_SYNTH_SLOTS; v++) {
      pnx_synth_set_instrument((uint8_t)v, &inst);
      pnx_synth_note_on((uint8_t)v, PHRASE[0][v], 200);
    }
    app.next_step_ms = pnx_platform_now_ms() + STEP_MS;
  }

  compose(&app);
  pnx_platform_set_post_frame_fn(post_frame);
  pnx_platform_set_audio_timer(audio_tick, &app, FEED_MS);
  pnx_platform_run(frame, &app);

  pnx_platform_audio_close();
  pnx_synth_shutdown();
  pnx_arena_destroy(&app.scene);
  pnx_arena_destroy(&app.persistent);
  return 0;
}
