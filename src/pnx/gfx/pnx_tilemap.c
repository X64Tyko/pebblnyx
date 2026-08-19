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

// One cell, wherever its entry word and atlas came from. Shared by both draw paths below
// so a wrap layer's per-cell loop and a non-wrap layer's per-WorldTile-block loop agree on
// every pixel decision -- palette remap, metatile dispatch, flip/rotate -- rather than two
// copies of the same logic drifting apart.
static void draw_cell(const PnxMap* map, uint16_t entry, PnxTarget* target, int32_t sx,
					  int32_t sy, int32_t T)
{
	const uint16_t id = entry & PNX_MAP_INDEX_MASK;

	uint16_t local		  = 0;
	const PnxAtlas* atlas = pnx_map_atlas(map, id, &local);
	if (!atlas)
		return; // its slot was evicted; the WorldTile pin makes this unreachable

	// One branch and one index for a recoloured zone. The map's table, when it has one,
	// names a different palette slot per tile; the pixel data is the atlas's either way.
	// It is indexed by the MAP's tile id, not the atlas's, so one table covers a map whose
	// tiles come from several tilesets.
	const PnxPalette* pal = map->tile_palette ? pnx_palette(map->tile_palette[id])
											  : pnx_atlas_tile_palette(atlas, (uint8_t)local);

	// An atlas is one layout or the other for its whole life, so this branch is free to the
	// predictor even though it sits inside the per-cell body: a map's atlases do not
	// alternate layout cell by cell in any real content.
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
		// plain one looks the palette up itself and would quietly ignore it.
		pnx_blit_metatile_with(target, atlas, (uint8_t)local, pal, sx, sy);
	}
	else
	{
		const uint8_t flip = (uint8_t)(((entry & PNX_MAP_FLIP_X) ? 1u : 0u) |
									   ((entry & PNX_MAP_FLIP_Y) ? 2u : 0u) |
									   ((entry & PNX_MAP_ROTATE) ? PNX_FLIP_ROTATE : 0u));
		pnx_blit_4bpp(target, pnx_atlas_tile(atlas, (uint8_t)local), pal, sx, sy, (int16_t)T,
					  (int16_t)T, flip);
	}
}

// One WorldTile's worth of visible cells, on a NON-wrap layer. Hoisting the loop out here
// is what pays for WorldTiles on the hot path: the slot lookup, the cell stride and the
// bounds against the layer's own edge are all resolved once per block of up to
// `worldtile^2` cells rather than once per cell.
static void draw_worldtile(const PnxMap* map, const PnxMapLayer* l, const PnxWorldTile* wt,
						   PnxTarget* target, const PnxCamera* camera, int32_t x0, int32_t y0,
						   int32_t x1, int32_t y1)
{
	const int32_t T	 = map->tile_px;
	const int32_t ox = (int32_t)wt->wx * l->worldtile;
	const int32_t oy = (int32_t)wt->wy * l->worldtile;
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
			const uint16_t index = (iw == 1) ? cell[0]
											 : (uint16_t)(cell[0] | ((uint16_t)cell[1] << 8));
			const uint8_t* d	 = map->cell_dict + (size_t)index * 2;
			const uint16_t entry = (uint16_t)(d[0] | ((uint16_t)d[1] << 8));

			draw_cell(map, entry, target, tx * T - camera->x, sy, T);
		}
	}
}

// A wrap layer's own draw path: one cell at a time through pnx_map_entry_layer, which is
// already wrap-aware (pnx_map_layer_wrap_xy, pnx_assets.h), rather than the WorldTile-block
// batching above -- that batching assumes a cell's block and its WorldTile-grid index move
// together, which stops being true the moment sampling wraps around the layer's own edge.
// A wrap layer is meant to be screen-sized (a parallax background), so the cell count this
// walks is bounded by one screen either way; the batched path stays the one large,
// non-wrap layers use.
static void draw_wrap_layer(const PnxMap* map, uint8_t layer, const PnxMapLayer* l,
							PnxTarget* target, const PnxCamera* camera, int32_t tx0,
							int32_t ty0, int32_t tx1, int32_t ty1)
{
	const int32_t T = map->tile_px;
	for (int32_t ty = ty0; ty <= ty1; ty++)
	{
		const int32_t sy = ty * T - camera->y;
		for (int32_t tx = tx0; tx <= tx1; tx++)
		{
			const uint16_t entry = pnx_map_entry_layer(map, layer, tx, ty);
			if (entry == PNX_MAP_NO_CELL)
				continue;
			draw_cell(map, entry, target, tx * T - camera->x, sy, T);
		}
	}
}

void pnx_tilemap_draw_layer(const PnxMap* map, uint8_t layer, PnxTarget* target,
							const PnxCamera* camera)
{
	if (!map || layer >= map->layer_count || map->tile_px == 0)
		return;
	const PnxMapLayer* l = &map->layers[layer];
	if (!l->slots)
		return;

	const int32_t T = map->tile_px;

	// Floor division, not truncation: at a negative camera x, truncation rounds toward
	// zero and drops the column that should be partly visible at the left edge.
	const int32_t first_tx = pnx_floor_div(camera->x, T);
	const int32_t first_ty = pnx_floor_div(camera->y, T);

	// One extra row and column, because the first is usually partly off screen.
	int32_t last_tx = first_tx + camera->view_w / T + 1;
	int32_t last_ty = first_ty + camera->view_h / T + 1;

	int32_t tx0 = first_tx, ty0 = first_ty;
	if (!l->wrap)
	{
		if (tx0 < 0)
			tx0 = 0;
		if (ty0 < 0)
			ty0 = 0;
		if (last_tx >= l->w)
			last_tx = l->w - 1;
		if (last_ty >= l->h)
			last_ty = l->h - 1;
	}
	if (last_tx < tx0 || last_ty < ty0)
		return;

	if (l->wrap)
	{
		draw_wrap_layer(map, layer, l, target, camera, tx0, ty0, last_tx, last_ty);
		return;
	}

	// Outer loop over WorldTiles, inner over the cells of each that the camera can see. The
	// visible rectangle is clipped into each block rather than tested per cell, so a cell
	// costs the same as it did before any of this existed.
	const int32_t shift = l->wt_shift;
	for (int32_t wy = ty0 >> shift; wy <= (last_ty >> shift); wy++)
	{
		for (int32_t wx = tx0 >> shift; wx <= (last_tx >> shift); wx++)
		{
			const uint8_t slot = l->wt_slot[(uint32_t)wy * l->wt_cols + wx];
			if (slot == PNX_MAP_NO_SLOT)
				continue;

			const PnxWorldTile* wt = &l->slots[slot];
			const int32_t ox = wx << shift, oy = wy << shift;
			int32_t x0 = tx0 > ox ? tx0 : ox;
			int32_t y0 = ty0 > oy ? ty0 : oy;
			int32_t x1 = last_tx < ox + wt->cell_w - 1 ? last_tx : ox + wt->cell_w - 1;
			int32_t y1 = last_ty < oy + wt->cell_h - 1 ? last_ty : oy + wt->cell_h - 1;
			if (x1 < x0 || y1 < y0)
				continue;

			draw_worldtile(map, l, wt, target, camera, x0, y0, x1, y1);
		}
	}
}

void pnx_tilemap_draw(const PnxMap* map, PnxTarget* target, const PnxCamera* camera)
{
	if (!map)
		return;
	pnx_tilemap_draw_layer(map, map->primary_layer, target, camera);
}

#if PNX_USE_LAYERS

// Four static trampolines, one per PNX_MAP_MAX_LAYERS index -- pnx_layers_draw's
// PNX_LAYER_CALLBACK carries one shared `ctx` for every callback layer in the array (see
// pnx_layer.h's own comment), so a per-entry layer INDEX cannot ride through it; a fixed,
// bounded set of thunks that each already know their own index is what lets
// pnx_tilemap_layers_build hand back real function pointers instead. The contract this
// puts on the caller: pass `map` itself as pnx_layers_draw's `ctx`. A game that also wants
// a custom PNX_LAYER_CALLBACK spliced into the same array reaches its own state some other
// way (a module-level static, or a HUD-style call that needs no ctx at all) -- the same
// constraint any two hand-written callbacks sharing one ctx already have.
#if PNX_MAP_MAX_LAYERS != 4
#error "pnx_tilemap_layers_build's trampolines assume PNX_MAP_MAX_LAYERS == 4"
#endif
static void draw_thunk0(void* ctx, PnxTarget* t, const PnxCamera* c)
{
	pnx_tilemap_draw_layer((const PnxMap*)ctx, 0, t, c);
}
static void draw_thunk1(void* ctx, PnxTarget* t, const PnxCamera* c)
{
	pnx_tilemap_draw_layer((const PnxMap*)ctx, 1, t, c);
}
static void draw_thunk2(void* ctx, PnxTarget* t, const PnxCamera* c)
{
	pnx_tilemap_draw_layer((const PnxMap*)ctx, 2, t, c);
}
static void draw_thunk3(void* ctx, PnxTarget* t, const PnxCamera* c)
{
	pnx_tilemap_draw_layer((const PnxMap*)ctx, 3, t, c);
}

uint8_t pnx_tilemap_layers_build(const PnxMap* map, PnxLayer* out)
{
	static void (*const thunks[PNX_MAP_MAX_LAYERS])(void*, PnxTarget*, const PnxCamera*) = {
		draw_thunk0,
		draw_thunk1,
		draw_thunk2,
		draw_thunk3,
	};
	if (!map || !out)
		return 0;
	for (uint8_t i = 0; i < map->layer_count; i++)
	{
		out[i].kind			= PNX_LAYER_CALLBACK;
		out[i].parallax_pct = map->layers[i].parallax_pct;
		out[i].as.draw		= thunks[i];
	}
	return map->layer_count;
}
#endif // PNX_USE_LAYERS

#endif // PNX_USE_TILEMAP
