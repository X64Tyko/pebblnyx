// Host tests for src/pnx/gfx/pnx_sprite.c's playback/collision-adjacent additions:
// PnxAnimState/pnx_anim_play/pnx_anim_frame (named clips with per-frame duration), the
// PnxSpriteFrame accessors (variable-size frames, per-frame origin, per-frame collision),
// and pnx_physics_collide_mask (the previously-unbuilt COMPLEX-mask walker).
//
// The PnxSprite fixture below is hand-built, not loaded from a blob -- these are pure
// logic over the struct's own layout, the same reasoning test_physics.c gives for
// hand-deriving its expected numbers rather than eyeballing them. The blob FORMAT itself
// (the byte layout pnx_sprite_load actually parses) is exercised by test_assets.c against
// real pipeline output instead, which is where a format bug would actually show up.

#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/gfx/pnx_sprite.h"
#include "../src/pnx/physics/pnx_physics.h"

#include <stdio.h>

extern int s_failures;
extern int s_checks;

#define S_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define S_CHECK_EQ(a, b)                                                                     \
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

// Two frames: frame 0 is 4x4, COMPLEX, kind HURT, one ink pixel at its bottom-right
// corner (3,3); frame 1 is 2x4, SCALED, kind WALL (the default), rect (0,1,2,3). Byte
// layout matches PNX_SPRITE_FRAME_BYTES exactly (pnx_assets.h's own comment above
// PnxSpriteFrame): u16 offset, u8 w, u8 h, u8 origin_x, u8 origin_y, u8 flags, u8 pad.
static const uint8_t s_frame_meta[] = {
	0,
	0,
	4,
	4,
	2,
	4,
	(PNX_COLLISION_KIND_HURT << 2) | PNX_COLLISION_COMPLEX,
	0,
	8,
	0,
	2,
	4,
	1,
	4,
	(PNX_COLLISION_KIND_WALL << 2) | PNX_COLLISION_SCALED,
	0,
};
// frame 0: 4*4/2 = 8 bytes; frame 1: 2*4/2 = 4 bytes. Content is never decoded through a
// palette here, so the values themselves are arbitrary.
static const uint8_t s_pixels[8 + 4]	= { 0 };
static const uint8_t s_frame_palette[2] = { 0, 0 };
// One SCALED record (frame 1): u16 frame, u8 x, y, w, h.
static const uint8_t s_scaled[] = { 1, 0, 0, 1, 2, 3 };
// One COMPLEX record (frame 0): u16 frame, then a 4x4 row-major MSB-first 1bpp mask --
// ink only at (3,3), pixel index 15, byte 1 bit 0x01.
static const uint8_t s_complex[] = { 0, 0, 0x00, 0x01 };

static PnxSprite fixture_sprite(void)
{
	PnxSprite s		= { 0 };
	s.pixels		= s_pixels;
	s.frame_meta	= s_frame_meta;
	s.frame_palette = s_frame_palette;
	s.scaled_rects	= s_scaled;
	s.complex_masks = s_complex;
	s.scaled_count	= 1;
	s.complex_count = 1;
	s.frame_count	= 2;
	return s;
}

static void test_frame_accessors(void)
{
	PnxSprite s = fixture_sprite();

	PnxSpriteFrame f0, f1;
	pnx_sprite_frame_get(&s, 0, &f0);
	pnx_sprite_frame_get(&s, 1, &f1);
	S_CHECK_EQ(f0.w, 4);
	S_CHECK_EQ(f0.h, 4);
	S_CHECK_EQ(f0.origin_x, 2);
	S_CHECK_EQ(f0.origin_y, 4);
	S_CHECK(f0.pixels == s_pixels + 0);
	S_CHECK_EQ(f1.w, 2);
	S_CHECK_EQ(f1.h, 4);
	S_CHECK_EQ(f1.origin_x, 1);
	S_CHECK_EQ(f1.origin_y, 4);
	S_CHECK(f1.pixels == s_pixels + 8);

	S_CHECK(pnx_sprite_frame(&s, 1) - pnx_sprite_frame(&s, 0) == 8);

	// flags = (kind << 2) | mode, the same byte tile_flags packs (pnx_assets.h).
	S_CHECK_EQ(PNX_COLLISION_MODE(pnx_sprite_frame_flags(&s, 0)), PNX_COLLISION_COMPLEX);
	S_CHECK_EQ(PNX_COLLISION_KIND(pnx_sprite_frame_flags(&s, 0)), PNX_COLLISION_KIND_HURT);
	S_CHECK_EQ(PNX_COLLISION_MODE(pnx_sprite_frame_flags(&s, 1)), PNX_COLLISION_SCALED);
	S_CHECK_EQ(PNX_COLLISION_KIND(pnx_sprite_frame_flags(&s, 1)), PNX_COLLISION_KIND_WALL);

	uint8_t x = 0, y = 0, w = 0, h = 0;
	S_CHECK(pnx_sprite_frame_scaled_rect(&s, 1, &x, &y, &w, &h));
	S_CHECK_EQ(x, 0);
	S_CHECK_EQ(y, 1);
	S_CHECK_EQ(w, 2);
	S_CHECK_EQ(h, 3);
	S_CHECK(!pnx_sprite_frame_scaled_rect(&s, 0, &x, &y, &w, &h)); // frame 0 is COMPLEX

	const uint8_t* mask = pnx_sprite_frame_complex_mask(&s, 0);
	S_CHECK(mask != NULL);
	S_CHECK(pnx_sprite_frame_complex_mask(&s, 1) == NULL); // frame 1 is SCALED
	if (mask)
	{
		S_CHECK(pnx_collision_mask_pixel(mask, 4, 3, 3));  // the one authored ink pixel
		S_CHECK(!pnx_collision_mask_pixel(mask, 4, 0, 0)); // everywhere else is empty
		S_CHECK(!pnx_collision_mask_pixel(mask, 4, 2, 3));
	}
}

// A 4-frame clip, fps=8 (125 ms/base-tick), durations [1, 2, 1, 2] -- frames 2 and 4 (1-
// indexed; array indices 1 and 3) hold twice as long as 1 and 3, per your own spec: "a 4
// frame 8fps animation can have 1,2,1,2 for frame durations meaning the second and 4th
// frames are twice as long as the 1st and 3rd." Cumulative tick boundaries: frame0 [0,1),
// frame1 [1,3), frame2 [3,4), frame3 [4,6) -- total 6 ticks = 750 ms.
static const uint8_t s_clip[]	   = { 5, 7, 5, 7 };
static const uint8_t s_durations[] = { 1, 2, 1, 2 };

static void test_anim_play_idempotent(void)
{
	PnxAnimState st = { 0 };
	pnx_anim_play(&st, s_clip, 4, 1000);
	S_CHECK(st.frames == s_clip);
	S_CHECK_EQ(st.count, 4);
	S_CHECK_EQ(st.start_ms, 1000);

	// Replaying the SAME clip pointer must not reset start_ms -- the ordinary usage is
	// calling this every frame with "whatever should be playing right now".
	pnx_anim_play(&st, s_clip, 4, 5000);
	S_CHECK_EQ(st.start_ms, 1000);

	// A genuinely different clip DOES reset, from wherever it's told to start.
	static const uint8_t other[] = { 9 };
	pnx_anim_play(&st, other, 1, 5000);
	S_CHECK(st.frames == other);
	S_CHECK_EQ(st.start_ms, 5000);
}

static void test_anim_frame_durations(void)
{
	PnxAnimState st = { 0 };
	pnx_anim_play(&st, s_clip, 4, 1000);

	// Within frame 0's [0,1) tick window (0..124 ms elapsed).
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, true, 1000), 5);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, true, 1000 + 124), 5);
	// Tick 1: frame 1's window starts, and holds through tick 2 (twice as long).
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, true, 1000 + 125), 7);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, true, 1000 + 374), 7);
	// Tick 3: back to frame 0's value (index 2), one tick wide again.
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, true, 1000 + 375), 5);
	// Tick 4: frame 3's value (index 3), two ticks wide.
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, true, 1000 + 500), 7);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, true, 1000 + 749), 7);
	// Tick 6 (750 ms): loops back to frame 0.
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, true, 1000 + 750), 5);

	// Non-looping: once the clip's total tick length is spent, it holds the LAST frame
	// rather than wrapping.
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, false, 1000 + 750), 7);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, s_durations, false, 1000 + 100000), 7);
}

static void test_anim_frame_equal_timing(void)
{
	// NULL durations -- the generated header's `#define NAME_CLIP_DURATIONS NULL` case --
	// means every frame holds exactly one base tick.
	PnxAnimState st = { 0 };
	pnx_anim_play(&st, s_clip, 4, 0);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, NULL, true, 0), 5);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, NULL, true, 125), 7);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, NULL, true, 250), 5);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, NULL, true, 375), 7);
	S_CHECK_EQ(pnx_anim_frame(&st, 8, NULL, true, 500), 5); // wraps: 4 ticks total

	// A single-frame clip degenerates cleanly: always that one frame, looping or not.
	static const uint8_t one[] = { 42 };
	PnxAnimState single		   = { 0 };
	pnx_anim_play(&single, one, 1, 0);
	S_CHECK_EQ(pnx_anim_frame(&single, 8, NULL, true, 0), 42);
	S_CHECK_EQ(pnx_anim_frame(&single, 8, NULL, true, 100000), 42);
	S_CHECK_EQ(pnx_anim_frame(&single, 8, NULL, false, 100000), 42);
}

#if PNX_USE_PHYSICS
static void test_collide_mask(void)
{
	// A 4x4 mask, ink only at local (3,3) -- the same fixture mask test_frame_accessors
	// reads via pnx_sprite_frame_complex_mask, walked here directly. Placed at world
	// origin (100, 100), so the one ink pixel sits at world (103, 103).
	static const uint8_t mask[] = { 0x00, 0x01 };

	// A 5px-radius ball resting 2px into the ink pixel, approaching from the left --
	// the same "resting 2px into a surface" scenario test_physics.c's own segment/AABB/
	// point tests already use, reused here so this checks pnx_physics_collide_mask
	// agrees with the primitives it is built on rather than asserting new numbers.
	PnxBall ball;
	pnx_physics_ball_init(&ball, 103 - 5 + 2, 103, 5);
	ball.vx = pnx_fx_from_int(3);
	S_CHECK(pnx_physics_collide_mask(&ball, mask, 4, 4, 100, 100, PNX_FX_ONE));
	S_CHECK(pnx_fx_to_int(ball.vx) < 0); // reflected off the ink pixel

	// An all-empty mask has no ink pixel to hit -- no contact, ball untouched.
	static const uint8_t empty_mask[] = { 0x00, 0x00 };
	PnxBall untouched;
	pnx_physics_ball_init(&untouched, 103 - 5 + 2, 103, 5);
	S_CHECK(!pnx_physics_collide_mask(&untouched, empty_mask, 4, 4, 100, 100, PNX_FX_ONE));
}
#endif

void test_sprite(void)
{
	printf("sprite\n");
	test_frame_accessors();
	test_anim_play_idempotent();
	test_anim_frame_durations();
	test_anim_frame_equal_timing();
#if PNX_USE_PHYSICS
	test_collide_mask();
#endif
}
