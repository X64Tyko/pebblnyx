// The road: per-row perspective geometry, plus the curve a car actually drives on.
//
// road_row/current_horizon_y/lane_center stay pure functions of a screen row or a
// world-Z position, so game.c (simulation) and render.c (drawing) can both call them
// without coordinating who owns what. The curve/elevation data is real `.c`-side state
// (track_randomize/track_advance, below) -- a small rolling window of generated
// segments, not the whole track: see TRACK_WINDOW_SEGMENTS's own comment (track.c) for
// why a streaming window replaced the earlier whole-track-pre-generated approach.

#pragma once

#include "pnx/pnx.h"

// Off by default -- enable with `PNX_DEFINES=N4P_LEGACY_FIXED_TRACK=1 pebble build` for
// the old hand-authored 7-segment test loop (reproducible dev/debug runs) instead of a
// real random track. Same shape as game.h's N4P_STEER_DEBUG_LOG; lives here rather than
// there because it's specifically about what track_randomize does, not general game
// behaviour -- track.h/.c stay the module that owns "what the track is" independently
// of game.c, same reasoning this file's own header comment already gives for the rest
// of the module.
#ifndef N4P_LEGACY_FIXED_TRACK
#define N4P_LEGACY_FIXED_TRACK 0
#endif

// Logical landscape space, as the player sees it holding the watch turned to
// PNX_ORIENT_BUTTONS_TOP. The physical framebuffer never rotates (still
// PNX_DISPLAY_WIDTH x PNX_DISPLAY_HEIGHT, portrait) -- LOGICAL_W/VIEW_H are that,
// swapped, matching tools/pnx_assets.py's rotate_dims for a landscape orientation.
// examples/pinball/src/c/table.h carries the same comment for BUTTONS_BOTTOM.
#define LOGICAL_W PNX_DISPLAY_HEIGHT
#define VIEW_H	  PNX_DISPLAY_WIDTH

// ---------------------------------------------------------------- road geometry
//
// True linear perspective, not the 1/depth kind: half_width is linear in SCREEN ROW,
// zero at the horizon row and ROAD_HALF_MAX at the near row. That's what makes a
// straight road's edges dead-straight lines converging at the horizon, matching an
// actual horizon line -- 1/depth (what this used to be) puts a hyperbolic bulge in the
// edges of even a perfectly flat straight road, which reads as the road climbing or
// dipping on its own, independent of whatever `track_elevation_at` says it's actually
// doing. `depth` is unrelated to width now -- it's still the row's world-Z stand-in for
// everything else (texture scroll, curve/elevation sampling), just no longer feeds the
// shape of the road itself.

#define DEPTH_FUDGE 24 // world-Z lookahead already "used up" by the near row itself

// Rows uphill/downhill shift where the road appears to vanish: cresting a hill blocks
// the view sooner (a smaller HORIZON_Y..VIEW_H-1 span, same idea as not being able to
// see past the top of a real hill), a valley opens it up. HORIZON_Y is the flat-ground
// baseline; current_horizon_y (below) is what everything else actually renders against.
#define HORIZON_Y			60
#define HORIZON_SLOPE_SHIFT 15 // screen rows the horizon moves per unit of current slope

// Paved half-width at the near plane. Deliberately less than PLAYER_LANE_MAX (game.h) --
// that gap IS the off-road margin: past ROAD_HALF_MAX is off the pavement (and slower,
// game_tick), past PLAYER_LANE_MAX is a hard wall so the player's own car sprite can
// never draw past the screen edge. Was 130 (wider than LOGICAL_W/2), which let the car
// draw off-screen entirely at full lateral deflection -- see PLAYER_LANE_MAX's comment.
#define ROAD_HALF_MAX 75
#define RUMBLE_FRAC	  5	 // rumble adds half_width/RUMBLE_FRAC per side
#define BAND_WORLD	  28 // world units per road/ground stripe
#define LANES		  4	 // LANES-1 dashed dividers split the width

typedef struct
{
	int32_t half_width;
	int32_t rumble_width;
	uint32_t depth;
} RoadRow;

// `horizon_y` is the CURRENT effective horizon (current_horizon_y, below) -- callers
// that sweep every row of one frame's road (draw_road, road_curve_offset,
// road_elevation_offset) compute it once and pass the same value to every row, so the
// linear taper stays consistent within that sweep even though it can change frame to
// frame with the player's own slope.
void road_row(int32_t y, int32_t horizon_y, RoadRow* out);

// The effective horizon row for the CURRENT slope at world-Z `distance` -- see
// HORIZON_SLOPE_SHIFT's own comment. Not a per-row lookup: one call per frame (or per
// accumulation sweep) is enough, since it only depends on where the player already is,
// not on how far ahead a given row looks.
int32_t current_horizon_y(uint32_t distance);

// Centre offset (world units, same scale as Game.lane_x / ROAD_HALF_MAX) of lane
// `lane`, 0 = leftmost. Traffic spawns snapped to one of these; the player is free
// (steering is continuous, not lane-snapped).
static inline int32_t lane_center(int32_t lane)
{
	const int32_t width = (ROAD_HALF_MAX * 2) / LANES;
	return -ROAD_HALF_MAX + width * lane + width / 2;
}

// ---------------------------------------------------------------- curve segments
//
// Randomly generated by default (track_randomize/track_advance, below) -- lengths in
// world-Z units (the same units Game.distance already counts in), curve in
// screen-offset-per-row-per-row (a second derivative: it accumulates once into a rate
// and again into a position, same as any pseudo-3D racer's curve maths -- see
// render.c's draw_road and game.c's centrifugal pull). Only ever queried within a small
// window ahead of the player's own position -- see track_advance's own comment for the
// exact bound -- so callers do not need to reason about how far the track has been
// generated; that is track_advance's job, called once per tick before any of this.
int32_t track_curve_at(int32_t world_z);

// Resets and reseeds the track: game.c calls this once at boot and once per restart, so
// every race is a genuinely new layout, not just the first one. Under
// N4P_LEGACY_FIXED_TRACK (above), `seed` is unused and this instead resets to the start
// of the old hand-authored 7-segment test loop, for reproducible dev/debug runs. Primes
// the window with an initial batch of segments before returning -- track_curve_at/
// track_elevation_at are safe to call immediately after, no separate priming step.
void track_randomize(uint32_t seed);

// Call once per tick, after distance advances (game.c's game_tick) -- grows the
// generated window forward as `distance` approaches its far edge, and drops segments
// fully behind `distance`. Must run before this tick's draw code: track_curve_at/
// track_elevation_at only ever look up against whatever the window already holds, they
// never generate anything themselves.
void track_advance(int32_t distance);

#define CURVE_SCALE 60 // divides accumulated curve into a screen offset; tuned by eye

// Screen-offset (already /CURVE_SCALE, same units draw_road's own `cx` uses) of the
// road centre at screen row `y`, given the player's current world-Z `distance`. Walks
// near (VIEW_H-1) down to `y` accumulating curve exactly like render.c's draw_road does
// for every row of the road itself -- shared here so a car that ISN'T the road (traffic)
// still lines up with the same bend. O(rows between VIEW_H-1 and y); fine for the
// handful of one-off lookups traffic needs, wrong tool if draw_road ever called this
// per row instead of accumulating inline as it already does.
int32_t road_curve_offset(uint32_t distance, int32_t y);

// ------------------------------------------------------------------- hills/valleys
//
// Same double-integration as curve (elevation -> rate -> a screen offset), just
// applied to the row's own Y instead of X: `draw_road` shifts each row's DRAWN screen
// row by the accumulated amount instead of its horizontal centre. Zero at the near
// plane by construction (accumulation starts there), same as curve, so the player's
// own fixed draw position never needs to move -- only the road/traffic ahead bends.
// No width or horizon-line adjustment for camera height -- a real 3D camera would need
// one, this doesn't have one, so hills read as "the road ahead rises and falls" rather
// than "the horizon rises and falls with it." A cheaper trick, not a full projection.
int32_t track_elevation_at(int32_t world_z);

// Divides accumulated elevation into a screen offset. Gentler than CURVE_SCALE on
// purpose: curve only ever shifts a row's X, so however far it drifts, each row still
// lands on its own distinct Y. Elevation shifts Y itself, so a steep enough
// accumulated slope pushes rows together (fine -- render.c's draw_road clamps dy to
// strictly decrease) or apart faster than 1 screen row per world-row (also fine --
// draw_road fills the resulting gap by stretching that row's own fill to the height it
// actually claims, rather than leaving skipped screen rows undrawn). 500 just keeps
// the peak offset in a visually sane range (~20px at the horizon for this TRACK's
// steepest segment); it no longer needs to avoid collision/gaps, draw_road handles
// both unconditionally.
#define ELEVATION_SCALE 500

// Screen-row offset (already /ELEVATION_SCALE) at screen row `y`, mirroring
// road_curve_offset exactly -- see that function's own comment.
int32_t road_elevation_offset(uint32_t distance, int32_t y);
