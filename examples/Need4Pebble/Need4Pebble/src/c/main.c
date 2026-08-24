// Need 4 Pebble -- an OutRun-style pseudo-3D racer. See ../DESIGN.md.
//
// Split as game.c (simulation) / render.c (drawing) / track.c (road geometry + curve
// data, shared by both) / this file (app states + main). Gas/brake are the top-edge
// shoulder buttons; steering is a touch drag or accelerometer tilt (toggled from the
// pause menu), since both ends of the button cluster are already claimed (see
// DESIGN.md's controls section).
//
// Four pnx_app states (src/pnx/app/pnx_app.h), one per screen this game has: title,
// driving, paused, game_over. The app stack itself now owns what the old hand-rolled frame()
// used to do by hand -- the fixed-step accumulator, the event pump, and "nothing
// simulates while paused/game-over" (that used to be two flags game_tick checked on its
// own top; now it's a structural property of driving's tick() only ever running while
// driving is the stack's uncovered top).

// This game's own stack never nests past depth 2 (driving -> paused, or driving ->
// game_over -- never both at once), against pnx_app.h's own default of 4. Must come
// before game.h's own transitive include of pnx_app.h (via pnx/pnx.h) to take effect --
// every byte matters on aplite's 24KB app image.
#define PNX_APP_MAX_DEPTH 2

#include "game.h"
#include "render.h"

// Forward-declared: driving's own input()/tick() push paused_ops/game_over_ops, but
// each PnxAppOps is defined right after the state that owns it (matching quickstart's
// declare-beside-its-functions layout) -- so driving's callbacks, written first, need
// these names in scope before the later structs exist.
static const PnxAppOps title_ops;
static const PnxAppOps driving_ops;
static const PnxAppOps paused_ops;
static const PnxAppOps game_over_ops;

// Shared by every state's own input(): pnx_input_frame() must run exactly once per
// rendered frame before any event is fed to pnx_input_event() (its own doc comment,
// pnx_input.h), regardless of which state is on top -- so each state calls this rather
// than duplicating the preamble with a subtly different (and easy to get wrong) copy.
// Draws from pnx_app_poll_event, not pnx_platform_poll_event directly -- pnx_app_frame
// already consumed FOCUS_LOST/FOCUS_GAINED for itself before a state's input() ever runs.
static void poll_input(void)
{
	pnx_input_frame();
	PnxEvent ev;
	while (pnx_app_poll_event(&ev))
		pnx_input_event(&ev);
}

static void title_input(void* ctx)
{
	poll_input();
	// Replace, not push: title is never returned to (no BACK-to-title anywhere in this
	// game), so it should exit outright rather than sit suspended under driving forever.
	if (pnx_input_pressed(PNX_BUTTON_SELECT))
		pnx_app_replace(&driving_ops);
	(void)ctx;
}

static void title_draw(void* ctx, PnxTarget* target)
{
	render_title((const Game*)ctx, target);
}

static const PnxAppOps title_ops = {
	.input = title_input,
	.draw  = title_draw,
};

static void driving_input(void* ctx)
{
	Game* g = (Game*)ctx;
	poll_input();
	if (pnx_input_pressed(PNX_BUTTON_BACK))
		pnx_app_push(&paused_ops);
	(void)g;
}

static void driving_tick(void* ctx)
{
	Game* g = (Game*)ctx;
	game_tick(g);
	// check_busted/tick_clock (inside game_tick) are what actually set this -- pushing
	// game_over here, right after, is safe specifically because pnx_app_frame re-reads
	// the stack's top after every tick, handing any ticks still owed this frame to
	// whoever is now on top (tests/test_app.c's own documented behaviour) rather than
	// continuing to feed a driving state that just got covered.
	if (g->game_over_reason != GAME_OVER_NONE)
		pnx_app_push(&game_over_ops);
}

static void driving_draw(void* ctx, PnxTarget* target)
{
	render_game((const Game*)ctx, target);
}

static const PnxAppOps driving_ops = {
	.input = driving_input,
	.tick  = driving_tick,
	.draw  = driving_draw,
};

static void paused_input(void* ctx)
{
	Game* g = (Game*)ctx;
	poll_input();
	if (pnx_input_pressed(PNX_BUTTON_BACK))
		pnx_app_pop(); // resumes driving
	else if (pnx_input_pressed(PNX_BUTTON_SELECT))
		game_toggle_steer_mode(g);
}

static void paused_draw(void* ctx, PnxTarget* target)
{
	render_paused((const Game*)ctx, target);
}

static const PnxAppOps paused_ops = {
	.input = paused_input,
	.draw  = paused_draw,
};

static void game_over_input(void* ctx)
{
	Game* g = (Game*)ctx;
	poll_input();
	if (pnx_input_pressed(PNX_BUTTON_BACK))
	{
		game_restart(g);
		pnx_app_pop(); // resumes driving, now freshly reset
	}
}

static void game_over_draw(void* ctx, PnxTarget* target)
{
	render_game_over((const Game*)ctx, target);
}

static const PnxAppOps game_over_ops = {
	.input = game_over_input,
	.draw  = game_over_draw,
};

int main(void)
{
	static Game g;
	if (!game_boot(&g))
		return 1;

	pnx_app_init(&g);
	pnx_app_push(&title_ops);

	// Flushed once, immediately, rather than on a button press the way lzssbench/
	// bitplanebench do it (those are dedicated benches with nothing else running yet) --
	// this project is already mid-gameplay from frame one, and periodic fps logging is
	// only useful if it starts right away.
	pnx_diag_flush();

	pnx_platform_run(pnx_app_frame, &g);

	game_shutdown(&g);
	return 0;
}
