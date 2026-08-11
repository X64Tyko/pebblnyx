// Pattern sequencer.
//
// Tracker-shaped, because that is the form that fits: patterns of rows, an order list
// that plays them, and a few channels. It is compact (a row is 2 bytes per channel), it
// loops without special cases, and it is what the music this framework is for was
// actually written in.
//
// Music is authored in the manifest and compiled to a resource, so no note numbers
// appear in C. Instruments are generated waveforms, so a song costs only its notes --
// a few hundred bytes rather than the tens of kilobytes a recorded loop would.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_SEQUENCER

#include "pnx_audio.h"
#include "../assets/pnx_assets.h"

#define PNX_MUSIC_CHANNELS 4
#define PNX_MUSIC_NO_NOTE 0
#define PNX_MUSIC_NOTE_OFF 1

typedef struct {
  const uint8_t *rows;        // pattern data, 2 bytes per channel per row
  const uint8_t *order;       // pattern indices to play in sequence
  const PnxEnvelope *instruments;
  const uint8_t *waveforms;   // one PnxWaveform per instrument
  uint8_t pattern_count;
  uint8_t order_length;
  uint8_t rows_per_pattern;
  uint8_t instrument_count;
  uint16_t tempo_bpm;
} PnxSong;

bool pnx_music_load(PnxSong *out, uint16_t asset_id);

// Starts a song. `loop` restarts from the order list's beginning at the end.
void pnx_music_play(const PnxSong *song, bool loop);
void pnx_music_stop(void);
bool pnx_music_playing(void);

// Called once per frame, before pnx_audio_update. Advances rows against the wall clock
// rather than counting frames, because frames are not evenly spaced -- a covered app
// drops to ~0.4fps and a frame-counted sequencer would slow the music with it.
void pnx_music_update(uint32_t now_ms);

// 0..255, applied on top of each note's own volume. For ducking music under dialogue.
void pnx_music_set_volume(uint8_t volume);

#endif  // PNX_USE_SEQUENCER
