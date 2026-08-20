#include "pnx_collision.h"

#if PNX_USE_COLLISION

// Every cell a box's pixel extent touches, inclusive of the far edge -- (x+w-1), not
// (x+w), or a box whose edge lands exactly on a cell boundary would test one cell past
// where it actually reaches. pnx_floor_div rather than plain division: a box straddling
// world x = -1 must test cell -1, not cell 0 (see core/pnx_fx.h).
static bool aabb_hits_solid_kind(const PnxMap* map, int32_t x, int32_t y, int32_t w, int32_t h,
								 uint8_t kind_mask)
{
	const int32_t tp  = map->tile_px;
	const int32_t cx0 = pnx_floor_div(x, tp);
	const int32_t cy0 = pnx_floor_div(y, tp);
	const int32_t cx1 = pnx_floor_div(x + w - 1, tp);
	const int32_t cy1 = pnx_floor_div(y + h - 1, tp);

	for (int32_t cy = cy0; cy <= cy1; cy++)
		for (int32_t cx = cx0; cx <= cx1; cx++)
			if (pnx_map_solid_kind(map, map->primary_layer, cx, cy, kind_mask))
				return true;
	return false;
}

bool pnx_collision_tiles_solid_kind(const PnxMap* map, const PnxAABB* box, uint8_t kind_mask)
{
	return aabb_hits_solid_kind(map, box->x, box->y, box->w, box->h, kind_mask);
}

bool pnx_collision_tiles_solid(const PnxMap* map, const PnxAABB* box)
{
	return pnx_collision_tiles_solid_kind(map, box,
										  PNX_COLLISION_KIND_BIT(PNX_COLLISION_KIND_WALL));
}

bool pnx_collision_move_kind(const PnxMap* map, PnxAABB* box, int32_t dx, int32_t dy,
							 uint8_t kind_mask)
{
	bool blocked = false;

	if (dx != 0)
	{
		const int32_t nx = box->x + dx;
		if (!aabb_hits_solid_kind(map, nx, box->y, box->w, box->h, kind_mask))
			box->x = nx;
		else
			blocked = true;
	}

	// Y is tested from wherever X landed, not from the original box -- that is the
	// whole of what makes this axis-SEPARATED: a box sliding along a wall keeps the
	// axis the wall does not block instead of being stopped dead by the other one.
	if (dy != 0)
	{
		const int32_t ny = box->y + dy;
		if (!aabb_hits_solid_kind(map, box->x, ny, box->w, box->h, kind_mask))
			box->y = ny;
		else
			blocked = true;
	}

	return blocked;
}

bool pnx_collision_move(const PnxMap* map, PnxAABB* box, int32_t dx, int32_t dy)
{
	return pnx_collision_move_kind(map, box, dx, dy,
								   PNX_COLLISION_KIND_BIT(PNX_COLLISION_KIND_WALL));
}

#endif // PNX_USE_COLLISION
