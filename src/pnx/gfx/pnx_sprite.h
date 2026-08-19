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

#endif // PNX_USE_SPRITES
