// Simulation state and the fixed-tick step. No rendering, no input polling beyond
// reading button state -- main.c owns the frame loop and event pump.

#pragma once

#include "pnx/pnx.h"

typedef struct
{
	PnxArena arena;

	PnxBall ball;
	PnxFlipper left, right;
	int32_t camera_y;

	bool ball_in_lane; // resting on the plunger, physics frozen, awaiting launch
	pnx_fx launch_charge;

	uint32_t ticks;
	uint32_t drains;
} Game;

void game_init(Game* g);
void game_serve_ball(Game* g);
void game_tick(Game* g);
