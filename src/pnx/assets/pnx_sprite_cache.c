#include "pnx_sprite_cache.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE

#include "pnx_bitplane.h"
#include "pnx_lru_cache_impl.h"

static PnxLruCache s_cache;

static uint32_t make_key(uint16_t asset_id, uint32_t offset)
{
	return ((uint32_t)asset_id << 20) | (offset & 0xFFFFFu);
}

bool pnx_sprite_cache_init(PnxArena* arena, uint16_t target_entries)
{
	const size_t slot_bytes = PNX_BITPLANE_PACKED_BYTES(PNX_SPRITE_CACHE_MAX_UNIT_PX);
	return pnx_lru_cache_init_impl(&s_cache, arena, target_entries, (uint16_t)slot_bytes,
								   PNX_SPRITE_CACHE_MAX_AGE);
}

// Recovers `frame`'s own unit index and compressed-stream offset -- the pipeline
// (compress_sprite_pixels_bitplane, tools/pnx_assets.py) walks ascending DISTINCT
// frame_meta offsets the same way, deduplicating identical packed frames onto one unit.
// O(frame_count), fine at the frame counts a sprite sheet actually has, paid only on a
// cache miss.
static bool find_unit(const PnxSprite* sprite, uint8_t frame, uint16_t* out_unit,
					  uint32_t* out_offset, uint8_t* out_w, uint8_t* out_h)
{
	if (frame >= sprite->frame_count)
		return false;
	const uint8_t* fe	  = sprite->frame_meta + (size_t)frame * PNX_SPRITE_FRAME_BYTES;
	const uint32_t target = (uint32_t)(fe[0] | ((uint32_t)fe[1] << 8));

	int32_t last  = -1;
	uint16_t unit = 0;
	for (;;)
	{
		int32_t best   = -1;
		uint8_t best_w = 0, best_h = 0;
		for (uint8_t i = 0; i < sprite->frame_count; i++)
		{
			const uint8_t* e  = sprite->frame_meta + (size_t)i * PNX_SPRITE_FRAME_BYTES;
			const int32_t off = (int32_t)(e[0] | ((uint32_t)e[1] << 8));
			if (off > last && (best == -1 || off < best))
			{
				best   = off;
				best_w = e[2];
				best_h = e[3];
			}
		}
		if (best == -1)
			return false; // unreachable: target came from this same frame_meta
		if ((uint32_t)best == target)
		{
			*out_unit	= unit;
			*out_offset = target;
			*out_w		= best_w;
			*out_h		= best_h;
			return true;
		}
		last = best;
		unit++;
	}
}

bool pnx_sprite_cache_get(const PnxSprite* sprite, uint8_t frame, PnxSpriteFrame* out)
{
	if (!sprite)
		return false;

	uint16_t unit;
	uint32_t offset;
	uint8_t w, h;
	if (!find_unit(sprite, frame, &unit, &offset, &w, &h))
		return false;

	const uint8_t* fe = sprite->frame_meta + (size_t)frame * PNX_SPRITE_FRAME_BYTES;
	out->w			  = w;
	out->h			  = h;
	out->origin_x	  = fe[4];
	out->origin_y	  = fe[5];
	out->flags		  = fe[6];

	const uint32_t key = make_key(sprite->asset_id, offset);
	bool hit;
	uint8_t* dst = pnx_lru_cache_find_or_prepare(&s_cache, key, &hit);
	if (!dst)
		return false;
	if (hit)
	{
		out->pixels = dst;
		return true;
	}

	uint8_t compressed[PNX_BITPLANE_PACKED_BYTES(PNX_SPRITE_CACHE_MAX_UNIT_PX)];
	const size_t clen = pnx_bitplane_sprite_fetch(sprite, unit, compressed, sizeof(compressed));
	if (clen == 0)
		return false;

	uint8_t decode_scratch[PNX_SPRITE_CACHE_MAX_UNIT_PX];
	const uint16_t n = (uint16_t)((uint32_t)w * h);
	if (!pnx_bitplane_decode(compressed, clen, dst, decode_scratch, n))
		return false;

	out->pixels = dst;
	return true;
}

void pnx_sprite_cache_tick(void)
{
	pnx_lru_cache_tick_impl(&s_cache);
}

void pnx_sprite_cache_reset(void)
{
	pnx_lru_cache_reset_impl(&s_cache);
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE
