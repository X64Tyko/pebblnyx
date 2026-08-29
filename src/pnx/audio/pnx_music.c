#include "pnx_music.h"

#if PNX_USE_SEQUENCER

#include "../core/pnx_diag.h"

#include <stdlib.h>
#include <string.h>

// Music holds the low priorities so effects can always steal a channel from it. A melody
// interrupted for one note is far less noticeable than an effect that never plays.
#define MUSIC_PRIORITY 1

// Fade applied when a note is replaced. Short enough not to overlap the next note at any
// sane tempo, long enough that the waveform has no step in it.
#define PNX_MUSIC_CUT_MS 6

static const PnxSong* s_song;
static bool s_loop;
static bool s_playing;
// Full scale. The mixer's fixed headroom already reserves room for four channels plus an
// effect, so attenuating here as well would only make the music quiet.
static uint8_t s_volume = 255;

static uint8_t s_order_pos;
static uint8_t s_row;
static uint32_t s_next_row_ms;
static uint8_t s_channel_voice[PNX_MUSIC_CHANNELS];

// A queued cross-song transition, applied by pnx_music_update once it reaches `s_pending_point`
// in whatever song is CURRENTLY playing. See pnx_music_queue_transition.
static const PnxSong* s_pending_song;
static bool s_pending_loop;
static PnxTransitionPoint s_pending_point;
static bool s_pending_active;

// Instruments are decoded into a small table rather than pointed at in the blob, because
// PnxEnvelope has padding a packed blob does not -- casting onto it would read whatever
// the compiler chose to leave between fields. MAX_INSTRUMENTS is now only a sanity cap on
// one song's own declared count (checked in pnx_music_load below), not a static array
// bound: s_env/s_wave are malloc'd sized to that song's REAL instrument count, freed and
// reallocated each load rather than reserving the worst case for every project that
// turns the sequencer on at all, whether or not any song it ships ever needs 16.
#define MAX_INSTRUMENTS 16

static PnxEnvelope* s_env;
static uint8_t* s_wave;

bool pnx_music_load(PnxSong* out, uint16_t asset_id)
{
	uint8_t patterns = 0, order_len = 0, rows = 0, instruments = 0;
	size_t payload = 0;
	const uint8_t* data =
		pnx_blob_load(asset_id, "PN", &patterns, &order_len, &rows, &instruments, &payload);
	if (!data)
		return false;

	if (patterns == 0 || order_len == 0 || rows == 0 || instruments == 0)
	{
		pnx_log("music %u: empty song (%u patterns, %u order, %u rows, %u instruments)",
				asset_id, patterns, order_len, rows, instruments);
		return false;
	}
	if (instruments > MAX_INSTRUMENTS)
	{
		pnx_log("music %u: %u instruments, PNX_MUSIC max is %u", asset_id, instruments,
				MAX_INSTRUMENTS);
		return false;
	}

	// u16 tempo, u8 channels, u8 pad, then instruments, order, patterns.
	if (payload < 4)
		return false;
	const uint16_t tempo   = (uint16_t)(data[0] | (data[1] << 8));
	const uint8_t channels = data[2];
	if (channels != PNX_MUSIC_CHANNELS)
	{
		pnx_log("music %u: %u channels, the sequencer has %u", asset_id, channels,
				PNX_MUSIC_CHANNELS);
		return false;
	}

	const size_t inst_bytes	 = (size_t)instruments * 8u;
	const size_t order_bytes = ((size_t)order_len + 3u) & ~(size_t)3u;
	const size_t row_bytes	 = (size_t)patterns * rows * channels * 2u;
	const size_t expected	 = 4 + inst_bytes + order_bytes + row_bytes;
	if (payload < expected)
	{
		pnx_log("music %u: needs %u bytes, blob has %u", asset_id, (unsigned)expected,
				(unsigned)payload);
		return false;
	}

	// Anything beyond the patterns is the optional synth instrument table: a count, the
	// record width, then the records. Detected by trailing payload rather than by a header
	// flag, because the music header's four bytes are all spoken for -- and additive, so
	// every song built before synth instruments existed still loads and still plays.
	//
	// The width is carried so a song written by a NEWER pipeline, with a wider record than
	// this build understands, is refused rather than misread as garbage instruments.
	out->synth			  = NULL;
	out->synth_count	  = 0;
	out->synth_stride	  = 0;
	size_t synth_size	  = 0;
	const size_t trailing = payload - expected;
	if (trailing >= 2)
	{
		const uint8_t* tail	 = data + expected;
		const uint8_t count	 = tail[0];
		const uint8_t stride = tail[1];
		if (count && stride)
		{
			if (stride != PNX_SYNTH_RECORD_BYTES)
			{
				pnx_log("music %u: synth record is %u bytes, this build reads %u -- skipping",
						asset_id, stride, (unsigned)PNX_SYNTH_RECORD_BYTES);
			}
			else if (trailing < 2u + (size_t)count * stride)
			{
				pnx_log("music %u: synth table wants %u bytes, %u remain", asset_id,
						(unsigned)(2u + (size_t)count * stride), (unsigned)trailing);
			}
			else
			{
				out->synth		  = tail + 2;
				out->synth_count  = count;
				out->synth_stride = stride;
				synth_size		  = 2u + (size_t)count * stride;
			}
		}
	}

	// Optional marker table, appended after whatever synth bytes were actually consumed above
	// (zero, if this song carries no synth table). Same trailing-payload detection as synth,
	// one level deeper: additive, so a song built before markers existed -- with or without a
	// synth table -- loads exactly as it did before this existed.
	out->marker_rows		= NULL;
	out->marker_count		= 0;
	const size_t marker_off = expected + synth_size;
	if (payload >= marker_off)
	{
		const size_t marker_trailing = payload - marker_off;
		if (marker_trailing >= 2)
		{
			const uint8_t* tail = data + marker_off;
			const uint8_t count = tail[0];
			if (count && marker_trailing >= 2u + (size_t)count * 2u)
			{
				out->marker_rows  = tail + 2;
				out->marker_count = count;
			}
		}
	}

	// Freed before realloc, not leaked: a project can load a different song later, and
	// each load fully replaces the previous one's decoded instrument table.
	free(s_env);
	free(s_wave);
	s_env  = (PnxEnvelope*)malloc(sizeof(PnxEnvelope) * instruments);
	s_wave = (uint8_t*)malloc(instruments);
	if (!s_env || !s_wave)
	{
		pnx_log("music %u: %u bytes refused", asset_id,
				(unsigned)(sizeof(PnxEnvelope) * instruments + instruments));
		free(s_env);
		free(s_wave);
		s_env  = NULL;
		s_wave = NULL;
		return false;
	}

	const uint8_t* ins = data + 4;
	for (uint8_t i = 0; i < instruments; i++)
	{
		const uint8_t* e = ins + (size_t)i * 8u;
		if (e[0] >= PNX_WAVE_COUNT)
		{
			pnx_log("music %u: instrument %u has waveform %u", asset_id, i, e[0]);
			return false;
		}
		s_wave[i]			= e[0];
		s_env[i].attack_ms	= (uint16_t)(e[1] | (e[2] << 8));
		s_env[i].decay_ms	= (uint16_t)(e[3] | (e[4] << 8));
		s_env[i].sustain	= e[5];
		s_env[i].release_ms = (uint16_t)(e[6] | (e[7] << 8));
	}

	out->order			  = ins + inst_bytes;
	out->rows			  = out->order + order_bytes;
	out->instruments	  = s_env;
	out->waveforms		  = s_wave;
	out->pattern_count	  = patterns;
	out->order_length	  = order_len;
	out->rows_per_pattern = rows;
	out->instrument_count = instruments;
	out->tempo_bpm		  = tempo;

	for (uint8_t i = 0; i < order_len; i++)
	{
		if (out->order[i] >= patterns)
		{
			pnx_log("music %u: order[%u] is pattern %u of %u", asset_id, i, out->order[i],
					patterns);
			return false;
		}
	}
	return true;
}

void pnx_music_play(const PnxSong* song, bool loop)
{
	if (!song || !song->rows || song->order_length == 0)
		return;
	s_song			 = song;
	s_loop			 = loop;
	s_playing		 = true;
	s_order_pos		 = 0;
	s_row			 = 0;
	s_next_row_ms	 = 0;
	s_pending_active = false; // an explicit new play() replaces whatever was queued, if anything
	memset(s_channel_voice, PNX_AUDIO_NO_VOICE, sizeof(s_channel_voice));
}

void pnx_music_queue_transition(const PnxSong* next, bool loop, PnxTransitionPoint at)
{
	if (!next || !next->rows || next->order_length == 0)
		return;
	s_pending_song	 = next;
	s_pending_loop	 = loop;
	s_pending_point	 = at;
	s_pending_active = true;
}

void pnx_music_stop(void)
{
	if (!s_playing)
		return;
	for (int c = 0; c < PNX_MUSIC_CHANNELS; c++)
	{
		if (s_channel_voice[c] != PNX_AUDIO_NO_VOICE)
		{
			pnx_audio_release(s_channel_voice[c]);
			s_channel_voice[c] = PNX_AUDIO_NO_VOICE;
		}
	}
	s_playing		 = false;
	s_song			 = NULL;
	s_pending_active = false;
}

bool pnx_music_playing(void)
{
	return s_playing;
}

uint8_t pnx_music_pattern(void)
{
	return (s_song && s_order_pos < s_song->order_length) ? s_song->order[s_order_pos] : 0;
}
uint8_t pnx_music_row(void)
{
	return s_row;
}
void pnx_music_set_volume(uint8_t volume)
{
	s_volume = volume;
}

// A tracker row is a sixteenth note, so a row lasts 60000 / (bpm * 4) ms.
static uint32_t row_ms(const PnxSong* s)
{
	const uint32_t bpm = s->tempo_bpm ? s->tempo_bpm : 120;
	const uint32_t ms  = 60000u / (bpm * 4u);
	return ms ? ms : 1u;
}

static void play_row(const PnxSong* s, uint8_t pattern, uint8_t row)
{
	const uint32_t stride = (uint32_t)s->rows_per_pattern * PNX_MUSIC_CHANNELS * 2u;
	const uint8_t* r =
		s->rows + (uint32_t)pattern * stride + (uint32_t)row * PNX_MUSIC_CHANNELS * 2u;

	for (int c = 0; c < PNX_MUSIC_CHANNELS; c++)
	{
		const uint8_t note		 = r[c * 2];
		const uint8_t instrument = r[c * 2 + 1];

		if (note == PNX_MUSIC_NO_NOTE)
			continue; // hold whatever is sounding

		// Fade the previous note out fast rather than cutting it. Cutting was a step
		// discontinuity in the waveform -- a click on every note change. A release long
		// enough to overlap the next note was the opposite problem, muddiness. A few
		// milliseconds is neither: too short to hear as a tail, long enough to have no edge.
		if (s_channel_voice[c] != PNX_AUDIO_NO_VOICE)
		{
			pnx_audio_release_in(s_channel_voice[c], PNX_MUSIC_CUT_MS);
			s_channel_voice[c] = PNX_AUDIO_NO_VOICE;
		}
#if PNX_USE_SYNTH
		// The synth voice releases rather than being cut, for the same reason: a step to
		// silence mid-waveform is a click, and the envelope's own release is what a note
		// ending is supposed to sound like.
		if (s->synth)
			pnx_synth_note_off(c);
#endif
		if (note == PNX_MUSIC_NOTE_OFF)
			continue;

		if (instrument >= s->instrument_count)
			continue;
		const uint8_t vol = (uint8_t)((255u * s_volume) >> 8);

#if PNX_USE_SYNTH
		// A song carrying synth instruments plays through them, and a channel maps 1:1 onto a
		// synth slot -- both are four, and both are the same musical idea.
		//
		// The record is decoded at NOTE-ON rather than at load. That costs 48 bytes of work a
		// few times a second instead of holding a decoded table for the whole song, and it is
		// what makes "push an instrument into a slot mid-song" fall out for free: the slot is
		// written just before the note starts, and pnx_synth_note_on copies it, so a note
		// already sounding keeps the instrument it began with.
		if (s->synth && instrument < s->synth_count)
		{
			PnxInstrument in;
			pnx_music_decode_instrument(s, instrument, &in);
			pnx_synth_set_instrument(c, &in);
			pnx_synth_note_on(c, note, vol);
			s_channel_voice[c] = PNX_AUDIO_NO_VOICE; // the synth owns this channel
			continue;
		}
#endif

		s_channel_voice[c] = pnx_audio_note((PnxWaveform)s->waveforms[instrument], note, vol,
											&s->instruments[instrument], MUSIC_PRIORITY);
	}
}

#if PNX_USE_SYNTH
// Decode one packed synth instrument.
//
// Byte offsets, mirrored in tools/pnx_assets.py pack_music_synth. Written out longhand
// rather than memcpy'd over the struct: the packed form has no padding and a fixed
// endianness, and the C struct has whatever the compiler chose -- copying one onto the
// other is the classic way to get a format that works on the machine that wrote it.
void pnx_music_decode_instrument(const PnxSong* s, uint8_t index, PnxInstrument* out)
{
	const uint8_t* r = s->synth + (size_t)index * s->synth_stride;
	memset(out, 0, sizeof(*out));

	out->osc_count = r[0] ? r[0] : 1;
	if (out->osc_count > PNX_SYNTH_OSCILLATORS)
		out->osc_count = PNX_SYNTH_OSCILLATORS;
	out->filter_mode	   = r[1];
	out->cutoff_base	   = r[2];
	out->resonance		   = r[3];
	out->cutoff_env_amount = r[4];
	out->lfo_target		   = r[5];
	out->lfo_rate		   = r[6];
	out->lfo_depth		   = r[7];
	out->pitch_env_amount  = (int16_t)(r[8] | (r[9] << 8));
	out->pitch_env_decay   = r[10];
	out->reverb_send	   = r[11];
	out->chorus_send	   = r[12];

	out->amp.attack_ms	= (uint16_t)(r[14] | (r[15] << 8));
	out->amp.decay_ms	= (uint16_t)(r[16] | (r[17] << 8));
	out->amp.sustain	= r[18];
	out->amp.release_ms = (uint16_t)(r[20] | (r[21] << 8));

	out->cutoff.attack_ms  = (uint16_t)(r[22] | (r[23] << 8));
	out->cutoff.decay_ms   = (uint16_t)(r[24] | (r[25] << 8));
	out->cutoff.sustain	   = r[26];
	out->cutoff.release_ms = (uint16_t)(r[28] | (r[29] << 8));

	for (int i = 0; i < PNX_SYNTH_OSCILLATORS; i++)
	{
		const uint8_t* o   = r + 30 + i * 6;
		out->osc[i].wave   = o[0];
		out->osc[i].volume = o[1];
		out->osc[i].detune = (int16_t)(o[2] | (o[3] << 8));
		out->osc[i].octave = (int8_t)o[4];
		out->osc[i].duty   = o[5];
	}
}
#endif

// True if `abs_row` (order_pos * rows_per_pattern + row, i.e. a position in the CURRENT
// playthrough's timeline, stable across pattern reuse from dedup) is one of `s`'s markers.
static bool at_marker_row(const PnxSong* s, uint16_t abs_row)
{
	for (uint8_t i = 0; i < s->marker_count; i++)
	{
		const uint8_t* m = s->marker_rows + (size_t)i * 2u;
		if ((uint16_t)(m[0] | (m[1] << 8)) == abs_row)
			return true;
	}
	return false;
}

// Deliberately does NOT touch s_next_row_ms, unlike pnx_music_play's init -- this runs
// mid-loop, inside pnx_music_update, where it already holds a correctly-scheduled "when's the
// next row" value the enclosing loop just advanced by the OLD song's per_row. Resetting it to
// 0 here would re-arm the "just started, catch up immediately" path on the very next while
// iteration and burst several of the new song's rows out in this one update() call instead of
// pacing them normally from here on.
static void apply_pending_transition(void)
{
	s_song		= s_pending_song;
	s_loop		= s_pending_loop;
	s_order_pos = 0;
	s_row		= 0;
	memset(s_channel_voice, PNX_AUDIO_NO_VOICE, sizeof(s_channel_voice));
	s_pending_active = false;
}

void pnx_music_update(uint32_t now_ms)
{
	if (!s_playing || !s_song)
		return;

	if (s_next_row_ms == 0)
		s_next_row_ms = now_ms;
	if (now_ms < s_next_row_ms)
		return;

	// Catch up at most a few rows. A covered app can return seconds late, and replaying
	// every missed row would fire a burst of notes at once -- the audio equivalent of the
	// sim fast-forwarding, which the frame loop clamps for the same reason.
	int budget = 4;
	while (now_ms >= s_next_row_ms && budget-- > 0)
	{
		const PnxSong* s	   = s_song;
		const uint32_t per_row = row_ms(s);

		play_row(s, s->order[s_order_pos], s_row);
		s_next_row_ms += per_row;

		if (++s_row >= s->rows_per_pattern)
		{
			s_row = 0;
			if (++s_order_pos >= s->order_length)
			{
				// A queued "pattern end" transition takes priority over stopping a
				// non-looping song that just reached its own end -- the last pattern's end
				// is a pattern boundary too, and going silent when a swap was already
				// requested is never the right call.
				if (s_pending_active && s_pending_point == PNX_TRANSITION_PATTERN_END)
				{
					apply_pending_transition();
					continue;
				}
				if (!s_loop)
				{
					pnx_music_stop();
					return;
				}
				s_order_pos = 0;
			}
		}

		// Checked AFTER advancing, against whatever the row/pattern cursor became this tick --
		// a transition queued to land "at pattern end" means the boundary just crossed, and one
		// queued for a marker means the row just reached is the marked one. `s_song` is
		// re-read at the top of the next iteration, so a swap here takes effect on the very
		// next row played, same tick, no gap.
		if (s_pending_active)
		{
			const bool at_pattern_end = (s_row == 0);
			const uint16_t abs_row	  = (uint16_t)s_order_pos * s->rows_per_pattern + s_row;
			const bool triggered =
				(s_pending_point == PNX_TRANSITION_PATTERN_END && at_pattern_end) || (s_pending_point == PNX_TRANSITION_NEXT_MARKER && at_marker_row(s, abs_row));
			if (triggered)
				apply_pending_transition();
		}
	}
	// Whatever remains unplayed is discarded rather than queued: being late is better than
	// being late AND wrong.
	if (now_ms > s_next_row_ms)
		s_next_row_ms = now_ms;
}

#endif // PNX_USE_SEQUENCER
