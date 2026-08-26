// main() for build/test_lzss (tests/Makefile's LZSS_OUT) -- PNX_COMPRESS_LZSS-specific
// behaviour lives in its own binary now (PNX_COMPRESS_MODE is a project-wide, mutually
// exclusive compile-time choice, so one binary cannot exercise all three modes the way
// the old, additive PNX_USE_*_COMPRESS flags let test_core.c do). Deliberately as small
// as test_core.c's own main() is large: just the two LZSS-specific suites, sharing their
// s_checks/s_failures/CHECK convention rather than test_core.c's own copy, since that one
// is compiled into a different binary entirely.

#include <stdio.h>

int s_failures;
int s_checks;

void test_sprite_compress(void);
void test_atlas_compress(void);

int main(void)
{
	printf("pebblnyx LZSS-mode tests\n");
	test_sprite_compress();
	test_atlas_compress();
	printf("\n%d checks, %d failures\n", s_checks, s_failures);
	return s_failures == 0 ? 0 : 1;
}
