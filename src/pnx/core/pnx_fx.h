// 16.16 fixed point.
//
// Fixed rather than float so that simulation is bit-identical across builds, which is
// a prerequisite for replay, and because the sim should not depend on FPU behaviour.
// Range is +/-32767 whole units, far beyond any map that fits in 128KB.
//
// Header-only and free-standing: no platform dependency, so it compiles on the host.

#pragma once

#include <stdint.h>
#include <stdbool.h>

typedef int32_t pnx_fx;

#define PNX_FX_SHIFT 16
#define PNX_FX_ONE	 (1 << PNX_FX_SHIFT)
#define PNX_FX_HALF	 (PNX_FX_ONE / 2)

// Shifts through uint32_t: left-shifting a NEGATIVE signed value is undefined
// behaviour, and this is called with negative coordinates constantly. Conversion back
// is two's complement on every target that matters and is mandated by C23.
static inline pnx_fx pnx_fx_from_int(int32_t i)
{
	return (pnx_fx)((uint32_t)i << PNX_FX_SHIFT);
}

// Arithmetic shift, so this floors toward negative infinity rather than truncating
// toward zero. That keeps world-to-tile conversion correct for negative coordinates,
// where a divide would land a pixel on the wrong side of the origin.
#define pnx_fx_to_int(v) ((int32_t)((v) >> PNX_FX_SHIFT))
#define pnx_fx_floor(v)	 ((int32_t)((v) >> PNX_FX_SHIFT))

static inline pnx_fx pnx_fx_mul(pnx_fx a, pnx_fx b)
{
	return (pnx_fx)(((int64_t)a * (int64_t)b) >> PNX_FX_SHIFT);
}

static inline pnx_fx pnx_fx_div(pnx_fx a, pnx_fx b)
{
	if (b == 0)
		return 0;
	return (pnx_fx)(((int64_t)a << PNX_FX_SHIFT) / b);
}

static inline pnx_fx pnx_fx_abs(pnx_fx v)
{
	return v < 0 ? -v : v;
}

static inline pnx_fx pnx_fx_clamp(pnx_fx v, pnx_fx lo, pnx_fx hi)
{
	return v < lo ? lo : (v > hi ? hi : v);
}

static inline int32_t pnx_clamp_i32(int32_t v, int32_t lo, int32_t hi)
{
	return v < lo ? lo : (v > hi ? hi : v);
}

// Floors toward negative infinity, unlike C's division which truncates toward zero.
// A camera at x = -1 must select tile -1, not tile 0, or the column at a map's left
// edge is dropped as the view scrolls past it.
static inline int32_t pnx_floor_div(int32_t a, int32_t b)
{
	const int32_t d = a / b;
	return (a % b != 0 && ((a < 0) != (b < 0))) ? d - 1 : d;
}

static inline int32_t pnx_min_i32(int32_t a, int32_t b)
{
	return a < b ? a : b;
}
static inline int32_t pnx_max_i32(int32_t a, int32_t b)
{
	return a > b ? a : b;
}

// Integer square root by Newton's method. Present so distance comparisons and standard
// deviations do not drag in libm, which would cost code budget for one operation.
static inline int32_t pnx_isqrt(int32_t v)
{
	if (v <= 0)
		return 0;
	int32_t x = v, y = (x + 1) / 2;
	while (y < x)
	{
		x = y;
		y = (x + v / x) / 2;
	}
	return x;
}