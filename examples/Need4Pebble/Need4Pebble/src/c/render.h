// Draws a Game to a PnxTarget: sky, road, car. No simulation, no input -- game.c owns
// the tick, main.c's app states (pnx_app) own the frame loop.

#pragma once

#include "game.h"
#include "pnx/pnx.h"

// Pure scene draw -- no menu/overlay content. The driving app state's own draw() (main.c)
// calls this directly; paused/game_over call render_paused/render_game_over instead
// (same scene, plus their own overlay on top).
void render_game(const Game* g, PnxTarget* target);
void render_paused(const Game* g, PnxTarget* target);
void render_game_over(const Game* g, PnxTarget* target);
void render_title(const Game* g, PnxTarget* target);
