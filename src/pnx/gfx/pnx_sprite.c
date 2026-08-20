#include "pnx_sprite.h"

#if PNX_USE_SPRITES

#include "../assets/pnx_assets.h"

void pnx_sprite_draw(const PnxSprite* sprite, PnxTarget* target, const PnxCamera* camera,
					 int32_t wx, int32_t wy, uint8_t frame, const PnxPalette* palette,
					 bool mirror)
{
	if (!sprite || frame >= sprite->frame_count)
		return;

	if (!palette)
		palette = pnx_sprite_frame_palette(sprite, frame);

	// Frames are not all one size (a tightly packed sheet's poses rarely are), so the
	// anchor is per-frame too: origin_x/origin_y name where THIS frame's own root sits,
	// rather than every frame sharing one w/2,h.
	PnxSpriteFrame f;
	pnx_sprite_frame_get(sprite, frame, &f);
	pnx_blit_4bpp(target, f.pixels, palette, wx - camera->x - f.origin_x,
				  wy - camera->y - f.origin_y, f.w, f.h, mirror);
}

// Builds `order` from every visible instance (every visible instance on `layer`, if
// `filter_layer` is set), then insertion-sorts it by feet Y. Shared by the whole-array and
// single-layer draw entry points below, so there is exactly one sort to get right rather
// than two copies that could drift.
//
// Insertion sort: n is small (a screen holds a handful of characters) and the order is
// nearly sorted frame to frame, which is the case insertion sort is best at and quicksort
// is worst at.
static uint8_t sprites_sort_by_y(const PnxSpriteInstance* instances, uint8_t count,
								 uint8_t* order, bool filter_layer, uint8_t layer)
{
	uint8_t n = 0;
	for (uint8_t i = 0; i < count; i++)
	{
		if (instances[i].flags & PNX_SPRITE_HIDDEN)
			continue;
		if (filter_layer && PNX_SPRITE_LAYER(instances[i].flags) != layer)
			continue;
		order[n++] = i;
	}

	for (uint8_t i = 1; i < n; i++)
	{
		const uint8_t key = order[i];
		int16_t j		  = (int16_t)i - 1;
		while (j >= 0 && instances[order[j]].y > instances[key].y)
		{
			order[j + 1] = order[j];
			j--;
		}
		order[j + 1] = key;
	}
	return n;
}

static void sprites_draw_ordered(const PnxSpriteInstance* instances, const uint8_t* order,
								 uint8_t n, PnxTarget* target, const PnxCamera* camera)
{
	for (uint8_t k = 0; k < n; k++)
	{
		const PnxSpriteInstance* s = &instances[order[k]];
		const PnxSprite* asset	   = pnx_scene_sprite(s->sprite);
		if (!asset)
			continue;

		const PnxPalette* pal =
			(s->palette == PNX_SPRITE_PALETTE_DEFAULT) ? NULL : pnx_palette(s->palette);

		pnx_sprite_draw(asset, target, camera, s->x, s->y, s->frame, pal,
						(s->flags & PNX_SPRITE_MIRROR) != 0);
	}
}

void pnx_sprites_draw_sorted(const PnxSpriteInstance* instances, uint8_t count, uint8_t* order,
							 PnxTarget* target, const PnxCamera* camera)
{
	if (!instances || !order)
		return;

	const uint8_t n = sprites_sort_by_y(instances, count, order, false, 0);
	sprites_draw_ordered(instances, order, n, target, camera);
}

void pnx_sprites_draw_layer(const PnxSpriteInstance* instances, uint8_t count, uint8_t* order,
							PnxTarget* target, const PnxCamera* camera, uint8_t layer)
{
	if (!instances || !order)
		return;

	const uint8_t n = sprites_sort_by_y(instances, count, order, true, layer);
	sprites_draw_ordered(instances, order, n, target, camera);
}

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
	// small" tradeoff sprites_sort_by_y's own comment already makes for this file.
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

#endif // PNX_USE_SPRITES
