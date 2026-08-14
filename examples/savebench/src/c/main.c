// Save timing benchmark: pnx_save (M5) on real hardware, for the first time.
//
// Host tests (tests/test_save.c) prove the chunking arithmetic against a mocked persist
// store. What they cannot prove is the timing the whole module was SIZED against: ~7ms
// per persist write regardless of size, ~297ms of will_focus warning before the app is
// covered. Those numbers came from an earlier probe project measuring raw
// persist_write_data calls directly -- never from this module, running its own chunking
// and versioning, on a watch. This app closes that gap, in three parts:
//
//   1. pnx_save_write at four payload sizes (1, 2, 5 and 16 chunks) -- confirms cost
//      scales with chunk count the way the design assumes, not with bytes.
//   2. pnx_save_begin/pnx_save_step spread across frames -- confirms the M5 "Done when"
//      claim directly: does a save spread across frames actually show no visible hitch,
//      i.e. does the worst single frame during it stay inside the ~35ms budget.
//   3. An ARMED test against a REAL notification. The app waits; you cover the watch;
//      the moment FOCUS_LOST arrives it does a blocking save-on-blur write and times
//      FOCUS_LOST -> save complete -> FOCUS_GAINED. This is the one thing a host or a
//      synthetic event can never stand in for -- an actual will_focus from actual
//      firmware, with an actual save racing it.
//
// Getting the numbers off the watch: same discipline as the other pnx benchmarks. Waits
// on SELECT so `pebble logs` can be attached first (part 3 especially needs this -- the
// result of covering the watch is only visible in the log, since the app may not get to
// render while covered); SELECT flushes the deferred ring to pass-through immediately;
// every stage logs its own result; every log line stays well under pnx_log's 96-char cap.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 512
#define SCENE_BYTES   (4 * 1024)

#define TEST_SLOT ((PnxSaveSlot)0)
#define SAVE_VERSION 1

// 1, 2, 5 and 16 chunks respectively at PNX_SAVE_CHUNK0_PAYLOAD=248 / PNX_PERSIST_KEY_
// BYTES=256 -- 4000B lands on 16 chunks, which is MEASUREMENTS.md's own "realistic 4KB
// save, 16 keys" reference case.
#define SIZE_COUNT 4
static const uint16_t SIZES[SIZE_COUNT] = { 24, 256, 1024, 4000 };
#define REPS_PER_SIZE 5

#define MAX_PAYLOAD 4000
static uint8_t s_payload[MAX_PAYLOAD];
static uint8_t s_readback[MAX_PAYLOAD];

static uint8_t chunk_count_for(size_t bytes) {
  if (bytes <= PNX_SAVE_CHUNK0_PAYLOAD) return 1;
  const size_t rest = bytes - PNX_SAVE_CHUNK0_PAYLOAD;
  return (uint8_t)(1 + (rest + PNX_PERSIST_KEY_BYTES - 1) / PNX_PERSIST_KEY_BYTES);
}

typedef enum {
  PHASE_IDLE,
  PHASE_WRITE_SIZES,
  PHASE_INCREMENTAL,
  PHASE_ARMED,      // waiting for a real FOCUS_LOST
  PHASE_COVERED,     // FOCUS_LOST seen, save done, waiting for a real FOCUS_GAINED
  PHASE_DONE,
} Phase;

typedef struct {
  PnxArena persistent, scene;
  PnxFont font;
  bool has_font;

  Phase phase;

  // Part 1
  uint8_t size_index, rep;
  uint32_t write_ms[SIZE_COUNT], write_calls[SIZE_COUNT], write_mismatches[SIZE_COUNT];
  char result[SIZE_COUNT][56];

  // Part 2
  uint32_t incr_worst_ms, incr_total_ms, incr_frames;

  // Part 3
  uint32_t t_lost, t_saved, t_gained;
  bool armed_save_ok;
  char armed_result[64];

  char status[64];
} App;

static void log_size_results(App *a) {
  for (int i = 0; i < SIZE_COUNT; i++) {
    const uint32_t us = a->write_calls[i] ? (a->write_ms[i] * 1000u) / a->write_calls[i] : 0;
    const uint8_t chunks = chunk_count_for(SIZES[i]);
    pnx_format(a->result[i], sizeof(a->result[i]), "%uB (%uc): %ums",
               (unsigned)SIZES[i], (unsigned)chunks, (unsigned)(us / 1000));
    pnx_log("savebench: %uB (%u chunks) -- %uus/write (%u reps, %u mismatched)",
            (unsigned)SIZES[i], (unsigned)chunks, (unsigned)us,
            (unsigned)a->write_calls[i], (unsigned)a->write_mismatches[i]);
  }
  // us/chunk from the two ends -- confirms "cost is per call/chunk, not per byte" the way
  // MEASUREMENTS.md's persist section already found for raw persist_write_data.
  const uint32_t us0 = a->write_calls[0] ? (a->write_ms[0] * 1000u) / a->write_calls[0] : 0;
  const uint32_t usN = a->write_calls[SIZE_COUNT - 1]
                      ? (a->write_ms[SIZE_COUNT - 1] * 1000u) / a->write_calls[SIZE_COUNT - 1]
                      : 0;
  const uint8_t c0 = chunk_count_for(SIZES[0]);
  const uint8_t cN = chunk_count_for(SIZES[SIZE_COUNT - 1]);
  const int32_t per_chunk = (cN > c0) ? ((int32_t)usN - (int32_t)us0) / (cN - c0) : 0;
  pnx_log("savebench: implied ~%dus/chunk (from %u-chunk and %u-chunk writes)",
          (int)per_chunk, (unsigned)c0, (unsigned)cN);
}

static void frame(void *ctx, uint32_t elapsed_ms, PnxTarget *target) {
  App *a = (App *)ctx;
  const uint32_t work_start = pnx_platform_now_ms();

  PnxEvent ev;
  while (pnx_platform_poll_event(&ev)) {
    if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_SELECT &&
        (a->phase == PHASE_IDLE || a->phase == PHASE_DONE)) {
      a->phase = PHASE_WRITE_SIZES;
      a->size_index = a->rep = 0;
      memset(a->write_ms, 0, sizeof(a->write_ms));
      memset(a->write_calls, 0, sizeof(a->write_calls));
      memset(a->write_mismatches, 0, sizeof(a->write_mismatches));
      for (int i = 0; i < SIZE_COUNT; i++) a->result[i][0] = '\0';
      a->armed_result[0] = '\0';
      pnx_diag_flush();
      pnx_log("savebench: run started -- %u sizes x %u reps, then incremental, then armed",
              (unsigned)SIZE_COUNT, (unsigned)REPS_PER_SIZE);
    }
    // FOCUS_LOST/GAINED are handled below regardless of button state -- they can arrive
    // any time PHASE_ARMED or PHASE_COVERED is active.
    //
    // t_lost is ev.time_ms -- stamped at delivery -- not `now`. While covered the app is
    // throttled and may not get a frame callback for a while, so timing against `now`
    // would measure from whenever this callback happened to run rather than from the
    // real FOCUS_LOST, silently shrinking the reported latency. Using the real timestamp
    // means "save done in Xms" is the true end-to-end risk window -- notification to
    // safely on flash -- which is the number the M5 design actually needs to hold under
    // the ~297ms will_focus warning, not just the write call's own isolated duration.
    if (ev.type == PNX_EVENT_FOCUS_LOST && a->phase == PHASE_ARMED) {
      a->t_lost = ev.time_ms;
      // The save-on-blur write itself: blocking, on purpose -- see the header comment
      // and pnx_save.h's own rationale for why save-on-blur does not spread.
      a->armed_save_ok = pnx_save_write(TEST_SLOT, s_payload, 24, SAVE_VERSION);
      a->t_saved = pnx_platform_now_ms();
      pnx_log("savebench: FOCUS_LOST -> save done in %ums (%s)",
              (unsigned)(a->t_saved - a->t_lost), a->armed_save_ok ? "ok" : "FAILED");
      a->phase = PHASE_COVERED;
    } else if (ev.type == PNX_EVENT_FOCUS_GAINED && a->phase == PHASE_COVERED) {
      a->t_gained = ev.time_ms;
      pnx_format(a->armed_result, sizeof(a->armed_result),
                 "lost->saved %ums, covered %ums",
                 (unsigned)(a->t_saved - a->t_lost), (unsigned)(a->t_gained - a->t_lost));
      pnx_log("savebench: FOCUS_GAINED -- covered %ums total, save %s",
              (unsigned)(a->t_gained - a->t_lost), a->armed_save_ok ? "ok" : "FAILED");
      a->phase = PHASE_DONE;
    }
  }

  pnx_gfx_clear(target, 0xC0);

  switch (a->phase) {
    case PHASE_WRITE_SIZES: {
      const size_t bytes = SIZES[a->size_index];
      for (size_t i = 0; i < bytes; i++) s_payload[i] = (uint8_t)(i ^ a->size_index);

      const uint32_t t0 = pnx_platform_now_ms();
      const bool ok = pnx_save_write(TEST_SLOT, s_payload, bytes, SAVE_VERSION);
      const uint32_t t1 = pnx_platform_now_ms();
      a->write_ms[a->size_index] += (t1 - t0);
      a->write_calls[a->size_index]++;

      // Verify, untimed -- this is a correctness check, not part of the write-cost
      // measurement above.
      size_t got = 0;
      memset(s_readback, 0, bytes);
      const bool loaded = ok && pnx_save_load(TEST_SLOT, s_readback, bytes, SAVE_VERSION, &got);
      if (!ok || !loaded || got != bytes || memcmp(s_payload, s_readback, bytes) != 0) {
        a->write_mismatches[a->size_index]++;
      }

      a->rep++;
      if (a->rep >= REPS_PER_SIZE) {
        a->rep = 0;
        pnx_log("savebench: %uB done -- %uus/write (%u reps)", (unsigned)bytes,
                (unsigned)((a->write_ms[a->size_index] * 1000u) / a->write_calls[a->size_index]),
                (unsigned)a->write_calls[a->size_index]);
        a->size_index++;
        if (a->size_index >= SIZE_COUNT) {
          log_size_results(a);
          a->phase = PHASE_INCREMENTAL;
          a->incr_worst_ms = 0;
          a->incr_total_ms = 0;
          a->incr_frames = 0;
          for (size_t i = 0; i < MAX_PAYLOAD; i++) s_payload[i] = (uint8_t)(i ^ 0x55);
          pnx_save_begin(TEST_SLOT, s_payload, MAX_PAYLOAD, SAVE_VERSION);
        }
      }
      pnx_format(a->status, sizeof(a->status), "size %u/%u rep %u/%u",
                 (unsigned)a->size_index + 1, SIZE_COUNT, (unsigned)a->rep + 1, REPS_PER_SIZE);
      break;
    }

    case PHASE_INCREMENTAL: {
      // Only reached while pnx_save_begin's payload still has chunks left -- see where
      // PHASE_INCREMENTAL is entered below -- so calling step() unconditionally here is
      // safe on every frame this case runs.
      const uint32_t t0 = pnx_platform_now_ms();
      pnx_save_step(TEST_SLOT);
      const uint32_t frame_ms = pnx_platform_now_ms() - t0;
      a->incr_total_ms += frame_ms;
      a->incr_frames++;
      if (frame_ms > a->incr_worst_ms) a->incr_worst_ms = frame_ms;

      pnx_format(a->status, sizeof(a->status), "incremental frame %u (worst %ums so far)",
                 (unsigned)a->incr_frames, (unsigned)a->incr_worst_ms);

      if (!pnx_save_pending(TEST_SLOT)) {
        pnx_log("savebench: incremental done -- %u frames, worst %ums, total %ums",
                (unsigned)a->incr_frames, (unsigned)a->incr_worst_ms,
                (unsigned)a->incr_total_ms);
        pnx_log("savebench: ARMED -- cover the watch with a notification now");
        a->phase = PHASE_ARMED;
      }
      break;
    }

    case PHASE_ARMED:
      pnx_format(a->status, sizeof(a->status), "ARMED -- cover the watch now");
      break;
    case PHASE_COVERED:
      pnx_format(a->status, sizeof(a->status), "covered -- waiting for it to clear");
      break;
    case PHASE_DONE:
      pnx_format(a->status, sizeof(a->status), "done -- SELECT to rerun");
      break;
    case PHASE_IDLE:
      pnx_format(a->status, sizeof(a->status), "SELECT to start (attach logs first)");
      break;
  }

  if (a->has_font) {
    pnx_text_draw(target, &a->font, "pnx savebench", 10, 20, 0xFF);
    pnx_text_draw(target, &a->font, a->status, 10, 40, 0xC7);

    if (a->phase == PHASE_INCREMENTAL || a->phase == PHASE_DONE) {
      int16_t y = 65;
      for (int i = 0; i < SIZE_COUNT && a->result[i][0]; i++) {
        pnx_text_draw(target, &a->font, a->result[i], 10, y, 0xFF);
        y = (int16_t)(y + 16);
      }
    }
    if (a->phase == PHASE_DONE && a->armed_result[0]) {
      pnx_text_draw(target, &a->font, a->armed_result, 10, 195, 0xF0);
    }
  }

  pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
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
  if (!a.has_font) pnx_log("savebench: font would not load -- nothing to draw");

  a.phase = PHASE_IDLE;

  pnx_platform_run(frame, &a);

  pnx_arena_destroy(&a.scene);
  pnx_arena_destroy(&a.persistent);
  return 0;
}
