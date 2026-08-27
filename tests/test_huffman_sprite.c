// Host test for the compress = "huffman" path in pnx_sprite_load (pnx_assets.c) --
// against a REAL pipeline build (fixtures/huffman_sprite/, same source art as
// fixtures/bitplane_sprite's own fixture). Mirrors test_bitplane_sprite.c exactly,
// including its deliberate frame 0 == frame 2 dedup case. Compiled only into
// build/test_huffman_full (tests/Makefile's HUFFMAN_FULL_SRC).

#include "../src/pnx/pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/assets/pnx_sprite_cache.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>

#define HS_DIR "fixtures/huffman_sprite/resources/"
#include "fixtures/huffman_sprite/gen.h"

extern int s_failures;
extern int s_checks;

#define HS_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_huffman_sprite(void);

static const char* s_files[] = PNX_ASSET_FILE_TABLE;
static uint32_t s_resources[PNX_ASSET_COUNT];
static char s_paths[PNX_ASSET_COUNT][96];

void test_huffman_sprite(void)
{
	printf("huffman_sprite\n");

	pnx_host_reset();
	for (int i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_resources[i] = (uint32_t)(i + 1);
		snprintf(s_paths[i], sizeof(s_paths[i]), "%s%s", HS_DIR, s_files[i]);
		FILE* f = fopen(s_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP huffman_sprite: %s not built -- run tools/pnx_assets.py in "
				"tests/fixtures/huffman_sprite\n",
				s_paths[i]);
			return;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_paths[i]);
	}

	PnxArena arena;
	HS_CHECK(pnx_arena_init(&arena, "huffman-sprite-arena", 20 * 1024, 4));
	HS_CHECK(pnx_assets_init(&arena, s_resources, PNX_ASSET_COUNT));
	HS_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));
	HS_CHECK(pnx_huffman_table_load(PNX_ASSET_HUFFMAN_TABLE_HUFFMAN_TABLE));
	HS_CHECK(pnx_sprite_cache_init(&arena, 8));
	pnx_sprite_cache_reset();

	PnxSprite sp;
	HS_CHECK(pnx_sprite_load(&sp, PNX_ASSET_SPRITE_QUAD));
	HS_CHECK(sp.frame_count == QUAD_FRAME_COUNT);

	PnxSpriteFrame f0, f1, f2, f3;
	pnx_sprite_frame_get(&sp, 0, &f0);
	pnx_sprite_frame_get(&sp, 1, &f1);
	pnx_sprite_frame_get(&sp, 2, &f2);
	pnx_sprite_frame_get(&sp, 3, &f3);
	HS_CHECK(f0.w == 16 && f0.h == 16);
	HS_CHECK(f1.w == 16 && f1.h == 16);
	HS_CHECK(f2.w == 16 && f2.h == 16);
	HS_CHECK(f3.w == 16 && f3.h == 16);
	HS_CHECK(f0.pixels && f1.pixels && f2.pixels && f3.pixels);

	// Frame 0 and frame 2 are the SAME source rect -- dedup means they should share one
	// physical pixel span (the same cache slot), proving the deduped-unit huffman
	// encoding round-tripped through the SAME cache entry both frame_meta entries
	// resolve to, not two independent decodes that just happen to agree.
	HS_CHECK(f0.pixels == f2.pixels);

	// Frame 1 and frame 3 are DIFFERENT source rects -- must NOT collide with frame 0 or
	// each other.
	HS_CHECK(f1.pixels != f0.pixels);
	HS_CHECK(f3.pixels != f0.pixels);
	HS_CHECK(f1.pixels != f3.pixels);

	// Every frame's pixels must actually be populated (not all-zero, which is what a
	// silently-failed decode leaving the cache pool's zero-fill in place would look like).
	bool any_nonzero_1 = false, any_nonzero_3 = false;
	for (int i = 0; i < 16 * 16 / 2; i++)
	{
		if (f1.pixels[i] != 0)
			any_nonzero_1 = true;
		if (f3.pixels[i] != 0)
			any_nonzero_3 = true;
	}
	HS_CHECK(any_nonzero_1);
	HS_CHECK(any_nonzero_3);
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN
