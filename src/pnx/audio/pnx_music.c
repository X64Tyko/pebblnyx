#include "pnx_music.h"

#if PNX_USE_SEQUENCER

#include "../core/pnx_diag.h"

#include <string.h>

// Music holds the low priorities so effects can always steal a channel from it. A melody
// interrupted for one note is far less noticeable than an effect that never plays.
#define MUSIC_PRIORITY 1

// Fade applied when a note is replaced. Short enough not to overlap the next note at any
// sane tempo, long enough that the waveform has no step in it.
#define PNX_MUSIC_CUT_MS 6

static const PnxSong *s_song;
static bool s_loop;
static bool s_playing;
// Full scale. The mixer's fixed headroom already reserves room for four channels plus an
// effect, so attenuating here as well would only make the music quiet.
static uint8_t s_volume = 255;

static uint8_t s_order_pos;
static uint8_t s_row;
static uint32_t s_next_row_ms;
static uint8_t s_channel_voice[PNX_MUSIC_CHANNELS];

// Instruments are decoded into a small fixed table rather than pointed at in the blob,
// because PnxEnvelope has padding a packed blob does not -- casting onto it would read
// whatever the compiler chose to leave between fields.
#define MAX_INSTRUMENTS 16

static PnxEnvelope s_env[MAX_INSTRUMENTS];
static uint8_t s_wave[MAX_INSTRUMENTS];

bool pnx_music_load(PnxSong *out, uint16_t asset_id) {
  uint8_t patterns = 0, order_len = 0, rows = 0, instruments = 0;
  size_t payload = 0;
  const uint8_t *data = pnx_blob_load(asset_id, "PN", &patterns, &order_len, &rows,
                                     &instruments, &payload);
  if (!data) return false;

  if (patterns == 0 || order_len == 0 || rows == 0 || instruments == 0) {
    pnx_log("music %u: empty song (%u patterns, %u order, %u rows, %u instruments)",
            asset_id, patterns, order_len, rows, instruments);
    return false;
  }
  if (instruments > MAX_INSTRUMENTS) {
    pnx_log("music %u: %u instruments, PNX_MUSIC max is %u",
            asset_id, instruments, MAX_INSTRUMENTS);
    return false;
  }

  // u16 tempo, u8 channels, u8 pad, then instruments, order, patterns.
  if (payload < 4) return false;
  const uint16_t tempo = (uint16_t)(data[0] | (data[1] << 8));
  const uint8_t channels = data[2];
  if (channels != PNX_MUSIC_CHANNELS) {
    pnx_log("music %u: %u channels, the sequencer has %u",
            asset_id, channels, PNX_MUSIC_CHANNELS);
    return false;
  }

  const size_t inst_bytes = (size_t)instruments * 8u;
  const size_t order_bytes = ((size_t)order_len + 3u) & ~(size_t)3u;
  const size_t row_bytes = (size_t)patterns * rows * channels * 2u;
  const size_t expected = 4 + inst_bytes + order_bytes + row_bytes;
  if (payload != expected) {
    pnx_log("music %u: needs %u bytes, blob has %u",
            asset_id, (unsigned)expected, (unsigned)payload);
    return false;
  }

  const uint8_t *ins = data + 4;
  for (uint8_t i = 0; i < instruments; i++) {
    const uint8_t *e = ins + (size_t)i * 8u;
    if (e[0] >= PNX_WAVE_COUNT) {
      pnx_log("music %u: instrument %u has waveform %u", asset_id, i, e[0]);
      return false;
    }
    s_wave[i] = e[0];
    s_env[i].attack_ms = (uint16_t)(e[1] | (e[2] << 8));
    s_env[i].decay_ms = (uint16_t)(e[3] | (e[4] << 8));
    s_env[i].sustain = e[5];
    s_env[i].release_ms = (uint16_t)(e[6] | (e[7] << 8));
  }

  out->order = ins + inst_bytes;
  out->rows = out->order + order_bytes;
  out->instruments = s_env;
  out->waveforms = s_wave;
  out->pattern_count = patterns;
  out->order_length = order_len;
  out->rows_per_pattern = rows;
  out->instrument_count = instruments;
  out->tempo_bpm = tempo;

  for (uint8_t i = 0; i < order_len; i++) {
    if (out->order[i] >= patterns) {
      pnx_log("music %u: order[%u] is pattern %u of %u",
              asset_id, i, out->order[i], patterns);
      return false;
    }
  }
  return true;
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

uint8_t pnx_music_pattern(void) {
  return (s_song && s_order_pos < s_song->order_length)
         ? s_song->order[s_order_pos] : 0;
}
uint8_t pnx_music_row(void) { return s_row; }
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

    // Fade the previous note out fast rather than cutting it. Cutting was a step
    // discontinuity in the waveform -- a click on every note change. A release long
    // enough to overlap the next note was the opposite problem, muddiness. A few
    // milliseconds is neither: too short to hear as a tail, long enough to have no edge.
    if (s_channel_voice[c] != PNX_AUDIO_NO_VOICE) {
      pnx_audio_release_in(s_channel_voice[c], PNX_MUSIC_CUT_MS);
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
