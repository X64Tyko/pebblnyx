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
	int32_t half					= (ROAD_HALF_MAX * rows_from_horizon) / visible_rows;
	if (half < 2)
		half = 2;
	out->half_width	  = half;
	out->rumble_width = half + pnx_max_i32(2, half / RUMBLE_FRAC);
	out->depth		  = (uint32_t)d;
}

typedef struct
{
	int32_t length;
	int32_t curve;
	int32_t elevation; // world-Y slope contribution per unit length, like curve but vertical
} RoadSegment;

static const RoadSegment TRACK[] = {
	{ 1000, 0, 0 }, // straight, flat
	{ 700, -2, 1 }, // gentle left, rising
	{ 900, 0, 0 },	// straight, crests then flat
	{ 500, 3, -1 }, // sharp right, falling
	{ 900, 0, 0 },	// straight, valley then flat
	{ 700, 1, 1 },	// gentle right, rising
	{ 900, 0, -1 }, // straight, falling back to start's elevation
};
#define TRACK_TOTAL_LENGTH (1000 + 700 + 900 + 500 + 900 + 700 + 900)

int32_t track_curve_at(int32_t world_z)
{
	int32_t z = world_z % TRACK_TOTAL_LENGTH;
	if (z < 0)
		z += TRACK_TOTAL_LENGTH;
	for (size_t i = 0; i < sizeof(TRACK) / sizeof(TRACK[0]); i++)
	{
		if (z < TRACK[i].length)
			return TRACK[i].curve;
		z -= TRACK[i].length;
	}
	return 0; // unreachable: z is already reduced mod the sum of every segment's length
}

int32_t track_elevation_at(int32_t world_z)
{
	int32_t z = world_z % TRACK_TOTAL_LENGTH;
	if (z < 0)
		z += TRACK_TOTAL_LENGTH;
	for (size_t i = 0; i < sizeof(TRACK) / sizeof(TRACK[0]); i++)
	{
		if (z < TRACK[i].length)
			return TRACK[i].elevation;
		z -= TRACK[i].length;
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
