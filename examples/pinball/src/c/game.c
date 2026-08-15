#include "game.h"
#include "table.h"

#include <string.h>

void game_serve_ball(Game* g)
{
	pnx_physics_ball_init(&g->ball, LANE_REST_X, LANE_REST_Y, BALL_RADIUS_PX);
	g->ball_in_lane	 = true;
	g->launch_charge = 0;
}

void game_init(Game* g)
{
	memset(g, 0, sizeof(*g));
	g->left	 = LEFT_FLIPPER_REST;
	g->right = RIGHT_FLIPPER_REST;
	game_serve_ball(g);
}

static pnx_fx update_flipper_swing(PnxFlipper* flip, bool held)
{
	const pnx_fx before = flip->swing;
	const pnx_fx rate	= held ? SWING_RISE_PER_TICK : -SWING_FALL_PER_TICK;
	pnx_fx after		= pnx_fx_clamp(before + rate, 0, PNX_FX_ONE);
	flip->swing			= after;
	return after - before;
}

void game_tick(Game* g)
{
	g->ticks++;

	const pnx_fx left_rate	= update_flipper_swing(&g->left, pnx_input_held(PNX_BUTTON_DOWN));
	const pnx_fx right_rate = update_flipper_swing(&g->right, pnx_input_held(PNX_BUTTON_UP));

	if (g->ball_in_lane)
	{
		if (pnx_input_held(PNX_BUTTON_SELECT))
			g->launch_charge = pnx_fx_clamp(g->launch_charge + CHARGE_RISE_PER_TICK, 0, PNX_FX_ONE);

		if (pnx_input_released(PNX_BUTTON_SELECT))
		{
			g->ball.vy		= -(LAUNCH_MIN_VY + pnx_fx_mul(g->launch_charge, LAUNCH_MAX_VY - LAUNCH_MIN_VY));
			g->ball.vx		= 0;
			g->ball_in_lane = false;
		}
	}
	else
	{
		pnx_physics_tick(&g->ball, GRAVITY);

		for (size_t i = 0; i < WALL_COUNT; i++)
			pnx_physics_collide_segment(&g->ball, &WALLS[i]);

		pnx_physics_collide_flipper(&g->ball, &g->left, left_rate);
		pnx_physics_collide_flipper(&g->ball, &g->right, right_rate);

		if (pnx_fx_to_int(g->ball.y) > TABLE_H + BALL_RADIUS_PX * 4)
		{
			g->drains++;
			pnx_log("drain #%u at tick %u", (unsigned)g->drains, (unsigned)g->ticks);
			game_serve_ball(g);
		}
	}

	// Centre the ball vertically, clamped to the table's own extent.
	g->camera_y = pnx_fx_to_int(g->ball.y) - VIEW_H / 2;
	g->camera_y = pnx_clamp_i32(g->camera_y, 0, TABLE_H - VIEW_H);
}
