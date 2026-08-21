// Need 4 Pebble -- an OutRun-style pseudo-3D racer. See ../DESIGN.md.
//
// Split as game.c (simulation) / render.c (drawing) / track.c (road geometry + curve
// data, shared by both) / this file (frame loop + event pump + main), the same shape
// examples/pinball uses. Gas/brake are the top-edge shoulder buttons; steering is a
// touch drag or accelerometer tilt (toggled from the pause menu), since both ends of
// the button cluster are already claimed (see DESIGN.md's controls section).

#include "game.h"
#include "render.h"

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	Game* g = (Game*)ctx;

	pnx_input_frame();
	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		pnx_input_event(&ev);
		if (ev.type != PNX_EVENT_BUTTON_DOWN)
			continue;
		// Read off the raw event queue, not pnx_input_pressed(): a toggle applied once
		// per TICK would double-fire on any frame that catches up more than one tick,
		// flipping straight back. One physical press is exactly one queued event,
		// regardless of how many ticks the frame that notices it runs.
		if (ev.button == PNX_BUTTON_BACK)
		{
			if (g->game_over_reason != GAME_OVER_NONE)
				game_restart(g); // BUSTED/TIME_UP: BACK restarts instead of pausing
			else
				game_toggle_pause(g);
		}
		else if (ev.button == PNX_BUTTON_SELECT)
			game_toggle_steer_mode(g); // no-op while driving; see its own comment (game.h)
	}

	// Clamped: a frame arriving after a notification can carry several seconds, and
	// without this the sim fast-forwards through all of it.
	g->accumulator_ms += elapsed_ms;
	const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
	if (g->accumulator_ms > max_ms)
		g->accumulator_ms = max_ms;

	while (g->accumulator_ms >= PNX_TICK_MS)
	{
		g->accumulator_ms -= PNX_TICK_MS;
		game_tick(g);
	}

	render_game(g, target);
}

int main(void)
{
	static Game g;
	if (!game_boot(&g))
		return 1;

	pnx_platform_run(frame, &g);

	game_shutdown(&g);
	return 0;
}
