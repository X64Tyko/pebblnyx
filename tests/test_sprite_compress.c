// Host test for `compress = "lzss"` against a real pipeline-built, LZSS-compressed
// sprite -- test_assets.py's pipeline round-trip (lzss_decompress, the Python mirror)
// proves the byte format is self-consistent; this proves the real C decoder
// (pnx_sprite_load's compressed branch, wired to pnx_lzss_decode) reads it back
// correctly, the same division of labour test_map_compress.c already has for compressed
// maps. Compiled only into build/test_lzss (tests/Makefile's LZSS_SRC).
//
// Uses fixtures/lzss_pixels/ (compress = "lzss", not fixtures/lzss/ itself, which stays
// uncompressed for test_map_compress.c's own PNX_COMPRESS_NONE binary -- see that
// fixture's own comment), a single flat (maximally repetitive, so it actually
// compresses) 16x16 sprite frame.

#include "../src/pnx/pnx_config.h"

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdbool.h>
#include <stdio.h>

#define LZSS_DIR "fixtures/lzss_pixels/resources/"
#include "fixtures/lzss_pixels/gen.h"

extern int s_failures;
extern int s_checks;

#define SC_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

void test_sprite_compress(void);

static const char* s_sc_files[] = PNX_ASSET_FILE_TABLE;
static uint32_t s_sc_resources[PNX_ASSET_COUNT];
static char s_sc_paths[PNX_ASSET_COUNT][80];

void test_sprite_compress(void)
{
	printf("sprite_compress\n");

	pnx_host_reset();
	for (int i = 0; i < PNX_ASSET_COUNT; i++)
	{
		s_sc_resources[i] = (uint32_t)(i + 1);
		snprintf(s_sc_paths[i], sizeof(s_sc_paths[i]), "%s%s", LZSS_DIR, s_sc_files[i]);
		FILE* f = fopen(s_sc_paths[i], "rb");
		if (!f)
		{
			printf(
				"  SKIP sprite_compress: %s not built -- run tools/pnx_assets.py in "
				"tests/fixtures/lzss\n",
				s_sc_paths[i]);
			return;
		}
		fclose(f);
		pnx_host_register_resource(s_sc_resources[i], s_sc_paths[i]);
	}

	PnxArena arena;
	SC_CHECK(pnx_arena_init(&arena, "sprite-compress-arena", 20 * 1024, 4));
	SC_CHECK(pnx_assets_init(&arena, s_sc_resources, PNX_ASSET_COUNT));
	SC_CHECK(pnx_palettes_load(PNX_ASSET_PALETTES_PALETTES));

	PnxSprite sp;
	SC_CHECK(pnx_sprite_load(&sp, PNX_ASSET_SPRITE_FLAT));

	PnxSpriteFrame f0;
	pnx_sprite_frame_get(&sp, 0, &f0);
	SC_CHECK(f0.w == 16 && f0.h == 16);

	// The one frame is a flat, single-colour region of the source sheet (make_sheet's own
	// shape, mirrored in the fixture's sheet.png) -- every nibble of its decoded 4bpp
	// pixels must be the SAME palette index, which is exactly the property a byte-offset
	// or short-decode bug in the compressed path would break first.
#if !PNX_DISPLAY_BW
	const uint8_t* px	   = pnx_sprite_frame(&sp, 0);
	const uint8_t first_hi = (uint8_t)(px[0] >> 4);
	const uint8_t first_lo = (uint8_t)(px[0] & 0x0F);
	SC_CHECK(first_hi == first_lo);
	bool uniform = true;
	for (size_t i = 0; i < (size_t)f0.w * f0.h / 2; i++)
	{
		if (px[i] != px[0])
		{
			uniform = false;
			break;
		}
	}
	SC_CHECK(uniform);
#endif
}
