// Stand-alone verification that a 1-bit build is exclusively 2bpp: it correctly decodes
// the pipeline's real ~bw resource, and correctly REFUSES the untagged colour one rather
// than silently misreading it. That refusal is the point -- an earlier version of this
// engine could carry both a 4bpp-indexed decoder and a 2bpp one in the same BW binary
// (auto-detecting which a given blob was, by size), which was the fix for a real shipped
// bug: 2bpp resources sent to a binary compiled to expect 4bpp ones. That dual-format
// path is gone now -- a build reads ONE format, unconditionally -- so this file's job
// changed from "prove both decode" to "prove the wrong one is rejected, not misread".
//
// Not part of the main `make test` binary: that build is deliberately colour (PNX_HOST_BW
// unset), and this needs PNX_HOST_BW=1. Compiled and run directly, one line, no
// continuations (a `//` comment ending in a backslash is a warning of its own):
//
//   cc -std=c11 -Wall -Wextra -Wno-unused-parameter -DPNX_PLATFORM_HOST -DPNX_HOST_BW=1 -DPNX_USE_SYNTH=1 -g -O1 test_pack2bit_bw.c ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c ../src/pnx/assets/pnx_assets.c ../src/pnx/platform/pnx_platform_host.c -o /tmp/test_pack2bit_bw && /tmp/test_pack2bit_bw

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>

#define RES_DIR "../examples/overworld/resources/"

static int s_checks, s_failures;

#define CHECK(cond)                                                  \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

int main(void)
{
#if !PNX_DISPLAY_BW
	(void)s_checks;
	(void)s_failures;
	printf("test_pack2bit_bw: build with -DPNX_HOST_BW=1, this is a no-op otherwise\n");
	return 0;
#else
	printf("test_pack2bit_bw (PNX_DISPLAY_BW=%d)\n", PNX_DISPLAY_BW);

	pnx_host_reset();

	enum
	{
		R_TILES_BW,
		R_TILES_COLOUR,
		R_COUNT,
	};
	pnx_host_register_resource(R_TILES_BW, RES_DIR "tiles~bw.bin");
	pnx_host_register_resource(R_TILES_COLOUR, RES_DIR "tiles.bin");

	static PnxArena arena;
	CHECK(pnx_arena_init(&arena, "arena", 68 * 1024, 4));
	const uint32_t resources[R_COUNT] = { R_TILES_BW, R_TILES_COLOUR };
	CHECK(pnx_assets_init(&arena, resources, R_COUNT));

	// A no-op on this build (PnxPalette's own comment, pnx_assets.h) -- still called, and
	// still expected to succeed trivially, because game init code is the same source on
	// every platform and must not need an #if just to skip this on BW.
	CHECK(pnx_palettes_load(0));

	// The real ~bw file the pipeline emits: decodes at the 2bpp formula.
	PnxAtlas bw = { 0 };
	CHECK(pnx_atlas_load(&bw, R_TILES_BW));
	CHECK(bw.tile_px > 0);
	CHECK(bw.tile_bytes == (uint16_t)(bw.tile_px * bw.tile_px / 4));

	// The untagged colour file, on THIS build: refused, not misread. tiles.bin is 4bpp,
	// this loader only ever computes the 2bpp expected size now, and the two cannot
	// coincidentally match for any real atlas -- see atlas_load_into's own comment for why.
	PnxAtlas colour = { 0 };
	CHECK(!pnx_atlas_load(&colour, R_TILES_COLOUR));

	printf("%d checks, %d failures\n", s_checks, s_failures);
	return s_failures ? 1 : 0;
#endif
}
