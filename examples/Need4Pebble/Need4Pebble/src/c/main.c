// Need 4 Pebble
//
// The fixed-timestep loop below is the shape every pebblnyx game takes. Frames arrive at
// ~37.33ms at best and carry seconds while the app is covered, so real time is
// accumulated and consumed in fixed ticks rather than trusted directly.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define ARENA_BYTES (24 * 1024)

typedef struct
{
	PnxArena arena;
	uint32_t accumulator_ms;
	uint32_t ticks;
} Game;

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	Game* g = (Game*)ctx;

	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_BACK)
		{
			pnx_platform_quit();
		}
	}

	// Clamped: a frame arriving after a notification can carry several seconds, and
	// without this the sim fast-forwards through all of it.
	g->accumulator_ms += elapsed_ms;
	const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
	if (g->accumulator_ms > max_ms) g->accumulator_ms = max_ms;

	while (g->accumulator_ms >= PNX_TICK_MS)
	{
		g->accumulator_ms -= PNX_TICK_MS;
		g->ticks++;
	}

	pnx_gfx_clear(target, 0xC0);
}

int main(void)
{
	static Game g;
	memset(&g, 0, sizeof(g));

	if (!pnx_arena_init(&g.arena, "scene", ARENA_BYTES, 4))
	{
		pnx_platform_log("arena init failed");
		return 1;
	}

	pnx_platform_run(frame, &g);
	pnx_arena_destroy(&g.arena);
	return 0;
}
