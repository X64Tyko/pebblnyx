// Host test for the compress = "bitplane" path in pnx_atlas_load (pnx_assets.c) -- the
// full real path, against a REAL pipeline build (fixtures/bitplane_atlas/): pnx_atlas_load
// -> pnx_atlas_tile/pnx_atlas_subtile -> pnx_tile_cache_get -> pnx_bitplane_atlas_fetch ->
// targeted pnx_platform_resource_read calls -> pnx_bitplane_decode. Compiled only into
// build/test_bitplane (tests/Makefile's BITPLANE_SRC).
//
// Supersedes an earlier, standalone version of this file that hand-built a separate
// per-tile "PT" resource via a bpeg_atlas_encode.py tool never wired into the real
// pipeline, and called pnx_bitplane_atlas_fetch directly with a NULL ctx -- incompatible
// with the current design, where pnx_bitplane_atlas_fetch's ctx is the already-loaded
// PnxAtlas itself (its own resource/stream_offset), not a raw resource id. There is no
// separate "PT" resource any more: a bitplane atlas's compressed tile units live in the
// SAME "PA" resource pnx_atlas_load already reads for metadata.

#include "../src/pnx/pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/assets/pnx_tile_cache.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

#define BA_DIR "fixtures/bitplane_atlas/resources/"
#include "fixtures/bitplane_atlas/gen.h"

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

static const char* s_files[] = PNX_ASSET_FILE_TABLE;
static uint32_t s_resources[PNX_ASSET_COUNT];
static char s_paths[PNX_ASSET_COUNT][96];

void test_bitplane_atlas(void)
{
	printf("bitplane_atlas\n");

	pnx_host_reset();
	for (int i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_resources[i] = (uint32_t)(i + 1);
		snprintf(s_paths[i], sizeof(s_paths[i]), "%s%s", BA_DIR, s_files[i]);
		FILE* f = fopen(s_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP bitplane_atlas: %s not built -- run tools/pnx_assets.py in "
				"tests/fixtures/bitplane_atlas\n",
				s_paths[i]);
			return;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_paths[i]);
	}

	PnxArena arena;
	BA_CHECK(pnx_arena_init(&arena, "bitplane-atlas-arena", 8 * 1024, 4));
	BA_CHECK(pnx_assets_init(&arena, s_resources, PNX_ASSET_COUNT));
	BA_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));
	BA_CHECK(pnx_tile_cache_init(&arena, 8, TILES_TILE_PX));
	pnx_tile_cache_reset();

	// pnx_atlas_load brings in metadata only under bitplane compression -- tile_palette/
	// tile_flags plus resource/stream_offset -- no pixel byte is bulk-read here.
	PnxAtlas atlas;
	BA_CHECK(pnx_atlas_load(&atlas, PNX_ASSET_ATLAS_TILES));
	BA_CHECK(atlas.tile_px == TILES_TILE_PX);
	BA_CHECK(atlas.tile_count == TILES_TILE_COUNT);
	BA_CHECK(!atlas.metatiles); // this fixture is small enough metatiling never pays off

	// Every tile decodes to something (not the cache pool's zero-fill left in place by a
	// silently-failed decode), and distinct tiles decode to distinct content.
	const uint8_t* floor  = pnx_atlas_tile(&atlas, TILES_TILE_FLOOR);
	const uint8_t* wall	  = pnx_atlas_tile(&atlas, TILES_TILE_WALL);
	const uint8_t* accent = pnx_atlas_tile(&atlas, TILES_TILE_ACCENT);
	BA_CHECK(floor && wall && accent);
	if (floor && wall && accent)
	{
		BA_CHECK(memcmp(floor, wall, TILES_TILE_BYTES) != 0);
		BA_CHECK(memcmp(floor, accent, TILES_TILE_BYTES) != 0);
		bool any_nonzero = false;
		for (int i = 0; i < TILES_TILE_BYTES; i++)
			if (floor[i] != 0)
				any_nonzero = true;
		BA_CHECK(any_nonzero);
	}

	// A repeat fetch of an already-cached tile returns the SAME pointer -- a hit, not a
	// fresh decode into a new pool slot (test_tile_cache.c already proves this via a
	// fetch-call counter against a mock; this is the same property through the real path).
	const uint8_t* floor_again = pnx_atlas_tile(&atlas, TILES_TILE_FLOOR);
	BA_CHECK(floor_again == floor);

	// Out-of-range tile index and an unregistered atlas resource both refuse cleanly.
	BA_CHECK(pnx_atlas_tile(&atlas, 250) == NULL);
	pnx_host_reset();
	pnx_tile_cache_reset();
	PnxAtlas gone;
	BA_CHECK(!pnx_atlas_load(&gone, PNX_ASSET_ATLAS_TILES));
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE
