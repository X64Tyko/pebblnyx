// Host tests for circle-vs-geometry collision (src/pnx/physics).
//
// Every number here is hand-derived from the module's own formulas (closest_point_on_*,
// resolve_contact in pnx_physics.c) rather than eyeballed, the same way the blitter's
// rotate test in test_gfx.c pins its expected pixel with the same maths the blitter
// itself runs -- a test that just checks "did SOMETHING happen" would pass against a
// push-out in the wrong direction or a bounce that adds energy instead of removing it.
//
// A single scenario -- a 5px-radius ball resting 2px into a horizontal surface at y=50,
// moving down at 3 px/tick -- is reused across segment, AABB and point collision so the
// three primitives are checked against the SAME numbers rather than three unrelated ones,
// which is what actually proves collide_aabb/collide_point agree with collide_segment
// rather than merely each seeming plausible on their own.

#include "../src/pnx/physics/pnx_physics.h"

#include <stdio.h>

extern int s_failures;
extern int s_checks;

#define PH_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define PH_CHECK_EQ(a, b)                                                                    \
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

void test_physics(void);

// ------------------------------------------------------------------------- shared rig

// A ball 2px into a surface whose top edge is the line y=50, radius 5, falling at
// 3 px/tick. Every one of the scenario's expected numbers (below) is derived from this
// exact setup -- change any of the three and they all need re-deriving by hand.
static PnxBall resting_ball(void)
{
	PnxBall ball;
	pnx_physics_ball_init(&ball, 50, 48, 5);
	ball.vy = pnx_fx_from_int(3);
	return ball;
}

// ------------------------------------------------------------------------------ segment

static void test_segment(void)
{
	// bounce = 0: resolve_contact's factor collapses to vdotn itself, and the reflection
	// term exactly cancels the incoming velocity -- the ball should stop DEAD, not merely
	// slow down. See resolve_contact's own derivation for why this is exact, not
	// approximate: with nx=0 (a horizontal surface), vy's post-collision value is
	// vy - (1+bounce)*vdotn, and vdotn IS vy*ny = -vy here, so at bounce=0 that is
	// vy - vy = 0.
	{
		PnxBall ball	 = resting_ball();
		PnxSegment floor = { { 0, 50 }, { 100, 50 }, 0 };
		const bool hit	 = pnx_physics_collide_segment(&ball, &floor);
		PH_CHECK(hit);
		// Pushed out to exactly touch the surface: centre at y=50-radius=45.
		PH_CHECK_EQ(pnx_fx_to_int(ball.y), 45);
		PH_CHECK_EQ(pnx_fx_to_int(ball.x), 50); // the x push is zero for a head-on hit
		PH_CHECK_EQ(ball.vy, 0);
	}

	// bounce = PNX_FX_ONE: perfectly elastic, so the ball leaves at exactly the speed it
	// arrived, reversed -- +3 down becomes -3 (up), not merely negative.
	{
		PnxBall ball	 = resting_ball();
		PnxSegment floor = { { 0, 50 }, { 100, 50 }, PNX_FX_ONE };
		PH_CHECK(pnx_physics_collide_segment(&ball, &floor));
		PH_CHECK_EQ(pnx_fx_to_int(ball.vy), -3);
	}

	// A ball nowhere near the segment must not be touched at all -- the push-out and the
	// reflection are both guarded by the same overlap test, so this also covers "does the
	// distance check actually gate the write".
	{
		PnxBall ball		  = resting_ball();
		ball.y				  = pnx_fx_from_int(0); // radius 5 at y=0 is nowhere near y=50
		PnxSegment floor	  = { { 0, 50 }, { 100, 50 }, 0 };
		const pnx_fx before_y = ball.y, before_vy = ball.vy;
		PH_CHECK(!pnx_physics_collide_segment(&ball, &floor));
		PH_CHECK_EQ(ball.y, before_y);
		PH_CHECK_EQ(ball.vy, before_vy);
	}

	// Leaving, not arriving: a ball already moving AWAY from the surface (vy < 0, up)
	// still gets pushed out of an overlap, but its velocity is left alone -- reflecting
	// it again would be adding a second bounce nothing hit.
	{
		PnxBall ball	 = resting_ball();
		ball.vy			 = pnx_fx_from_int(-2); // moving up, away from the floor
		PnxSegment floor = { { 0, 50 }, { 100, 50 }, PNX_FX_ONE };
		PH_CHECK(pnx_physics_collide_segment(&ball, &floor));
		PH_CHECK_EQ(pnx_fx_to_int(ball.y), 45);	 // still pushed out
		PH_CHECK_EQ(pnx_fx_to_int(ball.vy), -2); // velocity untouched
	}
}

// --------------------------------------------------------------------------------- aabb

static void test_aabb(void)
{
	// A box whose top edge is the SAME line the segment tests used (y=50, spanning the
	// ball's x) must resolve identically for a ball approaching from directly above --
	// closest_point_on_aabb clamps to the same (50,50) closest_point_on_segment found.
	{
		PnxBall ball = resting_ball();
		PnxBox floor = { { 0, 50 }, { 100, 60 }, 0 };
		PH_CHECK(pnx_physics_collide_aabb(&ball, &floor));
		PH_CHECK_EQ(pnx_fx_to_int(ball.y), 45);
		PH_CHECK_EQ(ball.vy, 0);
	}

	// From the left: a ball to the left of a box, radius overlapping the box's left
	// edge, must be pushed out along -x, not -y -- confirms the push direction actually
	// follows the closest point rather than always favouring one axis.
	{
		PnxBall ball;
		pnx_physics_ball_init(&ball, 48, 50, 5); // centre 2px left of the box's left edge
		ball.vx		= pnx_fx_from_int(3);		 // moving right, into the box
		PnxBox wall = { { 50, 0 }, { 100, 100 }, 0 };
		PH_CHECK(pnx_physics_collide_aabb(&ball, &wall));
		PH_CHECK_EQ(pnx_fx_to_int(ball.x), 45); // pushed back out to touch x=50
		PH_CHECK_EQ(pnx_fx_to_int(ball.y), 50); // no vertical push
		PH_CHECK_EQ(ball.vx, 0);				// bounce=0, stopped dead on this axis too
	}

	// From below: pushed out along +y this time, the mirror of the resting-ball case.
	{
		PnxBall ball;
		pnx_physics_ball_init(&ball, 50, 12, 5); // 2px above the box's bottom edge (y=10)
		ball.vy		   = pnx_fx_from_int(-3);	 // moving up, into the box
		PnxBox ceiling = { { 0, -100 }, { 100, 10 }, 0 };
		PH_CHECK(pnx_physics_collide_aabb(&ball, &ceiling));
		PH_CHECK_EQ(pnx_fx_to_int(ball.y), 15); // pushed down to touch y=10
		PH_CHECK_EQ(ball.vy, 0);
	}

	// The ball's own centre inside the box: closest_point_on_aabb clamps to the centre
	// itself (dist=0), the same "no defined push direction" case a zero-length segment
	// hits -- resolve_contact's documented fallback is straight up, which
	// PNX_FLIP_ROTATE... no: PnxBox's own header comment says explicitly. Pinned here so
	// a future change to that fallback is a deliberate edit, not a silent one.
	{
		PnxBall ball;
		pnx_physics_ball_init(&ball, 50, 50, 5); // dead centre of the box
		PnxBox box			  = { { 0, 0 }, { 100, 100 }, 0 };
		const pnx_fx before_x = ball.x;
		PH_CHECK(pnx_physics_collide_aabb(&ball, &box));
		PH_CHECK_EQ(ball.x, before_x);			// no horizontal push
		PH_CHECK_EQ(pnx_fx_to_int(ball.y), 45); // punted up by exactly the radius
	}

	// No overlap: box far away, nothing touched.
	{
		PnxBall ball = resting_ball();
		PnxBox far	 = { { 1000, 1000 }, { 1010, 1010 }, 0 };
		PH_CHECK(!pnx_physics_collide_aabb(&ball, &far));
	}
}

// -------------------------------------------------------------------------------- point

static void test_point(void)
{
	// A point IS a degenerate segment -- pnx_physics_collide_point is documented as
	// exactly collide_segment_moving's a==b branch, so the SAME resting-ball scenario
	// through a point sitting at the segment test's closest point (50,50) must reproduce
	// the segment result exactly, not merely something plausible.
	PnxBall ball	 = resting_ball();
	const PnxPixel p = { 50, 50 };
	PH_CHECK(pnx_physics_collide_point(&ball, p, 0));
	PH_CHECK_EQ(pnx_fx_to_int(ball.y), 45);
	PH_CHECK_EQ(ball.vy, 0);

	// A point elsewhere on the ball's radius (not directly below) pushes along whatever
	// direction connects the point to the centre -- checked here at 45 degrees-ish (a
	// point offset equally in x and y) so a formula that silently assumes an axis-aligned
	// normal (as the resting tests above all are) would still be caught.
	{
		PnxBall b2;
		pnx_physics_ball_init(&b2, 50, 50, 5);
		// dist_sq = 3^2 + 3^2 = 18, dist = pnx_isqrt(18) = 4 -- inside radius 5, a
		// genuine overlap, and off-axis in both x and y at once.
		const PnxPixel q = { 53, 53 };
		PH_CHECK(pnx_physics_collide_point(&b2, q, 0));
		// Pushed AWAY from q, i.e. further from (53,53) than the centre started -- both
		// x and y must have moved in the negative direction (away from the point).
		PH_CHECK(pnx_fx_to_int(b2.x) < 50);
		PH_CHECK(pnx_fx_to_int(b2.y) < 50);
	}
}

// ----------------------------------------------------------------------------- flipper

static void test_flipper(void)
{
	// At swing=0 the flipper is exactly at idle_tip and behaves like a plain static
	// segment from pivot to idle_tip -- swing_rate=0 means no tip velocity lent either.
	{
		PnxBall ball;
		// y=-2: 2px ABOVE a flipper lying along y=0 (smaller y is "up", same convention
		// resting_ball uses against its y=50 floor), radius 3 so it overlaps by 1px.
		pnx_physics_ball_init(&ball, 20, -2, 3);
		ball.vy			= pnx_fx_from_int(2); // moving down, into the flipper
		PnxFlipper flip = {
			{ 0, 0 },	 // pivot
			{ 40, 0 },	 // idle_tip
			{ 40, -20 }, // struck_tip
			0,			 // swing = idle
			0,			 // bounce
		};
		PH_CHECK(pnx_physics_collide_flipper(&ball, &flip, 0));
		PH_CHECK_EQ(pnx_fx_to_int(ball.y), -3); // pushed up to touch y=0
		PH_CHECK_EQ(ball.vy, 0);				// bounce=0, stopped
	}

	// A mid-swing strike lends half the tip's velocity to a ball it contacts, on top of
	// the ordinary reflection -- this is the one thing a static segment could never give
	// a ball, so it is worth pinning as its own number rather than folding into the
	// idle-flipper case above.
	{
		PnxBall ball;
		pnx_physics_ball_init(&ball, 20, -2, 3);
		ball.vy			= 0; // not moving into the surface -- isolates the lent-velocity term
		PnxFlipper flip = {
			{ 0, 0 },
			{ 40, 0 },
			{ 40, -16 }, // struck 16px above idle, not 20 -- see swing_rate's own comment
			0,			 // swing = idle: tip is still AT idle_tip this instant...
			0,
		};
		// ...but swinging at this rate: tip_vy = swing_rate * (struck.y - idle.y) =
		// swing_rate * -16. PNX_FX_ONE/16 is exactly 4096 (65536/16, no truncation) and
		// 4096 * fx(-16) shifts back down to exactly -PNX_FX_ONE -- picked so the result
		// is exact fixed-point arithmetic, not a rounded approximation of one. The ball
		// should gain half of that lent velocity: -PNX_FX_ONE/2, from a standing start.
		PH_CHECK(pnx_physics_collide_flipper(&ball, &flip, PNX_FX_ONE / 16));
		PH_CHECK_EQ(ball.vy, -(PNX_FX_ONE / 2));
	}
}

void test_physics(void)
{
	printf("physics\n");
	test_segment();
	test_aabb();
	test_point();
	test_flipper();
}
