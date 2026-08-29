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

// Declared unconditionally, PNX_USE_SEQUENCER or not: a build with the sequencer disabled
// (no speaker -- see PNX_USE_AUDIO's default in pnx_config.h) still has to compile game
// code that holds a PnxSong. See "opt-outs must stub, not delete" in docs/PORTING.md.
typedef struct
{
	const uint8_t* rows;  // pattern data, 2 bytes per channel per row
	const uint8_t* order; // pattern indices to play in sequence
	const PnxEnvelope* instruments;
	const uint8_t* waveforms; // one PnxWaveform per instrument

	// Optional synth instrument table, appended after the patterns. NULL for a song built
	// before synth instruments existed, which still plays through the plain mixer -- the
	// format extension is additive on purpose, so no existing song had to be rebuilt.
	const uint8_t* synth;
	uint8_t synth_count;
	uint8_t synth_stride;

	// Optional marker table, appended after the synth table (or straight after the patterns
	// when there is no synth table). Absolute row positions in this song's order-sequence
	// timeline -- i.e. order_pos * rows_per_pattern + row -- that a queued transition may land
	// on. Two raw bytes per entry, little-endian, NOT a uint16_t* -- the blob gives no alignment
	// guarantee, so every multi-byte field in this format is read out by hand rather than cast.
	// NULL/0 for a song built before markers existed, additive for the same reason synth is.
	const uint8_t* marker_rows;
	uint8_t marker_count;

	uint8_t pattern_count;
	uint8_t order_length;
	uint8_t rows_per_pattern;
	uint8_t instrument_count;
	uint16_t tempo_bpm;
} PnxSong;

// Where a queued song swap is allowed to land -- see pnx_music_queue_transition. Both are
// checked against the song CURRENTLY playing (the one being transitioned away from), not the
// one queued to play next.
typedef enum
{
	PNX_TRANSITION_PATTERN_END, // the next time the current pattern finishes
	PNX_TRANSITION_NEXT_MARKER, // the next row in the current song's own marker table
} PnxTransitionPoint;

#if PNX_USE_SEQUENCER

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

// Queues a swap to `next`, applied by pnx_music_update the next time it reaches `at` in the
// CURRENTLY PLAYING song -- not next update() call, whichever tick actually crosses that point,
// so the swap lands exactly on the boundary/marker regardless of when this was called. Silently
// ignored if `next` doesn't look like a loaded song. Replaces any transition already queued.
void pnx_music_queue_transition(const PnxSong* next, bool loop, PnxTransitionPoint at);

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

#else // !PNX_USE_SEQUENCER
//
// Inline no-ops, matching pnx_audio.h's rule: a game that calls pnx_music_play still
// compiles and links on a platform where this defaulted off, it simply plays nothing.

static inline bool pnx_music_load(PnxSong* out, uint16_t asset_id)
{
	(void)out;
	(void)asset_id;
	return false;
}

#if PNX_USE_SYNTH
static inline void pnx_music_decode_instrument(const PnxSong* s, uint8_t index,
											   PnxInstrument* out)
{
	(void)s;
	(void)index;
	if (out)
		*out = (PnxInstrument){ 0 };
}
#endif

static inline void pnx_music_play(const PnxSong* song, bool loop)
{
	(void)song;
	(void)loop;
}
static inline void pnx_music_stop(void)
{
}
static inline void pnx_music_queue_transition(const PnxSong* next, bool loop,
											  PnxTransitionPoint at)
{
	(void)next;
	(void)loop;
	(void)at;
}
static inline bool pnx_music_playing(void)
{
	return false;
}
static inline void pnx_music_update(uint32_t now_ms)
{
	(void)now_ms;
}
static inline void pnx_music_set_volume(uint8_t volume)
{
	(void)volume;
}
static inline uint8_t pnx_music_pattern(void)
{
	return 0;
}
static inline uint8_t pnx_music_row(void)
{
	return 0;
}

#endif // PNX_USE_SEQUENCER
