// Pebble Pinbeasts -- v1. One table, placeholder rectangles, no capture mechanic yet.
// See ../README.md for the control scheme and why it's landscape + scrolling.
//
// Split as game.c (simulation) / render.c (drawing) / table.h (shared data) / this file
// (frame loop + event pump + main). Keep growing that way rather than back into one file.

#include "game.h"
#include "render.h"

typedef struct
{
	Game game;
	uint32_t accumulator_ms;
	bool flushed_startup_logs;
	uint32_t started_ms;
} App;

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	App* app				  = (App*)ctx;
	const uint32_t work_start = pnx_platform_now_ms();

	pnx_input_frame();
	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		pnx_input_event(&ev);
		if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_BACK)
			pnx_diag_flush(); // BACK: pause/menu later, diagnostic flush for now
	}

	app->accumulator_ms += elapsed_ms;
	const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
	if (app->accumulator_ms > max_ms)
		app->accumulator_ms = max_ms;

	while (app->accumulator_ms >= PNX_TICK_MS)
	{
		app->accumulator_ms -= PNX_TICK_MS;
		game_tick(&app->game);
	}

	render_game(&app->game, target);

	if (!app->flushed_startup_logs && pnx_platform_now_ms() - app->started_ms > 3000)
	{
		app->flushed_startup_logs = true;
		pnx_diag_flush();
	}

	pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
}

int main(void)
{
	static App app;
	app.started_ms = pnx_platform_now_ms();
	game_init(&app.game);

	if (!pnx_arena_init_max(&app.game.arena, "game", PNX_ARENA_HEAP_RESERVE, 4))
	{
		pnx_platform_log("arena init failed");
		return 1;
	}

	pnx_input_init(PNX_ORIENT_BUTTONS_BOTTOM);
	pnx_platform_set_screen_lock(true); // backlight only, see platform/pnx_platform.h

	pnx_log("pinball: arena %u bytes, DOWN=left flipper UP=right flipper SELECT=launch",
			(unsigned)app.game.arena.capacity);

	pnx_platform_run(frame, &app);

	pnx_arena_destroy(&app.game.arena);
	return 0;
}
