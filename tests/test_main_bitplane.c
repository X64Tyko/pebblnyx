// main() for build/test_bitplane (tests/Makefile's BITPLANE_OUT) -- see test_main_lzss.c's
// own comment for why PNX_COMPRESS_MODE-specific suites each need their own binary and
// therefore their own main(), rather than sharing test_core.c's.

#include <stdio.h>

int s_failures;
int s_checks;

void test_bitplane_compress(void);
void test_tile_cache(void);
void test_bitplane_atlas(void);
void test_bitplane_sprite(void);
void test_sprite_cache(void);

int main(void)
{
	printf("pebblnyx bitplane-mode tests\n");
	test_bitplane_compress();
	test_tile_cache();
	test_bitplane_atlas();
	test_bitplane_sprite();
	test_sprite_cache();
	printf("\n%d checks, %d failures\n", s_checks, s_failures);
	return s_failures == 0 ? 0 : 1;
}
