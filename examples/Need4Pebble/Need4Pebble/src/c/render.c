#include "render.h"
#include "track.h"
// GROUND_TILE_PX/GROUND_LIGHT_* (draw_ground_tile_row's tile constants and anim clip) --
// game.h deliberately does NOT include this project-wide (see its own comment on
// TOURING_CRASH_CRASH_COUNT), but render.c is a leaf .c file, the same way game.c already
// includes it directly for its own asset ids.
#include "assets_gen.h"

// ------------------------------------------------------------------------- colour
//
// Raw GColor8 bytes (AARRGGBB, 2 bits/channel, alpha fixed opaque) -- what
// pnx_gfx_fill_rect/pnx_gfx_clear write directly, no palette indirection. Placeholder
// synthwave-adjacent values; DESIGN.md flags the actual art pass as not done yet.
#define RGB(r, g, b) (uint8_t)(0xC0 | ((r) << 4) | ((g) << 2) | (b))

#define COLOUR_GROUND_A RGB(0, 1, 2)
#define COLOUR_GROUND_B RGB(0, 0, 1)
#define COLOUR_ROAD_A	RGB(1, 0, 2)
#define COLOUR_ROAD_B	RGB(0, 0, 1)
// Paved shoulder, not a racetrack kerb -- a wide alternating white/red band (the
// original rumble strip) read as circuit curbing, not a real road's edge, per direct
// feedback. Two close, muted grey-tan shades (same alternating-band motion cue
// COLOUR_GROUND_A/B already use) instead of a bold two-colour stripe; COLOUR_EDGE_LINE
// (below) is the actual edge marking now, not the shoulder's own colour.
#define COLOUR_SHOULDER_A RGB(1, 1, 1)
#define COLOUR_SHOULDER_B RGB(1, 1, 0)
#define COLOUR_EDGE_LINE  RGB(3, 3, 3)	// white fog line painted at the pavement edge
#define EDGE_LINE_FRAC	  14			// edge line half-width = row.half_width/EDGE_LINE_FRAC
#define COLOUR_LINE		  RGB(0, 3, 3)	// cyan lane dividers
#define LANE_DASH_FRAC	  30			// dash half-width = row.half_width/LANE_DASH_FRAC -- a real
										// lane line's width relative to the road, not a bold neon bar
#define COLOUR_GRID		   RGB(3, 0, 3) // magenta ground grid, beyond the road edge
#define GRID_SPACING	   40			// world units between each ground grid line, beyond ROAD_HALF_MAX
#define GRID_LINE_COUNT	   3			// grid lines drawn per side
#define COLOUR_MENU_BG	   RGB(0, 0, 0) // opaque black -- no alpha blending exists to dim with
#define COLOUR_MENU_TEXT   RGB(3, 3, 3) // white
#define COLOUR_POLICE_RED  RGB(3, 0, 0)
#define COLOUR_POLICE_BLUE RGB(0, 0, 3)

// Deep indigo at the top fading to a warm horizon glow (SUNSET), or cool blues fading
// to a pale moonlit horizon (NIGHT) -- `sky_cycle_progress`, below, plus pnx_tween.h's
// `pnx_tween_gcolor8` cross-fade every entry between the two over the length of a
// drive, not just a fixed look.
// A run of flat `pnx_gfx_fill_rect` bands (the original two-colour sky) reads as
// visible steps at this palette's resolution (2 bits/channel, 64 colours total) --
// draw_sky dithers the single row where the ramp index changes instead of cutting hard,
// same trick pixel art has always used to fake a bigger palette.
static const uint8_t SKY_RAMP_SUNSET[] = {
	RGB(0, 0, 1), // top: near-black indigo
	RGB(0, 0, 2), // deep blue
	RGB(1, 0, 2), // purple
	RGB(2, 0, 2), // magenta
	RGB(3, 0, 1), // warm pink-red
	RGB(3, 1, 0), // horizon: orange glow
};
static const uint8_t SKY_RAMP_NIGHT[] = {
	RGB(0, 0, 1), // top: near-black blue (never pure black -- would blend into the road)
	RGB(0, 0, 1),
	RGB(0, 0, 2),
	RGB(0, 1, 2), // teal-blue
	RGB(0, 1, 3),
	RGB(1, 1, 3), // horizon: pale moonlit blue, not warm
};
#define SKY_RAMP_LEN (int32_t)(sizeof(SKY_RAMP_SUNSET) / sizeof(SKY_RAMP_SUNSET[0]))
_Static_assert(sizeof(SKY_RAMP_SUNSET) == sizeof(SKY_RAMP_NIGHT), "keyframes must match length");

// The sun's own gradient, core to rim, same two keyframes / same reason.
static const uint8_t SUN_RAMP_SUNSET[] = {
	RGB(3, 3, 1), // near-white yellow core
	RGB(3, 2, 0), // gold
	RGB(3, 1, 0), // orange
	RGB(3, 0, 1), // hot pink-red rim
};
static const uint8_t SUN_RAMP_NIGHT[] = {
	RGB(2, 2, 3), // pale moon-white core
	RGB(1, 1, 3),
	RGB(1, 0, 2),
	RGB(1, 0, 1), // dim maroon rim
};
#define SUN_RAMP_LEN (int32_t)(sizeof(SUN_RAMP_SUNSET) / sizeof(SUN_RAMP_SUNSET[0]))
_Static_assert(sizeof(SUN_RAMP_SUNSET) == sizeof(SUN_RAMP_NIGHT), "keyframes must match length");

// One full sunset -> night -> sunset breathe, in world units -- "much slower" than an
// earlier 80000 (still visibly moving within one play session), per direct feedback.
// 300000 is ~7-8 minutes each way even at MAX_SPEED held flat out -- a background mood
// shift, not something to watch tick by. A triangle wave rather than a hard loop-reset:
// driving forever never snaps the sky or sun back to a start position, it just keeps
// breathing between the two ends.
#define SKY_CYCLE_LENGTH 300000
// Position is anchored to the TOP of the screen (y=0), not the horizon -- explicitly
// asked for, after the horizon-relative version turned out to still move the sun with
// every hill and valley (`horizon_y` itself changes with the player's slope; keying
// position off it, even indirectly, brings that dependency back). `SUN_TOP_OFFSET` is
// how far down from y=0 the sun's centre sits at the cycle's start (t=0, "farther off
// to the right top of the screen"); `SUN_DROP_Y` carries it further down from there as
// the cycle advances, easing back over the second half. `horizon_y` is still used
// ONLY for the pixel-visibility bound below (`y < horizon_y`) -- a hill SHOULD occlude
// the sun the way it occludes the real horizon, that just must never feed back into
// where the sun's own centre is computed.
#define SUN_RADIUS	   36	   // fixed -- see draw_sun's own comment for why not horizon_y-scaled
#define SUN_TOP_OFFSET 12	   // px down from the top of the screen at t=0 (partly clipped
							   // by the top edge at this point -- peeking in, not fully risen)
#define SUN_START_X_OFFSET 80  // px right of screen centre at t=0
#define SUN_DRIFT_X		   160 // total horizontal travel, t=0 -> t=1000 (right -> left of centre)
#define SUN_DROP_Y		   70  // total vertical travel, t=0 -> t=1000 (down from its start height)

// 0 at a cycle's start/end (sunset), 1000 at its midpoint (night) -- fixed-point
// (x1000), not float; this build has no other use for libm either. Triangle, not
// sawtooth: distance only ever increases, but the MOOD should ease back rather than
// jump, so it rises for the first half of SKY_CYCLE_LENGTH and falls for the second.
//
// Deliberately NOT a PnxTween (pnx_tween.h): that primitive is a one-shot `from`..`to`
// run driven by elapsed real time (`now_ms`); this is a REPEATING triangle wave driven
// by world DISTANCE, which has no `now_ms` to hand it and never "finishes" -- forcing
// it through PnxTween would mean re-`pnx_tween_start`ing two tweens back to back by
// hand, which is no simpler than the three lines below. What DOES move to the shared
// library is the actual per-channel/per-value LERP once this has produced a t1000 --
// see `pnx_tween_gcolor8`/`pnx_tween_i32` at every call site below.
static int32_t sky_cycle_progress(uint32_t distance)
{
	const uint32_t phase = distance % SKY_CYCLE_LENGTH;
	const uint32_t half	 = SKY_CYCLE_LENGTH / 2;
	if (phase < half)
		return (int32_t)((phase * 1000) / half);			 // sunset -> night
	return (int32_t)(1000 - ((phase - half) * 1000) / half); // night -> sunset
}

// Screen-space rect (author/logical frame) -> framebuffer rect, for BUTTONS_TOP.
// Point form is fx = VIEW_H-1-ay, fy = ax (tools/pnx_assets.py rotate_point); extended
// to a rect by taking the far corner on the axis that mirrors (y here), the same way
// examples/pinball/src/c/render.c's fb_rect does for its own (BUTTONS_BOTTOM) rotation.
static void fb_rect(PnxTarget* target, int32_t ax, int32_t ay, int32_t aw, int32_t ah,
					uint8_t colour)
{
	pnx_gfx_fill_rect(target, VIEW_H - ay - ah, ax, ah, aw, colour);
}

// Same rotation as fb_rect, for pnx_gfx_fill_rect_dither.
static void fb_rect_dither(PnxTarget* target, int32_t ax, int32_t ay, int32_t aw, int32_t ah,
						   uint8_t colour_a, uint8_t colour_b)
{
	pnx_gfx_fill_rect_dither(target, VIEW_H - ay - ah, ax, ah, aw, colour_a, colour_b);
}

// Same point rotation, undecorated -- what pnx_text_draw's (x, y) wants directly. Text
// glyphs are pre-rotated at pack time just like sprite frames (docs/PLATFORM.md: "the
// buttons do not rotate... glyphs turn with everything else"), so for a
// PNX_ADVANCE_Y_* face (any BUTTONS_TOP/BOTTOM font) x is the baseline and y is where
// the pen starts -- pnx_text.h's own comment -- which is exactly (fx, fy) here.
static void fb_point(int32_t ax, int32_t ay, int32_t* fx, int32_t* fy)
{
	*fx = VIEW_H - 1 - ay;
	*fy = ax;
}

// y=0 is the top of the sky (farthest from the camera); y=HORIZON_Y-1 is the row right
// at the (flat-ground baseline) horizon. Static now, not driven by the per-frame
// current_horizon_y -- direct feedback, the same "moves with the ground horizon" issue
// draw_horizon_band's own header comment already describes and fixes for the cityscape,
// applying here too: the sky gradient's own extent/banding was visibly shifting with
// every hill and valley instead of reading as a fixed backdrop. The cityscape (drawn
// after this, extending well past HORIZON_Y -- see its own comment) is what bridges
// whatever gap opens between this static sky and the dynamic ground/road boundary now.
static int32_t sky_ramp_index(int32_t y, int32_t ramp_len)
{
	return pnx_clamp_i32((y * ramp_len) / HORIZON_Y, 0, ramp_len - 1);
}

static void draw_sky(PnxTarget* target, uint32_t distance)
{
	const int32_t t1000 = sky_cycle_progress(distance);
	uint8_t ramp[SKY_RAMP_LEN];
	for (int32_t i = 0; i < SKY_RAMP_LEN; i++)
		ramp[i] = pnx_tween_gcolor8(SKY_RAMP_SUNSET[i], SKY_RAMP_NIGHT[i], t1000);

	for (int32_t y = 0; y < HORIZON_Y; y++)
	{
		const int32_t idx	   = sky_ramp_index(y, SKY_RAMP_LEN);
		const int32_t prev_idx = (y == 0) ? idx : sky_ramp_index(y - 1, SKY_RAMP_LEN);
		if (idx != prev_idx)
			fb_rect_dither(target, 0, y, LOGICAL_W, 1, ramp[prev_idx], ramp[idx]);
		else
			fb_rect(target, 0, y, LOGICAL_W, 1, ramp[idx]);
	}
}

// A classic retrowave horizon sun: a filled circle (per-row half-width via
// `pnx_isqrt`, since a build has no libm), sliced by a few gap bands through the lower
// half -- the sun-behind-venetian-blinds look every retrowave horizon uses -- and
// gradient-shaded core-to-rim with the same dithered-boundary idea `draw_sky` uses.
//
// Fixed size (SUN_RADIUS) AND fixed position, anchored to the top of the screen
// (SUN_TOP_OFFSET) -- both used to track `horizon_y`, which changes every tick with
// the player's own slope, so the sun visibly resized and bobbed with every hill and
// valley instead of reading as a fixed background object. Reported directly, twice:
// first the resizing, then ("the sun is definitely still moving up and down based on
// hills and valleys") the position too, since only the size had been decoupled --
// followed by an explicit request to anchor off the top of the screen rather than the
// horizon at all, which is what SUN_TOP_OFFSET's own comment describes. `horizon_y` is
// still used below for the actual pixel-visibility bound (`y < horizon_y`), which is
// correct and wanted -- a hill SHOULD occlude the sun, the same way it occludes the
// real horizon -- it just must never feed back into where the sun's own centre is.
//
// Drifts from its start position (SUN_START_X_OFFSET/SUN_TOP_OFFSET, top-right of the
// screen) toward centre and down as `sky_cycle_progress` advances (the same cycle
// driving the sky's own colour, so the two always move together), easing back over the
// cycle's second half -- see SKY_CYCLE_LENGTH's own comment for why this breathes
// rather than resets.
static void draw_sun(PnxTarget* target, uint32_t distance, int32_t horizon_y)
{
	const int32_t radius = SUN_RADIUS;

	const int32_t t1000 = sky_cycle_progress(distance);
	uint8_t ramp[SUN_RAMP_LEN];
	for (int32_t i = 0; i < SUN_RAMP_LEN; i++)
		ramp[i] = pnx_tween_gcolor8(SUN_RAMP_SUNSET[i], SUN_RAMP_NIGHT[i], t1000);

	const int32_t sun_x_start = LOGICAL_W / 2 + SUN_START_X_OFFSET;
	const int32_t cx		  = pnx_tween_i32(sun_x_start, sun_x_start - SUN_DRIFT_X, t1000);
	const int32_t center_y	  = pnx_tween_i32(SUN_TOP_OFFSET, SUN_TOP_OFFSET + SUN_DROP_Y, t1000);
	const int32_t diameter	  = radius * 2;

	for (int32_t dy = -radius; dy < radius; dy++)
	{
		const int32_t y = center_y + dy;
		if (y < 0 || y >= horizon_y)
			continue;

		const int32_t hw = pnx_isqrt(radius * radius - dy * dy);
		if (hw <= 0)
			continue;

		// Gap bands: every 4th 4px-tall band through the lower half is skipped
		// entirely, letting the sky gradient behind the sun show through.
		if (dy > 0 && (((dy / 4) & 3) == 3))
			continue;

		const int32_t idx	   = pnx_clamp_i32(((dy + radius) * SUN_RAMP_LEN) / diameter, 0,
											   SUN_RAMP_LEN - 1);
		const int32_t prev_dy  = dy - 1;
		const int32_t prev_idx = pnx_clamp_i32(((prev_dy + radius) * SUN_RAMP_LEN) / diameter, 0,
											   SUN_RAMP_LEN - 1);
		if (dy > -radius && idx != prev_idx)
			fb_rect_dither(target, cx - hw, y, hw * 2, 1, ramp[prev_idx], ramp[idx]);
		else
			fb_rect(target, cx - hw, y, hw * 2, 1, ramp[idx]);
	}
}

// Draws one scanline row's shoulder+road band from g->road_chunk, scaled to this row's
// exact width. `total_half_width` is row.rumble_width -- that field already includes
// half_width (track.c: rumble_width = half + max(2, half/RUMBLE_FRAC)), it is not an
// additive extra, so it alone is both the sprite's scale target and where the ground
// fill (Part 2) has to pick up.
//
// Physical-space call shape mirrors fb_rect's own ax/ay -> x/y swap (VIEW_H - ay - ah,
// ax): the source frame's cross-track axis (frame.h, post pack-time buttons_top
// rotation) maps to the destination's physical ROW COUNT, which is the value that
// changes every scanline row -- see pnx_blit_4bpp_scaled's own header comment and
// road_chunk's assets.toml comment for why this is dst_h, not dst_w.
//
// PNX_DISPLAY_BW-only builds (flint, aplite, diorite) never call this: pnx_blit_4bpp_scaled
// itself does not exist on those (v1 has no ~bw path, see its own header comment), and
// draw_road's own call site guards on the same condition, falling back to the original
// flat fills there instead.
#if !PNX_DISPLAY_BW
static void fb_road_row_scaled(PnxTarget* target, const PnxSprite* road_chunk, int32_t cx,
							   int32_t total_half_width, int32_t dy, int32_t height,
							   uint8_t frame)
{
	PnxSpriteFrame f;
	pnx_sprite_frame_get(road_chunk, frame, &f);
	const PnxPalette* pal = pnx_sprite_frame_palette(road_chunk, frame);

	pnx_blit_4bpp_scaled(target, f.pixels, pal, VIEW_H - dy - height, cx - total_half_width,
						 f.w, f.h, height, total_half_width * 2);
}
#endif

// A fixed 8-tile pattern for horizon_band's 4 skyline variants (tiles 0-3), so the strip
// reads as an actual skyline rather than one tile repeated -- see assets.toml's own
// comment on the tile layout.
static const uint8_t HORIZON_PATTERN[] = { 0, 1, 2, 3, 1, 0, 3, 2 };
#define HORIZON_PATTERN_LEN (int32_t)(sizeof(HORIZON_PATTERN) / sizeof(HORIZON_PATTERN[0]))

// Divides accumulated curve into the horizon strip's own horizontal shift -- deliberately
// coarser than CURVE_SCALE*GROUND_TILE_PX (draw_ground's own divisor), so the backdrop
// drifts less than the road/ground do on a turn: the classic parallax depth cue, distant
// things move less. Tuned by eye, same as CURVE_SCALE/ELEVATION_SCALE (track.h).
#define HORIZON_SHIFT_DIVISOR (CURVE_SCALE * GROUND_TILE_PX * 3)

// One flat 16px-tall strip, positioned right above wherever the road's own horizon_y
// currently sits (elevation moves it exactly the way it moves everything else drawn this
// frame) -- not a fixed screen position, so a hill crest genuinely reveals more of the
// skyline rather than the strip floating disconnected from the road. Horizontal position
// shifts with accumulated road curve (HORIZON_SHIFT_DIVISOR above), sold as "you're
// turning" the same way the ground's own shift is, just at a distant-background rate.
static void draw_horizon_band(PnxTarget* target, const Game* g, int32_t horizon_y)
{
	if (!g->has_horizon_atlas)
		return;

	// Accumulated once across the whole visible depth (not per-row like draw_ground's
	// bands) -- this is a single flat strip, so it only needs ONE representative shift,
	// evaluated at the farthest point the road loop itself ever reaches.
	int32_t curve_dx = 0, curve_x = 0;
	for (int32_t y = VIEW_H - 1; y >= horizon_y; y--)
	{
		RoadRow row;
		road_row(y, horizon_y, &row);
		const int32_t world_z = (int32_t)(g->distance + row.depth);
		curve_dx += track_curve_at(world_z);
		curve_x += curve_dx;
	}
	const int32_t col_shift = curve_x / HORIZON_SHIFT_DIVISOR;

	const uint8_t lit = pnx_anim_frame(&g->horizon_lit_anim, HORIZON_BAND_LIT_FPS,
									   HORIZON_BAND_LIT_DURATIONS, HORIZON_BAND_LIT_LOOP,
									   pnx_platform_now_ms());

	// Static screen position -- HORIZON_Y (track.h), the flat-ground baseline, not the
	// per-frame `horizon_y` parameter (current_horizon_y, which bobs with elevation).
	// Direct feedback: tracking horizon_y made the whole skyline visibly move up and
	// down with every hill/valley, which read as broken rather than as parallax. Drawn
	// after sky/sun (render_game's own call order) but at a fixed rect, so it always
	// fully repaints its own area regardless of what horizon_y happened to be this
	// frame -- ground/road (drawn after this) still cover it from below exactly as
	// before.
	//
	// Extends past HORIZON_Y, not just up to it -- ground's own topmost band is anchored
	// from VIEW_H and stops at a 16px tile boundary, which does not generally land
	// exactly on horizon_y (current_horizon_y ranges HORIZON_Y +-15 today,
	// track.h's own HORIZON_SLOPE_SHIFT comment), so ground can stop short of it.
	// Rather than patching draw_ground's accumulator to chase an exact boundary, the
	// cityscape simply extends far enough past HORIZON_Y to always be there underneath
	// -- direct request: going downhill (a larger horizon_y, ground's own coverage
	// starting further down) should reveal MORE of the city, not a gap. Sized to
	// current_horizon_y's own documented worst case (track.c's clamp, `VIEW_H - 40`)
	// rather than today's actual (smaller) elevation range, so this does not need
	// revisiting if that range ever widens -- the comment there already flags it as a
	// real possibility ("if that ever changes"). The building art already touches each
	// tile's own bottom edge, so each additional identical row underneath reads as the
	// same buildings continuing down, not a seam.
	const int32_t band_top	= HORIZON_Y - HORIZON_BAND_TILE_PX;
	const int32_t deepest_y = VIEW_H - 40;
	const int32_t row_count = 1 + pnx_max_i32(0, (deepest_y - HORIZON_Y + HORIZON_BAND_TILE_PX - 1) / HORIZON_BAND_TILE_PX);

	for (int32_t row = 0; row < row_count; row++)
	{
		const int32_t row_top = band_top + row * HORIZON_BAND_TILE_PX;
		const int32_t phys_x  = VIEW_H - row_top - HORIZON_BAND_TILE_PX;

		// Row 0 only: the actual skyline art (transparent sky-coloured top, solid
		// building-coloured bottom, per assets.toml's own comment on the tile layout).
		// Rows 1+ (the safety extension past HORIZON_Y, above) are NOT drawn with this
		// same tile art repeated -- a building tile's transparent top has real sky
		// behind it at row 0 (draw_sky, drawn earlier this frame), but nothing does at
		// row 1+ (sky stops at HORIZON_Y, and this is the layer meant to fill in
		// underneath it), so stacking the same tile vertically left the transparent
		// portion of every extension row showing raw black -- exactly the black-bar
		// artifact this replaced. A flat solid fill (COLOUR_GROUND_B, the same 2-bit
		// value BUILDING's own art uses, so it matches the visible skyline's colour
		// exactly) has no transparent pixels to leak through, and reads as the
		// buildings' own shadowed base continuing down, which is what it is meant to be.
		if (row > 0)
		{
			fb_rect(target, 0, row_top, LOGICAL_W, HORIZON_BAND_TILE_PX, COLOUR_GROUND_B);
			continue;
		}

		// `x < LOGICAL_W`, not `x + TILE_PX <= LOGICAL_W`: LOGICAL_W is not generally a
		// multiple of HORIZON_BAND_TILE_PX (228 % 32 == 4 on emery, worse on narrower
		// platforms), and stopping the loop before the last tile would overshoot left
		// that remainder undrawn -- a real black gap at the far edge, not a rounding
		// nicety. pnx_blit_4bpp already clips to the target's own bounds, so drawing
		// one tile past LOGICAL_W here is exactly as safe as everywhere else in this
		// file that relies on the same clipping (fb_rect included).
		for (int32_t col = 0, x = 0; x < LOGICAL_W; x += HORIZON_BAND_TILE_PX, col++)
		{
			const int32_t world_col = col + col_shift;
			// (% then +len then % again): a safe, always-non-negative modulo regardless
			// of which way world_col has drifted -- C's % keeps the dividend's sign,
			// which a raw `% len` would turn into a negative array index on a left curve.
			const int32_t pattern_idx =
				((world_col % HORIZON_PATTERN_LEN) + HORIZON_PATTERN_LEN) % HORIZON_PATTERN_LEN;
			const uint8_t tile =
				(((world_col % 5) + 5) % 5 == 0) ? lit : HORIZON_PATTERN[pattern_idx];

			pnx_blit_4bpp(target, pnx_atlas_tile(&g->horizon_atlas, tile),
						  pnx_atlas_tile_palette(&g->horizon_atlas, tile), phys_x, x,
						  HORIZON_BAND_TILE_PX, HORIZON_BAND_TILE_PX, PNX_FLIP_NONE);
		}
	}
}

// Draws the WHOLE ground layer, full width, as its own pass -- before draw_road, which
// draws on top and covers wherever the road actually is. This replaced an earlier
// version that tracked the road's own curve-varying width per tile band (min/max across
// every scanline row in a band, to guarantee no overhang) -- correct, but real
// engineering weight for what is, once ground is simply a layer UNDER the road instead
// of clipped beside it, a non-problem: road repaints its own exact width every row
// regardless of what ground already put there.
//
// The topmost band (anchored from VIEW_H, stepping in fixed GROUND_TILE_PX increments)
// generally does NOT land exactly on horizon_y -- current_horizon_y ranges HORIZON_Y
// +-15 (track.h's HORIZON_SLOPE_SHIFT), so ground can stop up to a tile short of the
// real horizon line most frames. Deliberately not chased with an extra accumulator
// flush here: draw_horizon_band's own static band already extends a full second tile
// row past HORIZON_Y for exactly this reason (see its own header comment), so whatever
// ground leaves uncovered near the horizon shows the cityscape underneath instead of a
// gap -- direct request, not an incidental side effect: going downhill (a larger
// horizon_y, ground's own coverage starting further down the screen) is supposed to
// reveal more of the city, not less.
static void draw_ground(PnxTarget* target, const Game* g, int32_t horizon_y)
{
	const uint8_t light = g->has_ground_atlas
		? pnx_anim_frame(&g->ground_light_anim, GROUND_LIGHT_FPS, GROUND_LIGHT_DURATIONS,
						 GROUND_LIGHT_LOOP, pnx_platform_now_ms())
		: 0;

	// Curve accumulation, walked at SCANLINE granularity even though a tile band only
	// draws once per GROUND_TILE_PX rows -- same double-integration draw_road's own
	// curve_dx/curve_x does (track_curve_at -> rate -> offset), sampled at each band's
	// own boundary. This is what makes a band nearer the horizon show MORE accumulated
	// shift than one nearer the camera during a turn -- exactly draw_road's own "the
	// road curves more sharply toward the horizon" read, now shared by the ground
	// instead of the ground sitting static under a curving road. Recomputed here rather
	// than shared with draw_road's own loop: draw_ground is a separate, earlier pass
	// (this function's own header comment), and the accumulation itself is cheap integer
	// arithmetic, not the draw-call cost this whole feature exists to keep down.
	int32_t curve_dx = 0, curve_x = 0;
	int32_t next_boundary = VIEW_H - GROUND_TILE_PX;
	int32_t row_idx		  = 0;

	for (int32_t y = VIEW_H - 1; y >= horizon_y; y--)
	{
		RoadRow row;
		road_row(y, horizon_y, &row);
		const int32_t world_z = (int32_t)(g->distance + row.depth);
		curve_dx += track_curve_at(world_z);
		curve_x += curve_dx;

		if (y > next_boundary)
			continue;

		const int32_t band_top = next_boundary;
		const bool alt		   = ((row.depth + g->distance) / (BAND_WORLD * 2)) & 1;
		const int32_t phys_x   = VIEW_H - band_top - GROUND_TILE_PX;
		// Same units as draw_road's own `cx` (curve_x / CURVE_SCALE is a screen-pixel
		// offset); one more division turns that into a whole tile-column shift, since
		// this only ever moves which PATTERN a fixed screen column shows, not the
		// column's own screen position -- see the tile-selection line below.
		const int32_t col_shift = (curve_x / CURVE_SCALE) / GROUND_TILE_PX;

		if (!g->has_ground_atlas)
		{
			// Fallback if the placeholder atlas failed to load -- flat COLOUR_GROUND_A/B,
			// same full-width/horizon_y-clamped band shape the tile path below uses.
			fb_rect(target, 0, band_top, LOGICAL_W, GROUND_TILE_PX,
					alt ? COLOUR_GROUND_A : COLOUR_GROUND_B);
			next_boundary -= GROUND_TILE_PX;
			row_idx++;
			continue;
		}

		const uint8_t plain = alt ? 1 : 0; // GROUND tiles 0/1: the old COLOUR_GROUND_A/B pair

		// pnx_blit_metatile requires atlas->metatiles (quadrant-composed tiles) --
		// ground.bin is a plain/flat atlas (4 whole 16x16 tiles, metatiles unset), so
		// its tiles are read directly via pnx_atlas_tile + pnx_blit_4bpp, the same way
		// any other flat atlas tile is (pnx_atlas_tile's own comment). Square tile, so
		// the usual logical/physical w<->h swap (fb_rect's own transform) is a no-op.
		//
		// `x < LOGICAL_W`, not `x + TILE_PX <= LOGICAL_W`: same reasoning as
		// draw_horizon_band's own identical comment -- LOGICAL_W is not generally a
		// multiple of GROUND_TILE_PX, so stopping short of an overshooting last tile
		// leaves a real, visible gap at the far edge instead of a rounding nicety.
		for (int32_t col = 0, x = 0; x < LOGICAL_W; x += GROUND_TILE_PX, col++)
		{
			const uint8_t tile = ((col + col_shift + row_idx) % 4 == 0) ? light : plain;
			pnx_blit_4bpp(target, pnx_atlas_tile(&g->ground_atlas, tile),
						  pnx_atlas_tile_palette(&g->ground_atlas, tile), phys_x, x,
						  GROUND_TILE_PX, GROUND_TILE_PX, PNX_FLIP_NONE);
		}
		next_boundary -= GROUND_TILE_PX;
		row_idx++;
	}
}

static void draw_road(PnxTarget* target, const Game* g, int32_t horizon_y)
{
	const int32_t base_cx = LOGICAL_W / 2;
	int32_t curve_dx = 0, curve_x = 0;
	int32_t elev_dy = 0, elev_y = 0;
	// Curve only ever moves a row's X, so however far it drifts nothing overlaps --
	// each row still lands on its own Y. Elevation moves Y itself: past some slope two
	// rows want the same screen line (handled below by clamping dy to strictly
	// decrease), and going the OTHER way, a steep slope can want to skip several
	// screen lines between one world-row and the next. A single-pixel fb_rect at just
	// `dy` left those skipped lines untouched -- nothing else redraws the
	// horizon_y..VIEW_H-1 band, so they kept showing whatever an EARLIER frame's
	// (different elevation, different colour) sweep had left there, accumulating frame
	// over frame into dense, seemingly-random horizontal static. Measured: a single
	// frame's own dy sequence looked perfectly clean in logs -- the corruption was
	// temporal, not a within-frame collision. Fix is `height`, below: stretch each
	// row's fill to cover however many screen lines it's actually claiming, so nothing
	// is ever left undrawn.
	int32_t prev_dy = VIEW_H;

	// Ground is now drawn as its own pass, before this function is even called -- see
	// draw_ground's own header comment.

	// Near to far (VIEW_H-1 down to horizon_y): curve/elevation both have to
	// accumulate zero at the camera and grow toward the horizon, the same
	// double-integration every segment based pseudo-3D racer does (curve/elevation ->
	// rate -> offset), or the road would appear to bend/rise under a car that hasn't
	// moved. Draw order itself doesn't matter -- these are independent horizontal
	// strips -- only the accumulation direction does.
	for (int32_t y = VIEW_H - 1; y >= horizon_y; y--)
	{
		RoadRow row;
		road_row(y, horizon_y, &row);

		const int32_t world_z = (int32_t)(g->distance + row.depth);
		curve_dx += track_curve_at(world_z);
		curve_x += curve_dx;
		const int32_t cx = base_cx + curve_x / CURVE_SCALE;

		// Hills: shift this row's DRAWN screen row rather than its width or the
		// horizon line -- see track.h's own comment on why that's a cheaper trick
		// than a real camera-height projection, not a full one.
		elev_dy += track_elevation_at(world_z);
		elev_y += elev_dy;
		int32_t dy = y - elev_y / ELEVATION_SCALE;
		if (dy >= prev_dy)
			dy = prev_dy - 1;
		const int32_t height = prev_dy - dy; // rows this world-row covers; >=1, >1 fills a gap
		prev_dy				 = dy;

		const uint32_t band = (row.depth + g->distance) / BAND_WORLD;
		const bool alt		= (band & 1) != 0;

#if !PNX_DISPLAY_BW
		if (g->has_road_chunk)
			fb_road_row_scaled(target, &g->road_chunk, cx, row.rumble_width, dy, height,
							   alt ? 1 : 0);
		else
#endif
		{
			// BW builds, and the fallback if the placeholder sprite failed to load on a
			// colour build -- same fills this replaced, so either case degrades to the
			// old look instead of a gap.
			fb_rect(target, cx - row.rumble_width, dy, row.rumble_width * 2, height,
					alt ? COLOUR_SHOULDER_A : COLOUR_SHOULDER_B);
			fb_rect(target, cx - row.half_width, dy, row.half_width * 2, height,
					alt ? COLOUR_ROAD_A : COLOUR_ROAD_B);
		}

		// Edge/fog line: thin, solid white, straddling the pavement edge -- a real
		// road's edge marking, drawn over both the shoulder and road fills above so it
		// reads crisply regardless of which alternating band either one is on.
		const int32_t edge_hw = pnx_max_i32(1, row.half_width / EDGE_LINE_FRAC);
		fb_rect(target, cx - row.half_width - edge_hw, dy, edge_hw * 2, height, COLOUR_EDGE_LINE);
		fb_rect(target, cx + row.half_width - edge_hw, dy, edge_hw * 2, height, COLOUR_EDGE_LINE);

		// Lane dividers: LANES-1 dashed lines splitting the road width evenly. Alternate
		// bands only, and narrow (LANE_DASH_FRAC, not the old bar-width RUMBLE_FRAC-scale
		// value), so they read as broken lines rather than filled strips.
		if (!alt)
		{
			const int32_t dash_hw = pnx_max_i32(1, row.half_width / LANE_DASH_FRAC);
			for (int32_t lane = 1; lane < LANES; lane++)
			{
				const int32_t lx = cx - row.half_width + (row.half_width * 2 * lane) / LANES;
				fb_rect(target, lx - dash_hw, dy, dash_hw * 2, height, COLOUR_LINE);
			}
		}

		// pnx_blit_4bpp (draw_ground_tile_row) works on both colour and PNX_DISPLAY_BW
		// builds (unlike pnx_blit_4bpp_scaled above, which has no BW path) -- no BW
		// fallback needed here.
		// draw_ground's own fallback (no atlas loaded) draws nothing extra here -- it
		// falls back to the flat COLOUR_GROUND_A/B bands itself, same full-width/
		// horizon_y-clamped shape as the tile path, so there is nothing left for
		// draw_road's own loop to do for ground at all any more.

		// Perspective ground grid, beyond the road edge -- the vanishing-point cue the
		// lane dividers already give the road itself, extended onto the ground. Scaled
		// by row.half_width the same way draw_traffic's own screen_x scales a car sitting
		// off the road centre, so a grid line at a fixed WORLD offset still converges
		// toward the horizon correctly. Every other row only: full coverage doesn't read
		// differently at this resolution and would double the fb_rect calls in the
		// hottest loop in the renderer.
		if ((y & 1) == 0)
		{
			const int32_t grid_hw = pnx_max_i32(1, row.half_width / 20);
			for (int32_t i = 1; i <= GRID_LINE_COUNT; i++)
			{
				const int32_t world_offset	= ROAD_HALF_MAX + i * GRID_SPACING;
				const int32_t screen_offset = (world_offset * row.half_width) / ROAD_HALF_MAX;
				fb_rect(target, cx + screen_offset - grid_hw, dy, grid_hw * 2, height, COLOUR_GRID);
				fb_rect(target, cx - screen_offset - grid_hw, dy, grid_hw * 2, height, COLOUR_GRID);
			}
		}
	}
}

static const PnxCamera CAM0 = { 0, 0, 0, 0 };

// Shared by draw_traffic and draw_police for anything ahead of the player -- world-Z
// depth to screen row/tier, same convention both sprite sets already use
// (assets.toml: tier*9+angle, tier 0 nearest). draw_police's OWN "behind the player"
// case is different enough (no real rear horizon to project against) that it doesn't
// go through this.
static bool depth_to_row(int32_t relative_z, int32_t horizon_y, int32_t* out_y,
						 int32_t* out_tier)
{
	if (relative_z <= 0 || relative_z > (VIEW_H - horizon_y) + DEPTH_FUDGE)
		return false;
	*out_y	  = pnx_clamp_i32(VIEW_H - relative_z + DEPTH_FUDGE, horizon_y, VIEW_H - 1);
	*out_tier = pnx_clamp_i32(((relative_z - DEPTH_FUDGE) * 6) / (VIEW_H - horizon_y), 0, 5);
	return true;
}

// Its own sprite (g->traffic_car), not a reuse of the player's touring_normal handle --
// used to be, until "make the traffic cars 10% smaller" needed a real geometry change
// `variants` (a palette swap only) can't express. See assets.toml's "traffic car"
// section for how the smaller sheet/frame rects were derived.
static void draw_traffic(PnxTarget* target, const Game* g, int32_t horizon_y)
{
	if (!g->has_traffic_car)
		return;

	for (int i = 0; i < MAX_TRAFFIC; i++)
	{
		const Traffic* t = &g->traffic[i];
		if (!t->active)
			continue;

		// World-Z ahead of the player, converted the same way road_row's own `d`
		// converts a screen row to a depth -- inverted here to go depth -> row. Behind
		// the player, or past the last drawable row, isn't visible. Uses the SAME
		// current horizon draw_road just swept with, so a car doesn't wink in/out of
		// existence a row early/late relative to where the road itself stops.
		const int32_t relative_z = t->z - (int32_t)g->distance;
		if (relative_z <= 0 || relative_z > (VIEW_H - horizon_y) + DEPTH_FUDGE)
			continue;

		int32_t y = VIEW_H - relative_z + DEPTH_FUDGE;
		y		  = pnx_clamp_i32(y, horizon_y, VIEW_H - 1);

		RoadRow row;
		road_row(y, horizon_y, &row);

		// touring_normal's 6 tiers span the same depth range the road itself draws
		// across (horizon_y..VIEW_H-1) -- tier 0 nearest, 5 farthest, same convention
		// draw_road's own comment and assets.toml both use.
		int32_t tier = ((relative_z - DEPTH_FUDGE) * 6) / (VIEW_H - horizon_y);
		tier		 = pnx_clamp_i32(tier, 0, 5);

		const int32_t curve_offset = road_curve_offset(g->distance, y);
		const int32_t elev_offset  = road_elevation_offset(g->distance, y);
		// Traffic's lane_x is in the SAME world-lane scale the near plane's half_width
		// defines (ROAD_HALF_MAX); scaling it by this row's own half_width is what
		// makes a car in a fixed lane visually converge toward centre with distance,
		// same as the road edges and lane dividers already do.
		const int32_t screen_x	 = (t->lane_x * row.half_width) / ROAD_HALF_MAX;
		const int32_t ax_logical = LOGICAL_W / 2 + curve_offset + screen_x;
		const int32_t dy		 = y - elev_offset;

		// No palette override -- traffic_car's own pixels are already the recoloured
		// orange (baked into art/traffic_small.png at the source), unlike touring_normal's
		// own green original that the old shared-sprite approach had to override at draw
		// time.
		const uint8_t frame = (uint8_t)(tier * 9 + 4); // angle 4: facing straight ahead
		pnx_sprite_draw(&g->traffic_car, target, &CAM0, VIEW_H - 1 - dy, ax_logical, frame,
						NULL, false);
	}
}

// The pursuer (DESIGN.md's "Police: chase, not traffic") -- ahead of the player it's
// just another car on the road (depth_to_row, shared with draw_traffic); the normal
// mid-chase case is BEHIND, which has no real rear horizon to project against, so
// that's a stylised near-edge approach instead (POLICE_BEHIND_RANGE/ROW_MIN, game.h).
static void draw_police(PnxTarget* target, const Game* g, int32_t horizon_y)
{
	const Police* p = &g->police;
	if (!p->active)
		return;

	const int32_t relative_z = p->z - (int32_t)g->distance;
	int32_t y, tier;

	if (relative_z > 0)
	{
		if (!depth_to_row(relative_z, horizon_y, &y, &tier))
			return;
	}
	else
	{
		const int32_t gap = pnx_clamp_i32(-relative_z, 0, POLICE_BEHIND_RANGE);
		if (gap >= POLICE_BEHIND_RANGE)
			return;
		const int32_t row_back = POLICE_BEHIND_ROW_MIN +
			((POLICE_BEHIND_ROW_MAX - POLICE_BEHIND_ROW_MIN) * gap) / POLICE_BEHIND_RANGE;
		y	 = (VIEW_H - 1) - row_back;
		tier = pnx_clamp_i32(gap / (POLICE_BEHIND_RANGE / 2), 0, 1);
	}

	RoadRow row;
	road_row(y, horizon_y, &row);

	const int32_t curve_offset = road_curve_offset(g->distance, y);
	const int32_t elev_offset  = road_elevation_offset(g->distance, y);
	const int32_t screen_x	   = (p->lane_x * row.half_width) / ROAD_HALF_MAX;
	const int32_t ax_logical   = LOGICAL_W / 2 + curve_offset + screen_x;
	const int32_t dy		   = y - elev_offset;
	const int32_t wx		   = VIEW_H - 1 - dy;
	const int32_t wy		   = ax_logical;

	if (p->crash_ticks_left > 0)
	{
		if (!g->has_police_crash)
			return;
		const uint32_t elapsed = CRASH_TOTAL_TICKS - p->crash_ticks_left;
		uint32_t frame		   = elapsed / CRASH_TICKS_PER_FRAME;
		if (frame >= CRASH_FRAME_COUNT)
			frame = CRASH_FRAME_COUNT - 1;
		pnx_sprite_draw(&g->police_crash, target, &CAM0, wx, wy, (uint8_t)frame, NULL, false);
		return;
	}

	if (!g->has_police)
		return;
	const uint8_t frame = (uint8_t)(tier * 9 + 4); // angle 4: facing straight ahead
	pnx_sprite_draw(&g->police_car, target, &CAM0, wx, wy, frame, NULL, false);
}

static void draw_car(PnxTarget* target, const Game* g)
{
	// Pulls back from the near edge as speed rises -- PLAYER_NEAR_ROW_MIN (6, the old
	// fixed value) at a stop, PLAYER_NEAR_ROW_MAX at MAX_SPEED. Paired with the tier
	// swap below, this is the illusion of speed: no runtime sprite scale exists in
	// pebblnyx (DESIGN.md's own correction on that), so "the car looks smaller/farther
	// at speed" has to be the same trick traffic already uses for depth -- swap to a
	// farther pre-rendered tier -- not an actual scale. The few extra rows of pullback
	// also leave a little more room near the true near edge, which is where a chasing
	// police car (not built yet, DESIGN.md) will eventually sit.
	const int32_t near_row =
		PLAYER_NEAR_ROW_MIN + ((PLAYER_NEAR_ROW_MAX - PLAYER_NEAR_ROW_MIN) * g->speed) / MAX_SPEED;
	const int32_t ax_logical = LOGICAL_W / 2 + g->lane_x;
	const int32_t ay_logical = VIEW_H - near_row;
	const int32_t wx		 = VIEW_H - 1 - ay_logical;
	const int32_t wy		 = ax_logical;

	if (g->crash_ticks_left > 0)
	{
		if (!g->has_crash)
			return;
		// Ticks elapsed since impact, not ticks remaining -- the clip plays forward,
		// CRASH_HOLD_TICKS just means crash_ticks_left keeps counting down past the
		// point the frame index has already capped at the last (wreck) frame.
		const uint32_t elapsed = CRASH_TOTAL_TICKS - g->crash_ticks_left;
		uint32_t frame		   = elapsed / CRASH_TICKS_PER_FRAME;
		if (frame >= CRASH_FRAME_COUNT)
			frame = CRASH_FRAME_COUNT - 1;
		pnx_sprite_draw(&g->crash, target, &CAM0, wx, wy, (uint8_t)frame, NULL, false);
		return;
	}

	if (!g->has_car)
		return;

	// touring_normal is 6 distance tiers x 9 steering angles, row-major (assets.toml).
	// "Just a bit" (the ask) is PLAYER_TIER_MAX capping this well short of the 0-5
	// range traffic uses -- the player's own car isn't meant to visibly recede far,
	// only enough to sell speed.
	const uint8_t tier	= (uint8_t)(g->speed >= MAX_SPEED / 2 ? PLAYER_TIER_MAX : 0);
	const uint8_t angle = (uint8_t)(4 + g->steer_visual); // 0 full left .. 8 full right
	const uint8_t frame = (uint8_t)(tier * 9 + angle);

	// Same point rotation as fb_rect, applied to the anchor pnx_sprite_draw blits at --
	// its own camera subtraction is a no-op here (CAM0), and the frame's pixels/origin
	// are already pre-rotated by the asset pipeline, so no further rotation is needed.
	pnx_sprite_draw(&g->car, target, &CAM0, wx, wy, frame, NULL, false);
}

// Flashing red/blue light-bar wash over the near field while a pursuer is actively on
// the player's tail (POLICE_LIGHT_FLASH_TICKS/BAND_ROWS, game.h) -- the police sprite
// itself (draw_police) turned out too subtle to notice while actually driving, this is
// the much louder signal. `pnx_gfx` has no alpha blend for a filled rect (see
// COLOUR_MENU_BG's own comment), so "semi-transparent" is faked the same way the
// engine's own sprite dither already fakes coverage elsewhere: alternate rows are left
// untouched so the road/car underneath keeps showing through every other line.
static void draw_police_lights(PnxTarget* target, const Game* g)
{
	if (!g->police.active || g->police.crash_ticks_left > 0)
		return; // no wash once the chase itself has ended (cop wrecked)

	const bool red_phase = ((g->tick_count / POLICE_LIGHT_FLASH_TICKS) & 1) != 0;
	const uint8_t colour = red_phase ? COLOUR_POLICE_RED : COLOUR_POLICE_BLUE;

	// Dense (every other row) near the true near edge (row 0), widening gaps toward the
	// band's own far edge (row -> POLICE_LIGHT_BAND_ROWS) -- fades the wash out into the
	// road above it instead of stopping at a hard edge.
	for (int32_t row = 0; row < POLICE_LIGHT_BAND_ROWS;
		 row += 2 + (row * 6) / POLICE_LIGHT_BAND_ROWS)
		fb_rect(target, 0, (VIEW_H - 1) - row, LOGICAL_W, 1, colour);
}

#define COLOUR_HUD_TEXT	   COLOUR_MENU_TEXT
#define COLOUR_HUD_OUTLINE RGB(0, 0, 0)
#define HUD_MARGIN		   4

// ---------------------------------------------------------------- round-safe placement
//
// LOGICAL_W == VIEW_H on every round platform (chalk/gabbro, both PBL_ROUND -- their
// display is physically square-bounded, and pnx's own rotation is a no-op on a square),
// so the visible area is exactly the circle inscribed in that LOGICAL_W x LOGICAL_W box.
// docs/PORTING.md's own porting table names this exact problem ("Round corners | safe-
// area rect from per-row bounds | none" -- that "none" is its ENGINE SUPPORT column) --
// nothing in pebblnyx solves it yet, so this project builds the one piece of math it
// actually needs rather than guess a margin.
//
// A box anchored so its own near corner sits `m` pixels in from BOTH axes of whichever
// screen corner it's anchored to has that corner at logical point (m, m) (or the
// horizontal/vertical mirror for the other three corners -- same distance from centre
// either way). The box's FAR corner, extending inward by the box's own w/h, is always
// strictly closer to centre, so checking only the near corner is sufficient regardless
// of the box's own size: solving distance((m,m), centre) <= radius for the smallest
// integer m is `round_safe_margin` below. No sqrt/trig needed -- squared-distance
// comparison only, same integer-only constraint every other engine primitive in this
// codebase already respects (see pnx_fmt.h's own comment on why no libm exists here).
#ifdef PBL_ROUND
static int32_t round_safe_margin(void)
{
	static int32_t cached = -1;
	if (cached < 0)
	{
		const int32_t r = LOGICAL_W / 2;
		int32_t m		= 0;
		while (m < r)
		{
			const int32_t k = r - m; // distance from centre along EACH axis at (m, m)
			if (k * k * 2 <= r * r)	 // Pythagorean distance-from-centre <= r, squared
				break;
			m++;
		}
		cached = m;
	}
	return cached;
}
#define HUD_CORNER_MARGIN round_safe_margin()

// Smallest row `ay` at which the display's own inscribed circle is at least
// `half_width` wide -- for CENTRED content (draw_hud_event's banner), whose only real
// exposure is width, not a corner. Same integer-only circle test as round_safe_margin,
// solved for a different unknown (row, given a required width, rather than corner
// inset given a fixed offset) -- built after a first draft assumed centred content was
// automatically safe near the top of the screen and wasn't: confirmed clipped on the
// `chalk` emulator ("CHECKPOINT +500" ran past the bezel on the right) once the event
// banner's own text turned out wider than that assumption accounted for.
static int32_t round_safe_row_for_width(int32_t half_width)
{
	const int32_t r = LOGICAL_W / 2;
	for (int32_t ay = 0; ay < r; ay++)
	{
		const int32_t dy = r - ay;
		// hw = sqrt(r^2 - dy^2) >= half_width, compared squared to avoid the sqrt.
		if (r * r - dy * dy >= half_width * half_width)
			return ay;
	}
	return r; // unreached for any half_width <= r, the only input this is ever called with
}
#else
#define HUD_CORNER_MARGIN HUD_MARGIN
#endif

// Distance/DISTANCE_SCORE_DIVISOR, not a per-tick accumulation into Game.score --
// Game.distance is already a running total (game.h's own comment on why this reads it
// directly rather than double-booking distance into the event-driven score).
static uint32_t hud_total_score(const Game* g)
{
	return g->score + g->distance / DISTANCE_SCORE_DIVISOR;
}

// The Y just below whatever draw_hud itself drew -- shared so draw_hud_event's banner
// can sit clear of it without hardcoding the same figure a second time.
static int32_t hud_below_y(const Game* g)
{
	return HUD_CORNER_MARGIN + g->hud_font.line_height;
}

// SCORE (top-left) / TIME (top-right) -- outlined (pnx_text_draw_outlined), not boxed.
// An opaque backing rect was the first draft; dropped per direct feedback ("I hate the
// black background on the sprites") in favour of an outline, the same legibility idea
// with no rectangle pasted over the scene. Uses hud_font (the "Monster Racing" face)
// rather than menu_font -- see assets.toml's own comment on why the split: these two
// numbers are meant to read at a glance while actually driving, not just on a paused/
// game-over screen.
//
// HUD_CORNER_MARGIN insets BOTH score and timer on BOTH axes (not just horizontally) --
// on a round platform that resolves to round_safe_margin (its own comment above), which
// only holds if a box/glyph's near corner is inset by the SAME amount on x and y; on a
// rect platform it's just HUD_MARGIN either way, so this one code path is correct for
// both shapes without a separate branch (unlike the boxed draft, a continuous full-width
// bar genuinely needed one -- see git history/DESIGN.md if that reasoning matters again).
static void draw_hud(PnxTarget* target, const Game* g)
{
	if (!g->has_hud_font)
		return;

	const int32_t m			 = HUD_CORNER_MARGIN;
	const int32_t baseline_y = m + g->hud_font.baseline;

	char score_buf[16];
	pnx_format(score_buf, sizeof(score_buf), "%u", (unsigned)hud_total_score(g));
	int32_t fx, fy;
	fb_point(m, baseline_y, &fx, &fy);
	pnx_text_draw_outlined(target, &g->hud_font, score_buf, fx, fy, COLOUR_HUD_TEXT,
						   COLOUR_HUD_OUTLINE);

	// Ticks -> M:SS, floor (not round) -- "0" shows for however long is left in the
	// final partial second rather than reading as a beat early. MM:SS rather than a
	// bare seconds count so it can't be mistaken for the score at a glance -- both
	// would otherwise be plain numbers sitting in the same corner region.
	const int32_t secs_left = pnx_max_i32(0, g->timer_ticks_left) / 25;
	char time_buf[16];
	pnx_format(time_buf, sizeof(time_buf), "%d:%02d", (int)(secs_left / 60), (int)(secs_left % 60));
	const int32_t time_w = pnx_text_width(&g->hud_font, time_buf);
	fb_point(LOGICAL_W - m - time_w, baseline_y, &fx, &fy);
	pnx_text_draw_outlined(target, &g->hud_font, time_buf, fx, fy, COLOUR_HUD_TEXT,
						   COLOUR_HUD_OUTLINE);

#if PNX_USE_DIAGNOSTICS
	// Dev-only readout, compiled out entirely from a diagnostics-off (shipped) build.
	// menu_font, not hud_font -- hud_font (the Monster Racing face) is digits-only, so
	// the "fps" suffix and the decimal point silently dropped when this used hud_font,
	// leaving an unlabelled run of bare digits. menu_font's own digits come from the
	// same Monster Racing face (see the speedometer's comment above), so the numeric
	// part still reads identically; it just also has the letters this line needs.
	if (g->has_menu_font)
	{
		const PnxFrameStats* stats = pnx_diag_stats();
		char fps_buf[12];
		pnx_format(fps_buf, sizeof(fps_buf), "%u.%u fps", (unsigned)(stats->fps_x10 / 10),
				   (unsigned)(stats->fps_x10 % 10));
		fb_point(m, hud_below_y(g) + g->menu_font.baseline, &fx, &fy);
		pnx_text_draw_outlined(target, &g->menu_font, fps_buf, fx, fy, COLOUR_HUD_TEXT,
							   COLOUR_HUD_OUTLINE);
	}
#endif
}

// A stack of horizontal bars, not a rotated arc -- second direct correction after the
// arc draft: "purely horizontal bars... a synthwavey stack of slightly differently
// colored boxes... shift to a more saturated color when activated. Imagine a
// cornucopia resting vertically, the green tip on the bottom, wide opening on the
// top... remove chunks of horizontal sections so it has a transparent gradient look."
// Narrow/green at the bottom (near the pivot) widening to broad/red at the top -- the
// same horizontal-band technique draw_sky already uses for its own gradient, just
// discrete bars with real gaps between them instead of a continuous per-row sweep, and
// no rotation/trig at all: every bar is a plain axis-aligned fb_rect, right-aligned to
// the pivot, which is what dropping the arc buys back (the previous draft needed
// per-pixel rotated-rect plotting purely because ITS bars ran along a curve).
#define GAUGE_SEGMENTS	 6
#define GAUGE_BAR_HEIGHT 5
#define GAUGE_BAR_GAP	 3	   // the "erased" chunk between bars -- see this block's own
							   // top comment ("remove chunks... transparent gradient")
#define GAUGE_BAR_WIDTH_MIN 10 // segment 0 (bottom, green) -- the cornucopia's tip
#define GAUGE_BAR_WIDTH_MAX 42 // segment GAUGE_SEGMENTS-1 (top, red) -- its wide opening

// Cosmetic only -- turns the internal 0..MAX_SPEED simulation unit into a bigger,
// more "arcade speedometer" looking number for the display (real OutRun-style HUDs
// don't show physically accurate km/h either). Purely render.c's concern: game.c never
// sees this value, so it can't leak into anything that actually affects the sim.
#define SPEEDOMETER_SCALE 8

#define COLOUR_GAUGE_GREEN	RGB(0, 3, 0)
#define COLOUR_GAUGE_YELLOW RGB(3, 3, 0)
#define COLOUR_GAUGE_RED	RGB(3, 0, 0)
// What an unlit bar blends TOWARD -- a near-black navy rather than flat grey, so each
// bar's dim state keeps a hint of its own eventual hue (a dim green tip still reads as
// green-ish, not the same grey every other unlit bar gets) instead of one flat colour
// standing in for "not reached yet" on every bar alike.
#define COLOUR_GAUGE_DIM_BASE	 RGB(0, 0, 1)
#define COLOUR_GAUGE_DIM_BLEND_T 650 // 0..1000 toward COLOUR_GAUGE_DIM_BASE

// Green -> yellow -> red across the segment RUN (index 0..GAUGE_SEGMENTS-1), not across
// live speed -- this is the gauge's own fixed, printed-scale colouring (like a real
// tachometer's redline zone), separate from which segments are currently LIT (below).
// Same two-stop pnx_tween_gcolor8 cross-fade draw_sky/draw_sun already use for their own
// day-night ramps -- green->yellow over the first half of the run, yellow->red over the
// second, not one three-way blend, so the midpoint lands exactly on yellow.
static uint8_t gauge_segment_colour(int32_t index)
{
	const int32_t t1000 = (index * 1000) / (GAUGE_SEGMENTS - 1);
	if (t1000 <= 500)
		return pnx_tween_gcolor8(COLOUR_GAUGE_GREEN, COLOUR_GAUGE_YELLOW, t1000 * 2);
	return pnx_tween_gcolor8(COLOUR_GAUGE_YELLOW, COLOUR_GAUGE_RED, (t1000 - 500) * 2);
}

// The same bar's colour, dimmed -- "slightly differently colored boxes... shift to a
// more saturated color when activated," i.e. reached vs not-yet-reached is a SATURATION
// change on each bar's own hue, not a swap to one shared grey for every unlit bar.
static uint8_t gauge_segment_dim_colour(int32_t index)
{
	return pnx_tween_gcolor8(gauge_segment_colour(index), COLOUR_GAUGE_DIM_BASE,
							 COLOUR_GAUGE_DIM_BLEND_T);
}

// Bottom-right -- the classic spot for an arcade racer's speedometer, clear of the road
// ahead. Bars are right-aligned to a single shared edge and bottom-up stacked from a
// single pivot point, so (unlike the old rotated arc) the only thing that has to clear
// the round-safe/car-safe corner is that ONE pivot point -- every bar's own footprint is
// strictly further from the true corner than the pivot is, in both x (bars only ever
// extend LEFT of it) and y (bars only ever stack UPWARD from it), so nothing here needs
// its own separate safety check. See round_safe_margin's own comment above for why the
// round half of that is exact integer math, not a guess, and PLAYER_NEAR_ROW_MAX's own
// comment (game.h) for why the vertical constraint exists regardless of platform shape.
static void draw_speedometer(PnxTarget* target, const Game* g)
{
#ifdef PBL_ROUND
	const int32_t ax_inset = round_safe_margin();
	const int32_t ay_inset = pnx_max_i32(round_safe_margin(), PLAYER_NEAR_ROW_MAX + HUD_MARGIN);
#else
	const int32_t ax_inset = HUD_MARGIN;
	const int32_t ay_inset = PLAYER_NEAR_ROW_MAX + HUD_MARGIN;
#endif
	const int32_t right_x  = LOGICAL_W - ax_inset; // every bar's shared right edge
	const int32_t bottom_y = VIEW_H - ay_inset;	   // segment 0's own bottom edge

	// How many segments are LIT (the gauge's own value slider) -- a plain fraction of
	// MAX_SPEED, reaching every segment exactly at MAX_SPEED (GAUGE_SEGMENTS * MAX_SPEED
	// / MAX_SPEED == GAUGE_SEGMENTS, no off-by-one at the top of the range).
	const int32_t lit = (GAUGE_SEGMENTS * pnx_clamp_i32(g->speed, 0, MAX_SPEED)) / MAX_SPEED;

	int32_t stack_top = bottom_y; // tracked through the loop, reused below for the value
	for (int32_t i = 0; i < GAUGE_SEGMENTS; i++)
	{
		const int32_t width = GAUGE_BAR_WIDTH_MIN +
			(GAUGE_BAR_WIDTH_MAX - GAUGE_BAR_WIDTH_MIN) * i / (GAUGE_SEGMENTS - 1);
		const int32_t bar_bottom = bottom_y - i * (GAUGE_BAR_HEIGHT + GAUGE_BAR_GAP);
		const int32_t bar_top	 = bar_bottom - GAUGE_BAR_HEIGHT;
		const uint8_t colour	 = (i < lit) ? gauge_segment_colour(i) : gauge_segment_dim_colour(i);

		fb_rect(target, right_x - width, bar_top, width, GAUGE_BAR_HEIGHT, colour);
		stack_top = bar_top;
	}

	// The value itself, above the stack's own widest (top) bar -- the smaller menu font,
	// not the big hud one ("use the second smaller font from the menu... for the actual
	// speedometer value"), and no unit suffix -- an earlier draft had "N MPH"; direct ask
	// was "no MPH text," which this keeps: a bare number only. menu_font's own digits
	// come from the SAME Monster Racing face hud_font uses (assets.toml's `menu` font,
	// `overlay_source`) -- one plain draw call is enough; nothing here needs to know two
	// typefaces are involved.
	if (g->has_menu_font)
	{
		char buf[8];
		pnx_format(buf, sizeof(buf), "%d", (int)g->speed * SPEEDOMETER_SCALE);
		const int32_t w = pnx_text_width(&g->menu_font, buf);
		int32_t fx, fy;
		fb_point(right_x - w, stack_top - 3, &fx, &fy);
		pnx_text_draw_outlined(target, &g->menu_font, buf, fx, fy, COLOUR_HUD_TEXT,
							   COLOUR_HUD_OUTLINE);
	}
}

// A short "CHECKPOINT +500" style banner (Game.hud_event, set by game.c's award_score),
// centred just below the score/timer bar, fading in the sense that it simply disappears
// once hud_event_ticks_left reaches 0 -- pnx_gfx has no alpha blend to fade it out
// gradually (same constraint as everything else in this file that says so).
static void draw_hud_event(PnxTarget* target, const Game* g)
{
	if (!g->has_menu_font || g->hud_event_ticks_left == 0 || g->hud_event[0] == '\0')
		return;

	// One font, one draw call -- menu_font's own digits already come from Monster Racing
	// (assets.toml's `menu` font, `overlay_source`), so "CHECKPOINT +500" needs no
	// per-string font-switching here; the combining happened once, in the font asset.
	const int32_t w = pnx_text_width(&g->menu_font, g->hud_event);
	int32_t top_y	= hud_below_y(g) + 4;
#ifdef PBL_ROUND
	// Centred content's only real exposure on a round display is width, not a corner --
	// but width still has to be CHECKED, not assumed safe just because it's centred (see
	// round_safe_row_for_width's own comment for the report that caught this). Whichever
	// constraint needs the row pushed down further wins.
	top_y = pnx_max_i32(top_y, round_safe_row_for_width(w / 2 + HUD_MARGIN));
#endif
	const int32_t baseline_y = top_y + g->menu_font.baseline;

	int32_t fx, fy;
	fb_point((LOGICAL_W - w) / 2, baseline_y, &fx, &fy);
	pnx_text_draw_outlined(target, &g->menu_font, g->hud_event, fx, fy, COLOUR_HUD_TEXT,
						   COLOUR_HUD_OUTLINE);
}

// Drawn over an otherwise-frozen last frame (game_tick returns immediately while
// paused, so the scene underneath simply stops changing -- nothing here needs to know
// what road/traffic looked like when the pause happened).
static void draw_pause_menu(PnxTarget* target, const Game* g)
{
	if (!g->has_menu_font)
		return;

	fb_rect(target, 24, 48, LOGICAL_W - 48, 104, COLOUR_MENU_BG);

	const char* lines[] = {
		"PAUSED",
		g->use_tilt_steer ? "STEER: TILT" : "STEER: TOUCH",
		"SELECT: SWITCH",
		"BACK: RESUME",
	};
	const int32_t text_x = 40;
	int32_t text_y		 = 68;
	for (size_t i = 0; i < sizeof(lines) / sizeof(lines[0]); i++)
	{
		int32_t fx, fy;
		fb_point(text_x, text_y, &fx, &fy);
		pnx_text_draw(target, &g->menu_font, lines[i], fx, fy, COLOUR_MENU_TEXT);
		text_y += 22;
	}
}

// Drawn over an otherwise-frozen last frame, same idea as draw_pause_menu -- BACK
// restarts from here instead of resuming (main.c gates the call on game_over_reason).
// Text picks between the two ways a run can end (game.h's GameOverReason) -- getting
// caught vs. running out of clock read as different failures and should say so, not
// share one "BUSTED" for both. The final score is worth showing here regardless of
// which one ended the run.
static void draw_game_over(PnxTarget* target, const Game* g)
{
	if (!g->has_menu_font)
		return;

	fb_rect(target, 24, 40, LOGICAL_W - 48, 120, COLOUR_MENU_BG);

	char score_line[24];
	pnx_format(score_line, sizeof(score_line), "SCORE %u", (unsigned)hud_total_score(g));

	// "SCORE 13170" needs no font-switching here -- menu_font's own digits already come
	// from Monster Racing (assets.toml's `menu` font, `overlay_source`), same as every
	// other line in this loop.
	const char* lines[] = {
		g->game_over_reason == GAME_OVER_TIME_UP ? "TIME'S UP" : "BUSTED",
		score_line,
		"BACK: RESTART",
	};
	const int32_t text_x = 40;
	int32_t text_y		 = 74;
	for (size_t i = 0; i < sizeof(lines) / sizeof(lines[0]); i++)
	{
		int32_t fx, fy;
		fb_point(text_x, text_y, &fx, &fy);
		pnx_text_draw(target, &g->menu_font, lines[i], fx, fy, COLOUR_MENU_TEXT);
		text_y += 22;
	}
}

#if PNX_USE_DIAGNOSTICS
// Raw single-frame reading, not windowed like pnx_diag's own fps -- this stays "road Xms"
// (not renamed for draw_ground joining it) for log continuity with the earlier
// road-scaling measurements; it now times draw_ground + draw_road together, the two
// passes that draw the scene between the sky and the cars.
static uint32_t s_last_road_ms;
#endif

void render_game(const Game* g, PnxTarget* target)
{
	const int32_t horizon_y = current_horizon_y(g->distance);
	draw_sky(target, g->distance);
	draw_sun(target, g->distance, horizon_y);
	// Outside the "road Xms" timed block below on purpose -- Part 3's own real-device
	// checkpoint is real fps (pnx_diag_frame) before/after, not a change to that specific
	// number, since this draws in its own pass, not inside draw_road's.
	draw_horizon_band(target, g, horizon_y);
#if PNX_USE_DIAGNOSTICS
	const uint32_t road_start = pnx_platform_now_ms();
	draw_ground(target, g, horizon_y);
	draw_road(target, g, horizon_y);
	s_last_road_ms = pnx_platform_now_ms() - road_start;

	// Piggybacks pnx_diag's own ~1s window log rather than keeping a second timer:
	// close enough to that cadence at this project's real ~17fps (25 frames = ~1.5s),
	// good enough for an eyeballed number, not worth a second windowing scheme for.
	static uint32_t s_road_log_tick;
	if (++s_road_log_tick >= 25)
	{
		s_road_log_tick = 0;
		pnx_log("road %ums", (unsigned)s_last_road_ms);
	}
#else
	draw_ground(target, g, horizon_y);
	draw_road(target, g, horizon_y);
#endif
	draw_traffic(target, g, horizon_y);
	draw_police(target, g, horizon_y);
	draw_car(target, g);
	draw_police_lights(target, g);
	draw_hud(target, g);
	draw_speedometer(target, g);
	draw_hud_event(target, g);
}

// Pure scene draw plus its own overlay -- the paused/game_over app states' own draw()
// hooks (main.c) call exactly one of these three functions each, never render_game
// directly themselves while an overlay is up. Both overlays draw over an otherwise
// frozen scene by construction now: the app stack suspends driving's tick() while either
// is on top (main.c), so render_game here is redrawing the SAME distance/lane/traffic
// state every frame, not stale data left over from before the overlay appeared.
void render_paused(const Game* g, PnxTarget* target)
{
	render_game(g, target);
	draw_pause_menu(target, g);
}

void render_game_over(const Game* g, PnxTarget* target)
{
	render_game(g, target);
	draw_game_over(target, g);
}

// Shown once, before driving_ops is ever pushed (main.c) -- no game state to draw behind
// it, unlike the two above, so a plain fill rather than render_game underneath.
void render_title(const Game* g, PnxTarget* target)
{
	fb_rect(target, 0, 0, LOGICAL_W, VIEW_H, COLOUR_MENU_BG);

	if (!g->has_menu_font)
		return;

	const char* title  = "NEED 4 PEBBLE";
	const char* prompt = "SELECT: START";
	int32_t fx, fy;

	const int32_t title_w = pnx_text_width(&g->menu_font, title);
	fb_point((LOGICAL_W - title_w) / 2, VIEW_H / 2 - 20, &fx, &fy);
	pnx_text_draw(target, &g->menu_font, title, fx, fy, COLOUR_MENU_TEXT);

	const int32_t prompt_w = pnx_text_width(&g->menu_font, prompt);
	fb_point((LOGICAL_W - prompt_w) / 2, VIEW_H / 2 + 10, &fx, &fy);
	pnx_text_draw(target, &g->menu_font, prompt, fx, fy, COLOUR_MENU_TEXT);
}
