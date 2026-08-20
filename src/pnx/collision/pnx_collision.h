// AABB collision: sprite-vs-tile movement resolution and sprite-vs-sprite overlap.
//
// This module is NOT the source of truth for which tiles are solid -- that already
// exists (pnx_map_solid, assets/pnx_assets.h) and costs nothing extra to use, since
// collision is a property of the TILE (PnxAtlas.tile_flags) the pipeline bakes at build
// time regardless of who reads it at runtime. What is missing is the sweep: given a box
// and a proposed move, which of it is allowed. That is what a tile game otherwise
// hand-rolls once per project, usually slightly wrong at a wall corner the first time.
//
// AXIS-SEPARATED, not exact-edge. pnx_collision_move tries the X step, keeps it only if
// the destination box touches no solid cell, then does the same for Y from wherever X
// landed -- so a wall stops the axis it faces without also killing a diagonal slide
// along it, but a fast-moving box can land up to (speed - 1) px short of the wall rather
// than flush against it. That is invisible at tile-game speeds (a few px/tick) and
// exactly the corner-case a binary-search edge-snap would exist to fix if a game's speed
// ever made it visible -- not built here because nothing in this engine needs it yet.
//
// A box is plain min-corner + size, not the sprite's own feet-anchored (x,y) --
// pnx_sprite.h's PnxSpriteInstance has no width/height at all, since visual size lives in
// the sprite ASSET and gameplay hitboxes are routinely smaller than the art anyway (a
// tall sprite with a narrow footprint). pnx_aabb_from_feet is the one-line conversion for
// the common case of "hitbox matches the sprite's own footprint".

#pragma once

#include "../pnx_config.h"

#if PNX_USE_COLLISION

#include "../assets/pnx_assets.h"
#include "../core/pnx_fx.h"

#include <stdint.h>
#include <stdbool.h>

typedef struct
{
	int32_t x, y; // world pixels, min corner (top-left)
	int32_t w, h;
} PnxAABB;

// Builds a box from a feet-anchored position (see pnx_sprite.h's PnxSpriteInstance
// convention) and a width/height: centred horizontally on `feet_x`, extending upward
// from `feet_y` by `h`.
static inline PnxAABB pnx_aabb_from_feet(int32_t feet_x, int32_t feet_y, int32_t w, int32_t h)
{
	PnxAABB box = { feet_x - w / 2, feet_y - h, w, h };
	return box;
}

// Plain rectangle overlap. For sprite-vs-sprite -- an attack's hitbox against an enemy,
// two projectiles -- where neither side is tile geometry and there is nothing to sweep.
static inline bool pnx_aabb_overlap(const PnxAABB* a, const PnxAABB* b)
{
	return a->x < b->x + b->w && a->x + a->w > b->x && a->y < b->y + b->h &&
		a->y + a->h > b->y;
}

// True if `box` touches any solid tile cell. A point-in-time query -- "is it valid to be
// here" -- rather than a move; pnx_collision_move is what a game actually steps with.
bool pnx_collision_tiles_solid(const PnxMap* map, const PnxAABB* box);

// Moves `box` by (dx, dy) against `map`'s tile solidity, axis-separated as described
// above, and writes the result back into `box->x`/`box->y` in place. Returns true if
// EITHER axis was blocked this call (partially or fully) -- a game wanting "did I just
// hit a wall" for a sound or a squash-frame does not have to diff the box itself to find
// out.
bool pnx_collision_move(const PnxMap* map, PnxAABB* box, int32_t dx, int32_t dy);

// Same as pnx_collision_tiles_solid/pnx_collision_move, filtered by collision KIND
// (PNX_COLLISION_KIND_BIT/PNX_COLLISION_KIND_ALL, assets/pnx_assets.h) instead of always
// meaning "wall". `pnx_collision_tiles_solid`/`pnx_collision_move` are these with
// `kind_mask = PNX_COLLISION_KIND_BIT(PNX_COLLISION_KIND_WALL)` -- every existing caller
// keeps meaning exactly what it always did, since WALL was the only kind that existed
// before this.
bool pnx_collision_tiles_solid_kind(const PnxMap* map, const PnxAABB* box, uint8_t kind_mask);
bool pnx_collision_move_kind(const PnxMap* map, PnxAABB* box, int32_t dx, int32_t dy,
							 uint8_t kind_mask);

#endif // PNX_USE_COLLISION
