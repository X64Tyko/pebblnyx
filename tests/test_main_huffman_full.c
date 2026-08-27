// main() for build/test_huffman_full (tests/Makefile's HUFFMAN_FULL_OUT) -- the engine
// integration suite (loaders + caches + real pipeline fixtures), separate from
// build/test_huffman (the codec-only suite, HUFFMAN_SRC) the same way
// test_main_bitplane.c's suite is separate from a hypothetical codec-only bitplane
// binary -- see test_main_lzss.c's own comment for why each PNX_COMPRESS_MODE gets its
// own binary and therefore its own main().

#include <stdio.h>

int s_failures;
int s_checks;

void test_huffman_atlas(void);
void test_huffman_sprite(void);

int main(void)
{
	printf("pebblnyx huffman-mode integration tests\n");
	test_huffman_atlas();
	test_huffman_sprite();
	printf("\n%d checks, %d failures\n", s_checks, s_failures);
	return s_failures == 0 ? 0 : 1;
}
