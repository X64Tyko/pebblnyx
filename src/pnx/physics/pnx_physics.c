#include "pnx_physics.h"

#if PNX_USE_PHYSICS

// All collision math (distance, normal) runs in plain int32 pixel space, not fx -- see
// the file header in pnx_physics.h for why fx overflows here. `pnx_isqrt` (core/pnx_fx.h)
// is exactly the tool for that: a plain-int32 Newton sqrt with no fixed-point shift to
// get wrong.

void pnx_physics_ball_init(PnxBall* ball, int32_t x, int32_t y, int32_t radius_px)
{
	ball->x		 = pnx_fx_from_int(x);
	ball->y		 = pnx_fx_from_int(y);
	ball->vx	 = 0;
	ball->vy	 = 0;
	ball->radius = pnx_fx_from_int(radius_px);
}

void pnx_physics_tick(PnxBall* ball, pnx_fx gravity)
{
	ball->vy += gravity;
	ball->x += ball->vx;
	ball->y += ball->vy;
}

// Closest point on segment [a,b] to point p, all in plain pixel int32. Returns the
// squared distance from p to that point via *out_dist_sq, and the point itself via
// *out_x/*out_y -- callers need both, and recomputing one from the other would just be
// the same clamp a second time.
static void closest_point_on_segment(int32_t ax, int32_t ay, int32_t bx, int32_t by,
									 int32_t px, int32_t py, int32_t* out_x, int32_t* out_y,
									 int32_t* out_dist_sq)
{
	const int32_t abx = bx - ax, aby = by - ay;
	const int32_t apx = px - ax, apy = py - ay;
	const int32_t ab_len_sq = abx * abx + aby * aby;

	int32_t t_num = apx * abx + apy * aby; // unnormalised projection of ap onto ab
	int32_t cx, cy;
	if (ab_len_sq == 0 || t_num <= 0)
	{
		// Degenerate segment (a == b), or the projection falls behind A -- either way
		// the closest point is A itself.
		cx = ax;
		cy = ay;
	}
	else if (t_num >= ab_len_sq)
	{
		cx = bx;
		cy = by;
	}
	else
	{
		// t = t_num / ab_len_sq, clamped to [0,1] by the two branches above. Both
		// operands stay well inside int32 at table scale (a few hundred px), so this
		// is done in plain integer division rather than fx.
		cx = ax + (int32_t)(((int64_t)t_num * abx) / ab_len_sq);
		cy = ay + (int32_t)(((int64_t)t_num * aby) / ab_len_sq);
	}

	const int32_t dx = px - cx, dy = py - cy;
	*out_x		 = cx;
	*out_y		 = cy;
	*out_dist_sq = dx * dx + dy * dy;
}

// Closest point on an axis-aligned box [min,max] to point p, plain pixel int32 -- the box
// analogue of closest_point_on_segment above, and the same shape of function: clamp the
// point to the box's extent on each axis independently. Exact when p is outside the box;
// when p is already inside, every axis clamps to itself and this returns p with a
// distance of 0, which resolve_contact treats as the same "no defined push direction"
// case a degenerate segment does.
static void closest_point_on_aabb(int32_t minx, int32_t miny, int32_t maxx, int32_t maxy,
								  int32_t px, int32_t py, int32_t* out_x, int32_t* out_y,
								  int32_t* out_dist_sq)
{
	const int32_t cx = px < minx ? minx : (px > maxx ? maxx : px);
	const int32_t cy = py < miny ? miny : (py > maxy ? maxy : py);
	const int32_t dx = px - cx, dy = py - cy;
	*out_x		 = cx;
	*out_y		 = cy;
	*out_dist_sq = dx * dx + dy * dy;
}

// Shared by every collide_* entry point once it has reduced its own geometry down to "the
// closest point on it to the ball's centre" -- a segment, a box and a point all answer that
// question differently, but what happens once it is known (push out, reflect, lend a moving
// contact's velocity) is exactly the same math regardless of which kind of geometry asked.
// `tip_vx`/`tip_vy` are a moving contact's own velocity, added to the ball's outgoing
// velocity -- 0 for anything static.
static bool resolve_contact(PnxBall* ball, int32_t px, int32_t py, int32_t cx, int32_t cy,
							int32_t dist_sq, pnx_fx bounce, pnx_fx tip_vx, pnx_fx tip_vy)
{
	const int32_t r	   = pnx_fx_to_int(ball->radius);
	const int32_t r_sq = r * r;
	if (dist_sq >= r_sq)
		return false;

	const int32_t dist = pnx_isqrt(dist_sq);

	// Unit normal, expressed as an fx fraction so it composes with the ball's fx
	// velocity below without a second unit system. A contact point that coincides with
	// the ball's own centre (dist == 0 -- a segment the ball sits exactly on, or a box
	// the ball's centre has tunnelled inside) has no defined push direction; punt it
	// straight up rather than divide by zero.
	pnx_fx nx, ny;
	if (dist == 0)
	{
		nx = 0;
		ny = -PNX_FX_ONE;
	}
	else
	{
		nx = (pnx_fx)(((int64_t)(px - cx) * PNX_FX_ONE) / dist);
		ny = (pnx_fx)(((int64_t)(py - cy) * PNX_FX_ONE) / dist);
	}

	// Push the ball out to the surface along the normal. penetration is in fx pixels;
	// dist and r are small plain ints so promoting the difference to fx is safe.
	const pnx_fx penetration = pnx_fx_from_int(r - dist);
	ball->x += pnx_fx_mul(nx, penetration);
	ball->y += pnx_fx_mul(ny, penetration);

	// Reflect velocity about the normal: v' = v - (1 + bounce) * (v . n) * n. Velocity
	// components are small (a handful of px/tick), so every fx_mul below stays far
	// inside int32 -- unlike position, this one genuinely is safe in fx throughout.
	const pnx_fx vdotn = pnx_fx_mul(ball->vx, nx) + pnx_fx_mul(ball->vy, ny);
	if (vdotn < 0) // only reflect when moving INTO the surface, not already leaving it
	{
		const pnx_fx factor = pnx_fx_mul(PNX_FX_ONE + bounce, vdotn);
		ball->vx -= pnx_fx_mul(factor, nx);
		ball->vy -= pnx_fx_mul(factor, ny);
	}

	// Flipper strike: lend the tip's own velocity to the ball. Half-strength by design
	// -- a full 1:1 transfer plus the reflection above sends the ball off far faster
	// than anything the table's own bounce constants suggest, and this is the one
	// number in the module actually worth tuning per table once one exists to play.
	ball->vx += tip_vx / 2;
	ball->vy += tip_vy / 2;

	return true;
}

// Shared by pnx_physics_collide_segment and pnx_physics_collide_flipper (a flipper is
// just a segment whose endpoints move). `tip_vx`/`tip_vy` are the moving endpoint's own
// velocity, added to the ball's outgoing velocity on contact -- 0 for a static wall.
static bool collide_segment_moving(PnxBall* ball, int32_t ax, int32_t ay, int32_t bx,
								   int32_t by, pnx_fx bounce, pnx_fx tip_vx, pnx_fx tip_vy)
{
	const int32_t px = pnx_fx_to_int(ball->x);
	const int32_t py = pnx_fx_to_int(ball->y);

	int32_t cx, cy, dist_sq;
	closest_point_on_segment(ax, ay, bx, by, px, py, &cx, &cy, &dist_sq);
	return resolve_contact(ball, px, py, cx, cy, dist_sq, bounce, tip_vx, tip_vy);
}

bool pnx_physics_collide_segment(PnxBall* ball, const PnxSegment* seg)
{
	return collide_segment_moving(ball, seg->a.x, seg->a.y, seg->b.x, seg->b.y, seg->bounce, 0,
								  0);
}

bool pnx_physics_collide_aabb(PnxBall* ball, const PnxBox* box)
{
	const int32_t px = pnx_fx_to_int(ball->x);
	const int32_t py = pnx_fx_to_int(ball->y);

	int32_t cx, cy, dist_sq;
	closest_point_on_aabb(box->min.x, box->min.y, box->max.x, box->max.y, px, py, &cx, &cy,
						  &dist_sq);
	return resolve_contact(ball, px, py, cx, cy, dist_sq, box->bounce, 0, 0);
}

bool pnx_physics_collide_point(PnxBall* ball, PnxPixel p, pnx_fx bounce)
{
	// A point IS a degenerate segment (a == b) -- collide_segment_moving's own
	// closest-point math already has a dedicated branch for that, so this is the whole
	// implementation. See PNX_COLLISION_COMPLEX's own comment (pnx_assets.h) for what
	// still has to exist before a caller has a pixel to pass here.
	return collide_segment_moving(ball, p.x, p.y, p.x, p.y, bounce, 0, 0);
}

bool pnx_physics_collide_flipper(PnxBall* ball, const PnxFlipper* flip, pnx_fx swing_rate)
{
	// Lerp the tip between its two baked poses. idle/struck are authored pixel points;
	// promoting the difference to fx and back is safe because a flipper's own throw is
	// a few tens of pixels, nowhere near fx's overflow floor (see pnx_physics.h).
	const pnx_fx idle_x	  = pnx_fx_from_int(flip->idle_tip.x);
	const pnx_fx idle_y	  = pnx_fx_from_int(flip->idle_tip.y);
	const pnx_fx struck_x = pnx_fx_from_int(flip->struck_tip.x);
	const pnx_fx struck_y = pnx_fx_from_int(flip->struck_tip.y);

	const pnx_fx tip_x = idle_x + pnx_fx_mul(flip->swing, struck_x - idle_x);
	const pnx_fx tip_y = idle_y + pnx_fx_mul(flip->swing, struck_y - idle_y);

	// The tip's instantaneous velocity is just the swing's rate of change carried
	// through the same lerp direction -- how fast the pose is moving from idle toward
	// struck, scaled by how far apart those poses are.
	const pnx_fx tip_vx = pnx_fx_mul(swing_rate, struck_x - idle_x);
	const pnx_fx tip_vy = pnx_fx_mul(swing_rate, struck_y - idle_y);

	return collide_segment_moving(ball, flip->pivot.x, flip->pivot.y, pnx_fx_to_int(tip_x),
								  pnx_fx_to_int(tip_y), flip->bounce, tip_vx, tip_vy);
}

#endif // PNX_USE_PHYSICS
