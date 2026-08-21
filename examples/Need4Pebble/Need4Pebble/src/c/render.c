#include "render.h"
#include "track.h"
#include "assets_gen.h" // SPRITE_TOURING_NORMAL_PALETTE_TRAFFIC, draw_traffic's own recolour

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

// y=0 is the top of the sky (farthest from the camera); y=horizon_y-1 is the row right
// at the horizon. Ramp position is continuous in y/horizon_y rather than a fixed
// row-count banding, so this still holds up as horizon_y itself changes with the
// player's slope (current_horizon_y) -- a steep uphill's shorter sky still sees the
// full ramp, just compressed into fewer rows.
static int32_t sky_ramp_index(int32_t y, int32_t horizon_y, int32_t ramp_len)
{
	return pnx_clamp_i32((y * ramp_len) / horizon_y, 0, ramp_len - 1);
}

static void draw_sky(PnxTarget* target, uint32_t distance, int32_t horizon_y)
{
	const int32_t t1000 = sky_cycle_progress(distance);
	uint8_t ramp[SKY_RAMP_LEN];
	for (int32_t i = 0; i < SKY_RAMP_LEN; i++)
		ramp[i] = pnx_tween_gcolor8(SKY_RAMP_SUNSET[i], SKY_RAMP_NIGHT[i], t1000);

	for (int32_t y = 0; y < horizon_y; y++)
	{
		const int32_t idx	   = sky_ramp_index(y, horizon_y, SKY_RAMP_LEN);
		const int32_t prev_idx = (y == 0) ? idx : sky_ramp_index(y - 1, horizon_y, SKY_RAMP_LEN);
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

		fb_rect(target, cx - row.rumble_width, dy, row.rumble_width * 2, height,
				alt ? COLOUR_SHOULDER_A : COLOUR_SHOULDER_B);
		fb_rect(target, cx - row.half_width, dy, row.half_width * 2, height,
				alt ? COLOUR_ROAD_A : COLOUR_ROAD_B);

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

		const uint8_t ground = ((row.depth + g->distance) / (BAND_WORLD * 2)) & 1
			? COLOUR_GROUND_A
			: COLOUR_GROUND_B;
		fb_rect(target, 0, dy, cx - row.rumble_width, height, ground);
		fb_rect(target, cx + row.rumble_width, dy, LOGICAL_W - (cx + row.rumble_width), height,
				ground);

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

// Traffic reuses the player's own touring_normal handle (g->car) -- same asset, other
// tiers/angles, see game.h's own comment on why that's not a second sprite load.
static void draw_traffic(PnxTarget* target, const Game* g, int32_t horizon_y)
{
	if (!g->has_car)
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

		// Recoloured (orange, not the player's own green) so traffic reads as distinct
		// from the player's car at a glance, reported directly ("recolor the traffic
		// cars so they stand out on the road") -- same bitmap, a palette swap
		// (assets.toml's touring_normal `variants`), not a second sprite.
		const uint8_t frame = (uint8_t)(tier * 9 + 4); // angle 4: facing straight ahead
		pnx_sprite_draw(&g->car, target, &CAM0, VIEW_H - 1 - dy, ax_logical, frame,
						pnx_palette(SPRITE_TOURING_NORMAL_PALETTE_TRAFFIC), false);
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
// restarts from here instead of resuming (main.c gates the call on game_over).
static void draw_game_over(PnxTarget* target, const Game* g)
{
	if (!g->has_menu_font)
		return;

	fb_rect(target, 24, 48, LOGICAL_W - 48, 104, COLOUR_MENU_BG);

	const char* lines[] = {
		"BUSTED",
		"BACK: RESTART",
	};
	const int32_t text_x = 40;
	int32_t text_y		 = 88;
	for (size_t i = 0; i < sizeof(lines) / sizeof(lines[0]); i++)
	{
		int32_t fx, fy;
		fb_point(text_x, text_y, &fx, &fy);
		pnx_text_draw(target, &g->menu_font, lines[i], fx, fy, COLOUR_MENU_TEXT);
		text_y += 22;
	}
}

void render_game(const Game* g, PnxTarget* target)
{
	const int32_t horizon_y = current_horizon_y(g->distance);
	draw_sky(target, g->distance, horizon_y);
	draw_sun(target, g->distance, horizon_y);
	draw_road(target, g, horizon_y);
	draw_traffic(target, g, horizon_y);
	draw_police(target, g, horizon_y);
	draw_car(target, g);
	draw_police_lights(target, g);
	if (g->paused)
		draw_pause_menu(target, g);
	if (g->game_over)
		draw_game_over(target, g);
}
