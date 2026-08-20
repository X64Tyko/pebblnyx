// Sprite rendering with depth sorting.
//
// Sprites are feet-anchored: a position names the tile a character stands on, not the
// top-left of its art. A 16x24 sprite on 16px tiles overhangs upward by 8px, and having
// every caller subtract that by hand is how sprites end up half a tile out of place.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_SPRITES

#include "pnx_gfx.h"

// One drawable. Kept small and flat: this is the struct a game will hold hundreds of,
// and the probes measured layout as a wash at every scale that fits in 128KB, so the
// deciding factor is footprint.
typedef struct
{
	int32_t x, y;	// world pixels, at the FEET
	uint8_t sprite; // index into the scene's sprites
	uint8_t frame;
	uint8_t palette; // slot; PNX_SPRITE_PALETTE_DEFAULT for the asset's own
	uint8_t flags;
} PnxSpriteInstance;

#define PNX_SPRITE_MIRROR		   0x01
#define PNX_SPRITE_HIDDEN		   0x02
#define PNX_SPRITE_PALETTE_DEFAULT 0xFF

// A layer id lives in the top 4 bits of `flags` -- MIRROR/HIDDEN only use the bottom 2,
// and pnx_layer.h's PNX_LAYER_SPRITES kind reads this to pick an instance's layer out of
// a shared pool, at zero cost to the struct: pnx_sprite.h's own comment already calls out
// footprint, not layout, as what a game holding hundreds of these should be sized for.
// Up to 16 layers (0-15) for free.
#define PNX_SPRITE_LAYER_SHIFT	4
#define PNX_SPRITE_LAYER(flags) ((uint8_t)((flags) >> PNX_SPRITE_LAYER_SHIFT))
// The ceiling that shift implies, exposed so game code has a symbolic bound instead of a
// hardcoded 16 -- the framework does not prescribe what a sprite layer id MEANS (M13,
// alongside PNX_MAP_MAX_LAYERS, pnx_assets.h), only how many can exist.
#define PNX_SPRITE_LAYER_COUNT 16

void pnx_sprite_draw(const PnxSprite* sprite, PnxTarget* target, const PnxCamera* camera,
					 int32_t wx, int32_t wy, uint8_t frame, const PnxPalette* palette,
					 bool mirror);

// Draws instances back-to-front by feet Y, so a character lower on screen occludes one
// behind it. Sorts an index array rather than the instances, because moving 12-byte
// structs to satisfy the painter is pure waste when a byte index does.
//
// `order` must have room for `count` entries; it is scratch, not state.
void pnx_sprites_draw_sorted(const PnxSpriteInstance* instances, uint8_t count, uint8_t* order,
							 PnxTarget* target, const PnxCamera* camera);

// Same, restricted to instances whose PNX_SPRITE_LAYER matches `layer` -- "grounded
// enemies" and "fliers" are two calls against the SAME instance array with different
// layer ids, not two arrays to keep in sync. pnx_layer.h's PNX_LAYER_SPRITES layers call
// this; a single-layer game has no reason to and keeps calling pnx_sprites_draw_sorted.
void pnx_sprites_draw_layer(const PnxSpriteInstance* instances, uint8_t count, uint8_t* order,
							PnxTarget* target, const PnxCamera* camera, uint8_t layer);

// ------------------------------------------------------------------------ named clips
//
// A generated `[sprite.anim]` clip (tools/pnx_assets.py) resolves at build time to a
// `static const uint8_t NAME_CLIP_FRAMES[]` plus `_COUNT`/`_FPS`/`_LOOP`/`_DURATIONS`
// #defines -- no blob, no runtime lookup by name, the same handle convention every other
// generated asset id already follows. This is the playback state and the two calls a game
// threads those through.
//
// Wall-clock, not fixed-tick: an authored fps is a real-time rate the way a music
// sequencer's row timing is (pnx_music_update's own comment, audio/pnx_music.h -- "against
// the wall clock ... because frames are not evenly spaced"), not a simulation unit the way
// PNX_TICK_MS is. pnx_anim_frame is a pure function of elapsed time since pnx_anim_play, so
// a large gap (the app was covered) just resolves further into/past the clip -- no
// clamping needed the way a fixed-step accumulator would.
typedef struct
{
	const uint8_t* frames; // NULL until the first pnx_anim_play
	uint8_t count;
	uint32_t start_ms; // pnx_platform_now_ms() when this clip started
} PnxAnimState;

// Switches `state` to a new clip -- but only if `frames` differs from what is already
// playing, so calling this every frame with "whatever should be playing right now" (the
// ordinary usage: re-deciding the current clip from game state each frame) does not
// restart the loop on every call.
void pnx_anim_play(PnxAnimState* state, const uint8_t* frames, uint8_t count, uint32_t now_ms);

// The resolved SPRITE FRAME INDEX for `state` at `now_ms` -- not pixels; assign straight
// to PnxSpriteInstance.frame. `durations` is a generated `NAME_CLIP_DURATIONS` array (one
// entry per frame, in units of `fps`'s own tick -- `[1,2,1,2]` at fps=8 means frames 2 and
// 4 hold twice as long as 1 and 3) or NULL for equal timing (the generated header emits
// `#define NAME_CLIP_DURATIONS NULL` when a clip's `durations` were not authored, so a
// caller always passes `NAME_CLIP_DURATIONS` without needing to know which case it is).
// Wraps through the clip if `loop`, else holds the last frame once played through once.
uint8_t pnx_anim_frame(const PnxAnimState* state, uint8_t fps, const uint8_t* durations,
					   bool loop, uint32_t now_ms);

#endif // PNX_USE_SPRITES
