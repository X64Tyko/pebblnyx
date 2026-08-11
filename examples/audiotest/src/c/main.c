// M4 device test: sequenced music with effects firing over it.
//
// Host tests settle the lead arithmetic and the envelope stages. What they cannot settle
// is whether four sequenced channels plus effects actually survive a real frame cadence
// without the stream running dry -- so this reports underrun on screen and in the log,
// and fires effects hard enough to force voice stealing.
//
// SELECT fires a laser. DOWN fires an explosion. UP toggles the music.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 1024
#define SCENE_BYTES   (16 * 1024)

typedef struct {
  PnxArena persistent, scene;
  PnxSong song;
  const int8_t *laser;
  uint32_t laser_len, laser_hz;
  const int8_t *boom;
  uint32_t boom_len, boom_hz;

  bool ready;
  bool music_on;
  uint32_t ticks, accumulator_ms;
  uint32_t next_auto_ms;
  char hud[48];
  char hud2[48];
} App;

static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;

// A sample blob is rate, loop point, then PCM -- read directly rather than through a
// helper, because this is the only place that needs it and the shape is three fields.
static const int8_t *load_sample(uint16_t asset, uint32_t *len, uint32_t *hz) {
  size_t payload = 0;
  const uint8_t *d = pnx_blob_load(asset, "PW", NULL, NULL, NULL, NULL, &payload);
  if (!d || payload < 8) return NULL;
  *hz = (uint32_t)(d[0] | (d[1] << 8) | (d[2] << 16) | ((uint32_t)d[3] << 24));
  *len = (uint32_t)(payload - 8);
  return (const int8_t *)(d + 8);
}

static void frame(void *ctx, uint32_t elapsed_ms, PnxTarget *target) {
  App *a = (App *)ctx;
  const uint32_t now = pnx_platform_now_ms();
  const uint32_t work_start = now;

  PnxEvent ev;
  while (pnx_platform_poll_event(&ev)) {
    if (ev.type != PNX_EVENT_BUTTON_DOWN) continue;
    if (ev.button == PNX_BUTTON_SELECT && a->laser) {
      // Priority 4: an effect must be able to steal a channel from the music, which
      // holds priority 1. If it cannot, the melody silences the gameplay.
      pnx_audio_play_pri(a->laser, a->laser_len, PNX_AUDIO_NO_LOOP, a->laser_hz,
                         255, 4, NULL);
    } else if (ev.button == PNX_BUTTON_DOWN && a->boom) {
      pnx_audio_play_pri(a->boom, a->boom_len, PNX_AUDIO_NO_LOOP, a->boom_hz,
                         255, 5, NULL);
    } else if (ev.button == PNX_BUTTON_UP) {
      a->music_on = !a->music_on;
      if (a->music_on) pnx_music_play(&a->song, true);
      else pnx_music_stop();
    }
  }

  a->accumulator_ms += elapsed_ms;
  const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
  if (a->accumulator_ms > max_ms) a->accumulator_ms = max_ms;
  while (a->accumulator_ms >= PNX_TICK_MS) {
    a->accumulator_ms -= PNX_TICK_MS;
    a->ticks++;
  }

  // Fire effects unattended every ~1.4s, alternating, so the test stresses the mixer
  // without anyone holding a button.
  if (a->ready && now >= a->next_auto_ms) {
    static bool alternate;
    alternate = !alternate;
    if (alternate && a->laser) {
      pnx_audio_play_pri(a->laser, a->laser_len, PNX_AUDIO_NO_LOOP, a->laser_hz,
                         220, 4, NULL);
    } else if (a->boom) {
      pnx_audio_play_pri(a->boom, a->boom_len, PNX_AUDIO_NO_LOOP, a->boom_hz,
                         200, 5, NULL);
    }
    a->next_auto_ms = now + 1400;
  }

  // Music before the mixer: the sequencer starts notes, then the mixer renders them.
  pnx_music_update(now);
  pnx_audio_update(now);

  pnx_gfx_clear(target, 0xC0);

  const PnxAudioStats *au = pnx_audio_stats();
  const PnxFrameStats *fs = pnx_diag_stats();
  pnx_format(a->hud, sizeof(a->hud), "voices %u def %u cap %u",
             au->active_voices, (unsigned)au->worst_deficit,
             (unsigned)au->capacity);
  pnx_format(a->hud2, sizeof(a->hud2), "%u.%ufps  work %uus  %s",
             fs ? (unsigned)(fs->fps_x10 / 10) : 0,
             fs ? (unsigned)(fs->fps_x10 % 10) : 0,
             fs ? (unsigned)fs->work_us : 0,
             a->music_on ? "music" : "silent");

  if (a->ticks % 50 == 0) {
    if (a->ticks == 50) pnx_diag_flush();
    pnx_log("audio: %s | %s | short %u/%u carry %u", a->hud, a->hud2,
            (unsigned)au->short_writes, (unsigned)au->feeds,
            (unsigned)au->carried);
  }

  pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
}

static void draw_text(void *ctx) {
  App *a = (App *)ctx;
  pnx_platform_text_draw("pnx audio test", PNX_TEXT_MEDIUM, 0xFF, 6, 20, 190, 26);
  pnx_platform_text_draw(a->hud, PNX_TEXT_SMALL, 0xFF, 6, 56, 190, 20);
  pnx_platform_text_draw(a->hud2, PNX_TEXT_SMALL, 0xFF, 6, 76, 190, 20);
  pnx_platform_text_draw("SELECT laser\nDOWN explosion\nUP music on/off",
                        PNX_TEXT_SMALL, 0xFF, 6, 120, 190, 80);
}

int main(void) {
  static App a;
  memset(&a, 0, sizeof(a));

  if (!pnx_arena_init(&a.persistent, "persistent", PERSIST_BYTES, 4) ||
      !pnx_arena_init(&a.scene, "scene", SCENE_BYTES, 4)) {
    pnx_platform_log("arena init failed");
    return 1;
  }
  pnx_assets_init(&a.persistent, &a.scene, RESOURCES, PNX_ASSET_COUNT);

  if (!pnx_audio_init(PNX_AUDIO_16KHZ_8BIT, 85)) {
    pnx_log("audio would not open");
  }

  a.laser = load_sample(PNX_ASSET_SAMPLE_LASER, &a.laser_len, &a.laser_hz);
  a.boom = load_sample(PNX_ASSET_SAMPLE_EXPLOSION, &a.boom_len, &a.boom_hz);
  a.ready = pnx_music_load(&a.song, PNX_ASSET_MUSIC_THEME);

  if (a.ready) {
    pnx_music_play(&a.song, true);
    a.music_on = true;
  }
  pnx_log("start: song=%d (%u patterns, %ubpm) laser=%u boom=%u arena %u/%u",
          (int)a.ready, a.song.pattern_count, a.song.tempo_bpm,
          (unsigned)a.laser_len, (unsigned)a.boom_len,
          (unsigned)a.scene.used, (unsigned)a.scene.capacity);

  pnx_platform_set_text_fn(draw_text);
  pnx_platform_run(frame, &a);

  pnx_audio_shutdown();
  pnx_arena_destroy(&a.scene);
  pnx_arena_destroy(&a.persistent);
  return 0;
}
