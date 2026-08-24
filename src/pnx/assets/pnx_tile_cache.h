// Decoded-tile LRU cache for pnx_bitplane.c's random-access format -- see pnx_config.h's
// PNX_TILE_CACHE_SLOTS comment for why this caches DECODED pixels, not just compressed
// bytes: this engine's renderer has no dirty-tracking (pnx_tilemap_draw_layer redraws
// every visible tile every frame), so a compressed-only cache would still pay full decode
// cost on every blit regardless of cache hits. This cache pays decode once per MISS.
//
// Standalone, like pnx_bitplane.c: not wired into pnx_map_load/pnx_tilemap_draw_layer yet.
// Deliberately decoupled from resource-reading and the atlas blob format (neither of which
// yet has a per-tile-addressable on-disk layout to fetch from) -- the caller supplies a
// PnxTileFetchFn that turns (atlas_asset, tile_index) into compressed bytes however it
// knows how to; this module only owns caching, aging and decode.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_BITPLANE_COMPRESS

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Bytes one cached (packed 4bpp) tile occupies.
#define PNX_TILE_CACHE_SLOT_BYTES (((size_t)PNX_TILE_CACHE_TILE_PX * PNX_TILE_CACHE_TILE_PX + 1) / 2)

// Fetches tile `tile_index` of atlas asset `atlas_asset`'s compressed bytes into
// `scratch` (caller-owned, `scratch_cap` bytes -- sized by pnx_tile_cache_get's own
// caller, not this module, since it doesn't know the on-disk format either). Returns the
// compressed length, or 0 on failure (unknown tile, short read, whatever the real
// implementation's failure mode is). Called by pnx_tile_cache_get ONLY on a cache miss.
typedef size_t (*PnxTileFetchFn)(void* ctx, uint16_t atlas_asset, uint16_t tile_index,
								 uint8_t* scratch, size_t scratch_cap);

// Returns a pointer to `tile_index` of `atlas_asset`'s DECODED, packed-4bpp pixels
// (PNX_TILE_CACHE_SLOT_BYTES bytes) -- from cache on a hit (no decode work at all), or
// freshly fetched+decoded into a slot on a miss (evicting the oldest-aged occupied slot
// if none are free; see pnx_config.h, evicting by age IS LRU here since age already means
// "frames since last drawn"). Returns NULL if fetch or decode failed. The returned
// pointer is only valid until the next pnx_tile_cache_get call that evicts this slot --
// same "read it now, don't hold the pointer across a frame boundary" contract every other
// pnx cache-shaped API already has (PnxMap's own atlas pool, PnxAtlas.pixels).
const uint8_t* pnx_tile_cache_get(uint16_t atlas_asset, uint16_t tile_index,
								  PnxTileFetchFn fetch, void* fetch_ctx);

// Call once per frame: ages every occupied slot by one, releasing (freeing, not evicting
// -- the tile is simply gone, not replaced by anything) any slot that crosses
// PNX_TILE_CACHE_MAX_AGE. Does NOT touch slots pnx_tile_cache_get already reset this
// frame (a slot's age starts at 0 the tick after it's (re)used, not the same tick).
void pnx_tile_cache_tick(void);

// Frees every slot unconditionally -- a scene/map change, where every cached tile belongs
// to content that's about to stop existing. Distinct from letting slots age out: those are
// still valid content just not currently on screen, this is "none of it is valid anymore."
void pnx_tile_cache_reset(void);

#endif // PNX_USE_BITPLANE_COMPRESS
