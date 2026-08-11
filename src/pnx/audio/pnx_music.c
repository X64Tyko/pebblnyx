#include "pnx_music.h"

#if PNX_USE_SEQUENCER

#include "../core/pnx_diag.h"

#include <string.h>

// Music holds the low priorities so effects can always steal a channel from it. A melody
// interrupted for one note is far less noticeable than an effect that never plays.
#define MUSIC_PRIORITY 1

static const PnxSong *s_song;
static bool s_loop;
static bool s_playing;
static uint8_t s_volume = 200;

static uint8_t s_order_pos;
static uint8_t s_row;
static uint32_t s_next_row_ms;
static uint8_t s_channel_voice[PNX_MUSIC_CHANNELS];

bool pnx_music_load(PnxSong *out, uint16_t asset_id) {
  // Format after the header (patterns, order length, rows, instruments in bytes 3..6):
  //   u16 tempo, u8 channels, u8 pad
  //   instrument_count * (waveform, attack_lo, attack_hi, decay_lo, decay_hi,
  //                       sustain, release_lo, release_hi)   -- 8 bytes each
  //   order_length bytes
  //   pattern_count * rows_per_pattern * channels * 2 bytes
  (void)out; (void)asset_id;
  // Loading is wired in the same shape as the other assets; the pipeline emits this in
  // pnx_assets.py. Kept separate from the sequencer so a game can also build a song in
  // memory, which is what the tests do.
  return false;
}

void pnx_music_play(const PnxSong *song, bool loop) {
  if (!song || !song->rows || song->order_length == 0) return;
  s_song = song;
  s_loop = loop;
  s_playing = true;
  s_order_pos = 0;
  s_row = 0;
  s_next_row_ms = 0;
  memset(s_channel_voice, PNX_AUDIO_NO_VOICE, sizeof(s_channel_voice));
}

void pnx_music_stop(void) {
  if (!s_playing) return;
  for (int c = 0; c < PNX_MUSIC_CHANNELS; c++) {
    if (s_channel_voice[c] != PNX_AUDIO_NO_VOICE) {
      pnx_audio_release(s_channel_voice[c]);
      s_channel_voice[c] = PNX_AUDIO_NO_VOICE;
    }
  }
  s_playing = false;
  s_song = NULL;
}

bool pnx_music_playing(void) { return s_playing; }
void pnx_music_set_volume(uint8_t volume) { s_volume = volume; }

// A tracker row is a sixteenth note, so a row lasts 60000 / (bpm * 4) ms.
static uint32_t row_ms(const PnxSong *s) {
  const uint32_t bpm = s->tempo_bpm ? s->tempo_bpm : 120;
  const uint32_t ms = 60000u / (bpm * 4u);
  return ms ? ms : 1u;
}

static void play_row(const PnxSong *s, uint8_t pattern, uint8_t row) {
  const uint32_t stride = (uint32_t)s->rows_per_pattern * PNX_MUSIC_CHANNELS * 2u;
  const uint8_t *r = s->rows + (uint32_t)pattern * stride
                   + (uint32_t)row * PNX_MUSIC_CHANNELS * 2u;

  for (int c = 0; c < PNX_MUSIC_CHANNELS; c++) {
    const uint8_t note = r[c * 2];
    const uint8_t instrument = r[c * 2 + 1];

    if (note == PNX_MUSIC_NO_NOTE) continue;      // hold whatever is sounding

    // Release the channel's previous note before starting another, so the two do not
    // stack and double the channel's volume.
    if (s_channel_voice[c] != PNX_AUDIO_NO_VOICE) {
      pnx_audio_release(s_channel_voice[c]);
      s_channel_voice[c] = PNX_AUDIO_NO_VOICE;
    }
    if (note == PNX_MUSIC_NOTE_OFF) continue;

    if (instrument >= s->instrument_count) continue;
    const uint8_t vol = (uint8_t)((255u * s_volume) >> 8);
    s_channel_voice[c] = pnx_audio_note((PnxWaveform)s->waveforms[instrument], note,
                                        vol, &s->instruments[instrument],
                                        MUSIC_PRIORITY);
  }
}

void pnx_music_update(uint32_t now_ms) {
  if (!s_playing || !s_song) return;

  if (s_next_row_ms == 0) s_next_row_ms = now_ms;
  if (now_ms < s_next_row_ms) return;

  const PnxSong *s = s_song;
  const uint32_t per_row = row_ms(s);

  // Catch up at most a few rows. A covered app can return seconds late, and replaying
  // every missed row would fire a burst of notes at once -- the audio equivalent of the
  // sim fast-forwarding, which the frame loop clamps for the same reason.
  int budget = 4;
  while (now_ms >= s_next_row_ms && budget-- > 0) {
    play_row(s, s->order[s_order_pos], s_row);
    s_next_row_ms += per_row;

    if (++s_row >= s->rows_per_pattern) {
      s_row = 0;
      if (++s_order_pos >= s->order_length) {
        if (!s_loop) { pnx_music_stop(); return; }
        s_order_pos = 0;
      }
    }
  }
  // Whatever remains unplayed is discarded rather than queued: being late is better than
  // being late AND wrong.
  if (now_ms > s_next_row_ms) s_next_row_ms = now_ms;
}

#endif  // PNX_USE_SEQUENCER
