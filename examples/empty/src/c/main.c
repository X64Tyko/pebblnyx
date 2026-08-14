// The smallest complete pebblnyx game.
//
// It draws a moving bar and nothing else. Its purpose is not to be interesting -- it is
// the reference for the shape every game takes, and the baseline the size report is
// measured against. Whatever this build costs is the framework's fixed overhead; a real
// game's budget is 65,535 bytes minus this number.
//
// The fixed-timestep loop below is the part worth copying.

#include "pnx/pnx.h"

#include <string.h>

typedef struct
{
	PnxArena arena;

	uint32_t accumulator_ms; // leftover real time not yet consumed by a tick
	uint32_t ticks;			 // total sim ticks, the sim's own clock
	int32_t bar_x;
	int32_t bar_dir;

	bool flushed_startup_logs;
	uint32_t started_ms;
} Game;

// --------------------------------------------------------------------------- sim

// One fixed step. Takes no elapsed time on purpose: a fixed timestep is what makes the
// simulation reproducible, which replay and any deterministic save depend on.
static void sim_tick(Game* g)
{
	g->ticks++;

	g->bar_x += g->bar_dir * 3;
	if (g->bar_x < 0)
	{
		g->bar_x   = 0;
		g->bar_dir = 1;
	}
	if (g->bar_x > 180)
	{
		g->bar_x   = 180;
		g->bar_dir = -1;
	}
}

// ------------------------------------------------------------------------ render

static void render(Game* g, PnxTarget* target)
{
	const int16_t h = pnx_target_height(target);

	for (int16_t y = 0; y < h; y++)
	{
		PnxRow row = pnx_target_row(target, y);
		if (!row.data)
			continue;

		const int16_t span = (int16_t)(row.max_x - row.min_x + 1);
		memset(row.data + row.min_x, 0, (size_t)span);

		if (y >= 100 && y < 128)
		{
			for (int16_t x = 0; x < 20; x++)
			{
				const int16_t px = (int16_t)(g->bar_x + x);
				if (px >= row.min_x && px <= row.max_x)
				{
					row.data[px] = 0xFF; // opaque white in ARGB2222
				}
			}
		}
	}
}

// -------------------------------------------------------------------------- frame

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	Game* g					  = (Game*)ctx;
	const uint32_t work_start = pnx_platform_now_ms();

	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_SELECT)
		{
			// Deferred logs are flushed from a button because logs emitted during init are
			// dropped before the log stream attaches -- see core/pnx_diag.h.
			pnx_diag_flush();
		}
	}

	// Clamped accumulator. The clamp is doing real work: while a notification covers the
	// app it is throttled to roughly 0.4fps, so the next frame arrives carrying seconds
	// of elapsed time. Without the clamp the sim fast-forwards through all of it in one
	// frame, which looks like the game teleporting.
	g->accumulator_ms += elapsed_ms;

	const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
	if (g->accumulator_ms > max_ms)
		g->accumulator_ms = max_ms;

	while (g->accumulator_ms >= PNX_TICK_MS)
	{
		g->accumulator_ms -= PNX_TICK_MS;
		sim_tick(g);
	}

	render(g, target);

	// Flush automatically a few seconds in, so the startup diagnostics arrive even if
	// nobody presses anything.
	if (!g->flushed_startup_logs && pnx_platform_now_ms() - g->started_ms > 3000)
	{
		g->flushed_startup_logs = true;
		pnx_diag_flush();
	}

	pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
}

// --------------------------------------------------------------------------- main

int main(void)
{
	static Game g;
	memset(&g, 0, sizeof(g));
	g.bar_dir	 = 1;
	g.started_ms = pnx_platform_now_ms();

	// Heap, not static: static allocation shares the same 64KB uint16 ceiling as code,
	// while the heap has the rest of the 128KB. Anything of size belongs here.
	if (!pnx_arena_init(&g.arena, "game", 8 * 1024, 4))
	{
		pnx_platform_log("arena init failed");
		return 1;
	}

	pnx_log("pnx empty: arena %u bytes, touch=%d", (unsigned)g.arena.capacity,
			(int)pnx_platform_has_touch());

	pnx_platform_run(frame, &g);

	pnx_arena_destroy(&g.arena);
	return 0;
}
