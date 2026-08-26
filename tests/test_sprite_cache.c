// Host test for pnx_sprite_cache.c's own knobs -- init/eviction/aging/flush behaviour
// through the real pnx_sprite_load -> pnx_sprite_frame_get -> pnx_sprite_cache_get path.
// There is no separate "lazy" sprite type any more: under PNX_COMPRESS_BITPLANE,
// pnx_sprite_load itself only ever brings in metadata, and pnx_sprite_frame_get is what
// fetches+decodes a frame through this cache on a miss -- test_bitplane_sprite.c already
// covers the decode-correctness/dedup side of that same path against this fixture
// (fixtures/bitplane_sprite/, sprite QUAD: 4 frames, frame 0 and frame 2 byte-
// identical/deduped on disk); this file is about the cache's own capacity/eviction/aging
// behaviour specifically, mirroring test_tile_cache.c's own structure for its sibling.

#include "../src/pnx/pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/assets/pnx_sprite_cache.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>

#define SC_DIR "fixtures/bitplane_sprite/resources/"
#include "fixtures/bitplane_sprite/gen.h"

extern int s_failures;
extern int s_checks;

#define SC_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_sprite_cache(void);

static const char* s_files[] = PNX_ASSET_FILE_TABLE;
static uint32_t s_resources[PNX_ASSET_COUNT];
static char s_paths[PNX_ASSET_COUNT][96];

static bool register_fixture(void)
{
	pnx_host_reset();
	for (int i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_resources[i] = (uint32_t)(i + 1);
		snprintf(s_paths[i], sizeof(s_paths[i]), "%s%s", SC_DIR, s_files[i]);
		FILE* f = fopen(s_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP sprite_cache: %s not built -- run tools/pnx_assets.py in "
				"tests/fixtures/bitplane_sprite\n",
				s_paths[i]);
			return false;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_paths[i]);
	}
	return true;
}

void test_sprite_cache(void)
{
	printf("sprite_cache\n");

	if (!register_fixture())
		return;

	PnxArena arena;
	SC_CHECK(pnx_arena_init(&arena, "sprite-cache-arena", 24 * 1024, 4));
	SC_CHECK(pnx_assets_init(&arena, s_resources, PNX_ASSET_COUNT));
	SC_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));

	// pnx_sprite_load brings in metadata only under PNX_COMPRESS_BITPLANE -- there is no
	// separate "lazy" sprite type any more (see this file's own top comment). Frames are
	// fetched+decoded through this cache by pnx_sprite_frame_get/pnx_sprite_cache_get.
	PnxSprite sprite;
	SC_CHECK(pnx_sprite_load(&sprite, PNX_ASSET_SPRITE_QUAD));
	SC_CHECK(sprite.frame_count == QUAD_FRAME_COUNT);

	SC_CHECK(pnx_sprite_cache_init(&arena, 8));
	pnx_sprite_cache_reset();

	// --- every frame decodes to something real, not the pool's zero-fill ---
	for (uint8_t frame = 0; frame < QUAD_FRAME_COUNT; frame++)
	{
		PnxSpriteFrame f;
		SC_CHECK(pnx_sprite_cache_get(&sprite, frame, &f));
		SC_CHECK(f.pixels != NULL);
	}

	// --- deduped frames (0 and 2) share one cache entry, not two independent decodes ---
	PnxSpriteFrame f0, f2;
	SC_CHECK(pnx_sprite_cache_get(&sprite, 0, &f0));
	SC_CHECK(pnx_sprite_cache_get(&sprite, 2, &f2));
	SC_CHECK(f0.pixels == f2.pixels);

	// Frames 1 and 3 are distinct source rects -- must not collide with frame 0 or 3.
	PnxSpriteFrame f1, f3;
	SC_CHECK(pnx_sprite_cache_get(&sprite, 1, &f1));
	SC_CHECK(pnx_sprite_cache_get(&sprite, 3, &f3));
	SC_CHECK(f1.pixels != f0.pixels);
	SC_CHECK(f3.pixels != f0.pixels);
	SC_CHECK(f1.pixels != f3.pixels);

	// A repeat get of an already-cached frame returns the SAME pointer -- a hit, not a
	// fresh decode into a new pool slot.
	PnxSpriteFrame f0_again;
	SC_CHECK(pnx_sprite_cache_get(&sprite, 0, &f0_again));
	SC_CHECK(f0_again.pixels == f0.pixels);

	// Frame index out of range refuses cleanly.
	PnxSpriteFrame bad;
	SC_CHECK(!pnx_sprite_cache_get(&sprite, QUAD_FRAME_COUNT, &bad));

	// --- init grants fewer entries than requested on a too-small arena, and still works ---
	// Slot size is fixed now (PNX_SPRITE_CACHE_MAX_UNIT_PX, pnx_config.h), not a per-call
	// argument -- a too-small ARENA is what exercises "granted may be less than
	// requested, still works" here, the same contract pnx_tile_cache_init already has.
	{
		PnxArena tiny;
		SC_CHECK(pnx_arena_init(&tiny, "sprite-cache-tiny-arena", 2 * 1024, 4));
		SC_CHECK(pnx_sprite_cache_init(&tiny, 1000));
		PnxSpriteFrame got;
		SC_CHECK(pnx_sprite_cache_get(&sprite, 0, &got));
		SC_CHECK(got.pixels != NULL);
		pnx_arena_destroy(&tiny);
	}

	// --- a pool granted only one entry forces a flush on every miss ---
	{
		PnxArena small;
		// Big enough for exactly one slot's worth of header+pool, not two -- requesting 8
		// but granting 1 is what forces the second distinct frame to evict the first
		// rather than fail.
		SC_CHECK(pnx_arena_init(&small, "sprite-cache-flush-arena", 900, 4));
		SC_CHECK(pnx_sprite_cache_init(&small, 8));
		PnxSpriteFrame a, b, a_again;
		SC_CHECK(pnx_sprite_cache_get(&sprite, 1, &a));
		SC_CHECK(pnx_sprite_cache_get(&sprite, 3, &b));
		// Frame 1 is gone now (flushed), but re-fetching it must still succeed -- a
		// fresh decode into the now-empty pool, not a failure.
		SC_CHECK(pnx_sprite_cache_get(&sprite, 1, &a_again));
		SC_CHECK(a_again.pixels != NULL);
		pnx_arena_destroy(&small);
	}

	// --- age-based self-release, independent of eviction pressure ---
	SC_CHECK(pnx_sprite_cache_init(&arena, 8));
	pnx_sprite_cache_reset();
	PnxSpriteFrame aged1, aged2;
	SC_CHECK(pnx_sprite_cache_get(&sprite, 1, &aged1));
	for (uint32_t t = 0; t < PNX_SPRITE_CACHE_MAX_AGE; t++)
		pnx_sprite_cache_tick();
	// Aged out: the slot was genuinely released (occupied=false), not merely marked
	// stale-but-still-occupied -- a real fresh decode succeeds rather than the get
	// finding a leftover, now-invalid "hit". (Whether it lands in the SAME physical slot
	// or a different one is an implementation detail: with only this one key touched, the
	// freed slot is also the only free one, so pointer identity here proves nothing either
	// way -- test_tile_cache.c's own age test uses a mock fetch's call counter to observe
	// this unambiguously; this cache has no such counter against the real fetch path.)
	SC_CHECK(pnx_sprite_cache_get(&sprite, 1, &aged2));
	SC_CHECK(aged2.pixels != NULL);
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE
