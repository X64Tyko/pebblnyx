// Text draw benchmark: SDK text vs the glyph blitter at two bit depths, across three
// string lengths, isolated and on real hardware.
//
// Three questions, not one:
//
//   - How much does one call cost? (the original question)
//   - Does that cost scale with glyph count, or is it flat overhead? (extended for)
//   - How much does 2bpp (antialiased) cost over 1bpp (crisp) for the glyph blitter?
//     MEASUREMENTS.md's Render cost table flagged 2bpp as "strictly more expensive" by
//     reasoning -- a destination read and a three-channel blend per pixel -- and never
//     quantified it. SDK text has no depth knob we control, so this axis only applies to
//     the glyph path; PATH_SDK is still measured for the same reason it always was, as
//     the baseline the whole feature exists to beat.
//
// Method: three strings (5, 13 and 20 glyphs) each drawn through three paths -- SDK text,
// glyph blit at depth 1, glyph blit at depth 2 -- bracketed with pnx_platform_now_ms() and
// repeated many times a sub-phase to smooth the 1ms clock (see MEASUREMENTS.md's
// "Manufactured precision" trap). Paths are interleaved within each string length rather
// than run as three long blocks, so a slow drift over the run lands on all three rather
// than favouring whichever went first. From the short and long tiers, a two-point estimate
// of (fixed per-call cost) + (marginal cost per glyph) is computed for each path.
//
// Getting the numbers off the watch is a real design constraint, not an afterthought.
// `pebble install --logs` attaches AFTER init() -- logs from boot are lost (see
// pnx_diag.h) -- so:
//
//   - The benchmark does NOT start at boot. It waits on SELECT, on screen, so there is as
//     much time as needed to attach `pebble logs` FIRST.
//   - Pressing SELECT flushes immediately and switches the deferred ring to pass-through,
//     so from that point every pnx_log() call goes straight out rather than waiting to be
//     collected.
//   - Every sub-phase logs its own result as it finishes, and every log line stays well
//     under pnx_log's 96-char cap -- an earlier version of a combined summary line did
//     not, and silently truncated. Compact `sdk=`/`g1=`/`g2=` labels are chosen for
//     exactly this: three numbers on one line without getting anywhere near the cap.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 512
#define SCENE_BYTES   (4 * 1024)

// Three lengths, not one -- see the header comment. Chosen to stay on one line at 14px
// within the SDK box below without wrapping; MEDIUM is the original single-string test
// kept for continuity with earlier runs.
#define TIER_COUNT 3
static const char *TIER_STRING[TIER_COUNT] = {
  "HP 42",                  // SHORT
  "LV5  HP 42/60",          // MEDIUM -- the original benchmark string
  "LV5  HP 42/60  MP 8",    // LONG
};
// Counted characters, not measured advance width -- kerning and variable glyph width are
// exactly what is NOT being isolated here; each character is one blit (glyph path) or one
// unit of layout work (SDK path), which is the quantity the marginal-cost estimate below
// is against.
static const uint8_t TIER_GLYPHS[TIER_COUNT] = { 5, 13, 20 };

typedef enum { PATH_SDK, PATH_GLYPH1, PATH_GLYPH2, PATH_COUNT } Path;
static const char *PATH_NAME[PATH_COUNT] = { "sdk", "g1", "g2" };   // short: log line budget

#define FRAMES_PER_SUBPHASE 40   // ~1.6s at the locked 25fps render cadence
#define WARMUP_FRAMES       20   // settle the frame rate after SELECT, before timing starts

#define N_GLYPH_PER_FRAME 30    // cheap; many reps/frame keeps the run short
#define N_SDK_PER_FRAME    3    // ~1.4ms each -- 3 is ~4ms, well inside the frame budget

// One step per (tier, path) triple: 0=T0 sdk, 1=T0 g1, 2=T0 g2, 3=T1 sdk, ...
#define STEP_COUNT (TIER_COUNT * PATH_COUNT)

typedef enum {
  PHASE_IDLE,      // waiting for SELECT; nothing timed runs here
  PHASE_WARMUP,
  PHASE_RUN,
  PHASE_DONE,
} Phase;

typedef struct {
  PnxArena persistent, scene;
  PnxFont font1, font2;   // depth 1 (bench) and depth 2 (bench2)
  bool has_font1, has_font2;

  Phase phase;
  uint32_t phase_frame;
  uint8_t step;   // 0..STEP_COUNT-1 during PHASE_RUN

  uint32_t ms[PATH_COUNT][TIER_COUNT], calls[PATH_COUNT][TIER_COUNT];

  char status[64];
  char result[TIER_COUNT][56];
  char fit[PATH_COUNT][56];
} App;

static uint32_t per_call_us(uint32_t total_ms, uint32_t calls) {
  return calls ? (total_ms * 1000u) / calls : 0;
}

static uint8_t step_tier(uint8_t step) { return (uint8_t)(step / PATH_COUNT); }
static Path step_path(uint8_t step) { return (Path)(step % PATH_COUNT); }

static void start_bench(App *a) {
  a->phase = PHASE_WARMUP;
  a->phase_frame = 0;
  a->step = 0;
  memset(a->ms, 0, sizeof(a->ms));
  memset(a->calls, 0, sizeof(a->calls));
  for (int i = 0; i < TIER_COUNT; i++) a->result[i][0] = '\0';
  for (int i = 0; i < PATH_COUNT; i++) a->fit[i][0] = '\0';

  // Switches the deferred ring to pass-through -- see the header comment. Everything
  // logged from here on (including this line) goes straight out rather than waiting to
  // be collected, so a log stream attached at any point during the run, not just before
  // it, sees the rest of it live.
  pnx_diag_flush();
  pnx_log("textbench: run started -- %u tiers (%u/%u/%u glyphs) x sdk/g1(1bpp)/g2(2bpp)",
          (unsigned)TIER_COUNT, (unsigned)TIER_GLYPHS[0], (unsigned)TIER_GLYPHS[1],
          (unsigned)TIER_GLYPHS[2]);
}

// A signed two-point estimate: cost = fixed + marginal*glyphs, solved from the SHORT and
// LONG tiers (index 0 and TIER_COUNT-1). Not a regression -- three points is thin for
// one -- but enough to say whether a path's cost is mostly per-call overhead or mostly
// content, which is the actual question.
static void fit_two_point(uint32_t us_short, uint32_t us_long, int32_t *out_fixed,
                          int32_t *out_marginal) {
  const int32_t dg = (int32_t)TIER_GLYPHS[TIER_COUNT - 1] - (int32_t)TIER_GLYPHS[0];
  const int32_t dc = (int32_t)us_long - (int32_t)us_short;
  *out_marginal = dg ? dc / dg : 0;
  *out_fixed = (int32_t)us_short - (*out_marginal) * (int32_t)TIER_GLYPHS[0];
}

static void report_results(App *a) {
  uint32_t us[PATH_COUNT][TIER_COUNT];

  for (int t = 0; t < TIER_COUNT; t++) {
    for (int p = 0; p < PATH_COUNT; p++) us[p][t] = per_call_us(a->ms[p][t], a->calls[p][t]);

    pnx_format(a->result[t], sizeof(a->result[t]), "%ug sdk=%u g1=%u g2=%u",
               (unsigned)TIER_GLYPHS[t], (unsigned)us[PATH_SDK][t],
               (unsigned)us[PATH_GLYPH1][t], (unsigned)us[PATH_GLYPH2][t]);
    // Compact labels are what keep this well under pnx_log's 96-char cap with three
    // numbers on one line -- see the header comment.
    pnx_log("textbench: %ug -- sdk=%uus g1=%uus g2=%uus (%u calls each)",
            (unsigned)TIER_GLYPHS[t], (unsigned)us[PATH_SDK][t], (unsigned)us[PATH_GLYPH1][t],
            (unsigned)us[PATH_GLYPH2][t], (unsigned)a->calls[PATH_SDK][t]);
  }

  for (int p = 0; p < PATH_COUNT; p++) {
    int32_t fixed, marginal;
    fit_two_point(us[p][0], us[p][TIER_COUNT - 1], &fixed, &marginal);
    pnx_format(a->fit[p], sizeof(a->fit[p]), "%s ~%dus + %dus/glyph",
               PATH_NAME[p], (int)fixed, (int)marginal);
    pnx_log("textbench: FIT %s (from %u/%u glyphs) ~%dus + %dus/glyph", PATH_NAME[p],
            (unsigned)TIER_GLYPHS[0], (unsigned)TIER_GLYPHS[TIER_COUNT - 1],
            (int)fixed, (int)marginal);
  }
}

// Advances phase_frame and moves to the next step once this phase's length is hit --
// WARMUP_FRAMES for the warmup, FRAMES_PER_SUBPHASE for every (tier, path) step. Called
// once per frame, after this frame's own timed work (if any) has already run, so the
// transition never eats the frame that triggers it.
static void advance(App *a) {
  a->phase_frame++;
  const uint32_t length = (a->phase == PHASE_WARMUP) ? WARMUP_FRAMES : FRAMES_PER_SUBPHASE;
  if (a->phase_frame < length) return;
  a->phase_frame = 0;

  if (a->phase == PHASE_WARMUP) {
    a->phase = PHASE_RUN;
    return;
  }
  if (a->phase != PHASE_RUN) return;   // IDLE/DONE: only SELECT moves out; see frame()

  const uint8_t tier = step_tier(a->step);
  const Path path = step_path(a->step);
  pnx_log("textbench: %ug %s done -- %uus/call, %u calls", (unsigned)TIER_GLYPHS[tier],
          PATH_NAME[path], (unsigned)per_call_us(a->ms[path][tier], a->calls[path][tier]),
          (unsigned)a->calls[path][tier]);

  a->step++;
  if (a->step >= STEP_COUNT) {
    a->phase = PHASE_DONE;
    report_results(a);
  }
}

static void frame(void *ctx, uint32_t elapsed_ms, PnxTarget *target) {
  App *a = (App *)ctx;
  const uint32_t work_start = pnx_platform_now_ms();

  PnxEvent ev;
  while (pnx_platform_poll_event(&ev)) {
    if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_SELECT &&
        (a->phase == PHASE_IDLE || a->phase == PHASE_DONE)) {
      start_bench(a);
    }
  }

  pnx_gfx_clear(target, 0xC0);   // opaque black -- see resonant's IN_BLACK for the same value

  // The glyph side's timed work happens HERE, during the frame, like any other blit --
  // for BOTH bit depths, which font differs by which one is being timed this step.
  if (a->phase == PHASE_RUN && step_path(a->step) != PATH_SDK) {
    const uint8_t tier = step_tier(a->step);
    const Path path = step_path(a->step);
    const PnxFont *f = (path == PATH_GLYPH1) ? &a->font1 : &a->font2;
    const bool ready = (path == PATH_GLYPH1) ? a->has_font1 : a->has_font2;
    if (ready) {
      const uint32_t t0 = pnx_platform_now_ms();
      for (int i = 0; i < N_GLYPH_PER_FRAME; i++) {
        pnx_text_draw(target, f, TIER_STRING[tier], 10, 80, 0xFF);
      }
      const uint32_t t1 = pnx_platform_now_ms();
      a->ms[path][tier] += (t1 - t0);
      a->calls[path][tier] += N_GLYPH_PER_FRAME;
    }
  }

  if (a->has_font1) {
    switch (a->phase) {
      case PHASE_IDLE:
        pnx_format(a->status, sizeof(a->status), "SELECT to start (attach logs first)");
        break;
      case PHASE_WARMUP:
        pnx_format(a->status, sizeof(a->status), "starting...");
        break;
      case PHASE_RUN:
        pnx_format(a->status, sizeof(a->status), "%ug %s  %u/%u",
                   (unsigned)TIER_GLYPHS[step_tier(a->step)], PATH_NAME[step_path(a->step)],
                   (unsigned)a->phase_frame + 1, FRAMES_PER_SUBPHASE);
        break;
      case PHASE_DONE:
        pnx_format(a->status, sizeof(a->status), "done -- SELECT to rerun");
        break;
    }
    pnx_text_draw(target, &a->font1, "pnx textbench", 10, 20, 0xFF);
    pnx_text_draw(target, &a->font1, a->status, 10, 40, 0xC7);

    if (a->phase == PHASE_DONE) {
      int16_t y = 70;
      for (int t = 0; t < TIER_COUNT; t++) {
        pnx_text_draw(target, &a->font1, a->result[t], 10, y, 0xFF);
        y = (int16_t)(y + 18);
      }
      y = (int16_t)(y + 8);
      static const uint8_t FIT_INK[PATH_COUNT] = { 0xF0, 0xCC, 0xC7 };
      for (int p = 0; p < PATH_COUNT; p++) {
        pnx_text_draw(target, &a->font1, a->fit[p], 10, y, FIT_INK[p]);
        y = (int16_t)(y + 18);
      }
    }
  }

  advance(a);
  pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
}

// The SDK side's timed work happens HERE, after the frame's own blitting -- the SDK hook
// is only callable once the framebuffer is released, which is the whole reason E7 exists.
// This is the only place in the app pnx_platform_text_draw is called. Box width is 185px
// -- wide enough that the LONG tier (20 chars) still draws as one line rather than
// word-wrapping, which would silently change what is being timed.
static void post_frame(void *ctx) {
  App *a = (App *)ctx;
  if (a->phase != PHASE_RUN || step_path(a->step) != PATH_SDK) return;

  const uint8_t tier = step_tier(a->step);
  const uint32_t t0 = pnx_platform_now_ms();
  for (int i = 0; i < N_SDK_PER_FRAME; i++) {
    pnx_platform_text_draw(TIER_STRING[tier], PNX_TEXT_SMALL, 0xFF, 10, 80, 185, 20);
  }
  const uint32_t t1 = pnx_platform_now_ms();
  a->ms[PATH_SDK][tier] += (t1 - t0);
  a->calls[PATH_SDK][tier] += N_SDK_PER_FRAME;
}

int main(void) {
  static App a;
  memset(&a, 0, sizeof(a));

  if (!pnx_arena_init(&a.persistent, "persistent", PERSIST_BYTES, 4) ||
      !pnx_arena_init(&a.scene, "scene", SCENE_BYTES, 4)) {
    pnx_platform_log("arena init failed");
    return 1;
  }

  static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;
  pnx_assets_init(&a.persistent, &a.scene, RESOURCES, PNX_ASSET_COUNT);

  a.has_font1 = pnx_font_load(&a.font1, PNX_ASSET_FONT_BENCH);
  a.has_font2 = pnx_font_load(&a.font2, PNX_ASSET_FONT_BENCH2);
  if (!a.has_font1) pnx_log("textbench: 1bpp font would not load -- nothing to draw");
  if (!a.has_font2) pnx_log("textbench: 2bpp font would not load -- that tier stays 0");

  a.phase = PHASE_IDLE;

  pnx_platform_set_post_frame_fn(post_frame);
  pnx_platform_run(frame, &a);

  pnx_arena_destroy(&a.scene);
  pnx_arena_destroy(&a.persistent);
  return 0;
}
