// main() for build/test_bitplane_bw (tests/Makefile's BWBP_OUT) -- see test_main_lzss.c's
// own comment for why PNX_COMPRESS_MODE-specific suites each need their own binary.
// PNX_HOST_BW=1 on top of that here, since this one is specifically the BITPLANE x
// PNX_DISPLAY_BW combination no other binary exercises.

#include <stdio.h>

int s_failures;
int s_checks;

void test_bitplane_bw(void);

int main(void)
{
	printf("pebblnyx bitplane-mode BW tests\n");
	test_bitplane_bw();
	printf("\n%d checks, %d failures\n", s_checks, s_failures);
	return s_failures == 0 ? 0 : 1;
}
