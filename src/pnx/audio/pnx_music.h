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

#if PNX_USE_SYNTH
#include "pnx_synth.h"
#endif

#define PNX_MUSIC_CHANNELS 4
#define PNX_MUSIC_NO_NOTE  0
#define PNX_MUSIC_NOTE_OFF 1

// One packed synth instrument in a song blob. Fixed size, so the sequencer indexes
// instruments by number without a scan, and self-describing in width so a song carrying a
// wider record than this build understands is skipped rather than misread.
//
// The layout is mirrored in tools/pnx_assets.py pack_music_synth. Changing one without the
// other produces an instrument that loads and sounds wrong, which is the worst kind of
// mismatch -- so both sides name this constant.
#define PNX_SYNTH_RECORD_BYTES 48

typedef struct
{
	const uint8_t* rows;   // pattern data, 2 bytes per channel per row
	const uint8_t* order;  // pattern indices to play in sequence
	const PnxEnvelope* instruments;
	const uint8_t* waveforms;  // one PnxWaveform per instrument

	// Optional synth instrument table, appended after the patterns. NULL for a song built
	// before synth instruments existed, which still plays through the plain mixer -- the
	// format extension is additive on purpose, so no existing song had to be rebuilt.
	const uint8_t* synth;
	uint8_t synth_count;
	uint8_t synth_stride;
	uint8_t pattern_count;
	uint8_t order_length;
	uint8_t rows_per_pattern;
	uint8_t instrument_count;
	uint16_t tempo_bpm;
} PnxSong;

bool pnx_music_load(PnxSong* out, uint16_t asset_id);

#if PNX_USE_SYNTH
// Decode one packed synth instrument out of a loaded song. Exposed so a game can inspect
// or pre-load an instrument without waiting for the sequencer to reach a note that uses it.
void pnx_music_decode_instrument(const PnxSong* s, uint8_t index, PnxInstrument* out);
#endif

// Starts a song. `loop` restarts from the order list's beginning at the end.
void pnx_music_play(const PnxSong* song, bool loop);
void pnx_music_stop(void);
bool pnx_music_playing(void);

// Called once per frame, before pnx_audio_update. Advances rows against the wall clock
// rather than counting frames, because frames are not evenly spaced -- a covered app
// drops to ~0.4fps and a frame-counted sequencer would slow the music with it.
void pnx_music_update(uint32_t now_ms);

// 0..255, applied on top of each note's own volume. For ducking music under dialogue.
void pnx_music_set_volume(uint8_t volume);

// Where playback is, for diagnostics: a fault heard in one pattern and not the others
// names itself.
uint8_t pnx_music_pattern(void);
uint8_t pnx_music_row(void);

#endif	// PNX_USE_SEQUENCER
