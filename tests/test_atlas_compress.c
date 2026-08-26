// Host test for `compress = "lzss"` against a real pipeline-built, LZSS-compressed atlas
// -- test_assets.py's pipeline round-trip (lzss_decompress, the Python mirror) proves the
// byte format is self-consistent; test_map_compress.c proves the pool/`dst`-provided
// decode branch of `atlas_load_into` (the one every map actually uses), against its own
// uncompressed fixture. Neither exercises `pnx_atlas_load`'s own `dst=NULL`, fresh-arena-
// buffer branch, which is a different code path with no precedent to copy from (see that
// branch's own comment in pnx_assets.c). Compiled only into build/test_lzss
// (tests/Makefile's LZSS_SRC).
//
// Uses fixtures/lzss_pixels/ (compress = "lzss" -- see that fixture's own comment for why
// it is separate from fixtures/lzss/), whose own map "a" (loaded here too, `compress_maps`
// off) references this same atlas -- the pool-load path is cross-checked against the
// direct one below rather than a hand-typed expected byte sequence: both decode the
// identical on-disk compressed stream, so if either has an offset bug the two will
// disagree, tile role identities and all.

#include "../src/pnx/pnx_config.h"

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

#define LZSS_DIR "fixtures/lzss_pixels/resources/"
#include "fixtures/lzss_pixels/gen.h"

extern int s_failures;
extern int s_checks;

#define AC_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_atlas_compress(void);

static const char* s_ac_files[] = PNX_ASSET_FILE_TABLE;
static uint32_t s_ac_resources[PNX_ASSET_COUNT];
static char s_ac_paths[PNX_ASSET_COUNT][80];

void test_atlas_compress(void)
{
	printf("atlas_compress\n");

	pnx_host_reset();
	for (int i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_ac_resources[i] = (uint32_t)(i + 1);
		snprintf(s_ac_paths[i], sizeof(s_ac_paths[i]), "%s%s", LZSS_DIR, s_ac_files[i]);
		FILE* f = fopen(s_ac_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP atlas_compress: %s not built -- run tools/pnx_assets.py in "
				"tests/fixtures/lzss\n",
				s_ac_paths[i]);
			return;
		}
		fclose(f);
		pnx_host_register_resource(s_ac_resources[i], s_ac_paths[i]);
	}

	PnxArena arena;
	AC_CHECK(pnx_arena_init(&arena, "atlas-compress-arena", 20 * 1024, 4));
	AC_CHECK(pnx_assets_init(&arena, s_ac_resources, PNX_ASSET_COUNT));
	AC_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));

	// The direct, dst=NULL path: a fresh arena buffer sized for the DECODED pixels,
	// decoded straight out of the compressed blob (no scratch/reshape needed, unlike the
	// pool path -- see atlas_load_into's own comment for why the two differ).
	PnxAtlas direct;
	AC_CHECK(pnx_atlas_load(&direct, PNX_ASSET_ATLAS_TILES));
	AC_CHECK(direct.tile_px == TILES_TILE_PX);
	AC_CHECK(direct.tile_count == TILES_TILE_COUNT);
	AC_CHECK(direct.tile_bytes == TILES_TILE_BYTES);

	// The pool path: load map "a" (references this same atlas), pin it into a pool slot.
	// test_map_compress.c already proves this branch decodes correctly on its own; reused
	// here only as the independent reference to compare the direct path against.
	PnxMap m;
	AC_CHECK(pnx_map_load(&m, PNX_ASSET_MAP_A));

	uint16_t accent_local = 0;
	const PnxAtlas* pooled_accent =
		pnx_map_atlas(&m, pnx_map_tile(&m, 3, 4), &accent_local);
	AC_CHECK(pooled_accent != NULL);
	AC_CHECK((int)accent_local == TILES_TILE_ACCENT);

	uint16_t floor_local		 = 0;
	const PnxAtlas* pooled_floor = pnx_map_atlas(&m, pnx_map_tile(&m, 6, 6), &floor_local);
	AC_CHECK(pooled_floor != NULL);
	AC_CHECK((int)floor_local == TILES_TILE_FLOOR);

	// Same on-disk compressed stream, two different decode branches -- their output must
	// be byte-identical, which is exactly what an offset/length bug unique to one branch
	// would break.
	if (pooled_accent && pooled_floor)
	{
		const uint8_t* direct_accent = pnx_atlas_tile(&direct, TILES_TILE_ACCENT);
		const uint8_t* pool_accent	 = pnx_atlas_tile(pooled_accent, accent_local);
		AC_CHECK(direct_accent != NULL && pool_accent != NULL);
		if (direct_accent && pool_accent)
			AC_CHECK(memcmp(direct_accent, pool_accent, TILES_TILE_BYTES) == 0);

		const uint8_t* direct_floor = pnx_atlas_tile(&direct, TILES_TILE_FLOOR);
		const uint8_t* pool_floor	= pnx_atlas_tile(pooled_floor, floor_local);
		AC_CHECK(direct_floor != NULL && pool_floor != NULL);
		if (direct_floor && pool_floor)
			AC_CHECK(memcmp(direct_floor, pool_floor, TILES_TILE_BYTES) == 0);

		// The accent and floor tiles must not decode to the same bytes either -- a decode
		// that silently reads zeros or repeats one tile would pass the checks above by
		// accident if both compared buffers were equally wrong.
		AC_CHECK(memcmp(direct_accent, direct_floor, TILES_TILE_BYTES) != 0);
	}
}
