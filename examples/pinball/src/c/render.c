#include "render.h"
#include "table.h"

// Table-space rect -> framebuffer rect: subtract the camera, then rotate 90 degrees per
// tools/pnx_assets.py's rotate_point for ORIENT_BUTTONS_BOTTOM (fx,fy = ay, w-1-ax),
// extended from a point to a rect the same way any axis-aligned 90-degree rotation is:
// width and height swap, and the new min corner is the far corner's rotated position.
static void fb_rect(PnxTarget* target, int32_t tx, int32_t ty, int16_t tw, int16_t th,
					uint8_t colour, int32_t camera_y)
{
	const int32_t vy = ty - camera_y;
	const int32_t fx = vy;
	const int32_t fy = LOGICAL_W - tx - tw;
	pnx_gfx_fill_rect(target, fx, fy, th, tw, colour);
}

// One point, not a rect -- for stepping along a segment, where a bounding box would be
// wrong for anything but a near-vertical/near-horizontal one (see draw_segment).
static void fb_dot(PnxTarget* target, int32_t tx, int32_t ty, int16_t size, uint8_t colour,
				   int32_t camera_y)
{
	const int32_t fx = ty - camera_y;
	const int32_t fy = LOGICAL_W - tx;
	pnx_gfx_fill_rect(target, fx - size / 2, fy - size / 2, size, size, colour);
}

// No line primitive exists (see ../README.md), so a segment is walked as dots every
// ~3px, each rotated individually -- a bounding box over the two endpoints reads fine
// for the near-vertical walls but draws a solid block for anything genuinely diagonal,
// which the dome/lane-merge segments are.
static void draw_segment(PnxTarget* target, const PnxSegment* seg, int32_t camera_y)
{
	const int32_t dx	= seg->b.x - seg->a.x;
	const int32_t dy	= seg->b.y - seg->a.y;
	const int32_t adx	= dx < 0 ? -dx : dx;
	const int32_t ady	= dy < 0 ? -dy : dy;
	const int32_t steps = pnx_max_i32(pnx_max_i32(adx, ady) / 3, 1);
	for (int32_t i = 0; i <= steps; i++)
	{
		fb_dot(target, seg->a.x + dx * i / steps, seg->a.y + dy * i / steps, 3, 0xFF, camera_y);
	}
}

static void draw_flipper(PnxTarget* target, const PnxFlipper* flip, int32_t camera_y)
{
	const pnx_fx tip_x =
		pnx_fx_from_int(flip->idle_tip.x) +
		pnx_fx_mul(flip->swing, pnx_fx_from_int(flip->struck_tip.x - flip->idle_tip.x));
	const pnx_fx tip_y =
		pnx_fx_from_int(flip->idle_tip.y) +
		pnx_fx_mul(flip->swing, pnx_fx_from_int(flip->struck_tip.y - flip->idle_tip.y));

	const int32_t x0 = pnx_min_i32(flip->pivot.x, pnx_fx_to_int(tip_x));
	const int32_t x1 = pnx_max_i32(flip->pivot.x, pnx_fx_to_int(tip_x));
	const int32_t y0 = pnx_min_i32(flip->pivot.y, pnx_fx_to_int(tip_y));
	const int32_t y1 = pnx_max_i32(flip->pivot.y, pnx_fx_to_int(tip_y));

	fb_rect(target, x0, y0, (int16_t)pnx_max_i32(x1 - x0, 4), (int16_t)pnx_max_i32(y1 - y0, 4),
			0xFF, camera_y);
}

void render_game(const Game* g, PnxTarget* target)
{
	pnx_gfx_clear(target, 0x00);

	for (size_t i = 0; i < WALL_COUNT; i++)
		draw_segment(target, &WALLS[i], g->camera_y);

	draw_flipper(target, &g->left, g->camera_y);
	draw_flipper(target, &g->right, g->camera_y);

	const int32_t bx = pnx_fx_to_int(g->ball.x);
	const int32_t by = pnx_fx_to_int(g->ball.y);
	fb_rect(target, bx - BALL_RADIUS_PX, by - BALL_RADIUS_PX, (int16_t)(BALL_RADIUS_PX * 2),
			(int16_t)(BALL_RADIUS_PX * 2), 0xFF, g->camera_y);

	// Launch charge, a thin bar in the lane's own screen area -- silent unless charging.
	if (g->ball_in_lane && g->launch_charge > 0)
	{
		const int32_t filled = (int32_t)(((int64_t)g->launch_charge * 40) / PNX_FX_ONE);
		fb_rect(target, LANE_INNER_X, LANE_REST_Y + 8, (int16_t)pnx_max_i32(filled, 1), 3, 0xFF,
				g->camera_y);
	}
}
