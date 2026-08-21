// Host tests for pnx_tween.h.
//
// Pure integer math, no platform/asset dependency at all -- unlike most of this
// directory's other suites, nothing here needs a resource blob or a PnxTarget.

#include "../src/pnx/core/pnx_tween.h"

#include <stdio.h>

extern int s_failures;
extern int s_checks;

#define T_CHECK_EQ(a, b)                                                                     \
	do                                                                                       \
	{                                                                                        \
		s_checks++;                                                                          \
		const long _a = (long)(a), _b = (long)(b);                                           \
		if (_a != _b)                                                                        \
		{                                                                                    \
			printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n", __FILE__, __LINE__, #a, #b, _a, \
				   _b);                                                                      \
			s_failures++;                                                                    \
		}                                                                                    \
	} while (0)

#define T_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

// Every curve passes through (0,0) and (1000,1000) exactly -- swapping one easing
// function for another must never change a tween's start/end values, only its pacing.
static void test_ease_anchors(void)
{
	PnxEaseFn fns[] = {
		pnx_ease_linear,
		pnx_ease_in_quad,
		pnx_ease_out_quad,
		pnx_ease_in_out_quad,
		pnx_ease_in_cubic,
		pnx_ease_out_cubic,
		pnx_ease_in_out_cubic,
	};
	for (size_t i = 0; i < sizeof(fns) / sizeof(fns[0]); i++)
	{
		T_CHECK_EQ(fns[i](0), 0);
		T_CHECK_EQ(fns[i](1000), 1000);
	}
}

static void test_ease_shapes(void)
{
	T_CHECK_EQ(pnx_ease_linear(500), 500);

	// "in" starts slow: below the diagonal at the midpoint. "out" starts fast: above it.
	T_CHECK(pnx_ease_in_quad(500) < 500);
	T_CHECK(pnx_ease_out_quad(500) > 500);
	T_CHECK(pnx_ease_in_cubic(500) < 500);
	T_CHECK(pnx_ease_out_cubic(500) > 500);

	// Cubic is the more extreme curve -- further from the diagonal than quad at the
	// same input, on both sides.
	T_CHECK(pnx_ease_in_cubic(500) < pnx_ease_in_quad(500));
	T_CHECK(pnx_ease_out_cubic(500) > pnx_ease_out_quad(500));

	// in_out is built from in+out halves and must be exactly symmetric: the midpoint
	// of the curve is the midpoint of progress, for both shapes.
	T_CHECK_EQ(pnx_ease_in_out_quad(500), 500);
	T_CHECK_EQ(pnx_ease_in_out_cubic(500), 500);
	T_CHECK_EQ(pnx_ease_in_out_quad(0), 0);
	T_CHECK_EQ(pnx_ease_in_out_quad(1000), 1000);

	// Monotonic: none of these curves should ever reverse direction.
	int32_t prev = -1;
	for (int32_t t = 0; t <= 1000; t += 50)
	{
		const int32_t v = pnx_ease_in_out_cubic(t);
		T_CHECK(v >= prev);
		prev = v;
	}
}

static void test_tween_i32(void)
{
	T_CHECK_EQ(pnx_tween_i32(0, 100, 0), 0);
	T_CHECK_EQ(pnx_tween_i32(0, 100, 1000), 100);
	T_CHECK_EQ(pnx_tween_i32(0, 100, 500), 50);
	T_CHECK_EQ(pnx_tween_i32(100, 0, 250), 75); // descending, still linear in t1000
	T_CHECK_EQ(pnx_tween_i32(-50, 50, 500), 0); // crosses zero cleanly
}

static void test_tween_gcolor8(void)
{
	const uint8_t black = 0xC0;		   // RGB(0,0,0)
	const uint8_t grey	= 0xC0 | 0x2A; // RGB(2,2,2): 10 10 10

	T_CHECK_EQ(pnx_tween_gcolor8(black, grey, 0), black);
	T_CHECK_EQ(pnx_tween_gcolor8(black, grey, 1000), grey);
	T_CHECK_EQ(pnx_tween_gcolor8(black, grey, 500), 0xC0 | 0x15); // each channel 0->1

	// Alpha bits (the top two, always opaque in this codebase) survive unchanged
	// regardless of which colours are being interpolated between.
	T_CHECK_EQ(pnx_tween_gcolor8(black, grey, 500) & 0xC0, 0xC0);
}

static void test_pnx_tween_struct(void)
{
	PnxTween tw;
	pnx_tween_start(&tw, 0, 100, 1000, pnx_ease_linear, 1000);

	T_CHECK_EQ(pnx_tween_value(&tw, 500), 0);	 // before start_ms: reads as `from`
	T_CHECK_EQ(pnx_tween_value(&tw, 1000), 0);	 // AT start_ms (elapsed 0): still `from`,
												 // not `to` -- exercised specifically since
												 // this is the boundary a naive `<=` check
												 // gets backwards.
	T_CHECK_EQ(pnx_tween_value(&tw, 1500), 50);	 // halfway through
	T_CHECK_EQ(pnx_tween_value(&tw, 2000), 100); // exactly at the end
	T_CHECK_EQ(pnx_tween_value(&tw, 5000), 100); // long past the end: clamped, not extrapolated

	T_CHECK(!pnx_tween_done(&tw, 999));
	T_CHECK(!pnx_tween_done(&tw, 1999));
	T_CHECK(pnx_tween_done(&tw, 2000));
	T_CHECK(pnx_tween_done(&tw, 5000));

	// duration_ms == 0: legal, means "already done" the instant start_ms is reached --
	// no divide-by-zero, no special case the caller has to know about.
	PnxTween instant;
	pnx_tween_start(&instant, 5, 9, 0, pnx_ease_linear, 1000);
	T_CHECK_EQ(pnx_tween_value(&instant, 999), 5);	// still before start
	T_CHECK_EQ(pnx_tween_value(&instant, 1000), 9); // reached start == already done
	T_CHECK_EQ(pnx_tween_value(&instant, 2000), 9);
	T_CHECK(!pnx_tween_done(&instant, 999));
	T_CHECK(pnx_tween_done(&instant, 1000));

	// A non-linear ease actually changes the mid-tween value, not just the anchors --
	// confirms PnxTween really calls through `ease` rather than always linear.
	PnxTween eased;
	pnx_tween_start(&eased, 0, 1000, 1000, pnx_ease_in_quad, 0);
	T_CHECK_EQ(pnx_tween_value(&eased, 500), pnx_ease_in_quad(500));
	T_CHECK(pnx_tween_value(&eased, 500) < 500); // in_quad: slow start, below the diagonal
}

void test_tween(void)
{
	printf("tween\n");

	test_ease_anchors();
	test_ease_shapes();
	test_tween_i32();
	test_tween_gcolor8();
	test_pnx_tween_struct();
}
