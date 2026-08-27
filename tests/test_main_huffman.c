// main() for build/test_huffman (tests/Makefile's HUFFMAN_OUT) -- see test_main_lzss.c's
// own comment for why PNX_COMPRESS_MODE-specific suites each need their own binary and
// therefore their own main(), rather than sharing test_core.c's.

#include <stdio.h>

int s_failures;
int s_checks;

void test_huffman_compress(void);

int main(void)
{
	printf("pebblnyx huffman-mode tests\n");
	test_huffman_compress();
	printf("\n%d checks, %d failures\n", s_checks, s_failures);
	return s_failures == 0 ? 0 : 1;
}
