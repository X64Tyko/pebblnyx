#pragma once

#include "../pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE

#include "../core/pnx_arena.h"
#include <stdbool.h>
#include <stdint.h>

// Shared fixed-slot LRU core for pnx_tile_cache.c and pnx_sprite_cache.c. Internal header,
// not for direct project use. Real (non-inline) functions, defined once in
// pnx_lru_cache_impl.c -- both cache modules link against the same compiled copy instead
// of each silently paying for its own inlined instance of this logic, which is what a
// `static inline` version of this file cost before this became a real .c/.h split
// (~230B duplicated, measured via nm on a real emery build: the two-loop eviction scan in
// find_or_prepare alone was fully inlined into both pnx_sprite_cache_get and
// pnx_tile_cache_get, neither of which needs its own copy).
//
// Slots are FIXED size (the largest unit the caller will ever ask to store), not a
// variable-size pool: that is what makes eviction a real single-entry operation. Evicting
// the LRU entry frees exactly its own slot, which the next miss reuses immediately -- no
// bump pointer, no compaction, no "flush everything" fallback. The cost is real and
// deliberately not hidden: a slot sized to the largest unit wastes the gap between that
// and a smaller one sharing the pool (e.g. a 32px and a 16px atlas tile sharing one tile
// cache both cost a 32px slot). Pick `slot_bytes` per project from the content it actually
// caches, the same way PNX_TILE_CACHE_MAX_TILE_PX already asks a project to.
typedef struct
{
	uint32_t key;
	// Monotonic per-touch stamp (cache-wide counter, never reset by a tick) -- `age` alone
	// cannot break a tie between two entries touched in the SAME tick, and same-tick ties
	// are the common case, not an edge case: a renderer with no dirty-tracking touches its
	// whole visible working set once per tick (pnx_tile_cache.h's own comment), so most
	// eviction decisions happen while several entries all sit at age 0 together. Without
	// this, the age-only scan's `>=` comparison silently evicts the LAST-touched of a tied
	// group instead of the true least-recently-used one.
	uint32_t touch_seq;
	uint8_t age;
	bool occupied;
} PnxLruEntry;

typedef struct
{
	PnxLruEntry* entries;
	uint8_t* pool; // entry_count * slot_bytes
	uint32_t next_touch_seq;
	uint16_t entry_count;
	uint16_t slot_bytes;
	uint8_t max_age;
} PnxLruCache;

// Claims `target_entries` headers and `target_entries * slot_bytes` of pool from `arena`,
// granting less than requested if the arena cannot cover the full ask (same "still works,
// just with more misses" posture pnx_tile_cache_init has always documented). False only if
// not even one entry's worth of both could be granted.
bool pnx_lru_cache_init_impl(PnxLruCache* cache, PnxArena* arena, uint16_t target_entries,
							 uint16_t slot_bytes, uint8_t max_age);

void pnx_lru_cache_tick_impl(PnxLruCache* cache);

void pnx_lru_cache_reset_impl(PnxLruCache* cache);

// Returns the slot for `key` -- an existing one on a hit (`*out_hit = true`, age reset to
// 0), or a freshly claimed one on a miss (`*out_hit = false`): a free slot if one exists,
// else the single entry with the highest age (true LRU eviction, one entry, not a flush).
// NULL only if `cache` has zero entries (init failed or was never called).
uint8_t* pnx_lru_cache_find_or_prepare(PnxLruCache* cache, uint32_t key, bool* out_hit);

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE
