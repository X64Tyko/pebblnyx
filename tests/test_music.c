// Host tests for the sequencer's cross-song transition bookkeeping (pnx_music_queue_transition).
//
// Two tiny hand-built songs (bypassing pnx_music_load/blob parsing entirely -- this is about
// pnx_music_update's cursor/swap logic, not the loader) with distinct rows_per_pattern, so the
// row-wrap PERIOD after a swap proves which song is actually driving playback: song A wraps
// every 2 rows, song B every 3. Every row is silent (note byte 0 = PNX_MUSIC_NO_NOTE), so
// play_row never touches an instrument table -- no mixer/synth setup needed on the host.

#include "../src/pnx/audio/pnx_music.h"

#include <stdio.h>

extern int s_failures;
extern int s_checks;

#define MU_CHECK_EQ(a, b)                                                                    \
	do                                                                                       \
	{                                                                                        \
		s_checks++;                                                                          \
		const long _a = (long)(a), _b = (long)(b);                                           \
		if (_a != _b)                                                                        \
		{                                                                                    \
			printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n", __FILE__, __LINE__, #a, #b, _a, \
				   _b);                                                                      \
			s_failures++;                                                                    \
		}                                                                                    \
	} while (0)

void test_music(void);

// Silent rows: PNX_MUSIC_CHANNELS(4) * 2 bytes/cell, every byte 0.
static const uint8_t s_songA_rows[2 /*patterns*/ * 2 /*rows*/ * PNX_MUSIC_CHANNELS * 2] = { 0 };
static const uint8_t s_songA_order[2]													= { 0, 1 };
static const uint8_t s_songA_marker_raw[2]												= { 1, 0 }; // one marker, absolute row 1, LE

static const uint8_t s_songB_rows[1 /*pattern*/ * 3 /*rows*/ * PNX_MUSIC_CHANNELS * 2] = { 0 };
static const uint8_t s_songB_order[1]												   = { 0 };

static PnxSong make_song(const uint8_t* rows, const uint8_t* order, uint8_t order_length,
						 uint8_t rows_per_pattern, const uint8_t* marker_rows,
						 uint8_t marker_count, uint16_t loop_start_row)
{
	PnxSong s		   = { 0 };
	s.rows			   = rows;
	s.order			   = order;
	s.marker_rows	   = marker_rows;
	s.marker_count	   = marker_count;
	s.pattern_count	   = order_length; // not enforced by playback, just descriptive here
	s.order_length	   = order_length;
	s.rows_per_pattern = rows_per_pattern;
	s.instrument_count = 0;
	s.tempo_bpm		   = 15000; // 60000 / (15000*4) == 1ms/row exactly -- one row per 1ms tick
	s.loop_start_row   = loop_start_row;
	return s;
}

// Ticks pnx_music_update() forward by exactly one row (1ms at the tempo above) and returns the
// wall-clock passed in, so callers can chain `now = tick(now)`.
static uint32_t tick(uint32_t now_ms)
{
	pnx_music_update(now_ms);
	return now_ms + 1;
}

static void test_transition_at_pattern_end(void)
{
	PnxSong a = make_song(s_songA_rows, s_songA_order, 2, 2, NULL, 0, 0);
	PnxSong b = make_song(s_songB_rows, s_songB_order, 1, 3, NULL, 0, 0);

	pnx_music_play(&a, true);
	pnx_music_queue_transition(&b, true, PNX_TRANSITION_PATTERN_END);

	uint32_t now = 0;
	now			 = tick(now); // plays row 0 of pattern 0; row -> 1 (no wrap yet)
	MU_CHECK_EQ(pnx_music_row(), 1);

	now = tick(now); // row 1 -> wraps to 0, pattern boundary crossed -> transition fires here
	MU_CHECK_EQ(pnx_music_row(), 0);

	// From here song B (rows_per_pattern == 3) is driving playback -- confirm by the wrap
	// period, not by row value alone (both songs happen to read 0 right after a wrap).
	now = tick(now);
	MU_CHECK_EQ(pnx_music_row(), 1);
	now = tick(now);
	MU_CHECK_EQ(pnx_music_row(), 2);
	tick(now);
	MU_CHECK_EQ(pnx_music_row(), 0); // wrapped at 3 rows, not 2 -- this is song B

	pnx_music_stop();
}

static void test_transition_at_marker(void)
{
	PnxSong a = make_song(s_songA_rows, s_songA_order, 2, 2, s_songA_marker_raw, 1, 0);
	PnxSong b = make_song(s_songB_rows, s_songB_order, 1, 3, NULL, 0, 0);

	pnx_music_play(&a, true);
	pnx_music_queue_transition(&b, true, PNX_TRANSITION_NEXT_MARKER);

	uint32_t now = 0;
	now			 = tick(now);		 // plays row 0; row -> 1; abs_row (0*2+1=1) matches the marker --
									 // fires a whole pattern-boundary early, before A would ever wrap
	MU_CHECK_EQ(pnx_music_row(), 0); // reset by the swap, not "1" (which A itself would show)

	// Confirm song B's period-3 wrap, same as above -- proves the swap actually happened
	// rather than A coincidentally reading row 0 for an unrelated reason.
	now = tick(now);
	MU_CHECK_EQ(pnx_music_row(), 1);
	now = tick(now);
	MU_CHECK_EQ(pnx_music_row(), 2);
	tick(now);
	MU_CHECK_EQ(pnx_music_row(), 0);

	pnx_music_stop();
}

static void test_transition_at_natural_song_end(void)
{
	// A non-looping song that reaches its own last pattern's end with a PATTERN_END
	// transition queued should hand off to the next song rather than going silent -- the
	// last pattern's end is a pattern boundary too. Queued only AFTER the first boundary
	// (rather than before playback starts, like the other two tests) so it's still pending
	// specifically when order_pos wraps past order_length -- the one branch that would
	// otherwise call pnx_music_stop() instead of applying it.
	PnxSong a = make_song(s_songA_rows, s_songA_order, 2, 2, NULL, 0, 0);
	PnxSong b = make_song(s_songB_rows, s_songB_order, 1, 3, NULL, 0, 0);

	pnx_music_play(&a, false); // NOT looping
	uint32_t now = 0;
	now			 = tick(now); // pattern 0, row 0
	now			 = tick(now); // pattern 0, row 1 -> first boundary, order_pos -> 1
	MU_CHECK_EQ(pnx_music_row(), 0);

	pnx_music_queue_transition(&b, true, PNX_TRANSITION_PATTERN_END);
	now = tick(now); // pattern 1, row 0
	now = tick(now); // pattern 1, row 1 -> order_pos would wrap past order_length(2) here;
					 // the pending transition must take priority over pnx_music_stop()

	// Had the transition not taken priority, pnx_music_stop() would have fired and
	// pnx_music_playing() would read false.
	MU_CHECK_EQ(pnx_music_playing(), true);
	MU_CHECK_EQ(pnx_music_row(), 0);

	// Confirm it's actually song B (period-3 wrap), not A having merely looped around on
	// its own -- this song was started with loop=false, so A alone could not do this.
	now = tick(now);
	MU_CHECK_EQ(pnx_music_row(), 1);
	now = tick(now);
	MU_CHECK_EQ(pnx_music_row(), 2);
	tick(now);
	MU_CHECK_EQ(pnx_music_row(), 0);

	pnx_music_stop();
}

static void test_loop_start_point(void)
{
	// Song A (2 patterns x 2 rows, order [0,1], 4 rows total), loop point at absolute row 1
	// -- so once looping wraps, it should land back on abs row 1, never abs row 0 again.
	PnxSong a = make_song(s_songA_rows, s_songA_order, 2, 2, NULL, 0, 1);

	pnx_music_play(&a, true);
	uint32_t now = 0;
	now			 = tick(now); // abs 0 played; row -> 1
	MU_CHECK_EQ(pnx_music_row(), 1);
	now = tick(now); // abs 1 played; wraps to order_pos 1, row 0
	MU_CHECK_EQ(pnx_music_row(), 0);
	now = tick(now); // abs 2 played; row -> 1
	MU_CHECK_EQ(pnx_music_row(), 1);
	now = tick(now); // abs 3 played; end of order -> loops to loop_start_row(1), NOT row 0
	MU_CHECK_EQ(pnx_music_row(), 1);
	MU_CHECK_EQ(pnx_music_pattern(), 0); // order[0] -- order_pos landed on 0, not stuck at 2

	// One more full cycle from the loop point, proving it repeats THERE rather than firing
	// once: abs1 -> order_pos1,row0 -> abs2 -> row1 -> abs3 -> loops back to abs1 again.
	now = tick(now);
	MU_CHECK_EQ(pnx_music_row(), 0);
	MU_CHECK_EQ(pnx_music_pattern(), 1);
	now = tick(now);
	MU_CHECK_EQ(pnx_music_row(), 1);
	MU_CHECK_EQ(pnx_music_pattern(), 1);
	tick(now);
	MU_CHECK_EQ(pnx_music_row(), 1);
	MU_CHECK_EQ(pnx_music_pattern(), 0);

	pnx_music_stop();
}

static void test_loop_start_point_absent_wraps_to_zero(void)
{
	// Regression guard: loop_start_row defaults to 0 (a song built before loop points
	// existed, or one that never set one), so looping still wraps to the very start,
	// exactly as it did before this feature existed.
	PnxSong a	 = make_song(s_songA_rows, s_songA_order, 2, 2, NULL, 0, 0);
	uint32_t now = 0;
	pnx_music_play(&a, true);
	for (int i = 0; i < 4; i++)
		now = tick(now); // exactly one full pass through all 4 rows
	MU_CHECK_EQ(pnx_music_row(), 0);
	MU_CHECK_EQ(pnx_music_pattern(), 0);
	pnx_music_stop();
}

void test_music(void)
{
	printf("test_music\n");
	test_transition_at_pattern_end();
	test_transition_at_marker();
	test_transition_at_natural_song_end();
	test_loop_start_point();
	test_loop_start_point_absent_wraps_to_zero();
}
