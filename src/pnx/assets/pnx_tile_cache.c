#include "pnx_tile_cache.h"

#if PNX_USE_BITPLANE_COMPRESS

#include "pnx_bitplane.h"

#define PNX_TILE_CACHE_PIXELS ((size_t)PNX_TILE_CACHE_TILE_PX * PNX_TILE_CACHE_TILE_PX)
// Worst case a compressed tile can ever be: pnx_bitplane_decode's own raw-fallback escape
// hatch, header byte + the tile packed 4bpp verbatim -- never larger than that by
// construction (encode_unit always keeps whichever of the two forms is smaller).
#define PNX_TILE_CACHE_MAX_COMPRESSED (1 + PNX_TILE_CACHE_SLOT_BYTES)

typedef struct
{
	uint16_t atlas_asset;
	uint16_t tile_index;
	uint8_t age;
	bool occupied;
	uint8_t pixels[PNX_TILE_CACHE_SLOT_BYTES]; // packed 4bpp -- pnx_bitplane_decode's own output
} PnxTileSlot;

// Zero-initialised by the linker (occupied == false, same as pnx_tile_cache_reset would
// set) -- no separate init call needed, same posture as every other pnx module with
// file-scope state.
static PnxTileSlot s_slots[PNX_TILE_CACHE_SLOTS];

const uint8_t* pnx_tile_cache_get(uint16_t atlas_asset, uint16_t tile_index,
								  PnxTileFetchFn fetch, void* fetch_ctx)
{
	for (uint16_t i = 0; i < PNX_TILE_CACHE_SLOTS; i++)
	{
		PnxTileSlot* s = &s_slots[i];
		if (s->occupied && s->atlas_asset == atlas_asset && s->tile_index == tile_index)
		{
			s->age = 0; // ticks once per frame, resets to 0 on draw -- see pnx_config.h
			return s->pixels;
		}
	}

	// Miss: a free slot if one exists, else the slot with the largest age -- which IS
	// least-recently-drawn, since age already means exactly that (see pnx_tile_cache_tick).
	uint16_t target	   = 0;
	uint8_t target_age = 0;
	bool have_target   = false;
	for (uint16_t i = 0; i < PNX_TILE_CACHE_SLOTS; i++)
	{
		if (!s_slots[i].occupied)
		{
			// have_target already did its job for THIS iteration's own comparison below --
			// break exits before another iteration would ever read it again, so setting it
			// here would be a dead store (clang-analyzer-deadcode.DeadStores caught this).
			target = i;
			break;
		}
		if (!have_target || s_slots[i].age > target_age)
		{
			target		= i;
			target_age	= s_slots[i].age;
			have_target = true;
		}
	}

	uint8_t compressed[PNX_TILE_CACHE_MAX_COMPRESSED];
	const size_t clen =
		fetch(fetch_ctx, atlas_asset, tile_index, compressed, sizeof(compressed));
	if (clen == 0)
		return NULL;

	// pnx_bitplane_decode's own working buffer -- see pnx_bitplane.h's own comment for
	// why it can't decode straight into packed output. PNX_TILE_CACHE_PIXELS worst case
	// (64x64) is 4096 B; a transient stack buffer for the duration of one decode call,
	// not held resident, same shape as pnx_tile_cache_get's other locals.
	uint8_t decode_scratch[PNX_TILE_CACHE_PIXELS];
	PnxTileSlot* s = &s_slots[target];
	if (!pnx_bitplane_decode(compressed, clen, s->pixels, decode_scratch,
							 (uint16_t)PNX_TILE_CACHE_PIXELS))
		return NULL; // slot left exactly as it was -- a failed fetch/decode evicts nothing

	s->atlas_asset = atlas_asset;
	s->tile_index  = tile_index;
	s->age		   = 0;
	s->occupied	   = true;
	return s->pixels;
}

void pnx_tile_cache_tick(void)
{
	for (uint16_t i = 0; i < PNX_TILE_CACHE_SLOTS; i++)
	{
		if (!s_slots[i].occupied)
			continue;
		s_slots[i].age++;
		if (s_slots[i].age >= PNX_TILE_CACHE_MAX_AGE)
			s_slots[i].occupied = false;
	}
}

void pnx_tile_cache_reset(void)
{
	for (uint16_t i = 0; i < PNX_TILE_CACHE_SLOTS; i++)
		s_slots[i].occupied = false;
}

#endif // PNX_USE_BITPLANE_COMPRESS
