#pragma once

#include "../core/pnx_arena.h"
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

// Shared fixed-slot LRU core for pnx_tile_cache.c and pnx_sprite_cache.c. Internal header,
// not for direct project use.
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
static inline bool pnx_lru_cache_init_impl(PnxLruCache* cache, PnxArena* arena,
										   uint16_t target_entries, uint16_t slot_bytes,
										   uint8_t max_age)
{
	cache->entries		  = NULL;
	cache->pool			  = NULL;
	cache->entry_count	  = 0;
	cache->slot_bytes	  = slot_bytes;
	cache->max_age		  = max_age;
	cache->next_touch_seq = 0;

	if (!arena || target_entries == 0 || slot_bytes == 0)
		return false;

	uint16_t entries	   = target_entries;
	const size_t per_entry = sizeof(PnxLruEntry) + (size_t)slot_bytes;
	while (entries > 0 && (size_t)entries * per_entry > pnx_arena_remaining(arena))
		entries--;
	if (entries == 0)
		return false;

	cache->entries = (PnxLruEntry*)pnx_arena_alloc_hi(arena, (size_t)entries * sizeof(PnxLruEntry), 4);
	cache->pool	   = (uint8_t*)pnx_arena_alloc_hi(arena, (size_t)entries * slot_bytes, 4);
	if (!cache->entries || !cache->pool)
	{
		cache->entries	   = NULL;
		cache->pool		   = NULL;
		cache->entry_count = 0;
		return false;
	}
	memset(cache->entries, 0, (size_t)entries * sizeof(PnxLruEntry));
	cache->entry_count = entries;
	return true;
}

static inline void pnx_lru_cache_tick_impl(PnxLruCache* cache)
{
	for (uint16_t i = 0; i < cache->entry_count; i++)
	{
		PnxLruEntry* e = &cache->entries[i];
		if (!e->occupied)
			continue;
		e->age++;
		if (e->age >= cache->max_age)
			e->occupied = false;
	}
}

static inline void pnx_lru_cache_reset_impl(PnxLruCache* cache)
{
	for (uint16_t i = 0; i < cache->entry_count; i++)
		cache->entries[i].occupied = false;
}

// Returns the slot for `key` -- an existing one on a hit (`*out_hit = true`, age reset to
// 0), or a freshly claimed one on a miss (`*out_hit = false`): a free slot if one exists,
// else the single entry with the highest age (true LRU eviction, one entry, not a flush).
// NULL only if `cache` has zero entries (init failed or was never called).
static inline uint8_t* pnx_lru_cache_find_or_prepare(PnxLruCache* cache, uint32_t key,
													 bool* out_hit)
{
	if (cache->entry_count == 0)
		return NULL;

	for (uint16_t i = 0; i < cache->entry_count; i++)
	{
		PnxLruEntry* e = &cache->entries[i];
		if (e->occupied && e->key == key)
		{
			e->age		 = 0;
			e->touch_seq = cache->next_touch_seq++;
			*out_hit	 = true;
			return cache->pool + (size_t)i * cache->slot_bytes;
		}
	}

	// Highest age wins; among entries tied on age (the common case -- see PnxLruEntry's
	// own comment), lowest touch_seq (touched longest ago in real access order) wins.
	uint16_t target		= 0;
	uint8_t target_age	= 0;
	uint32_t target_seq = 0;
	bool have_target	= false;
	for (uint16_t i = 0; i < cache->entry_count; i++)
	{
		PnxLruEntry* e = &cache->entries[i];
		if (!e->occupied)
		{
			target		= i;
			have_target = true;
			break;
		}
		if (!have_target || e->age > target_age ||
			(e->age == target_age && e->touch_seq < target_seq))
		{
			target		= i;
			target_age	= e->age;
			target_seq	= e->touch_seq;
			have_target = true;
		}
	}

	PnxLruEntry* e = &cache->entries[target];
	e->key		   = key;
	e->age		   = 0;
	e->occupied	   = true;
	e->touch_seq   = cache->next_touch_seq++;
	(void)have_target; // entry_count > 0 above guarantees a target is always found
	*out_hit = false;
	return cache->pool + (size_t)target * cache->slot_bytes;
}
