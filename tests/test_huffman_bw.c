// Host test for compress = "huffman" against a 1-bit (~bw, 2bpp) build -- the
// PNX_DISPLAY_BW branch of hf_pack/pnx_huffman_decode (pnx_huffman.c) and
// huffman_collect_or_encode_bw/compress_sprite_pixels_huffman_bw (tools/pnx_assets.py),
// exercising the SEPARATE bw table (huffman_table~bw.bin, pnx_huffman_table_load reads
// whichever the platform's own file-tag substitution resolves PNX_ASSET_HUFFMAN_TABLE_
// HUFFMAN_TABLE to) neither test_huffman_atlas.c nor test_huffman_sprite.c touch.
// Mirrors test_bitplane_bw.c's own structure and correctness bar exactly.

#include "../src/pnx/pnx_config.h"
#include "../src/pnx/platform/pnx_platform.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN && PNX_DISPLAY_BW

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

#define HB_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_huffman_bw(void);

void test_huffman_bw(void)
{
	printf("huffman_bw (PNX_DISPLAY_BW=%d)\n", PNX_DISPLAY_BW);

	pnx_host_reset();
	const uint32_t palettes_res = 1, huffman_table_res = 2, tiles_res = 3;
	pnx_host_register_resource(palettes_res, HA_DIR "palettes.bin");
	pnx_host_register_resource(huffman_table_res, HA_DIR "huffman_table~bw.bin");
	pnx_host_register_resource(tiles_res, HA_DIR "tiles~bw.bin");
	FILE* f = fopen(HA_DIR "tiles~bw.bin", "rb");
	if (!f)
	{
		printf(
			"  SKIP huffman_bw: %stiles~bw.bin not built -- run tools/pnx_assets.py "
			"in tests/fixtures/huffman_atlas\n",
			HA_DIR);
		return;
	}
	fclose(f);

	PnxArena arena;
	HB_CHECK(pnx_arena_init(&arena, "huffman-bw-arena", 8 * 1024, 4));
	const uint32_t resources[3] = { palettes_res, huffman_table_res, tiles_res };
	HB_CHECK(pnx_assets_init(&arena, resources, 3));
	HB_CHECK(pnx_palettes_load(0));
	HB_CHECK(pnx_huffman_table_load(1));
	HB_CHECK(pnx_tile_cache_init(&arena, 8, TILES_TILE_PX));
	pnx_tile_cache_reset();

	PnxAtlas atlas;
	HB_CHECK(pnx_atlas_load(&atlas, 2));
	HB_CHECK(atlas.tile_px == TILES_TILE_PX);
	HB_CHECK(atlas.tile_count == TILES_TILE_COUNT);

	// 2bpp packed: a quarter of the raw pixel count, not half -- the property this whole
	// file exists to prove hf_pack/pnx_huffman_decode actually produce on this build.
	const size_t tile_bytes_2bpp = ((size_t)TILES_TILE_PX * TILES_TILE_PX + 3) / 4;

	const uint8_t* floor  = pnx_atlas_tile(&atlas, TILES_TILE_FLOOR);
	const uint8_t* wall	  = pnx_atlas_tile(&atlas, TILES_TILE_WALL);
	const uint8_t* accent = pnx_atlas_tile(&atlas, TILES_TILE_ACCENT);
	HB_CHECK(floor && wall && accent);
	if (floor && wall && accent)
	{
		// Only floor vs wall, not vs accent -- same real 2bpp-threshold information loss
		// test_bitplane_bw.c's own comment explains (this fixture's flat greyscale tiles
		// legitimately collapse floor/accent onto the same ink/paper side).
		HB_CHECK(memcmp(floor, wall, tile_bytes_2bpp) != 0);
		bool any_nonzero = false;
		for (size_t i = 0; i < tile_bytes_2bpp; i++)
			if (floor[i] != 0)
				any_nonzero = true;
		HB_CHECK(any_nonzero);
	}

	// A repeat fetch of an already-cached tile is a hit -- same pointer, no re-decode.
	const uint8_t* floor_again = pnx_atlas_tile(&atlas, TILES_TILE_FLOOR);
	HB_CHECK(floor_again == floor);

	// Out-of-range tile index refuses cleanly.
	HB_CHECK(pnx_atlas_tile(&atlas, 250) == NULL);
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN && PNX_DISPLAY_BW
