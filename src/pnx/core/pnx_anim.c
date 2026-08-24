#include "pnx_anim.h"

void pnx_anim_play(PnxAnimState* state, const uint8_t* frames, uint8_t count, uint32_t now_ms)
{
	if (!state || state->frames == frames)
		return;
	state->frames	= frames;
	state->count	= count;
	state->start_ms = now_ms;
}

uint8_t pnx_anim_frame(const PnxAnimState* state, uint8_t fps, const uint8_t* durations,
					   bool loop, uint32_t now_ms)
{
	if (!state || !state->frames || !state->count || !fps)
		return state && state->frames ? state->frames[0] : 0;

	// Elapsed wall-clock time, in whole base-ticks at `fps` -- durations are counted in
	// units of this tick (PnxAnimState's own comment), not milliseconds directly.
	const uint32_t elapsed = now_ms - state->start_ms;
	uint32_t tick		   = (elapsed * fps) / 1000u;

	// Total tick length of the clip. Summed fresh each call rather than cached anywhere:
	// count is always small (an animation clip is a handful of frames), the same "n is
	// small" tradeoff pnx_sprite.c's sprites_sort_by_y comment already makes for this.
	uint32_t total = 0;
	for (uint8_t i = 0; i < state->count; i++)
		total += durations ? durations[i] : 1;
	if (total == 0)
		return state->frames[0];

	if (loop)
	{
		tick %= total;
	}
	else if (tick >= total)
	{
		return state->frames[state->count - 1];
	}

	uint32_t acc = 0;
	for (uint8_t i = 0; i < state->count; i++)
	{
		acc += durations ? durations[i] : 1;
		if (tick < acc)
			return state->frames[i];
	}
	return state->frames[state->count - 1];
}
