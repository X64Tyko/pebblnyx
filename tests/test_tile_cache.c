// Host test for pnx_tile_cache.c -- the decoded-tile LRU cache that makes
// PNX_COMPRESS_BITPLANE viable against a renderer with no dirty-tracking (see
// pnx_tile_cache.h's own comment for the arena-backed, header+pool design). Runs against
// a mock fetch callback here rather than a real resource file -- test_bitplane_atlas.c
// covers the real pnx_bitplane_atlas_fetch path end to end; this one is purely about the
// cache's own hit/miss/eviction/aging behaviour, which doesn't need real content.
//
// Real content, not synthetic: the mock fetch serves real 16x16-pixel fixtures out of
// fixtures/bitplane/bpeg_fixtures.h (the same ones test_bitplane_compress.c decodes
// directly), keyed however a given test needs -- cache correctness doesn't depend on
// WHAT a tile's pixels are, only on whether (atlas_asset, tile_index) pairs are tracked
// as distinct entries, so eviction/aging tests can reuse a small set of real fixtures
// under many distinct synthetic keys.

#include "../src/pnx/pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE

#include "../src/pnx/assets/pnx_tile_cache.h"
#include "../src/pnx/core/pnx_arena.h"
#include "fixtures/bitplane/bpeg_fixtures.h"

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

// Local stand-in for the old compile-time TC_TEST_ENTRIES: how many distinct entries
// this test's own pnx_tile_cache_init grants, sized well past every loop below so "one
// more than every slot" tests still force a real eviction rather than hitting a target
// the arena silently couldn't grant in full.
#define TC_TEST_ENTRIES 8
#define TC_TEST_TILE_PX 16

#define TC_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_tile_cache(void);

// Four real 16x16 (256-pixel) fixtures, cycled across as many synthetic keys as a test
// needs -- tile_index % 4 picks which real content a given key serves.
static const BpegFixture* s_mock_units[4];
static int s_fetch_calls;

static size_t mock_fetch(void* ctx, uint16_t atlas_asset, uint16_t tile_index, uint8_t* scratch,
						 size_t scratch_cap)
{
	(void)ctx;
	(void)atlas_asset;
	s_fetch_calls++;
	const BpegFixture* f = s_mock_units[tile_index % 4];
	if (f->encoded_len > scratch_cap)
		return 0;
	memcpy(scratch, f->encoded, f->encoded_len);
	return f->encoded_len;
}

// A fetch that always returns deliberately malformed bytes (declares more colours than
// the encoded bit width could ever support once decoded) -- pnx_bitplane_decode must
// refuse, and pnx_tile_cache_get must propagate that as NULL without corrupting state.
static size_t bad_fetch(void* ctx, uint16_t atlas_asset, uint16_t tile_index, uint8_t* scratch,
						size_t scratch_cap)
{
	(void)ctx;
	(void)atlas_asset;
	(void)tile_index;
	if (scratch_cap < 3)
		return 0;
	scratch[0] = 0x0F; // k=16 (bits=4), but...
	scratch[1] = 0xFF; // ...only 2 bytes follow: nowhere near enough for an 8-entry
	scratch[2] = 0xFF; // offset table + any bitstream at all
	return 3;
}

static const BpegFixture* find_fixture(const char* name)
{
	for (int i = 0; i < BPEG_FIXTURE_COUNT; i++)
		if (strcmp(BPEG_FIXTURES[i].name, name) == 0)
			return &BPEG_FIXTURES[i];
	return NULL;
}

static void pack_expected(const BpegFixture* f, uint8_t* out)
{
	for (uint16_t j = 0; j < f->n; j += 2)
	{
		const uint8_t hi = f->expected[j];
		const uint8_t lo = (uint16_t)(j + 1) < f->n ? f->expected[j + 1] : 0;
		out[j / 2]		 = (uint8_t)((hi << 4) | lo);
	}
}

void test_tile_cache(void)
{
	printf("tile_cache\n");

	s_mock_units[0] = find_fixture("tiles_tile0");
	s_mock_units[1] = find_fixture("tiles_tile5");
	s_mock_units[2] = find_fixture("tiles_tile10");
	s_mock_units[3] = find_fixture("tiles_tile20");
	for (int i = 0; i < 4; i++)
		TC_CHECK(s_mock_units[i] != NULL);
	if (!s_mock_units[0] || !s_mock_units[1] || !s_mock_units[2] || !s_mock_units[3])
	{
		printf("  SKIP tile_cache: expected fixtures missing from bpeg_fixtures.h\n");
		return;
	}

	static PnxArena arena;
	TC_CHECK(pnx_arena_init(&arena, "tile-cache-test-arena", 16 * 1024, 4));
	TC_CHECK(pnx_tile_cache_init(&arena, TC_TEST_ENTRIES, TC_TEST_TILE_PX));

	pnx_tile_cache_reset();
	s_fetch_calls = 0;

	// --- miss, then hit: fetch called once, content correct, second call doesn't refetch ---
	const uint8_t* p1 = pnx_tile_cache_get(1, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(p1 != NULL);
	TC_CHECK(s_fetch_calls == 1);
	if (p1)
	{
		uint8_t expected[128];
		pack_expected(s_mock_units[0], expected);
		TC_CHECK(memcmp(p1, expected, sizeof(expected)) == 0);
	}

	const uint8_t* p1_again = pnx_tile_cache_get(1, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(p1_again == p1);	  // same slot, same pointer
	TC_CHECK(s_fetch_calls == 1); // NOT refetched -- the whole point of caching decoded pixels

	// A different tile of the SAME atlas is a genuine miss.
	const uint8_t* p2 = pnx_tile_cache_get(1, 1, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(p2 != NULL && p2 != p1);
	TC_CHECK(s_fetch_calls == 2);

	// The same tile_index under a DIFFERENT atlas_asset is also a genuine miss -- the key
	// is the pair, not either half alone.
	const uint8_t* p3 = pnx_tile_cache_get(2, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(p3 != NULL);
	TC_CHECK(s_fetch_calls == 3);

	// --- fill every slot with distinct keys, then one more forces an eviction ---
	pnx_tile_cache_reset();
	s_fetch_calls = 0;
	for (uint16_t i = 0; i < TC_TEST_ENTRIES; i++)
		TC_CHECK(pnx_tile_cache_get(100, i, TC_TEST_TILE_PX, mock_fetch, NULL) != NULL);
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES);

	// Every one of those SLOTS keys should still be a cache hit (nothing evicted itself
	// out with no pressure) before the cache is actually full-and-asked-for-one-more.
	for (uint16_t i = 0; i < TC_TEST_ENTRIES; i++)
		pnx_tile_cache_get(100, i, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES); // still no new fetches

	// tile_index 0 is the OLDEST now (every other key was touched after it in the loop
	// above) -- one more distinct key should evict exactly it, not some other slot.
	TC_CHECK(pnx_tile_cache_get(100, TC_TEST_ENTRIES, TC_TEST_TILE_PX, mock_fetch, NULL) != NULL);
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES + 1);
	pnx_tile_cache_get(100, 0, TC_TEST_TILE_PX, mock_fetch, NULL); // now a miss again -- it was evicted
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES + 2);

	// --- touching a slot protects it from being the next eviction victim ---
	pnx_tile_cache_reset();
	s_fetch_calls = 0;
	for (uint16_t i = 0; i < TC_TEST_ENTRIES; i++)
		pnx_tile_cache_get(200, i, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES);
	// Age every slot by one tick, then re-touch key 0 (resets ITS age to 0, so key 1 is
	// now the oldest, not key 0).
	pnx_tile_cache_tick();
	pnx_tile_cache_get(200, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES);									 // still a hit, no new fetch
	pnx_tile_cache_get(200, TC_TEST_ENTRIES, TC_TEST_TILE_PX, mock_fetch, NULL); // forces one eviction
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES + 1);
	pnx_tile_cache_get(200, 0, TC_TEST_TILE_PX, mock_fetch, NULL); // key 0 was just touched -- must survive
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES + 1);
	pnx_tile_cache_get(200, 1, TC_TEST_TILE_PX, mock_fetch, NULL); // key 1 was the real oldest -- must be gone
	TC_CHECK(s_fetch_calls == TC_TEST_ENTRIES + 2);

	// --- age-based self-release, independent of eviction pressure ---
	// No intermediate get() here on purpose -- observing occupancy at all means calling
	// get(), which itself resets age as a side effect (same as any real caller: there's
	// no way to "peek" a cache without touching it). So this checks only the one
	// property that CAN be checked without self-interference: crossing the configured
	// threshold with zero touches in between causes exactly one release+refetch.
	pnx_tile_cache_reset();
	s_fetch_calls = 0;
	TC_CHECK(pnx_tile_cache_get(300, 0, TC_TEST_TILE_PX, mock_fetch, NULL) != NULL);
	TC_CHECK(s_fetch_calls == 1);
	for (uint32_t t = 0; t < PNX_TILE_CACHE_MAX_AGE; t++)
		pnx_tile_cache_tick();
	pnx_tile_cache_get(300, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(s_fetch_calls == 2); // released and refetched

	// --- reset clears everything unconditionally ---
	pnx_tile_cache_get(400, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	const int before_reset = s_fetch_calls;
	pnx_tile_cache_reset();
	pnx_tile_cache_get(400, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(s_fetch_calls == before_reset + 1);

	// --- a real, cached tile survives a LATER failed fetch for a different key ---
	pnx_tile_cache_reset();
	s_fetch_calls		= 0;
	const uint8_t* good = pnx_tile_cache_get(500, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(good != NULL);
	uint8_t good_snapshot[128];
	memcpy(good_snapshot, good, sizeof(good_snapshot));

	const uint8_t* bad = pnx_tile_cache_get(500, 1, TC_TEST_TILE_PX, bad_fetch, NULL);
	TC_CHECK(bad == NULL); // malformed stream refused, not silently decoded as garbage
	// The slot(s) a failed decode almost touched must be untouched -- re-reading the
	// first key must still be a HIT (same content, no new fetch), not corrupted or evicted.
	const int calls_before	  = s_fetch_calls;
	const uint8_t* good_again = pnx_tile_cache_get(500, 0, TC_TEST_TILE_PX, mock_fetch, NULL);
	TC_CHECK(good_again != NULL);
	TC_CHECK(s_fetch_calls == calls_before); // still a hit
	TC_CHECK(memcmp(good_again, good_snapshot, sizeof(good_snapshot)) == 0);
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE
