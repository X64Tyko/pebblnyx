// Layer compositing: draw-order groups, parallax, and where the HUD sits on screen.
//
// Every pnx game today hand-sequences its own draw order in one function -- tilemap, then
// sprites, then HUD, then whatever overlay is open -- with nothing generic underneath it
// (resonant/src/c/main.c's game_draw is exactly this). This module is that generic thing:
// a small ordered list a game builds once, describing what draws in what order and how
// fast each layer scrolls relative to the camera, so a background can crawl behind a
// foreground that moves at the ordinary 1:1 rate, and a HUD layer never moves at all.
//
// Two kinds of layer. PNX_LAYER_CALLBACK is arbitrary -- a tilemap, a parallax background,
// a battle overlay, the HUD itself. PNX_LAYER_SPRITES draws the subset of ONE shared
// PnxSpriteInstance pool matching one layer id (PNX_SPRITE_LAYER, pnx_sprite.h), sorted
// back-to-front by feet Y the same way pnx_sprites_draw_sorted already does for a flat
// array -- "grounded enemies" and "fliers" are two PNX_LAYER_SPRITES layers with different
// ids into the SAME instance array, not two separate arrays a game has to keep in sync.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_LAYERS

#include "pnx_gfx.h"
#include "pnx_sprite.h" // Depends on PNX_USE_SPRITES the way pnx_collision depends on
						// tilemap's data types -- turning sprites off while a layer still
						// names PNX_LAYER_SPRITES is not a combination worth a #if; the
						// compiler says so plainly (undefined PnxSpriteInstance) if it
						// ever happens.

#define PNX_LAYER_CALLBACK 0
#define PNX_LAYER_SPRITES  1

// 255: ordinary 1:1 world motion, today's only behaviour and every existing game's
// implicit default. 0: fixed to the screen, no scroll at all -- what a HUD layer wants.
// Anything between scales the camera offset down before the layer draws, which is the
// whole of what makes a background crawl slower than the foreground: nothing else about
// drawing it changes.
#define PNX_LAYER_PARALLAX_WORLD  255
#define PNX_LAYER_PARALLAX_SCREEN 0

typedef void (*PnxLayerDrawFn)(void* ctx, PnxTarget* t, const PnxCamera* camera);

typedef struct
{
	uint8_t kind;		  // PNX_LAYER_CALLBACK or PNX_LAYER_SPRITES
	uint8_t parallax_pct; // PNX_LAYER_PARALLAX_* or anything between the two
	union
	{
		PnxLayerDrawFn draw;  // PNX_LAYER_CALLBACK
		uint8_t sprite_layer; // PNX_LAYER_SPRITES: matches PNX_SPRITE_LAYER(instance->flags)
	} as;
} PnxLayer;

// Draws every layer in array order -- painter's algorithm across layers, same as within
// one. `ctx` reaches every PNX_LAYER_CALLBACK layer unchanged, the same convention
// pnx_app.h's hooks already use. `order` is scratch shared across every PNX_LAYER_SPRITES
// layer in the pass, not state, the same as pnx_sprites_draw_sorted's own `order`.
void pnx_layers_draw(void* ctx, const PnxLayer* layers, uint8_t layer_count,
					 const PnxSpriteInstance* instances, uint8_t instance_count, uint8_t* order,
					 PnxTarget* target, const PnxCamera* camera);

#endif // PNX_USE_LAYERS
