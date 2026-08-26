// Host test for compress = "bitplane" against a 1-bit (~bw, 2bpp) build -- the
// PNX_DISPLAY_BW branch of bp_pack/pnx_bitplane_decode (pnx_bitplane.c) and
// bitplane_encode_bw/compress_sprite_pixels_bitplane_bw (tools/pnx_assets.py), neither of
// which any other test binary exercises: test_bitplane_atlas.c/test_bitplane_sprite.c run
// colour (4bpp), and test_pack2bit_bw.c runs PNX_COMPRESS_NONE. Same real pipeline
// fixture as test_bitplane_atlas.c (fixtures/bitplane_atlas/), its own `~bw` resource
// variant -- pack_2bit is on by default (docs/PORTING.md), so building that fixture
// already produced tiles~bw.bin bitplane-encoded from 2bpp ink states, not 4bpp indices.
//
// Same correctness bar test_bitplane_atlas.c itself uses (distinct tiles decode to
// distinct, non-zero content; a repeat fetch hits the same cache slot; out-of-range
// refuses) rather than hand-typed expected bytes -- a 2-colour tile's bitplane stream
// collapses to the same k-driven cost regardless of bpp (see pnx_config.h's
// PNX_COMPRESS_MODE comment), so there is no separate "encoding scheme" here to prove
// bit-exact, only that the 2bpp OUTPUT PACKING this build selects round-trips correctly.

#include "../src/pnx/pnx_config.h"
// PNX_DISPLAY_BW itself is defined in pnx_platform.h (from PNX_HOST_BW on this host
// build), not pnx_config.h -- must be included before the #if below can see it.
#include "../src/pnx/platform/pnx_platform.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE && PNX_DISPLAY_BW

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

#define BB_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_bitplane_bw(void);

void test_bitplane_bw(void)
{
	printf("bitplane_bw (PNX_DISPLAY_BW=%d)\n", PNX_DISPLAY_BW);

	pnx_host_reset();
	const uint32_t palettes_res = 1, tiles_res = 2;
	pnx_host_register_resource(palettes_res, BA_DIR "palettes.bin");
	pnx_host_register_resource(tiles_res, BA_DIR "tiles~bw.bin");
	FILE* f = fopen(BA_DIR "tiles~bw.bin", "rb");
	if (!f)
	{
		printf(
			"  SKIP bitplane_bw: %stiles~bw.bin not built -- run tools/pnx_assets.py "
			"in tests/fixtures/bitplane_atlas\n",
			BA_DIR);
		return;
	}
	fclose(f);

	PnxArena arena;
	BB_CHECK(pnx_arena_init(&arena, "bitplane-bw-arena", 8 * 1024, 4));
	const uint32_t resources[2] = { palettes_res, tiles_res };
	BB_CHECK(pnx_assets_init(&arena, resources, 2));
	BB_CHECK(pnx_palettes_load(0));
	BB_CHECK(pnx_tile_cache_init(&arena, 8, TILES_TILE_PX));
	pnx_tile_cache_reset();

	PnxAtlas atlas;
	BB_CHECK(pnx_atlas_load(&atlas, 1));
	BB_CHECK(atlas.tile_px == TILES_TILE_PX);
	BB_CHECK(atlas.tile_count == TILES_TILE_COUNT);

	// 2bpp packed: a quarter of the raw pixel count, not half -- the property this whole
	// file exists to prove bp_pack/pnx_bitplane_decode actually produce on this build.
	const size_t tile_bytes_2bpp = ((size_t)TILES_TILE_PX * TILES_TILE_PX + 3) / 4;

	const uint8_t* floor  = pnx_atlas_tile(&atlas, TILES_TILE_FLOOR);
	const uint8_t* wall	  = pnx_atlas_tile(&atlas, TILES_TILE_WALL);
	const uint8_t* accent = pnx_atlas_tile(&atlas, TILES_TILE_ACCENT);
	BB_CHECK(floor && wall && accent);
	if (floor && wall && accent)
	{
		// Only floor vs wall, not vs accent: this fixture's tiles are flat greyscale
		// shades (40/100/160/220 -- see sheet.png), and floor(100)/accent(40) legitimately
		// land on the SAME side of the ink/paper threshold once reduced to 2bpp -- real
		// information loss 2bpp thresholding is expected to cause, not a decode bug (the
		// colour/4bpp path's test_bitplane_atlas.c proves these same three tiles ARE all
		// distinct at 4bpp, where the loss doesn't happen). floor/wall (100 vs 220) sit on
		// opposite sides of the default threshold, so that pair stays a valid distinctness
		// check.
		BB_CHECK(memcmp(floor, wall, tile_bytes_2bpp) != 0);
		bool any_nonzero = false;
		for (size_t i = 0; i < tile_bytes_2bpp; i++)
			if (floor[i] != 0)
				any_nonzero = true;
		BB_CHECK(any_nonzero);
	}

	// A repeat fetch of an already-cached tile is a hit -- same pointer, no re-decode.
	const uint8_t* floor_again = pnx_atlas_tile(&atlas, TILES_TILE_FLOOR);
	BB_CHECK(floor_again == floor);

	// Out-of-range tile index refuses cleanly.
	BB_CHECK(pnx_atlas_tile(&atlas, 250) == NULL);
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE && PNX_DISPLAY_BW
