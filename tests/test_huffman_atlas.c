// Host test for the compress = "huffman" path in pnx_atlas_load (pnx_assets.c) -- the
// full real path, against a REAL pipeline build (fixtures/huffman_atlas/, same source
// sheet as fixtures/bitplane_atlas's own fixture): pnx_atlas_load -> pnx_atlas_tile ->
// pnx_tile_cache_get -> pnx_huffman_atlas_fetch -> targeted pnx_platform_resource_read
// calls -> pnx_huffman_decode, against the project-wide table pnx_huffman_table_load
// loaded first. Mirrors test_bitplane_atlas.c's own structure exactly. Compiled only
// into build/test_huffman_full (tests/Makefile's HUFFMAN_FULL_SRC).

#include "../src/pnx/pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/assets/pnx_tile_cache.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

#define HA_DIR "fixtures/huffman_atlas/resources/"
#include "fixtures/huffman_atlas/gen.h"

extern int s_failures;
extern int s_checks;

#define HA_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_huffman_atlas(void);

static const char* s_files[] = PNX_ASSET_FILE_TABLE;
static uint32_t s_resources[PNX_ASSET_COUNT];
static char s_paths[PNX_ASSET_COUNT][96];

void test_huffman_atlas(void)
{
	printf("huffman_atlas\n");

	pnx_host_reset();
	for (int i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_resources[i] = (uint32_t)(i + 1);
		snprintf(s_paths[i], sizeof(s_paths[i]), "%s%s", HA_DIR, s_files[i]);
		FILE* f = fopen(s_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP huffman_atlas: %s not built -- run tools/pnx_assets.py in "
				"tests/fixtures/huffman_atlas\n",
				s_paths[i]);
			return;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_paths[i]);
	}

	PnxArena arena;
	HA_CHECK(pnx_arena_init(&arena, "huffman-atlas-arena", 8 * 1024, 4));
	HA_CHECK(pnx_assets_init(&arena, s_resources, PNX_ASSET_COUNT));
	HA_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));
	HA_CHECK(pnx_huffman_table_load(PNX_ASSET_HUFFMAN_TABLE_HUFFMAN_TABLE));
	HA_CHECK(pnx_tile_cache_init(&arena, 8, TILES_TILE_PX));
	pnx_tile_cache_reset();

	// pnx_atlas_load brings in metadata only under huffman compression -- tile_palette/
	// tile_flags plus resource/stream_offset -- no pixel byte is bulk-read here.
	PnxAtlas atlas;
	HA_CHECK(pnx_atlas_load(&atlas, PNX_ASSET_ATLAS_TILES));
	HA_CHECK(atlas.tile_px == TILES_TILE_PX);
	HA_CHECK(atlas.tile_count == TILES_TILE_COUNT);
	HA_CHECK(!atlas.metatiles); // this fixture is small enough metatiling never pays off

	// Every tile decodes to something (not the cache pool's zero-fill left in place by a
	// silently-failed decode), and distinct tiles decode to distinct content.
	const uint8_t* floor  = pnx_atlas_tile(&atlas, TILES_TILE_FLOOR);
	const uint8_t* wall	  = pnx_atlas_tile(&atlas, TILES_TILE_WALL);
	const uint8_t* accent = pnx_atlas_tile(&atlas, TILES_TILE_ACCENT);
	HA_CHECK(floor && wall && accent);
	if (floor && wall && accent)
	{
		HA_CHECK(memcmp(floor, wall, TILES_TILE_BYTES) != 0);
		HA_CHECK(memcmp(floor, accent, TILES_TILE_BYTES) != 0);
		bool any_nonzero = false;
		for (int i = 0; i < TILES_TILE_BYTES; i++)
			if (floor[i] != 0)
				any_nonzero = true;
		HA_CHECK(any_nonzero);
	}

	// A repeat fetch of an already-cached tile returns the SAME pointer -- a hit, not a
	// fresh decode into a new pool slot.
	const uint8_t* floor_again = pnx_atlas_tile(&atlas, TILES_TILE_FLOOR);
	HA_CHECK(floor_again == floor);

	// Out-of-range tile index and an unregistered atlas resource both refuse cleanly.
	HA_CHECK(pnx_atlas_tile(&atlas, 250) == NULL);
	pnx_host_reset();
	pnx_tile_cache_reset();
	PnxAtlas gone;
	HA_CHECK(!pnx_atlas_load(&gone, PNX_ASSET_ATLAS_TILES));
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN
