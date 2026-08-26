// Decoded-sprite-frame LRU cache for bitplane-compressed sprites -- pnx_tile_cache.c's
// sibling, same fixed-slot design (see that module's own comment for why a fixed slot,
// not a variable-size pool, is what makes single-entry LRU eviction possible at all).
// Slots are sized to PNX_SPRITE_CACHE_MAX_UNIT_PX (pnx_config.h), the largest frame pixel
// count any sprite this project loads reaches -- pnx_sprite_load refuses a sprite whose
// largest reachable frame exceeds it.

#pragma once

#include "../core/pnx_arena.h"
#include "../pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE

#include "pnx_assets.h" // PnxSprite, PnxSpriteFrame

#include <stdbool.h>
#include <stdint.h>

// Claims this cache's own private arena sub-region -- same contract as
// pnx_tile_cache_init (granted count may be less than requested; still works, just with
// more misses). Call once, after a project's other fixed-need arena claims.
bool pnx_sprite_cache_init(PnxArena* arena, uint16_t target_entries);

// Fills `out` with `frame`'s decoded pixels (and its w/h/origin/flags, from `sprite`'s
// own frame_meta) -- from cache on a hit, or freshly fetched+decoded into a reclaimed LRU
// slot on a miss, exactly as pnx_tile_cache_get does. The cache key is `(sprite's
// asset_id, frame's own frame_meta offset)`, not the frame index -- two frame indices
// sharing one offset (already deduplicated by the pipeline) share one cache entry
// instead of decoding/storing the same bytes twice. `out->pixels` is only valid until
// this frame's own slot is evicted by a later call for a DIFFERENT frame -- same contract
// as pnx_tile_cache_get/PnxAtlas.pixels. False if `frame` is out of range or decode fails.
bool pnx_sprite_cache_get(const PnxSprite* sprite, uint8_t frame, PnxSpriteFrame* out);

// Call once per tick: ages every occupied entry, mirrors pnx_tile_cache_tick exactly
// (separate age counter from the tile cache's own -- PNX_SPRITE_CACHE_MAX_AGE, pnx_config.h).
void pnx_sprite_cache_tick(void);

// Frees every entry -- mirrors pnx_tile_cache_reset.
void pnx_sprite_cache_reset(void);

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE
