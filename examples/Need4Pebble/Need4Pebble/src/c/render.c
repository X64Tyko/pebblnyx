#include "render.h"
#include "track.h"

// ------------------------------------------------------------------------- colour
//
// Raw GColor8 bytes (AARRGGBB, 2 bits/channel, alpha fixed opaque) -- what
// pnx_gfx_fill_rect/pnx_gfx_clear write directly, no palette indirection. Placeholder
// synthwave-adjacent values; DESIGN.md flags the actual art pass as not done yet.
#define RGB(r, g, b) (uint8_t)(0xC0 | ((r) << 4) | ((g) << 2) | (b))

#define COLOUR_SKY_FAR	   RGB(1, 0, 2) // deep purple
#define COLOUR_SKY_NEAR	   RGB(2, 0, 3) // magenta glow toward the horizon
#define COLOUR_GROUND_A	   RGB(0, 1, 2)
#define COLOUR_GROUND_B	   RGB(0, 0, 1)
#define COLOUR_ROAD_A	   RGB(1, 0, 2)
#define COLOUR_ROAD_B	   RGB(0, 0, 1)
#define COLOUR_RUMBLE_A	   RGB(3, 3, 3)
#define COLOUR_RUMBLE_B	   RGB(3, 0, 1)
#define COLOUR_LINE		   RGB(0, 3, 3) // cyan lane dividers
#define COLOUR_MENU_BG	   RGB(0, 0, 0) // opaque black -- no alpha blending exists to dim with
#define COLOUR_MENU_TEXT   RGB(3, 3, 3) // white
#define COLOUR_POLICE_RED  RGB(3, 0, 0)
#define COLOUR_POLICE_BLUE RGB(0, 0, 3)

// Screen-space rect (author/logical frame) -> framebuffer rect, for BUTTONS_TOP.
// Point form is fx = VIEW_H-1-ay, fy = ax (tools/pnx_assets.py rotate_point); extended
// to a rect by taking the far corner on the axis that mirrors (y here), the same way
// examples/pinball/src/c/render.c's fb_rect does for its own (BUTTONS_BOTTOM) rotation.
static void fb_rect(PnxTarget* target, int32_t ax, int32_t ay, int32_t aw, int32_t ah,
					uint8_t colour)
{
	pnx_gfx_fill_rect(target, VIEW_H - ay - ah, ax, ah, aw, colour);
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

static void draw_sky(PnxTarget* target, int32_t horizon_y)
{
	for (int32_t y = 0; y < horizon_y; y++)
	{
		const uint8_t c = (y * 2 >= horizon_y) ? COLOUR_SKY_NEAR : COLOUR_SKY_FAR;
		fb_rect(target, 0, y, LOGICAL_W, 1, c);
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
				alt ? COLOUR_RUMBLE_A : COLOUR_RUMBLE_B);
		fb_rect(target, cx - row.half_width, dy, row.half_width * 2, height,
				alt ? COLOUR_ROAD_A : COLOUR_ROAD_B);

		// Lane dividers: LANES-1 dashed lines splitting the road width evenly. Alternate
		// bands only, and narrow, so they read as broken lines rather than filled strips.
		if (!alt)
		{
			const int32_t dash_hw = pnx_max_i32(1, row.half_width / 12);
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

		const uint8_t frame = (uint8_t)(tier * 9 + 4); // angle 4: facing straight ahead
		pnx_sprite_draw(&g->car, target, &CAM0, VIEW_H - 1 - dy, ax_logical, frame, NULL,
						false);
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
	draw_sky(target, horizon_y);
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
