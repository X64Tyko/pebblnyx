// Tilemap rendering.
//
// Scrolls by pixel, not by tile: the camera is in world pixels and the first column
// simply starts part-way off screen. Tile-quantised scrolling jerks at every boundary,
// and the cost of doing it properly is one modulo.
//
// Takes no atlas. A map owns the tilesets it draws from and streams them by WorldTile, so
// the drawing code asks the map which atlas a cell belongs to rather than being handed one
// and hoping it is the right one.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_TILEMAP

#include "pnx_gfx.h"

// Draws every tile the camera can see, and nothing else. A full screen is ~15x15 tiles
// at 16px, against a measured 2,350 us for 725 tile blits -- 7% of the frame budget.
//
// Cells whose WorldTile is not resident are skipped rather than drawn as something else:
// a hole is honest about a streamer that has fallen behind, where a substitute tile would
// look like content. pnx_tilemap_stream ahead of this is what stops it happening.
void pnx_tilemap_draw(const PnxMap* map, PnxTarget* target, const PnxCamera* camera);

// Bring what the camera can see into residency, plus the streamer's margin. Call once per
// frame BEFORE pnx_tilemap_draw. Returns the number of WorldTiles still missing, which is
// zero in ordinary play.
uint8_t pnx_tilemap_stream(PnxMap* map, const PnxCamera* camera);

// The blocking form, for a scene load or a warp -- anywhere there is no previous frame to
// show and a partly-loaded world would be visible as holes.
uint8_t pnx_tilemap_stream_now(PnxMap* map, const PnxCamera* camera);

// World size in pixels, for camera clamping. The tile size comes from the map, which
// carries it so that the camera can be clamped before any atlas is resident.
static inline int32_t pnx_tilemap_width(const PnxMap* m)
{
	return (int32_t)m->w * m->tile_px;
}
static inline int32_t pnx_tilemap_height(const PnxMap* m)
{
	return (int32_t)m->h * m->tile_px;
}

#endif // PNX_USE_TILEMAP
