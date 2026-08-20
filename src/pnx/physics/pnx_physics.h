// Circle-vs-segment ball physics: gravity, static table geometry, and flippers.
//
// The whole module rests on one simplification: a pinball table's *content* -- rails,
// slingshots, flipper pivots -- lives in pixel space (PnxPixel, int16) because it is
// authored once and never moves, while the BALL carries pnx_fx (16.16) position and
// velocity because it moves a fraction of a pixel per PNX_TICK_MS and integrating it in
// whole pixels stalls dead on a shallow slope instead of creeping down it.
//
// Collision math itself -- distances, normals -- is done back in plain pixel space,
// not fx. That is not laziness: dx/dy on a 228px-tall table squared and multiplied
// through pnx_fx_mul overflows pnx_fx's int32 well before the table edge (228px squared
// alone is ~1.9% of fx's headroom AFTER the implicit *65536 a squared fx value carries).
// Losing sub-pixel precision on a collision NORMAL costs nothing a player can feel; losing
// it on POSITION integration costs everything, so each stays in the unit that is actually
// safe for what it does.
//
// No sin/cos anywhere. A flipper is authored as two baked tip positions -- idle and
// struck -- and `swing` lerps between them. That is one multiply instead of a trig call
// and a lookup table, and it is genuinely all a pinball flipper's silhouette needs: it
// does not bend through the middle of its arc, so nothing is lost by not sampling it.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_PHYSICS

#include "../core/pnx_fx.h"

#include <stdint.h>
#include <stdbool.h>

// Table geometry lives here: int16 pixels, no sub-pixel precision, because it is
// authored once and never moves. Keeps a table's segment list cheap against the 64KB
// budget -- half the size of the fx equivalent.
typedef struct
{
	int16_t x, y;
} PnxPixel;

// One static wall/rail/slingshot edge. `bounce` is a 16.16 restitution scalar:
// PNX_FX_ONE reflects with no speed lost, 0 stops the ball dead against it. Real table
// steel is duller than PNX_FX_ONE; PNX_FX_ONE - PNX_FX_ONE/8 (~0.875) is a reasonable
// start for an ordinary rail, lower for felt-lined outlanes.
typedef struct
{
	PnxPixel a, b;
	pnx_fx bounce;
} PnxSegment;

// A static solid box -- a SOLID tile's whole cell, or a SCALED tile's inset rect, both
// expressed as world-pixel corners the caller works out from the map's tile grid (this
// module does not depend on pnx_assets, deliberately: table geometry here is authored
// content, not necessarily tile-driven, so the coupling belongs at the call site, not
// here). `bounce` means the same thing PnxSegment's does.
typedef struct
{
	PnxPixel min, max; // min <= max on both axes
	pnx_fx bounce;
} PnxBox;

typedef struct
{
	pnx_fx x, y;   // position, pixel-space, sub-pixel precision
	pnx_fx vx, vy; // velocity, pixels per tick
	pnx_fx radius; // fx so it composes with position/velocity math without a conversion
} PnxBall;

// A flipper: a segment that pivots between two baked poses rather than through an angle.
// `swing` is caller-driven, 0 (idle)..PNX_FX_ONE (fully struck) -- drive it from
// pnx_input_held with your own attack/release rate rather than snapping it between the
// two values, or the flipper has a pose but no motion for the ball to actually be hit BY.
typedef struct
{
	PnxPixel pivot;
	PnxPixel idle_tip, struck_tip;
	pnx_fx swing; // 0..PNX_FX_ONE
	pnx_fx bounce;
} PnxFlipper;

void pnx_physics_ball_init(PnxBall* ball, int32_t x, int32_t y, int32_t radius_px);

// Gravity is a constant downward accel in fx pixels/tick^2, added to vy and then
// integrated into position, once per call. Call this once per PNX_TICK_MS tick, not per
// rendered frame -- a table only ever has to choose the ONE constant that makes its fall
// speed feel right, not a per-frame recompute against a variable elapsed time.
//
// A reasonable starting point on a 200x228 table: gravity around PNX_FX_ONE/6 (roughly
// 0.17 px/tick^2, ~53 px/s^2 at 25 Hz) -- unmeasured, tune per table.
void pnx_physics_tick(PnxBall* ball, pnx_fx gravity);

// Resolves the ball against one static segment: if the ball's circle overlaps the
// segment, the position is pushed out along the collision normal and the velocity is
// reflected and scaled by `seg->bounce`. Returns true exactly when that happened, so a
// caller can fire a sound/light/score hook on CONTACT rather than re-testing every tick.
//
// O(1), and a table's full wall list is just a loop over this -- there is no broad phase
// here. Fine at pinball's entity count (one ball, a few dozen segments); revisit only if
// a table's segment count is ever measured costing more than that loop is worth.
bool pnx_physics_collide_segment(PnxBall* ball, const PnxSegment* seg);

// Resolves the ball against one static box, the same way pnx_physics_collide_segment
// resolves against one static segment -- push out along the normal, reflect velocity,
// scaled by `box->bounce`. Meant for a SOLID tile's cell or a SCALED tile's inset rect
// (see PNX_COLLISION_SOLID/SCALED, assets/pnx_assets.h): a caller building one from the
// map's tile grid just needs the cell's world-pixel corners, which is arithmetic this
// module has no reason to do on the caller's behalf.
//
// Like segment collision, this is closest-point-based, not full SAT: the closest point
// on the box to the ball's centre is the centre clamped to the box's extent on each axis.
// That is exact when the centre is OUTSIDE the box -- the ordinary case, a ball approaching
// a wall -- and degenerates the same way a zero-length segment does when the centre ends up
// INSIDE the box (every axis already clamps to itself, so dist is 0): the ball is punted
// straight up rather than the more correct "push out along the least-penetrated axis". A
// tunnelling ball landing dead centre in a wall is the case this costs, and it is the same
// tradeoff pnx_physics_collide_segment already makes for a ball sitting exactly on a wall.
bool pnx_physics_collide_aabb(PnxBall* ball, const PnxBox* box);

// A single point -- e.g. one "ink" pixel out of a COMPLEX tile's 1bpp mask -- resolved
// exactly like a segment collision, because it IS one: a point is a segment whose two ends
// coincide, and pnx_physics_collide_segment's own closest-point math already has a
// dedicated branch for that case (a degenerate a == b segment).
bool pnx_physics_collide_point(PnxBall* ball, PnxPixel p, pnx_fx bounce);

// Walks a COMPLEX mask (row-major MSB-first 1bpp, `w` x `h`, the same layout
// pnx_atlas_tile_complex_mask/pnx_sprite_frame_complex_mask hand back -- assets/
// pnx_assets.h) placed at world-pixel `origin_x`/`origin_y`, and resolves against
// whichever ink pixel is closest to the ball, the same closest-point contract every other
// primitive here already follows. False if the mask has no ink pixel at all (never true
// in practice for a real COMPLEX tile/frame, but a caller building one by hand could pass
// an all-empty one). This is the piece pnx_physics_collide_point's own comment used to
// describe as not built yet -- deliberately takes a raw mask/size/origin rather than a
// PnxAtlas/PnxSprite, so this module still does not depend on pnx_assets (see this file's
// own header comment); the caller unpacks the asset and bridges it here, same as PnxBox.
bool pnx_physics_collide_mask(PnxBall* ball, const uint8_t* mask, uint8_t w, uint8_t h,
							  int32_t origin_x, int32_t origin_y, pnx_fx bounce);

// Resolves against a flipper's CURRENT pose (idle_tip lerped toward struck_tip by
// `swing`), as an ordinary segment collision, plus one thing a static segment cannot
// give: the tip's own velocity. `swing_rate` is how fast `swing` changed THIS tick (fx
// units/tick, signed -- positive while striking); a fraction of the tip's resulting
// velocity is added to the ball's outgoing velocity on contact. Without that a flipper
// caught mid-swing only redirects the ball, it doesn't launch it -- and a pinball table
// whose flippers can only redirect, not launch, is missing the entire point of a
// flipper. Pass swing_rate = 0 to fall back to plain reflection off a currently-still
// flipper.
bool pnx_physics_collide_flipper(PnxBall* ball, const PnxFlipper* flip, pnx_fx swing_rate);

#endif // PNX_USE_PHYSICS
