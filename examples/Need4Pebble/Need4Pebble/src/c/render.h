// Draws a Game to a PnxTarget: sky, road, car. No simulation, no input -- game.c owns
// the tick, main.c owns the frame loop.

#pragma once

#include "game.h"
#include "pnx/pnx.h"

void render_game(const Game* g, PnxTarget* target);
