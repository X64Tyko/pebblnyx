#include "track.h"

int32_t current_horizon_y(uint32_t distance)
{
	const int32_t slope = track_elevation_at((int32_t)distance);
	const int32_t h		= HORIZON_Y + slope * HORIZON_SLOPE_SHIFT;
	// Safety clamp, not a tuning knob: elevation is a small discrete set (-1/0/1) today,
	// so this never actually engages, but nothing here should be able to push the
	// horizon past the near row or off the top of the screen if that ever changes.
	return pnx_clamp_i32(h, 20, VIEW_H - 40);
}

void road_row(int32_t y, int32_t horizon_y, RoadRow* out)
{
	const int32_t d = (VIEW_H - y) + DEPTH_FUDGE;

	const int32_t rows_from_horizon = y - horizon_y;
	const int32_t visible_rows		= (VIEW_H - 1) - horizon_y;
	// No floor here any more -- this function's own header comment already promises
	// "zero at the horizon row", and an earlier `if (half < 2) half = 2` contradicted
	// that: every row within 2 * visible_rows/ROAD_HALF_MAX of the horizon (a handful
	// of rows in practice) computed to the SAME clamped width, so road_chunk (a scaled
	// SPRITE, not a flat colour fill) rendered them as a flat-sided block instead of a
	// taper -- direct feedback, "the top few rows are straight vertical lines... breaks
	// the illusion of the road". Every real consumer of half_width already tolerates 0
	// safely (traffic/police screen_x scaling resolves to dead centre; edge/lane/grid
	// half-widths all have their own pnx_max_i32(1, ...) floor at the point they are
	// actually divided by something) -- pnx_max_i32(0, ...) below is defensive against
	// rows_from_horizon going negative, not a behavioural floor; every real call site
	// only ever passes y >= horizon_y, where rows_from_horizon is never negative anyway.
	const int32_t half = pnx_max_i32(0, (ROAD_HALF_MAX * rows_from_horizon) / visible_rows);
	out->half_width	   = half;
	out->rumble_width  = half + pnx_max_i32(2, half / RUMBLE_FRAC);
	out->depth		   = (uint32_t)d;
}

// int16_t/int8_t/int8_t (4 bytes/segment, naturally aligned): length is one of
// {500..1000}, curve is -3..3, elevation is -1..1 -- every real value fits comfortably
// narrower than int32_t, and every read already widens back to int32_t at the point of
// use (track_curve_at/track_elevation_at's own return types).
typedef struct
{
	int16_t length;
	int8_t curve;
	int8_t elevation; // world-Y slope contribution per unit length, like curve but vertical
} RoadSegment;

// A small ROLLING WINDOW, not the whole track: earlier this held TRACK_SEGMENT_COUNT
// segments (56, sized so a full race's worth of distance -- MAX_SPEED *
// RACE_TIMER_START_TICKS, game.h -- was covered before the track's own modulo wrapped
// back to the start) pre-generated at boot. That bounds memory by "how long can one race
// possibly run," which is both the wrong bound (a race isn't actually the limit on how
// far a player can travel -- nothing here enforces the timer stops movement) and a much
// bigger one than the road ever needs at once: render.c only ever queries
// [distance, distance + VIEW_H + DEPTH_FUDGE) (road_curve_offset/road_elevation_offset's
// own row sweep), a couple hundred world units, not tens of thousands. So: generate
// segments on demand as the player approaches the far edge of what's already generated,
// and drop segments once the player has fully passed them -- memory stays flat and small
// regardless of how long a session runs, "indefinite play" included, and there is no
// track LENGTH any more for a loop to wrap around at all.
#define TRACK_WINDOW_SEGMENTS 5	   // capacity; see track_advance's own margin math for why
								   // this comfortably covers the real lookahead need
#define TRACK_LOOKAHEAD_MARGIN 600 // world units to stay generated ahead of `distance` --
								   // >= VIEW_H + DEPTH_FUDGE (this platform's own real
								   // worst-case query, up to ~252 on emery's 228px
								   // height) with headroom for however many ticks pass
								   // between track_advance calls

static RoadSegment s_window[TRACK_WINDOW_SEGMENTS];
static uint8_t s_head;		  // ring index of the oldest still-valid segment
static uint8_t s_count;		  // how many of s_window's slots currently hold real content
static int32_t s_window_z;	  // world-Z where s_window[s_head] begins
static int32_t s_covered_end; // world-Z where the window's generated content currently
							  // ends (s_window_z + every held segment's length) -- kept
							  // incrementally rather than resummed every call
static bool s_prev_sharp;	  // anti-clustering state, carried across generate() calls
							  // so an evicted segment's own "was it sharp" isn't lost

#if !N4P_LEGACY_FIXED_TRACK
// xorshift32, the same algorithm (and for the same reason) game.c's own rng_next uses --
// but a fully separate state, not g->rng: that one is deliberately fixed-seeded so
// traffic stays reproducible run to run (its own comment, game.c), and randomizing the
// track was never asked to change that. Seeded fresh in track_randomize, not fixed.
static uint32_t s_track_rng;

static uint32_t track_rng_next(void)
{
	s_track_rng ^= s_track_rng << 13;
	s_track_rng ^= s_track_rng >> 17;
	s_track_rng ^= s_track_rng << 5;
	return s_track_rng;
}

// One segment's length/curve/elevation, drawn together rather than as three independent
// uniform rolls -- an independent roll can produce an implausible track (e.g. two sharp
// curves back to back with no breather), which the old hand-authored TRACK deliberately
// never did. `prev_sharp` forces this segment gentle-or-straight regardless of the
// weighted draw, the one anti-clustering rule this generator enforces. Weights are
// starting points tuned by eye, same posture as CURVE_SCALE/ELEVATION_SCALE already are
// (both this file) -- expect a real-device playtesting pass to adjust them.
static RoadSegment random_segment(bool prev_sharp)
{
	static const int32_t LENGTHS[] = { 500, 600, 700, 800, 900, 1000 };
	RoadSegment s;
	s.length = LENGTHS[track_rng_next() % (sizeof(LENGTHS) / sizeof(LENGTHS[0]))];

	// Bucket roll: 0-34 straight (35%), 35-84 gentle (50%), 85-99 sharp (15%) -- capped
	// at 85 (never reaching the sharp bucket) right after another sharp segment.
	const uint32_t bucket_max = prev_sharp ? 85u : 100u;
	const uint32_t bucket	  = track_rng_next() % bucket_max;
	const bool sign_positive  = (track_rng_next() & 1) != 0;
	if (bucket < 35)
	{
		s.curve = 0;
	}
	else if (bucket < 85)
	{
		const int32_t magnitude = 1 + (int32_t)(track_rng_next() % 2); // 1 or 2
		s.curve					= sign_positive ? magnitude : -magnitude;
	}
	else
	{
		s.curve = sign_positive ? 3 : -3;
	}

	// 0 (flat) 50%, -1/+1 25% each.
	const uint32_t elev_roll = track_rng_next() % 4;
	s.elevation				 = (elev_roll < 2) ? 0 : (elev_roll == 2 ? -1 : 1);

	return s;
}
#endif // !N4P_LEGACY_FIXED_TRACK

#if N4P_LEGACY_FIXED_TRACK
// Cycled through in order, wrapping the INDEX (not world-Z the way the old modulo lookup
// did) -- still the same short, memorable, reproducible test loop, just fed through the
// same streaming window every other build uses instead of a separate static-array code
// path. No anti-clustering needed: these are hand-authored, already paced on purpose.
static const RoadSegment LEGACY[] = {
	{ 1000, 0, 0 }, // straight, flat
	{ 700, -2, 1 }, // gentle left, rising
	{ 900, 0, 0 },	// straight, crests then flat
	{ 500, 3, -1 }, // sharp right, falling
	{ 900, 0, 0 },	// straight, valley then flat
	{ 700, 1, 1 },	// gentle right, rising
	{ 900, 0, -1 }, // straight, falling back to start's elevation
};
static uint8_t s_legacy_idx;
#endif

// Appends exactly one new segment to the window's tail -- the only place s_covered_end
// grows. Never called when s_count == TRACK_WINDOW_SEGMENTS (track_advance's own ensure-
// ahead loop stops before that; TRACK_LOOKAHEAD_MARGIN is sized so capacity is never
// actually reached in practice, see track.h's own comment on TRACK_WINDOW_SEGMENTS).
static void generate_next(void)
{
	RoadSegment s;
#if N4P_LEGACY_FIXED_TRACK
	s			 = LEGACY[s_legacy_idx];
	s_legacy_idx = (uint8_t)((s_legacy_idx + 1) % (sizeof(LEGACY) / sizeof(LEGACY[0])));
#else
	s			 = random_segment(s_prev_sharp);
	s_prev_sharp = (s.curve == 3 || s.curve == -3);
#endif
	s_window[(s_head + s_count) % TRACK_WINDOW_SEGMENTS] = s;
	s_count++;
	s_covered_end += s.length;
}

// Called once per tick (game.c's game_tick, right after g->distance advances): grows the
// window forward as the player approaches its far edge, and drops segments the player
// has fully passed. This is the whole "streaming" half of the design -- track_curve_at/
// track_elevation_at (below) are pure lookups against whatever track_advance last left
// in the window, they never generate or evict anything themselves.
void track_advance(int32_t distance)
{
	while (s_count < TRACK_WINDOW_SEGMENTS && s_covered_end < distance + TRACK_LOOKAHEAD_MARGIN)
		generate_next();

	while (s_count > 0 && s_window_z + s_window[s_head].length <= distance)
	{
		s_window_z += s_window[s_head].length;
		s_head = (uint8_t)((s_head + 1) % TRACK_WINDOW_SEGMENTS);
		s_count--;
	}
}

void track_randomize(uint32_t seed)
{
#if !N4P_LEGACY_FIXED_TRACK
	// xorshift never recovers from an all-zero state (track_rng_next's own algorithm) --
	// same guard game.c's rng_next's own fixed seed comment notes, needed here because
	// `seed` is real (pnx_platform_now_ms()-derived) and could plausibly be 0.
	s_track_rng = seed ? seed : 0x9E3779B9u;
#else
	(void)seed;
	s_legacy_idx = 0;
#endif
	s_head = s_count = 0;
	s_window_z = s_covered_end = 0;
	s_prev_sharp			   = false;
	track_advance(0); // prime the window before the first real query ever lands
}

int32_t track_curve_at(int32_t world_z)
{
	// Guaranteed in-range by track_advance's own invariant (called once per tick, before
	// any of this frame's draw code runs -- pnx_app_frame's tick-then-draw order) as long
	// as world_z stays within [distance, distance + TRACK_LOOKAHEAD_MARGIN), which every
	// real caller's own depth math (road_row's DEPTH_FUDGE-bounded `d`) already respects.
	int32_t z = world_z - s_window_z;
	for (uint8_t i = 0; i < s_count; i++)
	{
		const RoadSegment* s = &s_window[(s_head + i) % TRACK_WINDOW_SEGMENTS];
		if (z < s->length)
			return s->curve;
		z -= s->length;
	}
	return 0; // window fell behind (shouldn't happen -- see the comment above)
}

int32_t track_elevation_at(int32_t world_z)
{
	int32_t z = world_z - s_window_z;
	for (uint8_t i = 0; i < s_count; i++)
	{
		const RoadSegment* s = &s_window[(s_head + i) % TRACK_WINDOW_SEGMENTS];
		if (z < s->length)
			return s->elevation;
		z -= s->length;
	}
	return 0; // same fallback as track_curve_at
}

int32_t road_curve_offset(uint32_t distance, int32_t y)
{
	const int32_t horizon_y = current_horizon_y(distance);
	int32_t curve_dx = 0, curve_x = 0;
	for (int32_t row = VIEW_H - 1; row >= y; row--)
	{
		RoadRow r;
		road_row(row, horizon_y, &r);
		curve_dx += track_curve_at((int32_t)(distance + r.depth));
		curve_x += curve_dx;
	}
	return curve_x / CURVE_SCALE;
}

int32_t road_elevation_offset(uint32_t distance, int32_t y)
{
	const int32_t horizon_y = current_horizon_y(distance);
	int32_t elev_dy = 0, elev_y = 0;
	for (int32_t row = VIEW_H - 1; row >= y; row--)
	{
		RoadRow r;
		road_row(row, horizon_y, &r);
		elev_dy += track_elevation_at((int32_t)(distance + r.depth));
		elev_y += elev_dy;
	}
	return elev_y / ELEVATION_SCALE;
}
