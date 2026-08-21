#include "game.h"
#include "track.h"
#include "assets_gen.h"

#include <string.h>

static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;

// xorshift32. Deterministic seed on purpose -- traffic layout should be the same run
// to run while this is still a test track (DESIGN.md), not device-random noise that'd
// make a bug report un-reproducible.
static uint32_t rng_next(Game* g)
{
	g->rng ^= g->rng << 13;
	g->rng ^= g->rng >> 17;
	g->rng ^= g->rng << 5;
	return g->rng;
}

// Places `t` somewhere between SPAWN_AHEAD_MIN and +SPAWN_AHEAD_SPAN ahead of
// `ahead_of`, in a random lane, at a random speed within the traffic band. Used both
// at boot (staggered) and to recycle a car the player has passed or crashed into.
static void traffic_spawn(Game* g, Traffic* t, uint32_t ahead_of)
{
	t->active = true;
	t->z	  = (int32_t)ahead_of + TRAFFIC_SPAWN_AHEAD_MIN +
		(int32_t)(rng_next(g) % TRAFFIC_SPAWN_AHEAD_SPAN);
	t->lane_x = lane_center((int32_t)(rng_next(g) % LANES));
	t->speed  = TRAFFIC_MIN_SPEED +
		(int32_t)(rng_next(g) % (TRAFFIC_MAX_SPEED - TRAFFIC_MIN_SPEED + 1));
}

// Staggered so the whole set doesn't spawn bunched into one window. Shared by boot and
// game_restart -- identical either way, since restart doesn't want a different traffic
// layout from a fresh boot (rng is reseeded fixed in both, see their callers).
static void traffic_reset(Game* g)
{
	for (int i = 0; i < MAX_TRAFFIC; i++)
		traffic_spawn(g, &g->traffic[i], (uint32_t)(i * 150));
}

// No pursuer yet -- game_tick's police_tick spawns one once `cooldown_ticks` runs out
// and the player is moving fast enough to be worth chasing. `first` picks which
// cooldown range: a longer, more generous window before the very first chase of a run
// than between one chase ending and the next (POLICE_FIRST_SPAWN_* vs POLICE_RESPAWN_*,
// game.h).
static void police_reset(Game* g, bool first)
{
	Police* p				  = &g->police;
	p->active				  = false;
	p->crash_ticks_left		  = 0;
	p->speed				  = 0;
	const uint32_t min_ticks  = first ? POLICE_FIRST_SPAWN_MIN_TICKS : POLICE_RESPAWN_MIN_TICKS;
	const uint32_t span_ticks = first ? POLICE_FIRST_SPAWN_SPAN_TICKS : POLICE_RESPAWN_SPAN_TICKS;
	p->cooldown_ticks		  = min_ticks + (rng_next(g) % span_ticks);
}

bool game_boot(Game* g)
{
	memset(g, 0, sizeof(*g));
	g->rng = 0x9E3779B9u; // any nonzero fixed seed; xorshift never recovers from 0

	if (!pnx_arena_init(&g->persistent, "persistent", PERSIST_BYTES, 4) ||
		!pnx_arena_init(&g->scene, "scene", SCENE_BYTES, 4))
	{
		pnx_platform_log("arena init failed");
		return false;
	}

	pnx_assets_init(&g->persistent, &g->scene, RESOURCES, PNX_ASSET_COUNT);
	if (!pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES))
		pnx_platform_log("need4pebble: palettes would not load");
	g->has_car = pnx_sprite_load(&g->car, PNX_ASSET_SPRITE_TOURING_NORMAL);
	if (!g->has_car)
		pnx_platform_log("need4pebble: car sprite would not load");
	g->has_crash = pnx_sprite_load(&g->crash, PNX_ASSET_SPRITE_TOURING_CRASH);
	if (!g->has_crash)
		pnx_platform_log("need4pebble: crash sprite would not load");
	g->has_menu_font = pnx_font_load(&g->menu_font, PNX_ASSET_FONT_MENU);
	if (!g->has_menu_font)
		pnx_platform_log("need4pebble: menu font would not load");
	g->has_police = pnx_sprite_load(&g->police_car, PNX_ASSET_SPRITE_POLICE_NORMAL);
	if (!g->has_police)
		pnx_platform_log("need4pebble: police sprite would not load");
	g->has_police_crash = pnx_sprite_load(&g->police_crash, PNX_ASSET_SPRITE_POLICE_CRASH);
	if (!g->has_police_crash)
		pnx_platform_log("need4pebble: police crash sprite would not load");

	traffic_reset(g);
	police_reset(g, true);

	pnx_input_init(PNX_ORIENTATION);
	return true;
}

void game_restart(Game* g)
{
	g->rng = 0x9E3779B9u; // same fixed seed as boot -- see rng_next's own comment

	g->accumulator_ms		= 0;
	g->tick_count			= 0;
	g->distance				= 0;
	g->speed				= 0;
	g->accel_accum			= 0;
	g->lane_x				= 0;
	g->steer_visual			= 0;
	g->corner_penalty_accum = 0;
	g->crash_ticks_left		= 0;
	g->paused				= false;
	g->game_over			= false;
	// use_tilt_steer is a player preference, not run state -- left alone.

	traffic_reset(g);
	police_reset(g, true);
}

void game_shutdown(Game* g)
{
	pnx_arena_destroy(&g->scene);
	pnx_arena_destroy(&g->persistent);
}

static void traffic_tick(Game* g)
{
	for (int i = 0; i < MAX_TRAFFIC; i++)
	{
		Traffic* t = &g->traffic[i];
		if (!t->active)
			continue;
		t->z += t->speed;
		if (t->z + TRAFFIC_BEHIND_MARGIN < (int32_t)g->distance)
			traffic_spawn(g, t, g->distance);
	}
}

// Only called while not already crashing (game_tick gates this) -- one hit per tick is
// enough, and re-checking mid-crash would just find the same car again before
// traffic_spawn has had a tick to move it away.
static void check_collision(Game* g)
{
	for (int i = 0; i < MAX_TRAFFIC; i++)
	{
		Traffic* t = &g->traffic[i];
		if (!t->active)
			continue;

		const int32_t dz = t->z - (int32_t)g->distance;
		const int32_t dx = t->lane_x - g->lane_x;
		if (dz > -TRAFFIC_COLLIDE_Z && dz < TRAFFIC_COLLIDE_Z &&
			dx > -TRAFFIC_COLLIDE_LANE_HALF && dx < TRAFFIC_COLLIDE_LANE_HALF)
		{
			g->crash_ticks_left = CRASH_TOTAL_TICKS;
			g->speed			= 0;
			traffic_spawn(g, t, g->distance); // this one's gone; the slot lives on
			return;
		}
	}

	// The pursuing cop forcing a hit, not the player driving into it (DESIGN.md:
	// "tries to force a collision") -- same hitbox as traffic, but the cop itself
	// doesn't crash or get recycled here; it caused this, it keeps chasing. Relevant
	// right after this: check_busted's "0 velocity with a cop behind you" reads
	// police.active, which this hit doesn't touch.
	Police* p = &g->police;
	if (p->active && p->crash_ticks_left == 0)
	{
		const int32_t dz = p->z - (int32_t)g->distance;
		const int32_t dx = p->lane_x - g->lane_x; // >=0: cop is on the player's right
		if (dz > -TRAFFIC_COLLIDE_Z && dz < TRAFFIC_COLLIDE_Z &&
			dx > -TRAFFIC_COLLIDE_LANE_HALF && dx < TRAFFIC_COLLIDE_LANE_HALF)
		{
			g->crash_ticks_left = CRASH_TOTAL_TICKS;
			g->speed			= 0;
			// Forced steering: shoved to the side OPPOSITE the cop, not just stopped
			// in place -- see POLICE_RAM_SHOVE's own comment (game.h).
			const int32_t shove = (dx >= 0) ? -POLICE_RAM_SHOVE : POLICE_RAM_SHOVE;
			g->lane_x			= pnx_clamp_i32(g->lane_x + shove, -PLAYER_LANE_MAX, PLAYER_LANE_MAX);
		}
	}
}

// Chase AI. Spawns occasionally (a cooldown, not a per-tick chance -- see
// POLICE_FIRST_SPAWN_*/POLICE_RESPAWN_* in game.h), then closes toward
// POLICE_DESIRED_GAP and tracks the player's lane with a lag. The cop can crash on
// its own the same two ways the player can -- off-road, or into a traffic car -- which
// is the player's actual tool for shaking it (DESIGN.md: "forcing IT to crash into
// traffic/a wall").
static void police_tick(Game* g)
{
	Police* p = &g->police;

	if (p->crash_ticks_left > 0)
	{
		p->crash_ticks_left--;
		if (p->crash_ticks_left == 0)
		{
			p->active = false;
			police_reset(g, false); // start the cooldown toward the NEXT chase
		}
		return;
	}

	if (!p->active)
	{
		if (p->cooldown_ticks > 0)
		{
			p->cooldown_ticks--;
			return;
		}
		if (g->speed < POLICE_MIN_SPEED_TO_SPAWN)
			return; // wait for a real chase, not one starting from a crawl
		p->active				 = true;
		p->z					 = (int32_t)g->distance - POLICE_SPAWN_BEHIND;
		p->lane_x				 = g->lane_x;
		p->speed				 = g->speed;
		p->offset_x				 = 0;
		p->offset_target		 = 0;
		p->offset_retarget_ticks = 0; // picks a real (never-zero) target on the first active tick
		return;
	}

	// Correct toward POLICE_DESIRED_GAP rather than simply matching the player's
	// speed -- see that constant's own comment (game.h) for why plain matching would
	// never actually close in, and why this is also what makes the cop occasionally
	// force a hit without a separate ramming state.
	const int32_t gap	 = (int32_t)g->distance - p->z; // >0 == behind the player
	int32_t target_speed = g->speed;
	if (gap > POLICE_DESIRED_GAP)
		target_speed += POLICE_CATCHUP_BONUS;
	else if (gap < POLICE_DESIRED_GAP / 2)
		target_speed -= POLICE_CATCHUP_BONUS;
	p->speed = pnx_clamp_i32(target_speed, 0, MAX_SPEED);
	p->z += p->speed;

	// While the player is frozen mid-crash, game_tick's own stunned branch still calls
	// this (same as traffic_tick) so the chase keeps moving through it -- but the
	// correction above only nudges speed by POLICE_CATCHUP_BONUS a tick, nowhere near
	// fast enough to stop existing momentum carrying the cop straight past the
	// player's stalled distance and out in front for the whole stun window (up to
	// CRASH_TOTAL_TICKS at full speed). Pin it here instead: never ahead of a player
	// who isn't currently able to move.
	if (g->crash_ticks_left > 0)
		p->z = pnx_min_i32(p->z, (int32_t)g->distance);

	// Lateral drift: favour one side of the player, never dead centre, wandering
	// between sides over time rather than locking to one -- see POLICE_OFFSET_*'s own
	// comment (game.h) for why (both a feel request and what actually makes the cop's
	// own sprite visible instead of hiding under the player's).
	if (p->offset_retarget_ticks > 0)
		p->offset_retarget_ticks--;
	else
	{
		const bool favour_right = (rng_next(g) & 1) != 0;
		const int32_t mag		= POLICE_OFFSET_MIN +
			(int32_t)(rng_next(g) % (POLICE_OFFSET_MAX - POLICE_OFFSET_MIN + 1));
		p->offset_target		 = favour_right ? mag : -mag;
		p->offset_retarget_ticks = POLICE_OFFSET_RETARGET_MIN_TICKS +
			(rng_next(g) % POLICE_OFFSET_RETARGET_SPAN_TICKS);
	}
	p->offset_x += (p->offset_target - p->offset_x) / POLICE_OFFSET_EASE_DIVISOR;

	const int32_t target_lane_x = g->lane_x + p->offset_x;
	const int32_t dx			= target_lane_x - p->lane_x;
	p->lane_x += dx / POLICE_LANE_LAG_DIVISOR;

	// Off-road: a much less controlled driver than the player -- straying past the
	// edge wrecks it outright rather than just capping its speed (OFFROAD_MAX_SPEED
	// is a player-only mercy).
	RoadRow row;
	road_row(VIEW_H - 1, current_horizon_y((uint32_t)p->z), &row);
	if (p->lane_x > row.half_width || p->lane_x < -row.half_width)
	{
		p->crash_ticks_left = CRASH_TOTAL_TICKS;
		p->speed			= 0;
		return;
	}

	// Traffic: the same trap the player can fall into, applied to the cop -- steer it
	// into a car and it takes the hit instead of you.
	for (int i = 0; i < MAX_TRAFFIC; i++)
	{
		Traffic* t = &g->traffic[i];
		if (!t->active)
			continue;
		const int32_t dz  = t->z - p->z;
		const int32_t ddx = t->lane_x - p->lane_x;
		if (dz > -TRAFFIC_COLLIDE_Z && dz < TRAFFIC_COLLIDE_Z &&
			ddx > -TRAFFIC_COLLIDE_LANE_HALF && ddx < TRAFFIC_COLLIDE_LANE_HALF)
		{
			p->crash_ticks_left = CRASH_TOTAL_TICKS;
			p->speed			= 0;
			traffic_spawn(g, t, g->distance);
			return;
		}
	}
}

// "If you hit 0 velocity with a cop behind you it's game over" -- whatever put speed
// at 0 (a crash, or just braking to a stop mid-chase), not only the collision path.
// POLICE_BUST_RANGE, not POLICE_DESIRED_GAP: a cop that's a little behind on its own
// correction is still very much "right there" (see that constant's own comment).
static void check_busted(Game* g)
{
	if (g->game_over || g->speed != 0)
		return;
	const Police* p = &g->police;
	if (!p->active || p->crash_ticks_left > 0)
		return;
	const int32_t gap = (int32_t)g->distance - p->z;
	if (gap >= 0 && gap <= POLICE_BUST_RANGE)
		g->game_over = true;
}

void game_toggle_pause(Game* g)
{
	g->paused = !g->paused;
}

void game_toggle_steer_mode(Game* g)
{
	if (!g->paused)
		return; // reserved for the pause menu; a no-op bump of SELECT mid-drive
	g->use_tilt_steer = !g->use_tilt_steer;
}

// -1/0/1 from whichever steering input is active. Touch drag is unconditionally
// available; tilt needs the deadzone to keep a near-flat hold from reading as constant
// jitter -- see ACCEL_STEER_DEADZONE's own comment (game.h) for the axis caveat (the
// sign is confirmed now: raw `.y` had tilt-left steering right, backwards -- negated
// below).
static int8_t steer_input(const Game* g)
{
	if (!g->use_tilt_steer)
		return pnx_input_drag_dx();

	PnxAccel a;
	pnx_platform_accel_read(&a);
	if (a.y > ACCEL_STEER_DEADZONE)
		return -1;
	if (a.y < -ACCEL_STEER_DEADZONE)
		return 1;
	return 0;
}

void game_tick(Game* g)
{
	if (g->paused)
		return; // frozen: traffic, crash countdown, everything -- see game_toggle_pause
	if (g->game_over)
		return; // frozen until game_restart -- BACK's job while BUSTED, see main.c

	g->tick_count++;

	if (g->crash_ticks_left > 0)
	{
		// Stunned: no input, no distance (speed is already 0), but the world doesn't
		// stop around a wrecked car -- traffic (and the cop, if one's chasing) keeps
		// moving. check_busted here is what actually catches "hit 0 velocity with a
		// cop behind you" for the common case: the crash THAT set speed to 0.
		g->crash_ticks_left--;
		traffic_tick(g);
		police_tick(g);
		check_busted(g);
		return;
	}

	// The curve the player is ON, not some future row's -- computed before `distance`
	// moves this tick, so it reflects where the car actually is right now.
	const int32_t current_curve = track_curve_at((int32_t)g->distance);

	const bool brake = pnx_input_held(pnx_input_cluster(0)); // left top button
	const bool gas	 = pnx_input_held(pnx_input_cluster(2)); // right top button

	if (gas)
	{
		// Fractional acceleration via a carried remainder -- see ACCEL_NUM/ACCEL_DEN's
		// own comment (game.h). Releasing gas drops the carry rather than banking it, so
		// a tap-tap-tap on the button doesn't secretly accelerate faster than holding it.
		g->accel_accum += ACCEL_NUM;
		while (g->accel_accum >= ACCEL_DEN)
		{
			g->accel_accum -= ACCEL_DEN;
			g->speed++;
		}
	}
	else
	{
		g->accel_accum = 0;
		if (g->speed > 0)
			g->speed -= FRICTION;
	}
	if (brake)
		g->speed -= BRAKE_DECEL;
	g->speed = pnx_clamp_i32(g->speed, 0, MAX_SPEED);

	const int8_t target = (int8_t)(steer_input(g) * 4); // -1/0/1, touch drag or tilt
	if (g->steer_visual < target)
		g->steer_visual++;
	else if (g->steer_visual > target)
		g->steer_visual--;

	// A curve pulls the car toward its outside unless the player steers into it --
	// countersteering to hold the inside line is the one skill DESIGN.md's core loop
	// leans on ("Racing line matters").
	g->lane_x -= (current_curve * g->speed) / CENTRIFUGAL_DIVISOR;
	// Turn authority scales with speed -- see TURN_RATE_SPEED_CAP's own comment
	// (game.h). pnx_min_i32 is the clamp-not-reduce part: past the cap this is just
	// STEER_RATE again, same as before this existed.
	const int32_t turn_speed = pnx_min_i32(g->speed, TURN_RATE_SPEED_CAP);
	g->lane_x += (g->steer_visual * STEER_RATE * turn_speed) / TURN_RATE_SPEED_CAP;
	// PLAYER_LANE_MAX, not ROAD_HALF_MAX -- the gap between them is real off-road
	// territory (checked below), not just a renamed edge. See PLAYER_LANE_MAX's own
	// comment (game.h) for why it has to be this and not the road's own width.
	g->lane_x = pnx_clamp_i32(g->lane_x, -PLAYER_LANE_MAX, PLAYER_LANE_MAX);

	// Off the pavement caps speed (OFFROAD_MAX_SPEED) rather than braking it -- a
	// ceiling, not a drain, so the car keeps rolling off-road instead of grinding to a
	// stop (see OFFROAD_MAX_SPEED's own comment, game.h). On a curve, which side of
	// centre matters even before that: the inside of the bend (same sign as the curve)
	// holds full speed, the outside costs it -- the payoff for the line the
	// centrifugal pull above makes you fight to hold.
	// The near row's width is ROAD_HALF_MAX regardless of which horizon_y this is
	// computed against (it cancels out at y == VIEW_H-1 by construction), but pass the
	// real one anyway rather than lean on that -- cheap, and one less thing to notice
	// stops being true if road_row's formula ever changes.
	RoadRow near_row;
	road_row(VIEW_H - 1, current_horizon_y(g->distance), &near_row);
	if (g->lane_x > near_row.half_width || g->lane_x < -near_row.half_width)
	{
		g->speed				= pnx_min_i32(g->speed, OFFROAD_MAX_SPEED);
		g->corner_penalty_accum = 0;
	}
	else if (current_curve != 0)
	{
		const bool inside = (current_curve * g->lane_x) > 0;
		if (inside)
		{
			g->speed				= pnx_clamp_i32(g->speed + CORNER_INSIDE_BONUS, 0, MAX_SPEED);
			g->corner_penalty_accum = 0;
		}
		else
		{
			// Fractional, and strictly gentler than ACCEL -- see CORNER_OUTSIDE_NUM/DEN's
			// own comment (game.h) for why this must never be able to out-drain full gas.
			g->corner_penalty_accum += CORNER_OUTSIDE_NUM;
			while (g->corner_penalty_accum >= CORNER_OUTSIDE_DEN)
			{
				g->corner_penalty_accum -= CORNER_OUTSIDE_DEN;
				g->speed = pnx_max_i32(0, g->speed - 1);
			}
		}
	}
	else
	{
		g->corner_penalty_accum = 0;
	}

	traffic_tick(g);
	check_collision(g);
	police_tick(g);

	g->distance += (uint32_t)g->speed;

	// After the tick's own distance update -- a no-op when speed is 0 (the only case
	// check_busted cares about) either way, but keeps this reading as "state settled
	// for the tick" rather than "mid-update."
	check_busted(g);
}
