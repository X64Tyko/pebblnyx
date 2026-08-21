// Easing curves and time-based value interpolation.
//
// Generalizes a pattern that kept getting hand-rolled per game: a 0..1000 fixed-point
// progress value, driven by either elapsed time or (Need4Pebble's own sky/sun day-night
// cycle) accumulated world distance, feeding a per-channel colour or position lerp. This
// pulls that out once rather than leaving the next game to re-derive it.
//
// Fixed-point, not float, same reasoning as pnx_fx.h: this codebase avoids libm/FPU
// dependence everywhere else, and there is no reason a tween needs to be the exception.
// Progress is int32_t 0..1000, not 0..PNX_FX_ONE -- deliberately a different fixed point
// than pnx_fx's 16.16, because every caller so far (including the code this generalizes)
// thinks in round thousandths ("40% through"), not binary fractions, and forcing a
// conversion at every call site would cost more than it buys.
//
// Header-only, like pnx_fx.h: the whole API is small enough that a separate translation
// unit would only add a build-file entry for no benefit. Gated behind PNX_USE_TWEEN
// (pnx_config.h) unlike pnx_fx.h, though -- pnx_fx is foundational and unconditional;
// this is a removable convenience a game can opt out of, per direct request.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_TWEEN

#include "pnx_fx.h"

#include <stdint.h>
#include <stdbool.h>

// t1000 is always the INPUT progress, 0 at a tween's start and 1000 at its end. An
// easing function reshapes that into an OUTPUT progress on the same 0..1000 scale,
// which pnx_tween_i32/pnx_tween_gcolor8/PnxTween's own value getter then use to
// interpolate. Every curve here passes through (0,0) and (1000,1000) exactly, so
// swapping one for another never changes a tween's start/end values, only its pacing.
typedef int32_t (*PnxEaseFn)(int32_t t1000);

static inline int32_t pnx_ease_linear(int32_t t1000)
{
	return t1000;
}

static inline int32_t pnx_ease_in_quad(int32_t t1000)
{
	return (int32_t)(((int64_t)t1000 * t1000) / 1000);
}

static inline int32_t pnx_ease_out_quad(int32_t t1000)
{
	const int32_t u = 1000 - t1000;
	return 1000 - (int32_t)(((int64_t)u * u) / 1000);
}

// Quad in for the first half, quad out for the second -- the standard "slow, fast,
// slow" shape, built from the two halves above rather than a separate polynomial.
static inline int32_t pnx_ease_in_out_quad(int32_t t1000)
{
	if (t1000 < 500)
		return pnx_ease_in_quad(t1000 * 2) / 2;
	return 500 + pnx_ease_out_quad((t1000 - 500) * 2) / 2;
}

static inline int32_t pnx_ease_in_cubic(int32_t t1000)
{
	const int64_t t = t1000;
	return (int32_t)((t * t / 1000) * t / 1000);
}

static inline int32_t pnx_ease_out_cubic(int32_t t1000)
{
	const int64_t u = 1000 - t1000;
	return 1000 - (int32_t)((u * u / 1000) * u / 1000);
}

static inline int32_t pnx_ease_in_out_cubic(int32_t t1000)
{
	if (t1000 < 500)
		return pnx_ease_in_cubic(t1000 * 2) / 2;
	return 500 + pnx_ease_out_cubic((t1000 - 500) * 2) / 2;
}

// Plain integer lerp at a given (already-eased, if the caller wants that) progress.
// `from`/`to` may be in either order and t1000 is not clamped here -- a caller past a
// tween's own duration wants pnx_tween_value's clamping, not this, since an easing
// function with overshoot (a "back" curve, not provided above but a valid PnxEaseFn)
// can legitimately send t1000 outside 0..1000 mid-tween.
static inline int32_t pnx_tween_i32(int32_t from, int32_t to, int32_t t1000)
{
	return from + (int32_t)(((int64_t)(to - from) * t1000) / 1000);
}

// Same, per-channel, for this platform's GColor8 byte (2 bits/channel: 0xC0 | rrggbb --
// see any game's own RGB() macro, e.g. Need4Pebble's render.c). Channels are
// interpolated independently and re-packed; alpha (the top two bits) is left at the
// opaque 0xC0 every GColor8 in this codebase already uses.
static inline uint8_t pnx_tween_gcolor8(uint8_t from, uint8_t to, int32_t t1000)
{
	const int32_t r0 = (from >> 4) & 3, g0 = (from >> 2) & 3, b0 = from & 3;
	const int32_t r1 = (to >> 4) & 3, g1 = (to >> 2) & 3, b1 = to & 3;
	const int32_t r = pnx_tween_i32(r0, r1, t1000);
	const int32_t g = pnx_tween_i32(g0, g1, t1000);
	const int32_t b = pnx_tween_i32(b0, b1, t1000);
	return (uint8_t)(0xC0 | (pnx_clamp_i32(r, 0, 3) << 4) | (pnx_clamp_i32(g, 0, 3) << 2) |
					 pnx_clamp_i32(b, 0, 3));
}

// A value moving from `from` to `to` over `duration_ms`, shaped by `ease`. Pure
// function of elapsed time since pnx_tween_start (pnx_tween_value takes `now_ms`
// rather than advancing internal state) -- same idiom PnxAnimState/pnx_anim_frame
// (pnx_sprite.h) already use, and for the same reason: queryable any number of times a
// frame, from a paused or covered app, without drifting.
typedef struct
{
	int32_t from, to;
	uint32_t start_ms, duration_ms;
	PnxEaseFn ease;
} PnxTween;

// `duration_ms` of 0 is legal and means "already done" -- pnx_tween_value/
// pnx_tween_done treat now_ms >= start_ms as complete rather than dividing by zero.
static inline void pnx_tween_start(PnxTween* tw, int32_t from, int32_t to,
								   uint32_t duration_ms, PnxEaseFn ease, uint32_t now_ms)
{
	tw->from		= from;
	tw->to			= to;
	tw->start_ms	= now_ms;
	tw->duration_ms = duration_ms;
	tw->ease		= ease;
}

// Clamped to [start, end] outside the tween's own time span -- before start_ms this
// reads as `from`, at/after start_ms+duration_ms it reads as `to`, so a caller need not
// special-case "has it started yet" or "is it over" before asking for a value.
static inline int32_t pnx_tween_value(const PnxTween* tw, uint32_t now_ms)
{
	if (now_ms < tw->start_ms)
		return tw->from;
	const uint32_t elapsed = now_ms - tw->start_ms;
	if (tw->duration_ms == 0 || elapsed >= tw->duration_ms)
		return tw->to;
	const int32_t t1000 = (int32_t)(((uint64_t)elapsed * 1000) / tw->duration_ms);
	return pnx_tween_i32(tw->from, tw->to, tw->ease(t1000));
}

static inline bool pnx_tween_done(const PnxTween* tw, uint32_t now_ms)
{
	return now_ms >= tw->start_ms + tw->duration_ms;
}

#endif // PNX_USE_TWEEN
