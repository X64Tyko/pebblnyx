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

// Deliberately narrow: on aplite, `static` (not `const`) storage lands in .bss, unlike
// the old compile-time TRACK[] literal it replaces (which the linker put in flash) --
// int32_t x3 (12 bytes/segment) pushed aplite's tiny static-RAM region over budget by
// four figures once pnx_app's own state was linked in too (Part 2). Every real value
// fits comfortably narrower: length is one of {500..1000}, curve is -3..3, elevation is
// -1..1 -- int16_t/int8_t/int8_t (4 bytes/segment, naturally aligned, no padding) is a
// 3x shrink with zero behavioural change; every read already widens back to int32_t at
// the point of use (track_curve_at/track_elevation_at's own return types).
typedef struct
{
	int16_t length;
	int8_t curve;
	int8_t elevation; // world-Y slope contribution per unit length, like curve but vertical
} RoadSegment;

// Populated by track_randomize -- see its own comment and TRACK_SEGMENT_COUNT's (track.h).
// Not `const` any more: a random track is written into this once per race, not baked in
// at compile time. Slots past however many track_randomize actually used (always all of
// them outside N4P_LEGACY_FIXED_TRACK; only the first 7 under it) sit zero-initialised
// and unreachable -- s_track_total_length bounds the modulo below to real content only.
static RoadSegment s_track[TRACK_SEGMENT_COUNT];
static int32_t s_track_total_length;

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

void track_randomize(uint32_t seed)
{
#if N4P_LEGACY_FIXED_TRACK
	(void)seed;
	static const RoadSegment LEGACY[] = {
		{ 1000, 0, 0 }, // straight, flat
		{ 700, -2, 1 }, // gentle left, rising
		{ 900, 0, 0 },	// straight, crests then flat
		{ 500, 3, -1 }, // sharp right, falling
		{ 900, 0, 0 },	// straight, valley then flat
		{ 700, 1, 1 },	// gentle right, rising
		{ 900, 0, -1 }, // straight, falling back to start's elevation
	};
	s_track_total_length = 0;
	for (size_t i = 0; i < sizeof(LEGACY) / sizeof(LEGACY[0]); i++)
	{
		s_track[i] = LEGACY[i];
		s_track_total_length += LEGACY[i].length;
	}
#else
	// xorshift never recovers from an all-zero state (track_rng_next's own algorithm) --
	// same guard game.c's rng_next's own fixed seed comment notes, needed here because
	// `seed` is real (pnx_platform_now_ms()-derived) and could plausibly be 0.
	s_track_rng = seed ? seed : 0x9E3779B9u;

	s_track_total_length = 0;
	bool prev_sharp		 = false;
	for (int i = 0; i < TRACK_SEGMENT_COUNT; i++)
	{
		const RoadSegment s = random_segment(prev_sharp);
		s_track[i]			= s;
		s_track_total_length += s.length;
		prev_sharp = (s.curve == 3 || s.curve == -3);
	}
#endif
}

int32_t track_curve_at(int32_t world_z)
{
	int32_t z = world_z % s_track_total_length;
	if (z < 0)
		z += s_track_total_length;
	for (int i = 0; i < TRACK_SEGMENT_COUNT; i++)
	{
		if (z < s_track[i].length)
			return s_track[i].curve;
		z -= s_track[i].length;
	}
	return 0; // unreachable: z is already reduced mod the sum of every segment's length
}

int32_t track_elevation_at(int32_t world_z)
{
	int32_t z = world_z % s_track_total_length;
	if (z < 0)
		z += s_track_total_length;
	for (int i = 0; i < TRACK_SEGMENT_COUNT; i++)
	{
		if (z < s_track[i].length)
			return s_track[i].elevation;
		z -= s_track[i].length;
	}
	return 0; // unreachable, same as track_curve_at
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
