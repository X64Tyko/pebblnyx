#include "pnx_tilemap.h"

#if PNX_USE_TILEMAP

uint8_t pnx_tilemap_stream(PnxMap* map, const PnxCamera* camera)
{
	if (!map || !camera)
		return 0;
	return pnx_map_stream(map, camera->x, camera->y, camera->view_w, camera->view_h);
}

uint8_t pnx_tilemap_stream_now(PnxMap* map, const PnxCamera* camera)
{
	if (!map || !camera)
		return 0;
	return pnx_map_stream_now(map, camera->x, camera->y, camera->view_w, camera->view_h);
}

// One WorldTile's worth of visible cells. Hoisting the loop out here is what pays for
// WorldTiles on the hot path: the slot lookup, the cell stride and the bounds against the
// map's edge are all resolved once per block of 256 cells rather than once per cell.
static void draw_worldtile(const PnxMap* map, const PnxWorldTile* wt, PnxTarget* target,
						   const PnxCamera* camera, int32_t x0, int32_t y0, int32_t x1,
						   int32_t y1)
{
	const int32_t T	 = map->tile_px;
	const int32_t ox = (int32_t)wt->wx * map->worldtile;
	const int32_t oy = (int32_t)wt->wy * map->worldtile;
	// Hoisted out of the loop: idx_width is a per-MAP constant, so this branch is the same
	// on every cell of every WorldTile a draw call touches and predicts perfectly -- one
	// extra compare next to the atlas lookup/blit dispatch already sitting in this loop,
	// not a new source of per-cell cost.
	const int32_t iw = map->idx_width;

	for (int32_t ty = y0; ty <= y1; ty++)
	{
		const uint8_t* row = wt->cells + (size_t)(ty - oy) * wt->cell_w * iw;
		const int32_t sy   = ty * T - camera->y;

		for (int32_t tx = x0; tx <= x1; tx++)
		{
			// Stored index -> dictionary entry -> the same tile-id-plus-flags word this loop
			// always worked with (M12's own comment, pnx_assets.h's PnxMap.cell_dict).
			const uint8_t* cell	 = row + (size_t)(tx - ox) * iw;
			const uint16_t index = (iw == 1)
				? cell[0]
				: (uint16_t)(cell[0] | ((uint16_t)cell[1] << 8));
			const uint8_t* d	 = map->cell_dict + (size_t)index * 2;
			const uint16_t entry = (uint16_t)(d[0] | ((uint16_t)d[1] << 8));
			const uint16_t id	 = entry & PNX_MAP_INDEX_MASK;

			uint16_t local		  = 0;
			const PnxAtlas* atlas = pnx_map_atlas(map, id, &local);
			if (!atlas)
				continue; // its slot was evicted; the WorldTile pin makes this unreachable

			const int32_t sx = tx * T - camera->x;

			// One branch and one index for a recoloured zone. The map's table, when it has one,
			// names a different palette slot per tile; the pixel data is the atlas's either way.
			// It is indexed by the MAP's tile id, not the atlas's, so one table covers a map
			// whose tiles come from several tilesets.
			const PnxPalette* pal = map->tile_palette
				? pnx_palette(map->tile_palette[id])
				: pnx_atlas_tile_palette(atlas, (uint8_t)local);

			// An atlas is one layout or the other for its whole life, so this branch is free to
			// the predictor even though it now sits inside the per-cell body: a map's atlases do
			// not alternate layout cell by cell in any real content.
			if (pnx_atlas_is_metatiled(atlas))
			{
				// Flip/rotate are not applied to metatiles: mirroring or transposing a composed
				// tile means reordering the quadrants as well as transforming each one, and the
				// pipeline refuses BOTH on a metatiled atlas for that reason (check_flip_metatiles,
				// tools/pnx_assets.py -- it names the check after flip, but folds rotate into the
				// same "flipped" set it refuses on). So a metatiled atlas never carries either bit
				// on a real cell, and this path can stay ignorant of both.
				//
				// The _with form, so a metatiled atlas honours the map's palette remap too. The
				// plain one looks the palette up itself and would quietly ignore it -- which was
				// survivable while no metatiled atlas had ever been recoloured, and stopped being
				// so the moment a map could mix a flat tileset with a metatiled one.
				pnx_blit_metatile_with(target, atlas, (uint8_t)local, pal, sx, sy);
			}
			else
			{
				const uint8_t flip = (uint8_t)(((entry & PNX_MAP_FLIP_X) ? 1u : 0u) |
											   ((entry & PNX_MAP_FLIP_Y) ? 2u : 0u) |
											   ((entry & PNX_MAP_ROTATE) ? PNX_FLIP_ROTATE : 0u));
				pnx_blit_4bpp(target, pnx_atlas_tile(atlas, (uint8_t)local), pal, sx, sy,
							  (int16_t)T, (int16_t)T, flip);
			}
		}
	}
}

void pnx_tilemap_draw(const PnxMap* map, PnxTarget* target, const PnxCamera* camera)
{
	if (!map || !map->slots || map->tile_px == 0)
		return;

	const int32_t T = map->tile_px;

	// Floor division, not truncation: at a negative camera x, truncation rounds toward
	// zero and drops the column that should be partly visible at the left edge.
	const int32_t first_tx = pnx_floor_div(camera->x, T);
	const int32_t first_ty = pnx_floor_div(camera->y, T);

	// One extra row and column, because the first is usually partly off screen.
	int32_t last_tx = first_tx + camera->view_w / T + 1;
	int32_t last_ty = first_ty + camera->view_h / T + 1;

	const int32_t tx0 = first_tx < 0 ? 0 : first_tx;
	const int32_t ty0 = first_ty < 0 ? 0 : first_ty;
	if (last_tx >= map->w)
		last_tx = map->w - 1;
	if (last_ty >= map->h)
		last_ty = map->h - 1;
	if (last_tx < tx0 || last_ty < ty0)
		return;

	// Outer loop over WorldTiles, inner over the cells of each that the camera can see. The
	// visible rectangle is clipped into each block rather than tested per cell, so a cell
	// costs the same as it did before any of this existed.
	const int32_t shift = map->wt_shift;
	for (int32_t wy = ty0 >> shift; wy <= (last_ty >> shift); wy++)
	{
		for (int32_t wx = tx0 >> shift; wx <= (last_tx >> shift); wx++)
		{
			const uint8_t slot = map->wt_slot[(uint32_t)wy * map->wt_cols + wx];
			if (slot == PNX_MAP_NO_SLOT)
				continue;

			const PnxWorldTile* wt = &map->slots[slot];
			const int32_t ox = wx << shift, oy = wy << shift;
			int32_t x0 = tx0 > ox ? tx0 : ox;
			int32_t y0 = ty0 > oy ? ty0 : oy;
			int32_t x1 = last_tx < ox + wt->cell_w - 1 ? last_tx : ox + wt->cell_w - 1;
			int32_t y1 = last_ty < oy + wt->cell_h - 1 ? last_ty : oy + wt->cell_h - 1;
			if (x1 < x0 || y1 < y0)
				continue;

			draw_worldtile(map, wt, target, camera, x0, y0, x1, y1);
		}
	}
}

#endif // PNX_USE_TILEMAP
