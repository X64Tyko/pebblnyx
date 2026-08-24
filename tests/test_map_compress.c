// Host test for `compress_maps` (M12) against a real pipeline-built, LZSS-compressed map
// -- the integration test_assets.c's decoder fixture and test_assets.py's pipeline
// round-trip don't cover between them: does worldtile_load_run's compressed branch
// (pnx_assets.c) actually wire pnx_lzss_decode into a real PnxMap load correctly, offset
// arithmetic and all, not just "the two halves are each independently right".
//
// fixtures/lzss/ is a small, dedicated manifest (not one of the examples/) built with
// `compress_maps = true`, checked in like any other example's resources.

#include "../src/pnx/pnx_config.h"

#if PNX_USE_MAP_COMPRESS

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

#define LZSS_DIR "fixtures/lzss/resources/"
#include "fixtures/lzss/gen.h"

extern int s_failures;
extern int s_checks;

#define M_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_map_compress(void);

static const char* s_files[] = PNX_ASSET_FILE_TABLE;
static uint32_t s_resources[PNX_ASSET_COUNT];
static char s_paths[PNX_ASSET_COUNT][80];

void test_map_compress(void)
{
	printf("map_compress\n");

	pnx_host_reset();
	for (int i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_resources[i] = (uint32_t)(i + 1);
		snprintf(s_paths[i], sizeof(s_paths[i]), "%s%s", LZSS_DIR, s_files[i]);
		FILE* f = fopen(s_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP map_compress: %s not built -- run tools/pnx_assets.py in "
				"tests/fixtures/lzss\n",
				s_paths[i]);
			return;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_paths[i]);
	}

	PnxArena arena;
	M_CHECK(pnx_arena_init(&arena, "lzss-arena", 20 * 1024, 4));
	M_CHECK(pnx_assets_init(&arena, s_resources, PNX_ASSET_COUNT));
	M_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));

	PnxMap m;
	M_CHECK(pnx_map_load(&m, PNX_ASSET_MAP_A));
	const PnxMapLayer* l = &m.layers[m.primary_layer];
	M_CHECK(m.compressed);
	M_CHECK((int)l->w == MAP_A_W);
	M_CHECK((int)l->h == MAP_A_H);

	// Held whole (the map fits its pool), so pnx_map_load already ran the compressed
	// branch of worldtile_load_run for every WorldTile by the time this returns -- the
	// exact thing this file exists to prove works, not merely compiles.
	M_CHECK(pnx_map_resident(&m) == l->wt_cols * l->wt_rows);

	// The border is wall, the interior start tile is not -- same shape of assertion
	// test_assets.c already makes against an UNCOMPRESSED map, run here against a
	// compressed one so the two are checked against the same expectations.
	M_CHECK(pnx_map_solid(&m, 0, 0));
	M_CHECK(!pnx_map_solid(&m, 1, 1));

	// The accent tiles the manifest paints at (3,4)/(4,4) must read back as themselves,
	// not as whatever their neighbours are -- the one thing a byte-offset bug in the
	// compressed read/decode path would get wrong first.
	uint16_t accent_local = 0;
	const PnxAtlas* accent_atlas =
		pnx_map_atlas(&m, pnx_map_tile(&m, 3, 4), &accent_local);
	M_CHECK(accent_atlas != NULL);
	M_CHECK((int)accent_local == TILES_TILE_ACCENT);
	M_CHECK(pnx_map_tile(&m, 3, 4) == pnx_map_tile(&m, 4, 4));

	// And a floor tile elsewhere in a DIFFERENT WorldTile of the same bank must still read
	// correctly too -- proving the offset used inside the decoded bank, not just the first
	// WorldTile's.
	uint16_t floor_local		= 0;
	const PnxAtlas* floor_atlas = pnx_map_atlas(&m, pnx_map_tile(&m, 6, 6), &floor_local);
	M_CHECK(floor_atlas != NULL);
	M_CHECK((int)floor_local == TILES_TILE_FLOOR);
}

#else

void test_map_compress(void);
void test_map_compress(void)
{
}

#endif // PNX_USE_MAP_COMPRESS
