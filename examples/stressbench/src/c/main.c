// Combined-load frame cost: audio + graphics + text + an incremental save step, together,
// on real hardware -- DESIGN.md's open question #3 ("each subsystem's cost is measured;
// the sum never was"), answered directly rather than left as reasoning.
//
// Every frame, continuously: a synthetic full-screen graphics load (a checkerboard of
// pnx_gfx_fill_rect calls covering all 200x228 pixels -- NOT a real tilemap, which is
// already measured on its own in MEASUREMENTS.md's Render cost table; this is a stand-in
// with a comparable pixel-touching cost, so it does not need its own atlas/manifest), one
// glyph-blitted status line, and a continuously looping sample feeding through the
// existing audio timer. Every few seconds, ON TOP of all of that, an incremental save
// (pnx_save_begin, ~2000 bytes / ~8 chunks) starts and spends one pnx_save_step() per
// frame until it finishes -- which is the actual M5-intended usage, and the one thing
// this app exists to answer: does a save step landing in the SAME frame as the graphics
// and text load blow the ~35ms budget, and does it starve the audio timer enough to show
// up as a gap or a restart.
//
// Tracks the worst single frame seen with a save step active, the worst without one, and
// audio stats immediately before/after each save cycle -- a contention signal, since the
// audio feed runs on its own timer specifically so frame work cannot block it (see
// pnx_platform.h's own note on why), and this is the first real test of whether that
// isolation holds up when the frame side is also under load.
//
// Getting the numbers off the watch: same discipline as the other pnx benchmarks. Waits
// on SELECT so `pebble logs` can be attached first; SELECT flushes to pass-through
// immediately; every save cycle logs its own result; every log line stays under 96 chars.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 512
#define SCENE_BYTES   (4 * 1024)

#define TEST_SLOT ((PnxSaveSlot)0)
#define SAVE_VERSION 1
#define SAVE_PAYLOAD_BYTES 2000   // ~8 chunks at PNX_SAVE_CHUNK0_PAYLOAD=248 / 256 each
#define SAVE_INTERVAL_MS 3000     // a new save cycle starts this often once idle

static uint8_t s_payload[SAVE_PAYLOAD_BYTES];

typedef struct {
  PnxArena persistent, scene;
  PnxFont font;
  bool has_font;

  bool running;
  uint32_t frames;

  bool saving;
  uint32_t next_save_at_ms;
  uint32_t save_frames_this_cycle;
  PnxAudioStats stats_before_save;

  uint32_t worst_ms_with_save, worst_ms_no_save;
  uint32_t frames_with_save, frames_no_save;
  uint32_t sum_ms_with_save, sum_ms_no_save;

  char status[64];
  char result1[64], result2[64];
} App;

static void log_cycle_start(App *a) {
  a->saving = true;
  a->save_frames_this_cycle = 0;
  a->stats_before_save = *pnx_audio_stats();
  for (size_t i = 0; i < SAVE_PAYLOAD_BYTES; i++) s_payload[i] = (uint8_t)(i ^ 0xA5);
  pnx_save_begin(TEST_SLOT, s_payload, SAVE_PAYLOAD_BYTES, SAVE_VERSION);
  pnx_log("stress: save cycle started -- %u bytes", (unsigned)SAVE_PAYLOAD_BYTES);
}

static void log_cycle_end(App *a) {
  const PnxAudioStats *now_stats = pnx_audio_stats();
  pnx_log("stress: save cycle done -- %u frames, audio g +%u wg %u->%u",
          (unsigned)a->save_frames_this_cycle,
          (unsigned)(now_stats->left_playing - a->stats_before_save.left_playing),
          (unsigned)a->stats_before_save.worst_gap_ms, (unsigned)now_stats->worst_gap_ms);
  a->saving = false;
  a->next_save_at_ms = pnx_platform_now_ms() + SAVE_INTERVAL_MS;
}

static void draw_synthetic_graphics(PnxTarget *target) {
  static const uint8_t COLORS[2] = { 0xD5, 0xC7 };   // resonant's IN_DIM / IN_STEEL values
  const int16_t w = pnx_target_width(target), h = pnx_target_height(target);
  int idx = 0;
  for (int16_t y = 0; y < h; y = (int16_t)(y + 16)) {
    for (int16_t x = 0; x < w; x = (int16_t)(x + 16)) {
      const int16_t rw = (int16_t)((x + 16 <= w) ? 16 : (w - x));
      const int16_t rh = (int16_t)((y + 16 <= h) ? 16 : (h - y));
      pnx_gfx_fill_rect(target, x, y, rw, rh, COLORS[idx & 1]);
      idx++;
    }
  }
}

static void audio_tick(void *ctx) {
  (void)ctx;
  pnx_audio_update(pnx_platform_now_ms());
}

static void frame(void *ctx, uint32_t elapsed_ms, PnxTarget *target) {
  App *a = (App *)ctx;
  const uint32_t t0 = pnx_platform_now_ms();

  PnxEvent ev;
  while (pnx_platform_poll_event(&ev)) {
    if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_SELECT && !a->running) {
      a->running = true;
      a->frames = 0;
      a->saving = false;
      a->worst_ms_with_save = a->worst_ms_no_save = 0;
      a->frames_with_save = a->frames_no_save = 0;
      a->sum_ms_with_save = a->sum_ms_no_save = 0;
      a->next_save_at_ms = pnx_platform_now_ms() + SAVE_INTERVAL_MS;
      pnx_diag_flush();
      pnx_log("stress: run started -- %u byte saves every %ums", (unsigned)SAVE_PAYLOAD_BYTES,
              (unsigned)SAVE_INTERVAL_MS);
    }
  }

  pnx_gfx_clear(target, 0xC0);

  if (a->running) {
    a->frames++;
    draw_synthetic_graphics(target);

    if (!a->saving && pnx_platform_now_ms() >= a->next_save_at_ms) log_cycle_start(a);
    if (a->saving) {
      pnx_save_step(TEST_SLOT);
      a->save_frames_this_cycle++;
      if (!pnx_save_pending(TEST_SLOT)) log_cycle_end(a);
    }

    if (a->has_font) {
      pnx_format(a->status, sizeof(a->status), "frame %u  %s", (unsigned)a->frames,
                 a->saving ? "SAVING" : "idle");
      pnx_format(a->result1, sizeof(a->result1), "worst w/save %ums (%u fr)",
                 (unsigned)a->worst_ms_with_save, (unsigned)a->frames_with_save);
      pnx_format(a->result2, sizeof(a->result2), "worst no save %ums (%u fr)",
                 (unsigned)a->worst_ms_no_save, (unsigned)a->frames_no_save);
    }
  } else if (a->has_font) {
    pnx_format(a->status, sizeof(a->status), "SELECT to start (attach logs first)");
  }

  if (a->has_font) {
    pnx_text_draw(target, &a->font, "pnx stressbench", 10, 20, 0xFF);
    pnx_text_draw(target, &a->font, a->status, 10, 40, 0xC7);
    if (a->running) {
      pnx_text_draw(target, &a->font, a->result1, 10, 65, 0xF0);
      pnx_text_draw(target, &a->font, a->result2, 10, 85, 0xCC);
    }
  }

  // Measured AFTER all of this frame's work, including the save step and the synthetic
  // graphics -- this IS the combined-load number the whole app exists to produce.
  const uint32_t frame_ms = pnx_platform_now_ms() - t0;
  if (a->running) {
    if (a->saving) {
      a->frames_with_save++;
      a->sum_ms_with_save += frame_ms;
      if (frame_ms > a->worst_ms_with_save) {
        a->worst_ms_with_save = frame_ms;
        pnx_log("stress: new worst WITH save -- %ums (frame %u)", (unsigned)frame_ms,
                (unsigned)a->frames);
      }
    } else {
      a->frames_no_save++;
      a->sum_ms_no_save += frame_ms;
      if (frame_ms > a->worst_ms_no_save) {
        a->worst_ms_no_save = frame_ms;
        pnx_log("stress: new worst without save -- %ums (frame %u)", (unsigned)frame_ms,
                (unsigned)a->frames);
      }
    }
  }

  pnx_diag_frame(elapsed_ms, frame_ms);
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

  a.has_font = pnx_font_load(&a.font, PNX_ASSET_FONT_BENCH);
  if (!a.has_font) pnx_log("stress: font would not load -- nothing to draw");

  if (!pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 60)) pnx_log("stress: audio would not open");

  size_t payload = 0;
  const uint8_t *d = pnx_blob_load(PNX_ASSET_SAMPLE_TONE, "PW", NULL, NULL, NULL, NULL, &payload);
  if (d && payload > 8) {
    const uint32_t hz = (uint32_t)(d[0] | (d[1] << 8) | (d[2] << 16) | ((uint32_t)d[3] << 24));
    const uint32_t len = (uint32_t)(payload - 8);
    // Looped, so the mixer and the timer both have continuous work the whole run rather
    // than firing once and going idle -- an idle mixer is not the load this app tests.
    pnx_audio_play(( const int8_t *)(d + 8), len, 0, hz, 90);
  } else {
    pnx_log("stress: tone sample would not load -- running without audio load");
  }

  pnx_platform_set_audio_timer(audio_tick, &a, 10);

  a.running = false;
  pnx_platform_run(frame, &a);

  pnx_audio_shutdown();
  pnx_arena_destroy(&a.scene);
  pnx_arena_destroy(&a.persistent);
  return 0;
}
