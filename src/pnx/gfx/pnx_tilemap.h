// Tilemap rendering.
//
// Scrolls by pixel, not by tile: the camera is in world pixels and the first column
// simply starts part-way off screen. Tile-quantised scrolling jerks at every boundary,
// and the cost of doing it properly is one modulo.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_TILEMAP

#include "pnx_gfx.h"

// Draws every tile the camera can see, and nothing else. A full screen is ~15x15 tiles
// at 16px, against a measured 2,350 us for 725 tile blits -- 7% of the frame budget.
void pnx_tilemap_draw(const PnxMap *map, const PnxAtlas *atlas,
                      PnxTarget *target, const PnxCamera *camera);

// World size in pixels, for camera clamping.
static inline int32_t pnx_tilemap_width(const PnxMap *m, const PnxAtlas *a) {
  return (int32_t)m->w * a->tile_px;
}
static inline int32_t pnx_tilemap_height(const PnxMap *m, const PnxAtlas *a) {
  return (int32_t)m->h * a->tile_px;
}

#endif  // PNX_USE_TILEMAP
