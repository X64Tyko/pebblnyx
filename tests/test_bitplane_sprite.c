// Host test for the NEW compress_sprites = "bitplane" path in pnx_sprite_load
// (pnx_assets.c) -- against a REAL pipeline build (fixtures/bitplane_sprite/, a
// dedicated fixture kept separate from fixtures/lzss/ so this doesn't repurpose the
// fixture test_sprite_compress.c already depends on for the LZSS path specifically).
//
// Four frames, two byte-identical (frame 0 == frame 2) on purpose: proves the new
// per-deduped-unit bitplane encoding survives build_sprite_frame_meta's existing dedup
// rather than silently decoding a duplicate frame wrong.

#include "../src/pnx/pnx_config.h"

#if PNX_USE_BITPLANE_COMPRESS

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

#define BS_DIR "fixtures/bitplane_sprite/resources/"
#include "fixtures/bitplane_sprite/gen.h"

extern int s_failures;
extern int s_checks;

#define BS_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_bitplane_sprite(void);

static const char* s_files[] = PNX_ASSET_FILE_TABLE;
static uint32_t s_resources[PNX_ASSET_COUNT];
static char s_paths[PNX_ASSET_COUNT][96];

void test_bitplane_sprite(void)
{
	printf("bitplane_sprite\n");

	pnx_host_reset();
	for (int i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_resources[i] = (uint32_t)(i + 1);
		snprintf(s_paths[i], sizeof(s_paths[i]), "%s%s", BS_DIR, s_files[i]);
		FILE* f = fopen(s_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP bitplane_sprite: %s not built -- run tools/pnx_assets.py in "
				"tests/fixtures/bitplane_sprite\n",
				s_paths[i]);
			return;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_paths[i]);
	}

	PnxArena arena;
	BS_CHECK(pnx_arena_init(&arena, "bitplane-sprite-arena", 20 * 1024, 4));
	BS_CHECK(pnx_assets_init(&arena, s_resources, PNX_ASSET_COUNT));
	BS_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));

	PnxSprite sp;
	BS_CHECK(pnx_sprite_load(&sp, PNX_ASSET_SPRITE_QUAD));
	BS_CHECK(sp.frame_count == QUAD_FRAME_COUNT);

	PnxSpriteFrame f0, f1, f2, f3;
	pnx_sprite_frame_get(&sp, 0, &f0);
	pnx_sprite_frame_get(&sp, 1, &f1);
	pnx_sprite_frame_get(&sp, 2, &f2);
	pnx_sprite_frame_get(&sp, 3, &f3);
	BS_CHECK(f0.w == 16 && f0.h == 16);
	BS_CHECK(f1.w == 16 && f1.h == 16);
	BS_CHECK(f2.w == 16 && f2.h == 16);
	BS_CHECK(f3.w == 16 && f3.h == 16);

	// Frame 0 and frame 2 are the SAME source rect -- dedup means they should share one
	// physical pixel span, so their `pixels` pointers must be equal, not merely
	// byte-identical (proves the deduped-unit encoding round-tripped through the SAME
	// slot both frame_meta entries point at, not two independent decodes that just
	// happen to agree).
	BS_CHECK(f0.pixels == f2.pixels);

	// Frame 1 and frame 3 are DIFFERENT source rects -- must NOT collide with frame 0
	// or each other.
	BS_CHECK(f1.pixels != f0.pixels);
	BS_CHECK(f3.pixels != f0.pixels);
	BS_CHECK(f1.pixels != f3.pixels);

	// Every frame's pixels must actually be populated (not all-zero, which is what a
	// silently-failed decode leaving the arena's zero-fill in place would look like).
	bool any_nonzero_1 = false, any_nonzero_3 = false;
	for (int i = 0; i < 16 * 16 / 2; i++)
	{
		if (pnx_sprite_frame(&sp, 1)[i] != 0)
			any_nonzero_1 = true;
		if (pnx_sprite_frame(&sp, 3)[i] != 0)
			any_nonzero_3 = true;
	}
	BS_CHECK(any_nonzero_1);
	BS_CHECK(any_nonzero_3);
}

#else

void test_bitplane_sprite(void);
void test_bitplane_sprite(void)
{
}

#endif // PNX_USE_BITPLANE_COMPRESS
