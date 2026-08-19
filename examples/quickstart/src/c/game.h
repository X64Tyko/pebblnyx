// The QUICKSTART.md walkthrough's own game struct. See main.c for everything that
// touches it.

#pragma once

#include "pnx/pnx.h"
#include "assets_gen.h"

#define MAX_SPRITES 1
#define HERO		0

typedef struct
{
	PnxArena persistent, scene;
	PnxCamera camera;

	PnxSpriteInstance sprites[MAX_SPRITES];
	uint8_t order[MAX_SPRITES];
	uint8_t sprite_count;

	int32_t hero_tx, hero_ty; // tiles
	uint8_t walk_phase;

	int8_t want_dx, want_dy; // set by field_input, consumed by field_tick
} Game;

extern const PnxAppOps field_state;

uint8_t game_boot(Game* g);
void game_shutdown(Game* g);
