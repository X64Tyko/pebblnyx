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

// Draws every tile the camera can see on ONE layer, and nothing else. A full screen is
// ~15x15 tiles at 16px, against a measured 2,350 us for 725 tile blits -- 7% of the frame
// budget, at any one layer this framework has measured (the cost is per WorldTile drawn,
// not per layer that exists).
//
// Cells whose WorldTile is not resident are skipped rather than drawn as something else:
// a hole is honest about a streamer that has fallen behind, where a substitute tile would
// look like content. pnx_tilemap_stream ahead of this is what stops it happening.
//
// `pnx_tilemap_draw` draws the map's PRIMARY layer -- unchanged behaviour for the common,
// single-layer case. `pnx_tilemap_draw_layer` draws an explicit one; a multi-layer map
// composites by calling it once per layer in order, which `pnx_tilemap_layers_build`
// (below) does for a caller that just wants every declared layer drawn through
// pnx_layer.h's compositor.
void pnx_tilemap_draw(const PnxMap* map, PnxTarget* target, const PnxCamera* camera);
void pnx_tilemap_draw_layer(const PnxMap* map, uint8_t layer, PnxTarget* target,
							const PnxCamera* camera);

// Bring what the camera can see into residency, plus the streamer's margin, ACROSS EVERY
// LAYER the map declares -- pnx_map_stream already does the looping (M13), so this is a
// plain pass-through, not one call per layer. Call once per frame BEFORE drawing any
// layer. Returns the number of WorldTiles still missing, summed across every layer, which
// is zero in ordinary play.
uint8_t pnx_tilemap_stream(PnxMap* map, const PnxCamera* camera);

// The blocking form, for a scene load or a warp -- anywhere there is no previous frame to
// show and a partly-loaded world would be visible as holes.
uint8_t pnx_tilemap_stream_now(PnxMap* map, const PnxCamera* camera);

// World size in pixels, for camera clamping -- the PRIMARY layer's own extent, since the
// camera is clamped against where gameplay happens, not against a background or overlay
// layer that may be a different size. The tile size comes from the map, which carries it
// so that the camera can be clamped before any atlas is resident.
static inline int32_t pnx_tilemap_width(const PnxMap* m)
{
	return (int32_t)m->layers[m->primary_layer].w * m->tile_px;
}
static inline int32_t pnx_tilemap_height(const PnxMap* m)
{
	return (int32_t)m->layers[m->primary_layer].h * m->tile_px;
}

#if PNX_USE_LAYERS
#include "pnx_layer.h"

// Fills one PNX_LAYER_CALLBACK PnxLayer per layer the map declares (up to
// PNX_MAP_MAX_LAYERS, the array `out` must be sized to), each with that layer's own
// `parallax_pct_x`/`parallax_pct_y` and a draw function that calls pnx_tilemap_draw_layer
// for it. `out` is
// ready to hand to pnx_layers_draw with `ctx` == `map` -- a game that wants to splice a
// sprite layer or a custom callback layer between two map layers builds this first, then
// writes its own entries into the gaps before calling pnx_layers_draw, the same way it
// already assembles any other PnxLayer[] by hand. Returns the layer count written.
uint8_t pnx_tilemap_layers_build(const PnxMap* map, PnxLayer* out);
#endif

#endif // PNX_USE_TILEMAP
