#include "pnx_lru_cache_impl.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE || PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN

#include <string.h>

bool pnx_lru_cache_init_impl(PnxLruCache* cache, PnxArena* arena, uint16_t target_entries,
							 uint16_t slot_bytes, uint8_t max_age)
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

void pnx_lru_cache_tick_impl(PnxLruCache* cache)
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

void pnx_lru_cache_reset_impl(PnxLruCache* cache)
{
	for (uint16_t i = 0; i < cache->entry_count; i++)
		cache->entries[i].occupied = false;
}

uint8_t* pnx_lru_cache_find_or_prepare(PnxLruCache* cache, uint32_t key, bool* out_hit)
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

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE || PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN
