// Decoded-tile LRU cache for pnx_bitplane.c's random-access format -- see pnx_config.h's
// PNX_COMPRESS_BITPLANE comment for why this caches DECODED pixels, not just compressed
// bytes: this engine's renderer has no dirty-tracking (pnx_tilemap_draw_layer redraws
// every visible tile every frame), so a compressed-only cache would still pay full decode
// cost on every blit regardless of cache hits. This cache pays decode once per MISS.
//
// Arena-backed, not static: a project calls pnx_tile_cache_init once (after its other,
// fixed-need loads have already claimed their own arena space) with a TARGET entry count
// -- what it actually gets may be less, on a platform too tight to grant the full target,
// and the cache still works correctly either way, just with more misses.
//
// Slots are FIXED size, sized to `max_tile_px` (the largest tile any atlas this project
// caches has) -- see pnx_lru_cache_impl.h's own comment for why that is what makes a real
// single-entry LRU eviction possible: no compacting allocator, no whole-cache flush under
// pressure, evicting one entry frees exactly one reusable slot. Serves any atlas via one
// shared instance -- no per-atlas tile-size configuration, tile_px is passed at each
// pnx_tile_cache_get call, not fixed at compile time (only the SLOT size is, to the
// largest any call will ever pass).

#pragma once

#include "../core/pnx_arena.h"
#include "../pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE || PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Sizes the stack scratch buffer pnx_tile_cache_get's own fetch+decode step uses, and
// (via pnx_tile_cache_init's own `max_tile_px` argument) the cache's fixed slot size --
// must be >= the largest tile_px any caller ever passes.
#ifndef PNX_TILE_CACHE_MAX_TILE_PX
#define PNX_TILE_CACHE_MAX_TILE_PX 32
#endif

// Fetches tile `tile_index` of atlas asset `atlas_asset`'s compressed bytes into
// `scratch` (caller-owned, `scratch_cap` bytes) directly from ROM -- the compressed
// stream is never bulk-resident; only this one tile's bytes are read. Returns the
// compressed length, or 0 on failure (unknown tile, short read). Called by
// pnx_tile_cache_get ONLY on a cache miss. `ctx` is the loaded PnxAtlas the tile belongs
// to (pnx_bitplane_atlas_fetch, pnx_assets.c) -- passed back exactly as given to
// pnx_tile_cache_get, opaque to this module.
typedef size_t (*PnxTileFetchFn)(void* ctx, uint16_t atlas_asset, uint16_t tile_index,
								 uint8_t* scratch, size_t scratch_cap);

// Claims this cache's own private arena sub-region: up to `target_entries` slots, each
// `(max_tile_px*max_tile_px+1)/2` bytes (packed 4bpp). Granted count may be less than
// requested; still works, just with more misses. Call once, after a project's other
// fixed-need arena claims. False only if not even one slot could be granted.
bool pnx_tile_cache_init(PnxArena* arena, uint16_t target_entries, uint8_t max_tile_px);

// Returns a pointer to `tile_index` of `atlas_asset`'s DECODED, packed-4bpp pixels
// (`(tile_px*tile_px+1)/2` bytes) -- from cache on a hit (no decode work at all), or
// freshly fetched+decoded into a reclaimed LRU slot on a miss. `tile_px` must not exceed
// the `max_tile_px` this cache was initialised with. Returns NULL if fetch or decode
// failed. The returned pointer is only valid until this tile's own slot is evicted by a
// later pnx_tile_cache_get call for a DIFFERENT tile -- same "read it now, don't hold the
// pointer across a frame boundary" contract every other pnx cache-shaped API already has
// (PnxMap's own atlas pool, PnxAtlas.pixels).
const uint8_t* pnx_tile_cache_get(uint16_t atlas_asset, uint16_t tile_index, uint8_t tile_px,
								  PnxTileFetchFn fetch, void* fetch_ctx);

// Call once per tick: ages every occupied entry by one, releasing (freeing, not evicting
// -- the tile is simply gone, not replaced by anything) any entry that crosses
// PNX_TILE_CACHE_MAX_AGE. Does NOT touch entries pnx_tile_cache_get already reset this
// tick (an entry's age starts at 0 the tick after it's (re)used, not the same tick).
void pnx_tile_cache_tick(void);

// Frees every entry: a scene/map change, where every cached tile belongs to content
// that's about to stop existing. Distinct from letting entries age out: those are still
// valid content just not currently on screen, this is "none of it is valid any more."
void pnx_tile_cache_reset(void);

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE || PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN
