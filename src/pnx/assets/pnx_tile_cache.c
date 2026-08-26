#include "pnx_tile_cache.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE

#include "pnx_bitplane.h"
#include "pnx_lru_cache_impl.h"

static PnxLruCache s_cache;

static uint32_t make_key(uint16_t atlas_asset, uint16_t tile_index)
{
	return ((uint32_t)atlas_asset << 16) | tile_index;
}

bool pnx_tile_cache_init(PnxArena* arena, uint16_t target_entries, uint8_t max_tile_px)
{
	const size_t slot_bytes = PNX_BITPLANE_PACKED_BYTES((size_t)max_tile_px * max_tile_px);
	return pnx_lru_cache_init_impl(&s_cache, arena, target_entries, (uint16_t)slot_bytes,
								   PNX_TILE_CACHE_MAX_AGE);
}

const uint8_t* pnx_tile_cache_get(uint16_t atlas_asset, uint16_t tile_index, uint8_t tile_px,
								  PnxTileFetchFn fetch, void* fetch_ctx)
{
	if (!fetch)
		return NULL;

	const uint32_t key = make_key(atlas_asset, tile_index);
	bool hit;
	uint8_t* dst = pnx_lru_cache_find_or_prepare(&s_cache, key, &hit);
	if (!dst)
		return NULL;
	if (hit)
		return dst;

	uint8_t compressed[1 + PNX_BITPLANE_PACKED_BYTES((size_t)PNX_TILE_CACHE_MAX_TILE_PX * PNX_TILE_CACHE_MAX_TILE_PX)];
	const size_t clen = fetch(fetch_ctx, atlas_asset, tile_index, compressed, sizeof(compressed));
	if (clen == 0)
		return NULL;

	uint8_t decode_scratch[(size_t)PNX_TILE_CACHE_MAX_TILE_PX * PNX_TILE_CACHE_MAX_TILE_PX];
	const uint16_t n = (uint16_t)((uint32_t)tile_px * tile_px);
	if (!pnx_bitplane_decode(compressed, clen, dst, decode_scratch, n))
		return NULL;

	return dst;
}

void pnx_tile_cache_tick(void)
{
	pnx_lru_cache_tick_impl(&s_cache);
}

void pnx_tile_cache_reset(void)
{
	pnx_lru_cache_reset_impl(&s_cache);
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE
