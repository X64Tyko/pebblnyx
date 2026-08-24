// Host test for the missing piece connecting pnx_tile_cache.c to real data:
// pnx_bitplane_atlas_fetch (pnx_assets.c), reading individual tiles out of a real
// per-tile-addressable "PT" atlas blob (fixtures/bitplane/pt_atlas_tiles.bin, built by
// tools' bpeg_atlas_encode.py mirror from overworld's real "tiles" atlas content -- not
// part of the pipeline, this format isn't wired into it).
//
// This is the full real path, standalone: pnx_tile_cache_get -> pnx_bitplane_atlas_fetch
// -> two targeted pnx_platform_resource_read calls -> pnx_bitplane_decode, against an
// actual registered resource file, not a mock. What's still standalone is only that
// nothing in pnx_map_load/pnx_tilemap_draw_layer calls this path yet.

#include "../src/pnx/pnx_config.h"

#if PNX_USE_BITPLANE_COMPRESS

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/assets/pnx_tile_cache.h"
#include "../src/pnx/platform/pnx_platform_host.h"
#include "fixtures/bitplane/bpeg_fixtures.h" // ground truth: tiles_tile0/5/10/20

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define BA_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_bitplane_atlas(void);

#define PT_ATLAS_RESOURCE 1u

static const BpegFixture* find_fixture(const char* name)
{
	for (int i = 0; i < BPEG_FIXTURE_COUNT; i++)
		if (strcmp(BPEG_FIXTURES[i].name, name) == 0)
			return &BPEG_FIXTURES[i];
	return NULL;
}

void test_bitplane_atlas(void)
{
	printf("bitplane_atlas\n");

	const char* path = "fixtures/bitplane/pt_atlas_tiles.bin";
	FILE* f			 = fopen(path, "rb");
	if (!f)
	{
		printf("  SKIP bitplane_atlas: %s not built -- run bpeg_atlas_encode.py\n", path);
		return;
	}
	fclose(f);

	pnx_host_reset();
	static const uint32_t resources[2] = { 0, PT_ATLAS_RESOURCE };
	pnx_host_register_resource(PT_ATLAS_RESOURCE, path);

	static PnxArena arena;
	BA_CHECK(pnx_arena_init(&arena, "bitplane-atlas-arena", 4 * 1024, 4));
	BA_CHECK(pnx_assets_init(&arena, resources, 2));

	pnx_tile_cache_reset();

	// asset id 1 (index into the `resources` table above) is the PT atlas -- known real
	// tiles 0/5/10/20 must decode to exactly what test_bitplane_compress.c already
	// verified them to be, now via the FULL real path (targeted resource reads, not a
	// whole-blob read) instead of a hand-fed compressed blob.
	struct
	{
		uint16_t tile_index;
		const char* fixture_name;
	} known[] = {
		{ 0, "tiles_tile0" },
		{ 5, "tiles_tile5" },
		{ 10, "tiles_tile10" },
		{ 20, "tiles_tile20" },
	};

	for (size_t i = 0; i < sizeof(known) / sizeof(known[0]); i++)
	{
		const BpegFixture* fixture = find_fixture(known[i].fixture_name);
		BA_CHECK(fixture != NULL);
		if (!fixture)
			continue;

		const uint8_t* px =
			pnx_tile_cache_get(1, known[i].tile_index, pnx_bitplane_atlas_fetch, NULL);
		BA_CHECK(px != NULL);
		if (!px)
			continue;

		uint8_t expected[128];
		for (uint16_t j = 0; j < fixture->n; j += 2)
		{
			const uint8_t hi = fixture->expected[j];
			const uint8_t lo = (uint16_t)(j + 1) < fixture->n ? fixture->expected[j + 1] : 0;
			expected[j / 2]	 = (uint8_t)((hi << 4) | lo);
		}
		const bool match = memcmp(px, expected, sizeof(expected)) == 0;
		BA_CHECK(match);
		if (!match)
			printf("  MISMATCH: tile %u (%s)\n", known[i].tile_index, known[i].fixture_name);
	}

	// Out-of-range tile index and an unknown atlas asset both refuse cleanly.
	BA_CHECK(pnx_tile_cache_get(1, 9999, pnx_bitplane_atlas_fetch, NULL) == NULL);
	BA_CHECK(pnx_tile_cache_get(9999, 0, pnx_bitplane_atlas_fetch, NULL) == NULL);

	// A resource that genuinely can't be read (unregistered id) refuses cleanly rather
	// than crashing or returning garbage. pnx_tile_cache.c's own test (test_tile_cache.c)
	// already proves a cache HIT never re-invokes fetch at all via a call counter -- not
	// re-proven here, since pnx_host_register_resource appends rather than overwrites and
	// can't simulate "this real resource became unreadable" without a full host_reset.
	pnx_host_reset();
	pnx_tile_cache_reset();
	static const uint32_t no_resources[2] = { 0, PT_ATLAS_RESOURCE };
	BA_CHECK(pnx_assets_init(&arena, no_resources, 2));
	BA_CHECK(pnx_tile_cache_get(1, 0, pnx_bitplane_atlas_fetch, NULL) == NULL);

	pnx_arena_destroy(&arena);
}

#else

void test_bitplane_atlas(void);
void test_bitplane_atlas(void)
{
}

#endif // PNX_USE_BITPLANE_COMPRESS
