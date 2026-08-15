// Table geometry and tuning constants. Data only, no logic -- shared by game.c (physics)
// and render.c (drawing) without either owning it.

#pragma once

#include "pnx/pnx.h"

// Logical landscape space, as the player sees it holding the watch turned to
// PNX_ORIENT_BUTTONS_BOTTOM. The physical framebuffer never rotates (still
// PNX_DISPLAY_WIDTH x PNX_DISPLAY_HEIGHT, portrait) -- LOGICAL_W/VIEW_H are that,
// swapped, matching tools/pnx_assets.py's rotate_dims for a landscape orientation.
#define LOGICAL_W PNX_DISPLAY_HEIGHT
#define VIEW_H	  PNX_DISPLAY_WIDTH

// Table is taller than the viewport, so the camera scrolls to show it -- see render.c.
#define TABLE_H (VIEW_H * 2)

#define BALL_RADIUS_PX 5
#define WALL_BOUNCE	   (PNX_FX_ONE - PNX_FX_ONE / 8)
#define FLIPPER_BOUNCE (PNX_FX_ONE - PNX_FX_ONE / 6)

// Boundary traced from Reference/...Red Table.png (GBC panel, gitignored -- see
// ../README.md): one wide dome across the whole top, not a rectangle with a corner
// curve, and the serve lane's OUTER wall merges into that same dome on the right rather
// than a separate arc of its own. An earlier version curved the lane's INNER wall
// instead, which put the curve to the side of where the ball -- centred between the
// lane's two walls -- actually travels; fixed by making the dome's right side literally
// the outer wall's own continuation.
//
// Also coarser than that first attempt on purpose: those segments ran ~15-20px, too
// short for this module's discrete (non-swept) per-tick collision at real ball speeds.
// These run 30-90px.
#define LANE_OUTER_X  (LOGICAL_W - 10)
#define LANE_INNER_X  (LANE_OUTER_X - 20)
#define LANE_BOTTOM_Y (TABLE_H - 50)
#define LANE_MERGE_Y  55 // where the lane opens into the dome; shared vertex, no gap

static const PnxSegment WALLS[] = {
	{ .a = { 10, TABLE_H - 20 }, .b = { 10, 130 }, .bounce = WALL_BOUNCE },
	{ .a = { 10, 130 }, .b = { 45, 50 }, .bounce = WALL_BOUNCE },
	{ .a = { 45, 50 }, .b = { 120, 12 }, .bounce = WALL_BOUNCE },
	{ .a = { 120, 12 }, .b = { LANE_INNER_X, LANE_MERGE_Y }, .bounce = WALL_BOUNCE },
	{ .a = { LANE_INNER_X, LANE_MERGE_Y }, .b = { LANE_OUTER_X, 80 }, .bounce = WALL_BOUNCE },
	{ .a = { LANE_OUTER_X, 80 }, .b = { LANE_OUTER_X, TABLE_H - 20 }, .bounce = WALL_BOUNCE },
	{ .a	  = { LANE_INNER_X, LANE_BOTTOM_Y },
	  .b	  = { LANE_INNER_X, LANE_MERGE_Y },
	  .bounce = WALL_BOUNCE },
};
#define WALL_COUNT (sizeof(WALLS) / sizeof(WALLS[0]))

// Where a served ball rests, centred in the lane, until launched.
#define LANE_REST_X ((LANE_INNER_X + LANE_OUTER_X) / 2)
#define LANE_REST_Y (LANE_BOTTOM_Y - 10)

// Bottom of the table -- DOWN is the leftmost cluster button in this orientation, UP the
// rightmost (see input/pnx_input.h), so DOWN/UP are the left/right flippers.
static const PnxFlipper LEFT_FLIPPER_REST = {
	.pivot		= { 55, TABLE_H - 18 },
	.idle_tip	= { 30, TABLE_H - 4 },
	.struck_tip = { 90, TABLE_H - 36 },
	.bounce		= FLIPPER_BOUNCE,
};

static const PnxFlipper RIGHT_FLIPPER_REST = {
	.pivot		= { LOGICAL_W - 55, TABLE_H - 18 },
	.idle_tip	= { LOGICAL_W - 30, TABLE_H - 4 },
	.struck_tip = { LOGICAL_W - 90, TABLE_H - 36 },
	.bounce		= FLIPPER_BOUNCE,
};

// A playfield is tilted a few degrees, not vertical -- effective "down the slope" accel
// is g*sin(tilt), roughly 0.11 of g at a real table's ~6.5 degrees. Applied to the old
// (unrealistic, free-fall) constant as a rough dampening factor, not a calibrated figure.
#define GRAVITY (PNX_FX_ONE / 50)

#define SWING_RISE_PER_TICK (PNX_FX_ONE / 3)
#define SWING_FALL_PER_TICK (PNX_FX_ONE / 2)

// Charges to full in ~800ms held; release fires the launch regardless of charge, so even
// a tap gives SOME velocity (LAUNCH_MIN_VY) -- a real plunger does too.
#define CHARGE_RISE_PER_TICK (PNX_FX_ONE / 20)
#define LAUNCH_MIN_VY		 (PNX_FX_ONE * 4)
#define LAUNCH_MAX_VY		 (PNX_FX_ONE * 10)
