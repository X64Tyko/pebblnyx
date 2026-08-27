#!/usr/bin/env python3
"""pebblnyx asset pipeline: declarative manifest -> packed blobs + generated header.

No C file should ever name a source PNG, a pixel offset, a sheet layout or a magic tile
id. The manifest is the single source of truth; this tool is the only thing that reads
it, and the header it emits is the only thing game code sees.

TOML rather than JSON because manifests carry real knowledge in their comments -- why a
sheet needs a colour key, why a building is a solid block rather than a hollow room --
and JSON cannot hold a comment. `tomllib` is stdlib from Python 3.11.

**This tool fails the build on invalid content.** That is the whole point of it. Content
bugs do not crash on a watch; they present as "nothing happens", with a binary that
looks perfectly fine. A door drawn inside a sealed building produced a warp that could
never fire and cost real debugging time. Every check here exists because something was
silently wrong once.

Usage:
    tools/pnx_assets.py <manifest.toml> [--out DIR] [--header PATH] [--preview]
"""

import argparse
import json
import os
import re
import sys
import tomllib

# The `.pnxmap` source format. Its own module because both this pipeline and the editor
# read and write it, and the format is the contract between them.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pnx_mapfile as mapfile                                  # noqa: E402
import pnx_huffman_codec as hfc                                # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageFont = None

# ARGB2222. 0x00 is fully transparent; opaque colours carry alpha 0b11 in the top bits.
TRANSPARENT = 0x00
OPAQUE = 0xC0

# Collision is a TILE property, not a cell one -- a wall's shape does not change
# depending on where it is placed, so it lives once per art tile (PnxAtlas.tile_flags,
# repurposed -- see finish_atlas) rather than being carried, or overridden, per cell.
# SCALED and COMPLEX carry their actual shape (an inset rect / a 1bpp mask) in their own
# sparse per-tile-id tables, keyed the same way -- too big for one byte each.
COLLISION_NONE = 0
COLLISION_SOLID = 1
COLLISION_SCALED = 2
COLLISION_COMPLEX = 3
COLLISION_NAMES = {"solid": COLLISION_SOLID, "scaled": COLLISION_SCALED,
                   "complex": COLLISION_COMPLEX}

# KIND: what touching the shape MEANS, orthogonal to the MODE above (which only says its
# shape) -- see PNX_COLLISION_KIND_* (pnx_assets.h) for the runtime side. Packed into the
# same byte as mode (`(kind << 2) | mode`) by finish_atlas/finish_sprite, so this costs
# nothing beyond the byte mode already had a tile/frame's-worth of. WALL is 0 so a
# `[[atlas.collision]]`/`[[sprite.collision]]` entry with no `kind =` -- every one written
# before this existed -- means exactly what it always did.
COLLISION_KIND_WALL = 0
COLLISION_KIND_HURT = 1
COLLISION_KIND_HIT = 2
COLLISION_KIND_OVERLAP = 3
COLLISION_KIND_NAMES = {"wall": COLLISION_KIND_WALL, "hurt": COLLISION_KIND_HURT,
                        "hit": COLLISION_KIND_HIT, "overlap": COLLISION_KIND_OVERLAP}

ANIM_DEFAULT_FPS = 8

# Pre-shift warp bit in the legend's flag byte -- unrelated to collision now, so back to
# owning the low bit on its own rather than sharing with a collision mode.
TILE_WARP = 0x01

# Map cell bits, mirrored from PNX_MAP_* in pnx_assets.h.
#
# Was 10 bits of tile id + 2 flip bits + 4 unused, historically commented "reserved for
# a per-cell palette" -- never built, and tile_palette already covers per-tile
# recolouring. Collision moving to being a tile property (above) frees the 2 bits an
# earlier pass spent on it here; ROTATE spends only 1 of them back, so there is 1 bit
# still genuinely free after this.
#
# ROTATE is transpose, not an angle -- a single bit, not two. {rotate, flip_x, flip_y} is
# 3 independent bits spanning all 8 symmetries of a square exactly (identity, the two
# axis flips, 180 degrees, the two diagonal flips, and both 90-degree rotations, which
# fall out of rotate combined with one flip) -- a rotation-angle encoding would have
# needed a 4th bit to express the same 8 states without redundancy against flip.
MAP_INDEX_MASK = 0x03FF
MAP_FLIP_X = 0x0400
MAP_FLIP_Y = 0x0800
MAP_ROTATE = 0x1000
MAP_WARP = 0x2000
# A placement-authored u8 tag, same idea as warp: declared per legend char / .pnxmap tile
# table entry ("extended = N"), folded into the bit here, the actual value living in the
# WorldTile's own sparse table (slice_worldtiles) since a u16 cell has no room to carry it
# inline. 0 means "no tag" and never sets the bit, so an ordinary map that never uses this
# pays nothing -- ext_count is 0 in every WorldTile it slices. What a nonzero value MEANS
# is entirely the game's business; this only gets it from manifest to pnx_map_extended.
MAP_EXTENDED = 0x4000
# 0x8000 still free


def fold_flag_into_entry(entry, flag):
    """The pre-shift warp bit -> its home in the cell's own u16.

    Called once per cell at the same point id/flip are already assembled into `entry`,
    so warp lives in the one place from here on -- `flags[i]` (the pre-shift array)
    still gets built alongside for the Python-side reachability checks, but never
    reaches a compiled blob after this.
    """
    return entry | (MAP_WARP if flag & TILE_WARP else 0)

# Blob format versions. A mismatch between a stale .bin and a newer runtime is exactly
# the kind of failure that presents as garbage pixels rather than an error, so every
# blob is tagged and the runtime checks.
BLOB_VERSION = 15  # v15: "PS" sprite frames are no longer fixed-stride -- each frame
                   # carries its own 8-byte record (offset/w/h/origin_x/origin_y/flags/
                   # pad) in a new frame_meta table instead of sharing one sprite-wide
                   # w/h/frame_bytes, so a tightly packed sheet's frames can differ in
                   # size and each names its own root. Frames also gain per-frame
                   # collision -- the same SCALED-rect/COMPLEX-mask sparse tables "PA"
                   # already carries, keyed by frame instead of tile. And BOTH "PA" and
                   # "PS" tile_flags/frame flags bytes gain a KIND nibble
                   # (COLLISION_KIND_*) packed above the existing 2-bit mode, in the
                   # same byte. See finish_sprite/finish_atlas and PnxSprite/
                   # PNX_COLLISION_KIND (pnx_assets.h).
                   # v14: a map is 1..PNX_MAP_MAX_LAYERS streamed layers instead of one
                   # implicit grid (M13). The header's four generic bytes are now
                   # layer_count/primary_layer/warp_count/pad (were w/h/warp_count/
                   # worldtile); the preamble carries a shared 12-byte fixed section
                   # (atlas_count, tile_px, flags, atlas_slots, tile_total, dict_count,
                   # pool_bytes) then one 14-byte fixed directory entry PER LAYER
                   # (w, h, worldtile, cols, rows, bank_shift, want_slots, slot_bytes,
                   # first_bank_asset, parallax_pct_x, parallax_pct_y, wrap) followed
                   # immediately by that
                   # layer's own wt_mask -- see finish_map's own layout comment. This
                   # pass emits exactly one layer (layer_count=1, primary_layer=0) for
                   # every `[[map]]`; multi-layer authoring (`[[map.layer]]`) is not
                   # wired into the manifest yet, only the format supports it.
                   # v13: map preamble carries a cell dictionary (build_cell_dictionary) --
                   # a WorldTile's cells are 1- or 2-byte indices into it rather than raw
                   # u16 entry words, since every real map measured uses only a handful of
                   # distinct entry values. v12: atlas blobs carry baked SCALED rects /
                   # COMPLEX masks (finish_atlas); WorldTile payloads carry a sparse
                   # EXTENDED table (slice_worldtiles). v11: collision/warp moved into the
                   # cell word; the old tile_flags[]-by-id override bytes were dropped
                   # (retired, not reused for either of the above).
MAGIC_ATLAS = b"PA"
MAGIC_SPRITE = b"PS"
MAGIC_MAP = b"PM"
MAGIC_DIALOG = b"PD"
MAGIC_PALETTES = b"PP"
MAGIC_HUFFMAN_TABLE = b"PH"
MAGIC_SCENES = b"PC"
MAGIC_MUSIC = b"PN"
MAGIC_SAMPLE = b"PW"
MAGIC_FONT = b"PF"
# A 9-slice panel: one packed image plus four border-inset bytes ahead of the pixels,
# same reasoning as MAGIC_ATLAS's SCALED/COMPLEX tables (finish_atlas) -- appended to the
# body rather than squeezed into the fixed header, which has one spare field beyond w/h,
# not four. No BLOB_VERSION bump: this is a new, independent magic, not a change to the
# byte layout of anything BLOB_VERSION already promises about "PS"/"PA"/etc.
MAGIC_NINE_SLICE = b"N9"
# A WorldTile bank. Stamped like everything else: a bank is geometry, so one left over
# from a build in the other orientation is a scrambled world rather than a stale sample,
# and M4c's rule that every blob carries its orientation exists for exactly that.
MAGIC_BANK = b"PK"
# A HUD window: placed elements, anchored per orientation (see src/pnx/gfx/
# pnx_hud_window.h's own top comment) -- not a picture, but still stamped with the build's
# orientation like everything else, since anchor resolution assumes PNX_DISPLAY_WIDTH/
# HEIGHT match the build this blob shipped with.
MAGIC_HUD_WINDOW = b"HW"

HEADER_BYTES = 8

# Two different ceilings, both spelled 256, and confusing them is easy:
#
#   MAX_RESOURCES_SIZE_APPSTORE  0x40000  256 KB of BYTES in the whole .pbpack -- a
#                                         warning, and what `budget_bytes` defaults to
#   MAX_RESOURCES_SIZE           0x100000   1 MB of bytes -- the hard error
#   pbpack table_size                 256   resource ENTRIES, whatever they weigh
#
# Read from the SDK's own pebble_sdk_platform.py and pbpack.py rather than recalled. The
# entry count is the one that started mattering with WorldTile banks: a map is now a
# resource plus a bank per few WorldTiles, so a project of large maps runs out of entries
# long before it runs out of bytes.
PBPACK_MAX_RESOURCES = 256
RESOURCE_BYTES_APPSTORE = 0x40000
RESOURCE_BYTES_HARD = 0x100000

# ------------------------------------------------------------------------- worldtiles
#
# A map is stored as a grid of WorldTiles -- square blocks of map CELLS that are the unit
# of residency. Not to be confused with a metatile, which is the deduplicated 8x8 quadrant
# inside an atlas: a metatile is art, a WorldTile is a piece of the world.
#
# Every map is sliced, including small ones. When a map's whole grid fits the pool, all of
# it loads at map load and nothing is ever evicted -- so a 32x24 map costs what it always
# did and the runtime needs no second code path for "small".
WORLDTILE_DEFAULT = 16
WORLDTILE_MIN = 4
WORLDTILE_MAX = 32

# The map cell is a u16 with 10 bits of tile index, so a map's atlases share one id space
# of this size. See PNX_MAP_INDEX_MASK.
MAP_TILE_IDS = 1024

# WorldTile payloads are split across BANK resources of about this size rather than living
# in one contiguous run inside the map's own resource.
#
# This is not a tidiness decision. A `resource_load_byte_range` call costs by the size of
# the RESOURCE IT IS AGAINST, not by the offset within it or the bytes actually asked for
# -- so a WorldTile two thirds of the way through a 75KB map cost as much as one at the
# start (13 ms either way), and holding a 192x192 world whole, one big read per WorldTile
# against that same 75KB resource, took two seconds. Measured on device, including the
# controlled sweep that pinned the cause down to resource size rather than offset; see
# docs/MEASUREMENTS.md's "Flash / resource reads". Banking caps that per-call cost at the
# bank's own size instead of the map's, which is the only lever that touches the term that
# dominates.
#
# 8KB is the trade: smaller banks cost less per read and cost more resources, and a
# .pbpack has a bounded number of those (256 entries). At 8KB a 516-byte WorldTile pays
# roughly the size-driven cost of an 8KB resource per call rather than a 75KB one.
WORLDTILE_BANK_BYTES = 8192

# Hardcoded because emery is the only platform the engine builds for today; M9's
# per-platform carve is where this becomes a table. It is used only to size the resident
# WorldTile window, so being wrong here costs slots, not correctness.
SCREEN_W, SCREEN_H = 200, 228

# One WorldTile of margin on EACH SIDE of what the view can touch, so the streamer has a
# frame or more of lead before the player reaches a boundary rather than loading at the
# moment the tile becomes visible. Per side rather than per axis because the player can
# walk either way and the streamer has no velocity to lean on.
WORLDTILE_MARGIN = 1


# What one resident WorldTile costs beyond its cells: the PnxWorldTile descriptor in the
# slot array. Approximate, and only used to compare sizes against each other -- being a few
# bytes out cannot change which size wins, because the term it competes with is quadratic.
WORLDTILE_SLOT_OVERHEAD = 20


def worldtile_resident_bytes(w, h, tile_px, worldtile, resident=False):
    """What a map would hold resident at this WorldTile size, or None if it cannot.

    Three terms, and they pull in different directions, which is the whole reason this is
    picked by arithmetic rather than by a default:

      the pool     slots * (cells + header) -- grows as the SQUARE of the size
      descriptors  one per slot -- so it follows the pool
      the lookup   one byte per WorldTile in the whole MAP -- grows as the size shrinks

    **Which way the answer goes depends on whether the map streams.** A streaming map holds
    a fixed number of WorldTiles -- the view plus a margin ring -- so a bigger WorldTile
    means a bigger ring holding world nobody can see, and the pool dominates: smaller wins
    until the lookup catches up. A map held whole has no ring at all, every term scales with
    the count, and bigger wins because there are fewer per-tile headers and descriptors.

    On a 200x228 screen at 16px tiles: streaming wants 8 and 16 costs twice as much; held
    whole wants 16 and 8 costs 14% more. One rule, two answers, both right for their mode.
    """
    cols = (w + worldtile - 1) // worldtile
    rows = (h + worldtile - 1) // worldtile
    n = cols * rows
    if resident:
        # A slot per WorldTile, and the slot index is a byte.
        if n > 255:
            return None
        slots = n
    else:
        win = worldtile_window(tile_px, worldtile)
        slots = min(n, win[0] * win[1])
    slot_bytes = (4 + worldtile * worldtile * 2 + 3) & ~3
    return slots * (slot_bytes + WORLDTILE_SLOT_OVERHEAD) + n


def pick_worldtile(name, w, h, tile_px, resident=False):
    """The WorldTile size with the smallest resident cost, and what it beat.

    Sized rather than defaulted, for the same reason the pipeline measures both atlas
    layouts and picks: the answer depends on the screen, the map and whether it streams, a
    default is right for one shape of content and quietly wrong for the rest, and the
    arithmetic is free.
    """
    scored = []
    for s in (4, 8, 16, 32):
        if not WORLDTILE_MIN <= s <= WORLDTILE_MAX:
            continue
        cost = worldtile_resident_bytes(w, h, tile_px, s, resident)
        if cost is not None:
            scored.append((cost, s))
    if not scored:
        raise BuildError(
            f"map {name!r}: {w}x{h} cannot be held whole at any WorldTile size -- the "
            f"largest, {WORLDTILE_MAX}, still needs more than the 255 slots the format can "
            f"address. A map this size is one that has to stream; drop `resident`.")
    scored.sort()
    best_bytes, best = scored[0]
    return best, best_bytes, max(b for b, _ in scored)


def worldtile_window(tile_px, worldtile):
    """How many WorldTiles must be resident at once, as (cols, rows).

    A view `view_px` wide can touch floor((view_px - 1) / span) + 2 WorldTiles of `span`
    pixels each -- the +2, not +1, because the worst alignment starts one pixel before a
    boundary and so reaches one WorldTile further than a view of the same width that
    happens to be aligned. Getting this wrong costs a column of tiles at the screen edge
    exactly when the camera is between WorldTiles, which reads as flicker rather than as a
    missing slot.

    The margin is then added on top, and it is what the pool is really for: without it a
    WorldTile is read in the frame it becomes visible, and any hitch in that read is a gap
    on screen. With it there is a whole WorldTile of walking in hand.
    """
    span = tile_px * worldtile
    return tuple((v - 1) // span + 2 + 2 * WORLDTILE_MARGIN for v in (SCREEN_W, SCREEN_H))


# ------------------------------------------------------------------------ orientation
#
# Named for where the BUTTON CLUSTER ends up, because that is the thing the author is
# actually choosing. "landscape_left" says nothing about whether the device or the image
# turned, and every codebase that uses it has an argument about which way it means; the
# cluster is a physical object the author can point at.
#
# Portrait puts it under one thumb (a menu, an RPG). On the top edge it falls under both
# index fingers and reads as shoulder triggers (a shooter); on the bottom edge, under both
# thumbs, as flippers (pinball). On the left edge it is portrait's mirror -- a half-turn,
# not a quarter one, so it is the one case that does not swap width and height. See
# docs/PLATFORM.md -- this is only possible because the device is played off the wrist in
# two hands.
ORIENT_BUTTONS_RIGHT = 0     # portrait: the display's native orientation
ORIENT_BUTTONS_TOP = 1       # image rotated clockwise into the framebuffer
ORIENT_BUTTONS_BOTTOM = 2    # image rotated anticlockwise into the framebuffer
ORIENT_BUTTONS_LEFT = 3      # image rotated 180 degrees; dimensions unchanged

# The two that swap width and height -- everything that carves or packs a grid checks
# this rather than "orient != RIGHT", because buttons_left does not belong in that set.
LANDSCAPE_ORIENTS = (ORIENT_BUTTONS_TOP, ORIENT_BUTTONS_BOTTOM)

ORIENTATIONS = {
    "buttons_right": ORIENT_BUTTONS_RIGHT,
    "buttons_top": ORIENT_BUTTONS_TOP,
    "buttons_bottom": ORIENT_BUTTONS_BOTTOM,
    "buttons_left": ORIENT_BUTTONS_LEFT,
    # `portrait` is what everyone will type for the default, and it is unambiguous
    # because there is only one of it. The landscape spellings deliberately have no
    # alias: "landscape" alone does not say which way up, and guessing is how content
    # ships upside down.
    "portrait": ORIENT_BUTTONS_RIGHT,
}

ORIENT_NAMES = {ORIENT_BUTTONS_RIGHT: "buttons_right",
                ORIENT_BUTTONS_TOP: "buttons_top",
                ORIENT_BUTTONS_BOTTOM: "buttons_bottom",
                ORIENT_BUTTONS_LEFT: "buttons_left"}

# Which way the pen walks between glyphs, stamped into the font blob. Pre-rotation costs
# exactly one piece of engine code, here: a glyph bitmap turned on its side still blits
# like any other rectangle, but the next glyph is no longer to the right of it.
#
# It is a property of the FONT rather than of the project, both because that is where the
# runtime needs it -- a blob that draws itself correctly cannot be paired with the wrong
# constant -- and because the axis is the same mechanism a vertical script wants. A font
# set top-to-bottom is ADVANCE_Y_POS with unrotated glyphs; landscape is the same field
# with rotated ones.
ADVANCE_X_POS = 0     # left to right: portrait, and every Latin face
ADVANCE_Y_POS = 1     # top to bottom
ADVANCE_Y_NEG = 2     # bottom to top
ADVANCE_X_NEG = 3     # right to left: buttons_left, portrait turned upside down

ORIENT_ADVANCE = {ORIENT_BUTTONS_RIGHT: ADVANCE_X_POS,
                  ORIENT_BUTTONS_TOP: ADVANCE_Y_POS,
                  ORIENT_BUTTONS_BOTTOM: ADVANCE_Y_NEG,
                  ORIENT_BUTTONS_LEFT: ADVANCE_X_NEG}


def parse_orientation(value, where):
    if value is None:
        return ORIENT_BUTTONS_RIGHT
    if value not in ORIENTATIONS:
        raise BuildError(f"{where}: unknown orientation {value!r} "
                         f"(known: {', '.join(sorted(ORIENTATIONS))})")
    return ORIENTATIONS[value]


def rotate_point(x, y, w, h, orient):
    """A point in the author's frame -> the same point in the framebuffer's.

    `w, h` are the dimensions of the thing being rotated, in the AUTHOR's frame.

    Derivation, once, so nothing downstream has to re-do it. The author works in the frame
    they see: ax to the right, ay down. The framebuffer never rotates -- fx, fy stay the
    display's own axes. Holding the device so the button cluster (physically the right
    edge) sits along the TOP means the physical +x axis now points up in the author's
    view, which is fx = h-1-ay, fy = ax. Along the BOTTOM is the same rotation the other
    way. LEFT is a half-turn rather than a quarter one -- both axes invert and neither
    swaps: fx = w-1-ax, fy = h-1-ay.
    """
    if orient == ORIENT_BUTTONS_TOP:
        return h - 1 - y, x
    if orient == ORIENT_BUTTONS_BOTTOM:
        return y, w - 1 - x
    if orient == ORIENT_BUTTONS_LEFT:
        return w - 1 - x, h - 1 - y
    return x, y


def rotate_origin(ox, oy, w, h, orient):
    """A sprite frame's root -> the same point in the framebuffer's frame, for
    pack_sprite's per-frame origin.

    Not rotate_point: that formula has a `-1` because it indexes a PIXEL (a cell in
    [0,w-1]x[0,h-1]), and a sprite's origin is a BOUNDARY coordinate instead (0..w, 0..h
    inclusive -- oy = h is the ordinary "feet at the bottom edge" default, one past the
    last row, the same way rotate_border treats a rect's edges as insets rather than
    pixel indices). Dropping the `-1` is the whole difference: the same four physical
    rotations rotate_point derives, applied to a continuous boundary instead of a discrete
    cell.
    """
    if orient == ORIENT_BUTTONS_TOP:
        return h - oy, ox
    if orient == ORIENT_BUTTONS_BOTTOM:
        return oy, w - ox
    if orient == ORIENT_BUTTONS_LEFT:
        return w - ox, h - oy
    return ox, oy


def rotate_rect(rx, ry, rw, rh, w, h, orient):
    """A sprite frame's authored SCALED collision rect -> the same rect in the
    framebuffer's frame, for parse_shape_collision's per-frame `rect`.

    Both corners are boundary coordinates (rotate_origin's own domain), so this is just
    rotate_origin applied twice -- the near corner and the far corner -- normalised back
    into (x, y, w, h). No new derivation: a rect IS two points.
    """
    x0, y0 = rotate_origin(rx, ry, w, h, orient)
    x1, y1 = rotate_origin(rx + rw, ry + rh, w, h, orient)
    nx, ny = min(x0, x1), min(y0, y1)
    return nx, ny, abs(x1 - x0), abs(y1 - y0)


def rotate_dims(w, h, orient):
    return (h, w) if orient in LANDSCAPE_ORIENTS else (w, h)


def rotate_grid(buf, w, h, orient, stride=1):
    """Rotate a row-major grid of `stride`-byte cells. Returns (buf, new_w, new_h).

    One function for pixels, tiles and flag planes alike: a tilemap rotates exactly the
    way its pixels do, which is the whole reason pre-rotation works at all. Rotate the
    grid and rotate each cell's art, and the ordinary portrait blit puts a correct
    landscape image on the screen.
    """
    if orient == ORIENT_BUTTONS_RIGHT:
        return bytes(buf), w, h
    nw, nh = rotate_dims(w, h, orient)
    out = bytearray(len(buf))
    for y in range(h):
        for x in range(w):
            nx, ny = rotate_point(x, y, w, h, orient)
            src = (y * w + x) * stride
            dst = (ny * nw + nx) * stride
            out[dst:dst + stride] = buf[src:src + stride]
    return bytes(out), nw, nh


def rotate_border(bl, bt, br, bb, w, h, orient):
    """A 9-slice panel's four border insets, rotated the same way its pixels are.

    Reuses rotate_point rather than a hand-derived edge-permutation table: the inner
    rect's two opposite corners (in the AUTHOR's w x h frame) rotate to two points in the
    framebuffer's nw x nh frame, and the new border sizes are just those points' distance
    from the new edges. One primitive, trusted everywhere else in this file a coordinate
    needs to survive a build-time rotation (map starts, tile planes -- see rotate_point's
    own comment), rather than a second, independently-derived transform that could disagree
    with it.
    """
    nw, nh = rotate_dims(w, h, orient)
    x0, y0 = rotate_point(bl, bt, w, h, orient)
    x1, y1 = rotate_point(w - 1 - br, h - 1 - bb, w, h, orient)
    lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
    lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
    return lo_x, lo_y, nw - 1 - hi_x, nh - 1 - hi_y


def rotate_levels(rows, orient):
    """The same rotation over a list-of-lists, which is how glyphs are rasterised."""
    if orient == ORIENT_BUTTONS_RIGHT or not rows:
        return rows
    h, w = len(rows), len(rows[0])
    nw, nh = rotate_dims(w, h, orient)
    out = [[0] * nw for _ in range(nh)]
    for y in range(h):
        for x in range(w):
            nx, ny = rotate_point(x, y, w, h, orient)
            out[ny][nx] = rows[y][x]
    return out

# Index 0 of every palette is transparent, following the SNES convention. Costs a slot
# (15 usable of 16) and measured at 0.1% more bytes across five real tilesets, in
# exchange for uniform transparency and a blitter that can reject a pixel before it ever
# reads the palette.
PALETTE_ENTRIES = 16
PALETTE_USABLE = PALETTE_ENTRIES - 1
TRANSPARENT_INDEX = 0
PALETTE_BYTES = PALETTE_ENTRIES

# A 1-bit platform never indexes a palette at all -- its atlas/sprite pixels are ~bw
# resources (pack_unit_2bpp, below) with ink/paper/dither already baked in per pixel, and
# a solid fill or a glyph classifies its own raw colour directly (pnx_bw_is_ink,
# pnx_gfx.c). An earlier version of this pipeline gave PnxPalette a per-entry ink mask for
# a 4bpp-plus-mask fallback path; that path is gone (a build is exclusively one art format
# now), and palettes.bin is colour-only again -- no ~bw variant, no second format.

# Metatiling trades render time for space, so auto-selection needs a threshold rather
# than "any saving at all". See docs/MEASUREMENTS.md for the device numbers behind it.
#
# Lowered from 0.25 after measuring the five real sheets WITH mirror-aware dedup: quadrant
# reuse is 1.30-1.40x, not the 1.96x measured before mirrors existed, because the two
# optimisations overlap -- a mirrored quadrant pair was already being collapsed. Metatiles
# now save 14-19% on full sheets, so a 25% gate meant they could never fire. 12% keeps the
# gate meaningful while letting a real saving through, and the render cost it buys is
# affordable: 35% more frame time takes us from 14.5% of the budget to 19.7%.
METATILE_MIN_SAVING = 0.12

DEPTH_4BPP = 4
DEPTH_6BPP = 6

# The appstore rejects bundles over 256KB of resources. The hard device cap is 1MB, but
# shipping is the constraint that matters.
DEFAULT_BUDGET = 256 * 1024


class BuildError(Exception):
    """Raised for any invalid content. Always fatal -- never warn and continue."""


# --------------------------------------------------------------------------- colour

def parse_colorkey(spec, where):
    """`colorkey = [r, g, b]`, or nothing at all.

    Absent is not a missing value, it is a choice: art with a real alpha channel needs no
    key, and most of it has one. The key exists for the sheets that do not -- the ones
    drawn on magenta or on a flat background colour, which is how a great deal of tile art
    is still distributed.
    """
    key = spec.get("colorkey")
    if key is None:
        return None
    if (not isinstance(key, (list, tuple)) or len(key) != 3
            or not all(isinstance(c, int) and 0 <= c <= 255 for c in key)):
        raise BuildError(f"{where}: colorkey must be three integers 0-255 -- "
                         f"e.g. colorkey = [255, 0, 255] -- not {key!r}")
    return tuple(key)


def to_gcolor8(rgba, colorkey=None):
    r, g, b, a = rgba
    if a < 128:
        return TRANSPARENT
    if colorkey and (r, g, b) == tuple(colorkey):
        return TRANSPARENT
    return OPAQUE | ((r >> 6) << 4) | ((g >> 6) << 2) | (b >> 6)


def load_sheet(root, name):
    if Image is None:
        raise BuildError("Pillow is required for image assets: pip install pillow")
    path = os.path.join(root, name)
    if not os.path.exists(path):
        raise BuildError(f"missing sheet: {path}")
    return Image.open(path).convert("RGBA")


def blob_header(magic, a=0, b=0, c=0, d=0, orient=ORIENT_BUTTONS_RIGHT):
    """Common 8-byte prefix: magic[2], version, four format-specific bytes, orientation.

    Padded to 8 rather than the 7 it needs, so that pixel data begins at an aligned
    offset. The blitter reads tile rows as words where it can, and a 7-byte header would
    make every one of those accesses unaligned.

    The byte that padding left over now carries the orientation the BUILD was made at, so
    a blob left over from a portrait build cannot be drawn sideways by a landscape one --
    which presents as scrambled art rather than as an error.

    Every blob carries it, including the ones with no geometry to rotate. A song does not
    care which way the watch is held, but stamping it 0 would make "orientation-free" and
    "built portrait" the same byte, and the loader could no longer tell a stale atlas from
    a legitimate one. One manifest builds one way at a time, so the uniform stamp costs
    nothing and the check stays exact.
    """
    return (magic + bytes([BLOB_VERSION, a & 0xFF, b & 0xFF, c & 0xFF, d & 0xFF])
            + bytes([orient]))


# ---------------------------------------------------------------------------- atlas

def pack_atlas(root, spec, orient=ORIENT_BUTTONS_RIGHT):
    """Quantise a region to GColor8 and deduplicate identical tiles.

    Dedup is what makes a large sheet usable: raw, the probe's 1280x1248 source is
    1560KB, past even the 1MB device ceiling. Region selection plus dedup brings it into
    range; compression alone would not, because resources are stored already packed.

    Tiles are rotated (`orient`, the whole-sheet device-orientation turn, not the
    per-tile transpose dedup below) as they are carved, BEFORE dedup, so that everything
    below this line -- the mirror/rotation keys, the flip and MAP_ROTATE bits a map entry
    carries, the metatile quadrants -- describes the framebuffer's frame rather than the
    author's. Doing it afterwards would leave a flip_x pair recorded for what is now a
    vertical mirror, which nothing would notice until a tile picker started emitting
    those bits.
    """
    name = spec["name"]
    im = load_sheet(root, spec["sheet"])
    px = im.load()
    T = int(spec["tile"])
    rx, ry, rw, rh = spec["region"]
    max_tiles = int(spec.get("max_tiles", 255))

    # Pixel offset: where the tile GRID starts, separate from `region`'s own tile-unit
    # origin. A sheet whose art does not begin flush with the top-left corner -- a
    # margin, a shared border strip, a sprite sheet with padding baked in by whatever
    # drew it -- has no tile-aligned pixel to call (0,0), so `region` alone could never
    # name its first cell. `offset` is that missing degree of freedom: it moves the WHOLE
    # grid by up to T-1 px before `region` starts counting whole tiles from it.
    ox, oy = (int(v) for v in spec.get("offset", (0, 0)))
    if ox < 0 or oy < 0:
        raise BuildError(f"atlas {name!r}: offset {[ox, oy]} must not be negative")

    # Tiles the author dropped in the editor, as indices into the REGION read left to
    # right, top to bottom -- which is the order the editor lays them out, so the number
    # in the manifest is the cell that was clicked.
    #
    # Excluded before dedup, not after, because that is what the author is looking at:
    # a grid of sheet positions. Dropping a deduplicated tile instead would silently take
    # every other position that happens to share its pixels.
    # Named `ckey`, not `key`, because the tile-dedup loop below binds `key` to each
    # tile's bytes. It used to share the name, so the colour key survived exactly one
    # tile and every tile after it was read with a bytes object as its key -- which
    # matches nothing, so the background stayed opaque. Transparency that works on the
    # first tile of a sheet and silently nowhere else is not a crash; it is art that
    # looks wrong on a watch.
    ckey = parse_colorkey(spec, f"atlas {name!r}")

    excluded = set()
    for e in spec.get("exclude", []):
        if isinstance(e, (list, tuple)) and len(e) == 2:
            excluded.add((int(e[0]), int(e[1])))         # absolute sheet coordinates
        else:
            i = int(e)
            if not 0 <= i < rw * rh:
                raise BuildError(f"atlas {name!r}: exclude index {i} is outside the "
                                 f"{rw}x{rh} region ({rw * rh} cells)")
            excluded.add((rx + i % rw, ry + i // rw))

    sheet_w, sheet_h = im.size
    if ox + (rx + rw) * T > sheet_w or oy + (ry + rh) * T > sheet_h:
        raise BuildError(
            f"atlas {name!r}: region {rx},{ry} {rw}x{rh} tiles of {T}px, offset by "
            f"{[ox, oy]}px, runs past the sheet ({sheet_w}x{sheet_h}px). Region is in "
            f"TILE units, offset is in PIXELS -- both count from the sheet's top-left.")

    def flip_x(b):
        return b"".join(bytes(reversed(b[j * T:(j + 1) * T])) for j in range(T))

    def flip_y(b):
        return b"".join(bytes(b[(T - 1 - j) * T:(T - j) * T]) for j in range(T))

    # Swap rows and columns -- the same transpose PNX_MAP_ROTATE names (pnx_gfx.c's
    # pnx_blit_4bpp, PNX_FLIP_ROTATE) and pnx_assets.h's MAP_ROTATE comment enumerates.
    # Only ever applied to a square tile, same as the blitter.
    def transpose(b):
        return bytes(b[c * T + r] for r in range(T) for c in range(T))

    # Mirror- AND rotation-aware dedup. A tile that is a mirror OR a transpose of one
    # already kept does not need its own copy -- the map entry carries the flip bits plus
    # MAP_ROTATE, and the blitter reads the source transposed and/or backwards. Together
    # {rotate, flip_x, flip_y} span all 8 symmetries of a square (see MAP_ROTATE's own
    # comment above), so this is the full saving available, not just the mirror half of it.
    #
    # IMPORTANT: once rotate is involved, flip_x/flip_y do not compose onto the transposed
    # buffer the naive way. pnx_gfx.c's rotate path computes the destination's source
    # column from the destination ROW (gated by FLIP_X) and its source row from the
    # destination COLUMN (gated by FLIP_Y) -- which, worked through, means engine bit
    # FLIP_X after a transpose is this module's flip_y, and engine bit FLIP_Y after a
    # transpose is this module's flip_x. Getting this backwards would dedup a tile against
    # an orientation the watch never actually draws, which is a silent art bug, not a
    # crash -- so the swap below is deliberate, not a typo.
    unique, seen, empty, mirrored = [], {}, 0, 0
    origin = []          # sheet position each unique tile was first seen at
    for ty in range(ry, ry + rh):
        for tx in range(rx, rx + rw):
            if (tx, ty) in excluded:
                continue
            buf = bytearray(T * T)
            for j in range(T):
                for i in range(T):
                    ri, rj = rotate_point(i, j, T, T, orient)
                    buf[rj * T + ri] = to_gcolor8(px[ox + tx * T + i, oy + ty * T + j], ckey)
            key = bytes(buf)
            if not any(key):
                empty += 1
                continue
            if key in seen:
                if seen[key][1]:
                    mirrored += 1
                continue
            if len(unique) >= max_tiles:
                continue
            idx = len(unique)
            unique.append(key)
            origin.append((tx, ty))
            # The true orientation is registered first so it always wins a later exact
            # match; mirrors and rotations only claim keys nothing else has taken.
            seen[key] = (idx, 0)
            tkey = transpose(key)
            for variant, bits in ((flip_x(key), 1),
                                  (flip_y(key), 2),
                                  (flip_x(flip_y(key)), 3),
                                  (tkey, 4),
                                  # Swapped deliberately -- see the comment above transpose().
                                  (flip_y(tkey), 5),
                                  (flip_x(tkey), 6),
                                  (flip_x(flip_y(tkey)), 7)):
                if variant not in seen:
                    seen[variant] = (idx, bits)

    if not unique:
        raise BuildError(
            f"atlas {name!r}: region contains no non-empty tiles"
            + (f" -- all {len(excluded)} remaining cells were excluded" if excluded
               else ""))
    if len(unique) > 255:
        raise BuildError(f"atlas {name!r}: {len(unique)} tiles, but map tile indices "
                         f"are u8 -- cap max_tiles at 255")

    repaired = 0
    fixed = []
    for t in unique:
        t2, merged = reduce_colours(t)
        if merged:
            repaired += 1
        fixed.append(t2)

    # Palette variants: the same tileset recoloured. Read each variant at the SAME sheet
    # positions the unique tiles came from, so no second dedup pass can disagree with the first.
    variants = []
    for vpath in spec.get("variants", []):
        vname = os.path.splitext(os.path.basename(vpath))[0]
        vim = load_sheet(root, vpath)
        if vim.size != im.size:
            raise BuildError(f"atlas {name!r}: variant {vpath!r} is {vim.size[0]}x{vim.size[1]} "
                             f"but the base sheet is {sheet_w}x{sheet_h} -- a palette variant must "
                             f"share the base's layout exactly")
        vpx = vim.load()
        bijection = {}
        for (tx, ty), base_tile in zip(origin, unique):
            for j in range(T):
                for i in range(T):
                    # Walk the SHEET's frame and index the rotated tile, rather than the
                    # other way round: any complaint below then names the pixel the author
                    # can find in their PNG.
                    ri, rj = rotate_point(i, j, T, T, orient)
                    b = base_tile[rj * T + ri]
                    v = to_gcolor8(vpx[ox + tx * T + i, oy + ty * T + j], ckey)
                    if (b == TRANSPARENT) != (v == TRANSPARENT):
                        raise BuildError(
                            f"atlas {name!r}: variant {vpath!r} differs in TRANSPARENCY at sheet "
                            f"tile {(tx, ty)} pixel {(i, j)}. A palette variant may change any "
                            f"colour but must not move a pixel or change what is see-through.")
                    if b == TRANSPARENT:
                        continue
                    if bijection.setdefault(b, v) != v:
                        raise BuildError(
                            f"atlas {name!r}: variant {vpath!r} is not a consistent recolour -- "
                            f"base colour 0x{b:02X} maps to both 0x{bijection[b]:02X} and "
                            f"0x{v:02X}. Recolour by mapping each colour to exactly one other.")
        if len(set(bijection.values())) != len(bijection):
            raise BuildError(f"atlas {name!r}: variant {vpath!r} merges two base colours into one, "
                             f"so it cannot share the base's pixel data. Keep the recolour "
                             f"one-to-one.")
        variants.append({"name": vname, "map": bijection})

    print(f"  atlas {name}: {rw*rh} considered, {empty} empty, {len(fixed)} unique")
    for v in variants:
        print(f"    palette variant {v['name']}: {len(v['map'])} colours remapped")
    if mirrored:
        saved = mirrored * (T * T // 2)
        print(f"    {mirrored} tile(s) matched a mirror or rotation of another and were "
              f"dropped ({saved} bytes saved)")
    if repaired:
        print(f"    NOTE {repaired} tile(s) exceeded {PALETTE_USABLE} colours and were "
              f"reduced -- edit the art to avoid this")

    anim = parse_atlas_anim(spec.get("anim", {}), len(fixed), name)

    return {"name": name, "tiles": fixed, "tile_px": T, "out": spec["out"],
            # Default OFF, not "auto": the runtime cannot read the metatile layout yet,
            # and a default that emits blobs the loader rejects is worse than no
            # feature at all. Flip to "auto" when pnx_atlas_load understands it.
            "repaired": repaired, "metatiles": spec.get("metatiles", False),
            "variants": variants, "anim": anim,
            # Sheet TILE coordinates (not pixels -- `offset` already folded in above) each
            # packed tile was first carved from, index-aligned with `tiles`. Not consumed
            # by the build; the editor uses it to resolve a raw sheet cell back to the
            # packed index it became, which is what makes a carved tile editable by name
            # rather than only by number.
            "origin": origin,
            # Empty and mirror/rotation counts, not just printed: the editor's `analyse()`
            # prices a carve by calling this function directly (rather than re-implementing
            # dedup a second time) and needs both numbers for its own stats.
            "empty": empty, "mirrored": mirrored}


def build_metatiles(tiles, pal_of, T, quiet=False):
    """Split each tile into 8x8 quadrants and deduplicate the bank.

    Measured on five real tilesets: 8,700 quadrant slots collapse to 4,436 unique, 1.96x
    BEFORE mirror-aware dedup; 1.33x after it, since the two overlap,
    for a total saving of 1.72x against flat tiles. The palette constraint -- a quadrant
    must fit the palette of the tile it belongs to -- costs almost nothing, because tiles
    that share quadrants tend to share palettes anyway (4,308 unique without it).

    Indices are u16: 4,436 does not fit a byte, and pretending otherwise would truncate
    silently.
    """
    half = T // 2
    bank, index = [], {}
    defs = []

    for tile, pal in zip(tiles, pal_of):
        quad_ids = []
        for qy in (0, half):
            for qx in (0, half):
                sub = tuple(tile[(qy + j) * T + qx + i]
                            for j in range(half) for i in range(half))
                key = (sub, pal)
                if key not in index:
                    index[key] = len(bank)
                    bank.append(sub)
                quad_ids.append(index[key])
        defs.append(quad_ids)

    if len(bank) > 0xFFFF:
        raise BuildError(f"metatiles: {len(bank)} sub-tiles exceeds the u16 index limit")

    if not quiet:
        print(f"    metatiles: {len(tiles) * 4} quadrants -> {len(bank)} unique "
              f"({len(tiles) * 4 / len(bank):.2f}x reuse)")
    return bank, defs


# ---------------------------------------------------------------------- global-table Huffman
# (compress = "huffman", PNX_COMPRESS_HUFFMAN): a project-wide two-pass build -- pass 1
# ("collect") runs every atlas/sprite through their NORMAL tile-selection/palette logic
# unchanged, but instead of encoding pixel bodies, stashes each unit's own crush-ready
# indices keyed by (asset name, unit index); once every atlas and sprite has contributed,
# ONE shared run-length Huffman table is built from the pooled statistics
# (tools/pnx_huffman_codec.py's build_shared_table) and every unit's real body is encoded
# against it; pass 2 ("final") re-runs the SAME atlas/sprite logic (shared's palette set
# is already fully settled before either pass runs -- settle_palettes, called once in
# build() before any finish_atlas/finish_sprite -- so re-running produces identical
# per-unit indices, not a second independent decision) and looks up each unit's
# already-encoded bytes by that same key instead of re-encoding.
#
# Module-level rather than threaded through every call site's signature because this
# pipeline runs once per process invocation, single-threaded, start to finish -- the same
# posture compress_atlases/compress_sprites' own module-wide, not-per-asset scope already
# has.
_huffman_phase = None       # None | "collect" | "final"
_huffman_collect = {}       # (asset_name, unit_index) -> indices, populated in "collect"
_huffman_final = {}         # (asset_name, unit_index) -> encoded bytes, used in "final"

# A ~bw platform never ships the colour blob at all (docs/PORTING.md's file-tag
# substitution picks one or the other per platform, never both), so its run-length
# statistics are pooled into their OWN table rather than diluting the colour one with
# 2bpp ink-state runs a colour build will never decode -- same reasoning
# bitplane_encode_bw already gets by being a fully separate encoder from bitplane_encode,
# not a shared one branching on bit depth.
_huffman_collect_bw = {}
_huffman_final_bw = {}


def _huffman_collect_or_encode(collect, final, asset_name, units_indices):
    """Shared by huffman_collect_or_encode/huffman_collect_or_encode_bw: in the "collect"
    pass, records each unit's own (already palette-packed-and-unpacked-back-to-indices --
    the same prep encode_bitplane_unit's callers already do, just handed in rather than
    redone here) indices for the global table build; in the "final" pass, returns each
    unit's already-encoded bytes (huffman_finalize_table/_bw already computed them)
    wrapped in the same offset-table-plus-concat shape encode_bitplane_units uses --
    format-agnostic, so it's reused here rather than duplicated.
    """
    units_indices = list(units_indices)
    if _huffman_phase == "collect":
        for i, indices in enumerate(units_indices):
            collect[(asset_name, i)] = indices
        return b""  # discarded -- this pass's blobs are never used
    if _huffman_phase == "final":
        units = [final[(asset_name, i)] for i in range(len(units_indices))]
        return encode_bitplane_units(units)
    raise BuildError("huffman_collect_or_encode called outside a collect/final pass")


def huffman_collect_or_encode(asset_name, units_indices):
    return _huffman_collect_or_encode(_huffman_collect, _huffman_final, asset_name,
                                      units_indices)


def huffman_collect_or_encode_bw(asset_name, units_indices):
    return _huffman_collect_or_encode(_huffman_collect_bw, _huffman_final_bw, asset_name,
                                      units_indices)


def _huffman_finalize(collect, max_code_len):
    """Shared by huffman_finalize_table/_bw: builds ONE table from everything a collect
    pass gathered, and pre-computes every unit's real encoded bytes against it. Returns
    (table_bytes, encoded) -- table_bytes is the table's own wire bytes
    (tools/pnx_huffman_codec.py's encode_table), to be emitted as its own resource;
    encoded is the {key: bytes} map the matching "final" pass reads from.
    """
    keys = list(collect.keys())
    pixel_lists = list(collect.values())
    codes, run_bits, per_unit = hfc.build_shared_table(pixel_lists, max_code_len)
    table_bytes = hfc.encode_table(codes, run_bits)

    encoded = {}
    for key, tup in zip(keys, per_unit):
        n, local, order, k, bpp, start_bit, runs = tup
        if k > 1 and any(r not in codes for r in runs):
            # Defensive, not expected to trigger: the table was built from these exact
            # units' own runs, so every run length it needs to cover is already in
            # `codes`. Kept anyway rather than trusting that by construction -- a
            # KeyError here would otherwise crash the build instead of falling back.
            off_table = bytes((order[i] << 4) | (order[i + 1] if i + 1 < k else 0)
                              for i in range(0, k, 2))
            encoded[key] = (bytes([0x80 | ((k - 1) & 0xF)]) + off_table
                           + hfc.pack_raw_indices(local))
        else:
            encoded[key] = hfc.encode_unit_with_table(n, local, order, k, bpp, start_bit,
                                                       runs, codes)
    print(f"    huffman: {len(keys)} units pooled into one project-wide table, "
          f"{len(codes)} distinct run lengths, table={len(table_bytes)} B")
    return table_bytes, encoded


def huffman_finalize_table(max_code_len=16):
    global _huffman_final
    table_bytes, _huffman_final = _huffman_finalize(_huffman_collect, max_code_len)
    return table_bytes


def huffman_finalize_table_bw(max_code_len=16):
    global _huffman_final_bw
    table_bytes, _huffman_final_bw = _huffman_finalize(_huffman_collect_bw, max_code_len)
    return table_bytes


def finish_atlas(atlas, collision, shared, orient=ORIENT_BUTTONS_RIGHT, pack_2bit=False,
                 compress_atlases=False):
    """Palettise and pack. Called once map compilation has settled tile flags.

    `shared` is the running list of palettes from earlier atlases, so a later atlas
    reuses or extends what already exists instead of building its own.

    `pack_2bit` additionally builds `atlas["blob_bw"]` (docs/PORTING.md's `~bw` escape
    hatch): the SAME header, per-tile palette assignment, flags and (if metatiled) index
    table as `atlas["blob"]`, with only the pixel payload re-encoded at 2bpp instead of
    4bpp -- see pack_unit_2bpp. Sharing that structure is what lets the ~bw variant be a
    pure pixel-format swap rather than a second copy of the whole packing decision.

    `collision` is parse_atlas_collision's {local index: (mode, kind, extra)} -- mode and
    kind both pack into the one `flags` byte (`(kind << 2) | mode`), but SCALED's rect and
    COMPLEX's mask are too big for one byte each and get their own SPARSE tail tables
    instead, appended after the pixel payload in both `blob` and `blob_bw` (the shape does
    not depend on pixel format, so there is no reason for two copies of it). A COMPLEX
    tile with no explicit mask defaults to its own opacity -- pack_collision_mask against
    `tiles[i]` -- which is why this has to run here rather than in parse_atlas_collision:
    the tile's own pixels are not settled (mirrored, rotated, carved) until now.
    """
    tiles, T = atlas["tiles"], atlas["tile_px"]
    sets = atlas_colour_sets(atlas)

    before = len(shared)
    palettes, assign = merge_palettes(sets, shared, frozen=True)
    shared[:] = palettes

    if any(a is None for a in assign):
        raise BuildError(f"atlas {atlas['name']!r}: a tile still exceeds "
                         f"{PALETTE_USABLE} colours after reduction")
    if len(palettes) > 255:
        raise BuildError(f"atlas {atlas['name']!r}: {len(palettes)} palettes, but the "
                         f"per-tile palette index is a u8")

    default = (COLLISION_NONE, COLLISION_KIND_WALL, None)
    flags = bytes(((collision.get(i, default)[1] << 2) | collision.get(i, default)[0])
                  for i in range(len(tiles)))

    # SCALED rects and COMPLEX masks, sparse -- most tiles are NONE or plain SOLID, which
    # already fit in `flags` above and cost nothing more here. `kind` is not repeated into
    # either sparse table: it is already packed into `flags` above, one byte per tile,
    # which fully determines a tile's (mode, kind) regardless of whether it also has an
    # entry here.
    scaled = bytearray()
    scaled_count = 0
    complex_masks = bytearray()
    complex_count = 0
    for i, (mode, kind, extra) in sorted(collision.items()):
        if mode == COLLISION_SCALED:
            rx, ry, rw, rh = extra
            scaled += i.to_bytes(2, "little") + bytes([rx, ry, rw, rh])
            scaled_count += 1
        elif mode == COLLISION_COMPLEX:
            # `extra` is already-packed mask bytes when the manifest authored one
            # explicitly (parse_atlas_collision); otherwise fall back to the tile's own
            # opacity, computed now because this is the first point the art is settled.
            mask = extra if extra is not None else pack_collision_mask(tiles[i], T, T)
            complex_masks += i.to_bytes(2, "little") + mask
            complex_count += 1
    shapes = (scaled_count.to_bytes(2, "little") + bytes(scaled)
             + complex_count.to_bytes(2, "little") + bytes(complex_masks))

    atlas_bitplane = compress_atlases == "bitplane"
    atlas_huffman = compress_atlases == "huffman"
    pixel_compress = False if (atlas_bitplane or atlas_huffman) else compress_atlases

    def bitplane_encode(units_pixels_and_palettes):
        """bitplane-compressed pixel data replaces raw pixels in "PA" when enabled.
        Format: offset_table((unit_count+1) x u16 LE) + concat(encoded units). Metadata
        (assign, flags, metatiles, shapes) stays resident; pixels stay in ROM. The
        addressable UNIT is a whole tile when flat, or a deduplicated quadrant when
        metatiled -- callers pass whichever list of (pixels, palette) pairs matches, so a
        metatiled atlas's bitplane units are the same `bank` every other encoding of it
        already deduplicates against, not one unit per original tile.
        """
        units = []
        for px, pal in units_pixels_and_palettes:
            packed	= pack_unit_4bpp(px, pal)
            indices = unpack_4bpp_to_indices(packed, len(px))
            units.append(encode_bitplane_unit(indices))
        return encode_bitplane_units(units)

    def bitplane_encode_bw(units_pixels):
        """`bitplane_encode`'s `~bw` counterpart: same offset-table + concat(units)
        format, but each unit's indices are ink states (0-3, bw_pixel_states) rather than
        a palette-relative index -- there is no palette on a 1-bit build. A 2-colour tile
        (the common case: plain ink-on-paper art with no dither) costs ~1 bit/pixel here,
        the same as it would on a colour build's bitplane path with 2 real colours --
        bitplane's cost is driven by a unit's own distinct-value count, not by bpp.
        """
        units = [encode_bitplane_unit(bw_pixel_states(px), bpp=2) for px in units_pixels]
        return encode_bitplane_units(units)

    # Decide by arithmetic, not by the flag alone. Metatile reuse scales with the size and
    # repetitiveness of a tileset: 1.29x across five full sheets, but only 1.04-1.12x on a
    # 64-tile hand-picked region, where the 9-byte definitions can outweigh the saving.
    #
    # `metatiles` accepts four things, because "auto" cannot know what a given game values:
    #
    #   true / false   force it on or off
    #   "auto"         use the default threshold
    #   0.0 .. 1.0     use THIS threshold -- 0.0 means any saving at all, 0.30 means only
    #                  take it when the saving is large
    #
    # A number is the useful form for an artist tuning one atlas: reuse is a property of how
    # the art was drawn, and a tileset with deliberate repetition may be worth metatiling at
    # a margin that a painterly one is not.
    want = atlas.get("metatiles")
    threshold = METATILE_MIN_SAVING
    if isinstance(want, bool):
        pass                                  # explicit; no arithmetic needed
    elif isinstance(want, (int, float)):
        threshold = float(want)
        if not 0.0 <= threshold <= 1.0:
            raise BuildError(
                f"atlas {atlas['name']!r}: metatiles = {want} is out of range. Give a "
                f"fraction from 0.0 (take any saving) to 1.0 (never), or true/false/\"auto\".")
        want = "auto"
    if want in (None, "auto"):
        bank, _defs = build_metatiles(tiles, assign, T, quiet=True)
        flat_size = len(tiles) * (T * T // 2)
        meta_size = len(bank) * (T * T // 8) + len(tiles) * 9
        saving = 1.0 - meta_size / flat_size

        # Metatiles are NOT free at runtime: measured on device they cost ~35% more
        # frame time (5,100 -> 6,900 us), because each tile row becomes two clipped
        # spans instead of one. So a saving has to be worth that, not merely positive.
        # 8.6% for 35% render was a bad trade; 14-19% on a real sheet is not, because
        # resources are the binding constraint here and frame time is not.
        want = saving >= threshold
        verdict = "chosen" if want else "skipped"
        print(f"    metatiles {verdict}: {meta_size:,} B vs {flat_size:,} B flat "
              f"({saving * 100:.0f}% saved, need {threshold * 100:.0f}% to "
              f"pay for ~35% more render time)")

    if want:
        bank, defs = build_metatiles(tiles, assign, T)

        # Each quadrant packs against the palette of the tile it came from, which the
        # (sub, palette) dedup key guarantees is consistent across every use.
        pal_of_sub = {}
        for tile_i, quad_ids in enumerate(defs):
            for qid in quad_ids:
                pal_of_sub[qid] = assign[tile_i]
        pixels = b"".join(pack_unit_4bpp(bank[i], palettes[pal_of_sub[i]])
                          for i in range(len(bank)))

        table = bytearray()
        for quad_ids in defs:
            for qid in quad_ids:
                table += qid.to_bytes(2, "little")

        if atlas_bitplane:
            pixel_flags = 2
            pixel_body = bitplane_encode(
                (bank[i], palettes[pal_of_sub[i]]) for i in range(len(bank)))
        elif atlas_huffman:
            pixel_flags = 4
            pixel_body = huffman_collect_or_encode(
                atlas["name"],
                (unpack_4bpp_to_indices(pack_unit_4bpp(bank[i], palettes[pal_of_sub[i]]),
                                        len(bank[i])) for i in range(len(bank))))
        else:
            pixel_flags, pixel_body = compress_pixel_body(pixels, pixel_compress, tag_len=True)
        body = (len(bank).to_bytes(2, "little") + b"\0\0"
                + pad4(bytes(assign)) + pad4(flags) + bytes(table) + pixel_body + shapes)
        atlas["blob"] = (blob_header(MAGIC_ATLAS, T, len(tiles), 1, pixel_flags, orient=orient)
                         + body)
        atlas["subtiles"] = len(bank)
        # A map's atlas pool slot has to hold the DECODED pixels regardless of whether
        # this blob is compressed on disk -- `resident_bytes` is what "blob" would have
        # been with `pixels` written raw, i.e. the size compression never shrinks below
        # at runtime. Equal to len(blob) whenever compress_atlases left this uncompressed.
        # bitplane-compressed atlases are never eagerly decoded into a pool, so their
        # resident_bytes is metadata-only.
        atlas["resident_bytes"] = (8 + 4 + len(pad4(bytes(assign))) + len(pad4(flags))
                                   + len(table) + (0 if (atlas_bitplane or atlas_huffman) else len(pixels))
                                   + len(shapes))

        if pack_2bit:
            # Same table, same assign, same flags, same shapes -- pack_unit_2bpp needs no
            # palette, so the quadrant's raw pixels are all that changes.
            if atlas_bitplane:
                flags_bw	   = 2
                body_bw_pixels = bitplane_encode_bw(bank[i] for i in range(len(bank)))
            elif atlas_huffman:
                flags_bw	   = 4
                body_bw_pixels = huffman_collect_or_encode_bw(
                    atlas["name"], (bw_pixel_states(bank[i]) for i in range(len(bank))))
            else:
                pixels_bw = b"".join(pack_unit_2bpp(bank[i]) for i in range(len(bank)))
                flags_bw, body_bw_pixels = compress_pixel_body(pixels_bw, pixel_compress,
                                                               tag_len=True)
            body_bw = (len(bank).to_bytes(2, "little") + b"\0\0"
                      + pad4(bytes(assign)) + pad4(flags) + bytes(table) + body_bw_pixels
                      + shapes)
            atlas["blob_bw"] = (blob_header(MAGIC_ATLAS, T, len(tiles), 1, flags_bw,
                                            orient=orient) + body_bw)
    else:
        pixels = b"".join(pack_unit_4bpp(t, palettes[a]) for t, a in zip(tiles, assign))
        if atlas_bitplane:
            pixel_flags = 2
            pixel_body = bitplane_encode((t, palettes[a]) for t, a in zip(tiles, assign))
        elif atlas_huffman:
            pixel_flags = 4
            pixel_body = huffman_collect_or_encode(
                atlas["name"],
                (unpack_4bpp_to_indices(pack_unit_4bpp(t, palettes[a]), len(t))
                 for t, a in zip(tiles, assign)))
        else:
            pixel_flags, pixel_body = compress_pixel_body(pixels, pixel_compress, tag_len=True)
        body = pad4(bytes(assign)) + pad4(flags) + pixel_body + shapes
        atlas["blob"] = (blob_header(MAGIC_ATLAS, T, len(tiles), 0, pixel_flags, orient=orient)
                         + body)
        atlas["subtiles"] = 0
        # See the metatiled branch above for why this is tracked separately from len(blob).
        atlas["resident_bytes"] = (8 + len(pad4(bytes(assign))) + len(pad4(flags))
                                   + (0 if (atlas_bitplane or atlas_huffman) else len(pixels))
                                   + len(shapes))

        if pack_2bit:
            if atlas_bitplane:
                flags_bw	   = 2
                body_bw_pixels = bitplane_encode_bw(tiles)
            elif atlas_huffman:
                flags_bw	   = 4
                body_bw_pixels = huffman_collect_or_encode_bw(
                    atlas["name"], (bw_pixel_states(t) for t in tiles))
            else:
                pixels_bw = b"".join(pack_unit_2bpp(t) for t in tiles)
                flags_bw, body_bw_pixels = compress_pixel_body(pixels_bw, pixel_compress,
                                                               tag_len=True)
            body_bw = pad4(bytes(assign)) + pad4(flags) + body_bw_pixels + shapes
            atlas["blob_bw"] = (blob_header(MAGIC_ATLAS, T, len(tiles), 0, flags_bw,
                                            orient=orient) + body_bw)
    atlas["palettes"] = palettes
    atlas["assign"] = assign
    atlas["tile_flags"] = flags # kept beside the blob, not just baked into it -- see
                                # "assign" above for the same reasoning (tests/editor
                                # read this instead of re-parsing bytes back out)
    atlas["scaled_rects"] = {i: extra for i, (mode, kind, extra) in collision.items()
                             if mode == COLLISION_SCALED}
    atlas["complex_tiles"] = sorted(i for i, (mode, kind, extra) in collision.items()
                                    if mode == COLLISION_COMPLEX)

    # A palette variant becomes an ORDERED palette per palette the atlas uses, whose k-th entry
    # is the recolour of the base's k-th entry. Ordered matters: an ordinary palette is a set that
    # palette_bytes sorts and pack_unit_4bpp indexes by the same sort, and two recolours sort
    # differently. Pinning position is what lets the variant share the base's pixel data, which is
    # the entire saving -- ~12,000 bytes of atlas for a handful of palette entries.
    atlas["variant_tables"] = {}
    for v in atlas.get("variants", []):
        table = bytearray()
        remap = {}
        for slot in assign:
            if slot in remap:
                continue
            base_order = sorted(shared[slot]) if not isinstance(shared[slot], Ordered) \
                         else list(shared[slot])
            recoloured = Ordered(v["map"].get(c, c) for c in base_order)
            for i, p in enumerate(shared):
                if isinstance(p, Ordered) and tuple(p) == tuple(recoloured):
                    remap[slot] = i
                    break
            else:
                shared.append(recoloured)
                remap[slot] = len(shared) - 1
        for slot in assign:
            table.append(remap[slot])
        atlas["variant_tables"][v["name"]] = bytes(table)
        print(f"    variant {v['name']}: {len(set(remap.values()))} palette(s), "
              f"{len(table)} B per map that uses it (against {len(tiles) * (T*T//2):,} B "
              f"for a second atlas)")

    shape_note = ""
    if scaled_count or complex_count:
        shape_note = (f", {scaled_count} scaled rect(s) + {complex_count} complex "
                     f"mask(s) ({len(shapes):,} B)")
    print(f"    {atlas['name']}: uses {len(set(assign))} palette(s), "
          f"{len(palettes) - before} new to the project{shape_note}")
    return atlas


# --------------------------------------------------------------------- palettes

def shape_signature(buf):
    """Colours numbered by first appearance, with transparent PINNED to 0.

    Pinning is not cosmetic. Left free, two frames could match where one's transparent pixels
    align with the other's opaque ones, and sharing a bitmap between them would render one
    full of holes.
    """
    m = {TRANSPARENT: 0}
    out = bytearray(len(buf))
    for i, v in enumerate(buf):
        if v not in m:
            m[v] = len(m)
        out[i] = m[v]
    return bytes(out)


def colour_order(frames):
    """Opaque colours in order of first appearance across frames.

    This is what makes a variant's palette positionally match the base's: identical shape
    signatures mean identical first-appearance traversal, so the k-th colour of one recolour
    corresponds to the k-th of another.
    """
    order, seen = [], set()
    for f in frames:
        for v in f:
            if v != TRANSPARENT and v not in seen:
                seen.add(v)
                order.append(v)
    return order


class Ordered(tuple):
    """A palette whose ENTRY ORDER is meaningful and must not be sorted or merged.

    Ordinary palettes are sets: `palette_bytes` sorts them and `pack_unit_4bpp` derives each
    colour's 4bpp index from that same sort, so the two agree. Palette-swapped sprites break
    that, because two recolours sort into different orders and would pack to different index
    data -- defeating the whole point of sharing one bitmap. An Ordered palette pins the
    correspondence instead: index k means entry k, whatever the colour values happen to be.
    """


def merge_palettes(colour_sets, existing=None, frozen=False):
    """Greedy merge of per-unit colour sets into palettes of PALETTE_USABLE colours.

    Merging rather than deduplicating is the whole trick. Deduplicating identical sets
    leaves 391 palettes across five real tilesets; merging any sets whose union still
    fits leaves 43 -- losslessly, because a tile is perfectly happy in a palette that
    merely contains its colours.

    `existing` lets a later atlas reuse or extend palettes an earlier one built, which is
    how sharing is discovered rather than declared.

    `frozen` forbids growing or adding a palette: a set may only be assigned to one that
    ALREADY contains it. That is what packing needs, and the distinction is not academic.
    A palette's entries are sorted by `palette_bytes` and `pack_unit_4bpp` derives each
    colour's 4bpp index from that same sort -- so growing a palette after something has
    been packed against it renumbers every entry, and every pixel already written now
    names a different colour. It does not fail; the atlas simply draws in a sprite's
    colours. See settle_palettes for the two-phase order that makes this safe.
    """
    # Ordered palettes pass through untouched. Merging one would reorder it, which silently
    # remaps every pixel of the sprite that depends on its positions.
    palettes = [p if isinstance(p, Ordered) else set(p) for p in (existing or [])]

    # Largest first: a big set placed early leaves room for small ones to join it, where
    # the reverse strands them.
    order = sorted(range(len(colour_sets)), key=lambda i: -len(colour_sets[i]))
    assign = {}

    for i in order:
        colours = colour_sets[i]
        if len(colours) > PALETTE_USABLE:
            assign[i] = None            # too wide for any palette; stored 6bpp
            continue
        for pi, p in enumerate(palettes):
            if isinstance(p, Ordered):
                continue
            if frozen:
                if colours <= p:
                    assign[i] = pi
                    break
            elif len(p | colours) <= PALETTE_USABLE:
                palettes[pi] = p | colours
                assign[i] = pi
                break
        else:
            if frozen:
                # settle_palettes ran over exactly these sets, so one must contain this
                # one. Reaching here means the two passes disagreed, which would corrupt
                # pixel data silently -- so it is an error rather than a new palette.
                raise BuildError("palette planning is inconsistent: a unit's colours fit "
                                 "no settled palette. This is a pipeline bug, not a "
                                 "content one.")
            palettes.append(set(colours))
            assign[i] = len(palettes) - 1

    return palettes, [assign[i] for i in range(len(colour_sets))]


def atlas_colour_sets(atlas):
    return [frozenset(c for c in t if c != TRANSPARENT) for t in atlas["tiles"]]


def sprite_colour_sets(sprite):
    return [frozenset(c for c in f if c != TRANSPARENT) for f in sprite["frames"]]


def settle_palettes(atlases, sprites, shared, nine_slices=()):
    """Grow `shared` to cover every atlas, sprite and nine_slice BEFORE any pixel data is
    packed.

    This exists because packing and merging cannot be interleaved. Packing an atlas fixes
    its 4bpp indices against the sort order of the palette it was assigned; a later sprite
    whose colours merge into that same palette changes the sort, and the atlas is now
    drawing someone else's colours. The symptom is a map rendered in the wrong palette --
    no error, no crash, and nothing in the build output suggesting where to look.

    Two passes make it impossible: this one settles every palette, then packing runs with
    `frozen=True` and cannot move anything under an asset already written.

    Sprites WITH variants are skipped: those build Ordered palettes and only ever append,
    and appending is safe -- it cannot renumber an existing entry. A nine_slice never has
    variants, so it always goes through the plain merge below.
    """
    for a in atlases:
        shared[:] = merge_palettes(atlas_colour_sets(a), shared)[0]
    for sp in sprites:
        if sp.get("variants"):
            continue
        shared[:] = merge_palettes(sprite_colour_sets(sp), shared)[0]
    for ns in nine_slices:
        shared[:] = merge_palettes(sprite_colour_sets(ns), shared)[0]


def gcolor_luminance(c):
    """0..30: the same R*3 + G*6 + B*1 weighting tile_stats already uses for `mean`, reused
    here rather than invented fresh so a project has one notion of "dark" throughout."""
    r, g, b = gcolor_rgb(c)
    return 3 * r + 6 * g + b


# Half of gcolor_luminance's 0..30 range -- the default split between ink and paper, used
# by every build unless a project overrides it. Named so the editor's live 1-bit preview
# (tools/pnx_editor.py's slider) and this default cannot silently drift apart.
DEFAULT_INK_THRESHOLD = 15


def palette_bytes(palette):
    """16 entries, index 0 transparent. Sets are sorted for deterministic builds; an
    Ordered palette keeps the order it was given, because its positions are already
    referenced by packed pixel data. Colour only -- a 1-bit platform never loads
    palettes.bin at all (PnxPalette's own comment, pnx_assets.h)."""
    entries = [TRANSPARENT_INDEX] + (list(palette) if isinstance(palette, Ordered)
                                     else sorted(palette))
    if len(entries) > PALETTE_ENTRIES:
        raise BuildError(f"palette has {len(entries)} entries, max {PALETTE_ENTRIES}")
    entries = entries + [0] * (PALETTE_ENTRIES - len(entries))
    return bytes(entries)


def pack_unit_4bpp(pixels, palette):
    """Two pixels per byte, high nibble first."""
    order = list(palette) if isinstance(palette, Ordered) else sorted(palette)
    lut = {c: i + 1 for i, c in enumerate(order)}
    lut[0] = TRANSPARENT_INDEX
    out = bytearray()
    for i in range(0, len(pixels), 2):
        out.append((lut[pixels[i]] << 4) | lut[pixels[i + 1]])
    return bytes(out)


# `~bw` escape hatch (docs/PORTING.md): 2 bits per pixel, MSB first, 4 pixels per byte --
# half the bytes of 4bpp indexed storage, against the quarter a bare ink/paper bit would
# cost and could not round-trip transparency through. States, keeping 0's meaning from the
# 4bpp path -- "the blitter never touches this pixel":
#
#   00 transparent   -- skipped, exactly like index 0 in the 4bpp path
#   01 paper         -- opaque, light  (luminance >= the ink threshold)
#   10 ink           -- opaque, dark   (luminance <  the ink threshold)
#   11 dither        -- a partially-transparent GColor8 alpha (the two middle levels M3's
#                        blend LUT exists for on colour, with no colour-only equivalent on
#                        a 1-bit screen) becomes a checkerboard of ink and paper instead of
#                        being rounded to one or the other -- the nearest thing to "grey"
#                        available at one bit per pixel. Decoded from (x + y) & 1 at blit
#                        time, not stored: the pattern is a function of screen position, so
#                        storing it per pixel would only be spending bytes to hold a
#                        constant.
#
# Not palette-indexed at all, unlike pack_unit_4bpp: ink/paper/dither is a property of the
# RAW GColor8 value's own alpha and luminance, not of which merged palette a tile landed in
# -- which is also why a ~bw variant can share the 4bpp variant's metatile index table and
# per-tile palette assignment untouched; only the pixel payload differs between the two.
def bw_pixel_state(c, threshold=DEFAULT_INK_THRESHOLD):
    """One raw GColor8 value's 2-bit ink state -- see pack_unit_2bpp's own comment for
    what each of the 4 values means. Pulled out to its own function because
    encode_bitplane_unit's BW path (finish_atlas/finish_sprite's `~bw` branches under
    `compress = "bitplane"`) needs the flat per-pixel state list BEFORE it is packed into
    bytes, the same way the colour bitplane path needs indices before pack_unit_4bpp."""
    if c == TRANSPARENT:
        return 0
    alpha = (c >> 6) & 3
    if alpha != 3:              # 0b01 or 0b10: a partially-transparent GColor8
        return 3
    return 2 if gcolor_luminance(c) < threshold else 1


def bw_pixel_states(pixels, threshold=DEFAULT_INK_THRESHOLD):
    return [bw_pixel_state(c, threshold) for c in pixels]


def pack_unit_2bpp(pixels, threshold=DEFAULT_INK_THRESHOLD):
    return pack_2bpp_raw_states(bw_pixel_states(pixels, threshold))


def pack_2bpp_raw_states(states):
    """4 states/byte, high bits first -- pack_unit_2bpp's own layout, but from already-
    computed 0-3 ink states rather than raw GColor8 values. encode_bitplane_unit's BW raw-
    fallback escape hatch uses this so it stays pixel-identical to what an uncompressed ~bw
    build would ship, same reasoning as pack_unit_4bpp_raw_indices for the colour path."""
    out = bytearray()
    for i in range(0, len(states), 4):
        byte = 0
        for k in range(4):
            j = i + k
            s = states[j] if j < len(states) else 0
            byte |= s << (6 - 2 * k)
        out.append(byte)
    return bytes(out)


def gcolor_rgb(c):
    return ((c >> 4) & 3, (c >> 2) & 3, c & 3)


def reduce_colours(pixels, limit=PALETTE_USABLE):
    """Merge the nearest colour pair until only `limit` distinct colours remain.

    A tile needing more than 15 colours is a content problem, not a format problem, so
    this is a repair with a loud report rather than a second bit depth. Measured on five
    real tilesets it fires on 10 tiles of 2,175 (0.5%); the two nearest colours in a
    two-bit-per-channel space differ by one step in one channel, against art already
    quantised from 16.7M colours.
    """
    colours = {c for c in pixels if c != TRANSPARENT}
    merged = 0
    remap = {}

    while len(colours) > limit:
        best, pair = None, None
        for a in colours:
            for b in colours:
                if a >= b:
                    continue
                ar, ag, ab = gcolor_rgb(a)
                br, bg, bb = gcolor_rgb(b)
                d = (ar-br)**2 + (ag-bg)**2 + (ab-bb)**2
                if best is None or d < best:
                    best, pair = d, (a, b)
        a, b = pair
        remap[a] = b
        colours.discard(a)
        merged += 1

    if not merged:
        return pixels, 0

    # Chase transitive remaps so a colour merged twice lands on its final value.
    def final(c):
        while c in remap:
            c = remap[c]
        return c

    return tuple(final(c) for c in pixels), merged


def pad4(data):
    return data + b"\0" * ((-len(data)) % 4)


def pack_collision_mask(buf, w, h):
    """A tile's or sprite frame's own opacity, as a 1bpp mask: 1 where it has ink, 0 where
    it is transparent. `buf` is a w*h GColor8 buffer (atlas["tiles"]'/a sprite frame's own
    format -- TRANSPARENT is the sentinel, same convention pack_atlas already carves tiles
    into). `w`/`h` are independent (not assumed square) so this serves both an atlas's
    always-square tiles and a sprite's not-necessarily-square frames alike.

    Row-major, MSB first -- the same bit order pack_unit_2bpp already uses, so "packed
    bits" means one thing in this file rather than two conventions that happen to coexist.
    """
    out = bytearray((w * h + 7) // 8)
    for i, v in enumerate(buf):
        if v != TRANSPARENT:
            out[i // 8] |= 0x80 >> (i % 8)
    return bytes(out)


def unpack_collision_mask(mask, w, h):
    """Inverse of pack_collision_mask: packed 1bpp bytes -> a list of h '#'/'.' row
    strings of w characters each. Not used by the build -- it never needs a mask back as
    text -- but the editor does, to show what a tile's/frame's mask currently looks like
    (authored or its own derived opacity) before someone repaints it.
    """
    rows = []
    for j in range(h):
        row = []
        for i in range(w):
            n = j * w + i
            row.append("#" if mask[n // 8] & (0x80 >> (n % 8)) else ".")
        rows.append("".join(row))
    return rows


def parse_collision_mask_ascii(text, w, h, where, idx):
    """A COMPLEX tile's/frame's AUTHORED mask: h rows of w characters, '#' ink / '.'
    empty -- the same ASCII-art convention a map's `rows` already uses, so this reads the
    same way at a glance and survives a diff the same way. Overrides pack_collision_mask's
    default (the art's own opacity) with a shape drawn on purpose: a curve that should
    collide narrower than its silhouette suggests, say, or collision deliberately simpler
    than the art.

    Built by converting the text to a synthetic GColor8 buffer and handing it to
    pack_collision_mask, rather than packing bits directly -- one bit-packing routine for
    the whole file, not two conventions that happen to agree today.
    """
    rows = text.strip("\n").split("\n")
    if len(rows) != h or any(len(r) != w for r in rows):
        raise BuildError(f"{where}: {idx}'s mask must be exactly {h} rows of {w} "
                         f"characters ('#' ink, '.' empty), got "
                         f"{len(rows)} row(s) of {[len(r) for r in rows]} chars")
    buf = bytearray(w * h)
    for j, row in enumerate(rows):
        for i, ch in enumerate(row):
            if ch not in "#.":
                raise BuildError(f"{where}: {idx}'s mask has {ch!r} "
                                 f"at {i},{j} -- only '#' (ink) and '.' (empty) are "
                                 f"allowed")
            buf[j * w + i] = OPAQUE if ch == "#" else TRANSPARENT
    return pack_collision_mask(buf, w, h)


# ------------------------------------------------------------------ semantic tiles

def tile_stats(buf, T):
    """Per-tile measures used to pick semantic tiles automatically."""
    opaque = [b for b in buf if b != TRANSPARENT]
    if not opaque:
        return None
    lum = [3 * ((b >> 4) & 3) + 6 * ((b >> 2) & 3) + (b & 3) for b in opaque]
    mean = sum(lum) / len(lum)
    var = sum((v - mean) ** 2 for v in lum) / len(lum)
    edges, n = 0, 0
    for j in range(T):
        for i in range(T - 1):
            a, b = buf[j * T + i], buf[j * T + i + 1]
            if a != TRANSPARENT and b != TRANSPARENT:
                edges += abs((3 * ((a >> 4) & 3)) - (3 * ((b >> 4) & 3)))
                n += 1
    return {"opaque_frac": len(opaque) / (T * T), "mean": mean,
            "std": var ** 0.5, "edge": edges / max(n, 1)}


def autopick_tiles(atlas, names):
    """Choose floor / wall / accent-like tiles from a packed set.

    "Looks like a wall" is not computable, so this picks for *legibility*: a flat tile
    for the first name, the most visually distant flat tile for the second, the busiest
    for the third. Intended for prototyping before there is an artist -- a manifest that
    names tiles explicitly is always better, and `semantic` overrides this.
    """
    tiles, T = atlas["tiles"], atlas["tile_px"]
    stats = [(i, tile_stats(b, T)) for i, b in enumerate(tiles)]
    solid = [(i, s) for i, s in stats if s and s["opaque_frac"] > 0.99]
    if not solid:
        solid = [(i, s) for i, s in stats if s]
    if not solid:
        raise BuildError(f"atlas {atlas['name']!r}: nothing opaque enough to autopick")

    picked = {}
    # Flat AND visible: penalise near-black, or the flattest tile in a dark cave tileset
    # is pure black and the whole map reads as an empty void.
    base = min(solid, key=lambda kv: kv[1]["std"] + max(0.0, 4.0 - kv[1]["mean"]) * 2.0)
    picked[names[0]] = base[0]

    if len(names) > 1:
        far = max(solid, key=lambda kv: abs(kv[1]["mean"] - base[1]["mean"])
                  - kv[1]["std"] * 0.1)
        picked[names[1]] = far[0]

    for extra in names[2:]:
        taken = set(picked.values())
        candidates = [kv for kv in solid if kv[0] not in taken]
        if not candidates:
            raise BuildError(f"atlas {atlas['name']!r}: too few distinct tiles to "
                             f"autopick {len(names)} roles")
        picked[extra] = max(candidates, key=lambda kv: kv[1]["edge"])[0]

    print(f"    autopicked: " + ", ".join(f"{k}={v}" for k, v in picked.items()))
    return picked


def parse_shape_collision(entries, resolve_index, count, shape_wh, where):
    """Shared by parse_atlas_collision and parse_sprite_collision -> {index: (mode, kind,
    extra)}. `resolve_index(entry)` turns one entry's own index key (a role name or int
    for an atlas tile, an int for a sprite frame -- the two differ only in how the index
    is NAMED, not in anything below) into a plain int; `shape_wh(index)` returns that
    index's own (w, h) for rect/mask bounds checking, which for an atlas is always
    (T, T) and for a sprite frame is that frame's own, since frames are not uniform size
    (see finish_sprite's own comment). `where` is a human-readable label ("atlas %r"/
    "sprite %r") for error messages.

    `extra` is None for SOLID, an (x, y, w, h) local rect for SCALED, and for COMPLEX
    either None (the mask defaults to the art's own ink, computed once pixels are settled
    in finish_atlas/finish_sprite -- not here, since that pixel data is not final yet) or
    already-packed mask bytes when the entry authors one explicitly (`mask`,
    parse_collision_mask_ascii) -- an override, not a description of the art, so it is
    parsed eagerly here rather than deferred.
    """
    out = {}
    for entry in entries:
        idx = resolve_index(entry)
        if not 0 <= idx < count:
            raise BuildError(f"{where}: collision names {idx}, out of range "
                             f"(0..{count - 1})")
        if idx in out:
            raise BuildError(f"{where}: {idx} has two collision entries")

        type_name = entry.get("type")
        if type_name not in COLLISION_NAMES:
            raise BuildError(f"{where}: collision type {type_name!r} for {idx}, must be "
                             f"one of {', '.join(sorted(COLLISION_NAMES))}")
        mode = COLLISION_NAMES[type_name]

        kind_name = entry.get("kind", "wall")
        if kind_name not in COLLISION_KIND_NAMES:
            raise BuildError(f"{where}: collision kind {kind_name!r} for {idx}, must be "
                             f"one of {', '.join(sorted(COLLISION_KIND_NAMES))}")
        kind = COLLISION_KIND_NAMES[kind_name]

        w, h = shape_wh(idx)
        extra = None
        if mode == COLLISION_SCALED:
            rect = entry.get("rect")
            if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                   or not all(isinstance(v, int) and not isinstance(v, bool) for v in rect)):
                raise BuildError(f"{where}: {idx} is scaled but `rect` is not four "
                                 f"integers [x, y, w, h]")
            rx, ry, rw, rh = rect
            if rw <= 0 or rh <= 0 or rx < 0 or ry < 0 or rx + rw > w or ry + rh > h:
                raise BuildError(f"{where}: {idx}'s rect {list(rect)} does not fit "
                                 f"inside its {w}x{h} bounds")
            extra = (rx, ry, rw, rh)
        elif "rect" in entry:
            raise BuildError(f"{where}: {idx} is {type_name}, not scaled -- `rect` only "
                             f"means something there")

        if mode == COLLISION_COMPLEX and "mask" in entry:
            mask_text = entry["mask"]
            if not isinstance(mask_text, str):
                raise BuildError(f"{where}: {idx}'s mask must be a string of rows, not "
                                 f"{mask_text!r}")
            extra = parse_collision_mask_ascii(mask_text, w, h, where, idx)
        elif "mask" in entry:
            raise BuildError(f"{where}: {idx} is {type_name}, not complex -- `mask` "
                             f"only means something there")

        out[idx] = (mode, kind, extra)
    return out


def parse_atlas_collision(spec, atlas, roles):
    """[[atlas.collision]] -> {local tile index: (COLLISION_*, COLLISION_KIND_*, extra)}.

    Collision is a property of the ART TILE (see COLLISION_NAMES' comment), so it is
    declared here, on the atlas, keyed the same way `semantic` roles already are -- not on
    the legend, and not per map cell.
    """
    T = atlas["tile_px"]

    def resolve(entry):
        tile = entry.get("tile")
        if isinstance(tile, str):
            if tile not in roles:
                raise BuildError(f"atlas {atlas['name']!r}: collision names role "
                                 f"{tile!r}, which this atlas does not define. It "
                                 f"provides: {', '.join(sorted(roles)) or '(none)'}")
            return roles[tile]
        if isinstance(tile, int) and not isinstance(tile, bool):
            return tile
        raise BuildError(f"atlas {atlas['name']!r}: collision entry `tile` must be a "
                         f"role name or a tile index, not {tile!r}")

    return parse_shape_collision(spec.get("collision", []), resolve, len(atlas["tiles"]),
                                 lambda i: (T, T), f"atlas {atlas['name']!r}")


def parse_atlas_anim(anim_spec, tile_count, name):
    """[atlas.anim] -> validated {name: value}. Same shape and validation as
    parse_sprite_anim (below) -- a clip normalises to {"frames": [...], "fps": int,
    "loop": bool, "durations": [...] or None} regardless of which TOML form authored it
    -- except "frames" here are ATLAS TILE INDICES, not sprite frame indices, and `tile
    count` is the atlas's own POST-DEDUP tile count (mirror/rotation-collapsed `fixed`,
    the same index space collision/roles/autopick already validate against), not the raw
    sheet grid -- an authored index has to name a tile that still exists after dedup.

    Deliberately the same generated-handle shape as [sprite.anim] (tools/pnx_assets.py's
    own generate_header: NAME_ANIM_FRAMES[]/_COUNT/_FPS/_LOOP/_DURATIONS) so a game
    resolves an animated tile id through the exact same pnx_anim_play/pnx_anim_frame
    (core/pnx_anim.h) a sprite clip uses -- one playback primitive, not a second one
    invented for tiles.
    """
    out = {}
    for anim_name, value in anim_spec.items():
        if isinstance(value, bool):
            raise BuildError(f"atlas {name!r}: anim {anim_name!r} must be a tile index, "
                             f"a list of tile indices, or a table, not a bool")
        if isinstance(value, int):
            if not 0 <= value < tile_count:
                raise BuildError(f"atlas {name!r}: anim {anim_name!r} points at tile "
                                 f"{value}, but there are only {tile_count}")
            out[anim_name] = value
            continue

        if isinstance(value, list):
            clip_frames, fps, loop, durations = value, ANIM_DEFAULT_FPS, True, None
        elif isinstance(value, dict):
            unknown = set(value) - {"frames", "fps", "loop", "durations"}
            if unknown:
                raise BuildError(f"atlas {name!r}: anim {anim_name!r} has unknown "
                                 f"key(s) {sorted(unknown)}")
            if "frames" not in value:
                raise BuildError(f"atlas {name!r}: anim {anim_name!r} needs `frames`")
            clip_frames = value["frames"]
            fps = value.get("fps", ANIM_DEFAULT_FPS)
            loop = value.get("loop", True)
            durations = value.get("durations")
        else:
            raise BuildError(f"atlas {name!r}: anim {anim_name!r} must be a tile index, "
                             f"a list of tile indices, or a table, not {value!r}")

        if not isinstance(clip_frames, list) or not clip_frames or len(clip_frames) > 255:
            raise BuildError(f"atlas {name!r}: anim {anim_name!r} needs 1..255 tile "
                             f"indices, got {clip_frames!r}")
        for fi in clip_frames:
            if not isinstance(fi, int) or isinstance(fi, bool) or not 0 <= fi < tile_count:
                raise BuildError(f"atlas {name!r}: anim {anim_name!r} names tile "
                                 f"{fi!r}, but there are only {tile_count}")
        if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 255:
            raise BuildError(f"atlas {name!r}: anim {anim_name!r} fps must be an "
                             f"integer 1..255, not {fps!r}")
        if not isinstance(loop, bool):
            raise BuildError(f"atlas {name!r}: anim {anim_name!r} loop must be "
                             f"true/false, not {loop!r}")
        if durations is not None:
            if not isinstance(durations, list) or len(durations) != len(clip_frames):
                raise BuildError(f"atlas {name!r}: anim {anim_name!r} durations must "
                                 f"be a list of exactly {len(clip_frames)} integers, "
                                 f"one per frame")
            for d in durations:
                if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 255:
                    raise BuildError(f"atlas {name!r}: anim {anim_name!r} duration "
                                     f"{d!r} must be an integer 1..255")

        out[anim_name] = {"frames": list(clip_frames), "fps": fps, "loop": loop,
                          "durations": list(durations) if durations is not None else None}
    return out


# --------------------------------------------------------------------------- sprite

def parse_sprite_anim(anim_spec, frame_count, name):
    """[sprite.anim] -> validated {name: value}. TOML already distinguishes the three
    forms an entry can take, so there is no parsing here, only validating and defaulting:

        stand = 0                                    a single pose (int), unchanged
        walk = [1, 2, 1, 0]                          a clip (list): default fps/loop
        attack = { frames = [4, 5, 6], fps = 12,      a clip (table): explicit fps/loop/
                   loop = false, durations = [1,2,1] }  durations

    A clip value normalises to {"frames": [...], "fps": int, "loop": bool, "durations":
    [...] or None} regardless of which of the two clip forms it came from -- codegen
    branches on whether a name's value is `int` (pose) or `dict` (clip), never on which
    TOML form authored it.
    """
    out = {}
    for anim_name, value in anim_spec.items():
        if isinstance(value, bool):
            raise BuildError(f"sprite {name!r}: anim {anim_name!r} must be a frame "
                             f"index, a list of frame indices, or a table, not a bool")
        if isinstance(value, int):
            if not 0 <= value < frame_count:
                raise BuildError(f"sprite {name!r}: anim {anim_name!r} points at frame "
                                 f"{value}, but there are only {frame_count}")
            out[anim_name] = value
            continue

        if isinstance(value, list):
            clip_frames, fps, loop, durations = value, ANIM_DEFAULT_FPS, True, None
        elif isinstance(value, dict):
            unknown = set(value) - {"frames", "fps", "loop", "durations"}
            if unknown:
                raise BuildError(f"sprite {name!r}: anim {anim_name!r} has unknown "
                                 f"key(s) {sorted(unknown)}")
            if "frames" not in value:
                raise BuildError(f"sprite {name!r}: anim {anim_name!r} needs `frames`")
            clip_frames = value["frames"]
            fps = value.get("fps", ANIM_DEFAULT_FPS)
            loop = value.get("loop", True)
            durations = value.get("durations")
        else:
            raise BuildError(f"sprite {name!r}: anim {anim_name!r} must be a frame "
                             f"index, a list of frame indices, or a table, not "
                             f"{value!r}")

        if not isinstance(clip_frames, list) or not clip_frames or len(clip_frames) > 255:
            raise BuildError(f"sprite {name!r}: anim {anim_name!r} needs 1..255 frame "
                             f"indices, got {clip_frames!r}")
        for fi in clip_frames:
            if not isinstance(fi, int) or isinstance(fi, bool) or not 0 <= fi < frame_count:
                raise BuildError(f"sprite {name!r}: anim {anim_name!r} names frame "
                                 f"{fi!r}, but there are only {frame_count}")
        if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 255:
            raise BuildError(f"sprite {name!r}: anim {anim_name!r} fps must be an "
                             f"integer 1..255, not {fps!r}")
        if not isinstance(loop, bool):
            raise BuildError(f"sprite {name!r}: anim {anim_name!r} loop must be "
                             f"true/false, not {loop!r}")
        if durations is not None:
            if not isinstance(durations, list) or len(durations) != len(clip_frames):
                raise BuildError(f"sprite {name!r}: anim {anim_name!r} durations must "
                                 f"be a list of exactly {len(clip_frames)} integers, "
                                 f"one per frame")
            for d in durations:
                if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 255:
                    raise BuildError(f"sprite {name!r}: anim {anim_name!r} duration "
                                     f"{d!r} must be an integer 1..255")

        out[anim_name] = {"frames": list(clip_frames), "fps": fps, "loop": loop,
                          "durations": list(durations) if durations is not None else None}
    return out


def pack_sprite(root, spec, orient=ORIENT_BUTTONS_RIGHT):
    """Frames, their origins and their collision are all validated and parsed in the
    author's frame, then rotated into the framebuffer's together with the pixels -- so
    the numbers in an error message are the numbers in the manifest, and only this
    function's own return turns over.

    A tightly packed sheet's frames are not one uniform size (unlike an atlas's tiles):
    each `frames` entry stands on its own, `[x, y, w, h]` or `[x, y, w, h, ox, oy]` --
    the trailing pair is this frame's own ROOT (pnx_sprite_draw's anchor point),
    defaulting to `w/2, h` (centred, feet at the bottom) when omitted, which reproduces
    the old fixed-anchor behaviour for any sprite that does not need per-frame control.
    """
    name = spec["name"]
    im = load_sheet(root, spec["sheet"])
    px = im.load()
    key = parse_colorkey(spec, f"sprite {name!r}")
    sheet_w, sheet_h = im.size

    frames, dims, origins = [], [], []
    for idx, entry in enumerate(spec["frames"]):
        if len(entry) == 4:
            x, y, w, h = entry
            ox, oy = w // 2, h
        elif len(entry) == 6:
            x, y, w, h, ox, oy = entry
        else:
            raise BuildError(f"sprite {name!r}: frame {idx} must be [x, y, w, h] or "
                             f"[x, y, w, h, ox, oy] (with an explicit origin), got "
                             f"{len(entry)} values")
        if not (0 <= ox <= w and 0 <= oy <= h):
            raise BuildError(f"sprite {name!r}: frame {idx}'s origin {ox},{oy} must "
                             f"fall within its own {w}x{h} bounds")
        if x + w > sheet_w or y + h > sheet_h:
            raise BuildError(f"sprite {name!r}: frame {idx} at {x},{y} {w}x{h} runs "
                             f"past the sheet ({sheet_w}x{sheet_h})")
        # A frame's PIXEL COUNT has to be even, not its width: 4bpp packing is a flat
        # stream, two pixels to a byte, with no per-row padding. Rotation swaps the
        # dimensions and leaves the product alone, so this holds in either orientation --
        # a 15x20 frame is legal portrait and stays legal at 20x15. Checked per-frame,
        # not once for the whole sprite: frames are not uniform size any more.
        if (w * h) % 2:
            raise BuildError(f"sprite {name!r}: frame {idx} is {w}x{h}, an odd pixel "
                             f"count, which cannot pack two-per-byte at 4bpp")
        buf = bytearray(w * h)
        for j in range(h):
            for i in range(w):
                buf[j * w + i] = to_gcolor8(px[x + i, y + j], key)
        frames.append(bytes(buf))
        dims.append((w, h))
        origins.append((ox, oy))

    anim = parse_sprite_anim(spec.get("anim", {}), len(frames), name)

    def resolve_frame(entry):
        frame = entry.get("frame")
        if not isinstance(frame, int) or isinstance(frame, bool):
            raise BuildError(f"sprite {name!r}: collision entry `frame` must be an "
                             f"integer, not {frame!r}")
        return frame

    collision = parse_shape_collision(spec.get("collision", []), resolve_frame,
                                      len(frames), lambda i: dims[i], f"sprite {name!r}")

    # Rotate pixels, origin and any AUTHORED collision shape together -- see
    # rotate_origin's/rotate_rect's own comments for why an origin/rect needs its own
    # (boundary, not pixel-index) transform. An auto-derived COMPLEX mask (extra is None)
    # needs no rotation here: it is computed later, in build_sprite_shapes, straight from
    # the already-rotated pixels finish_sprite hands it.
    rotated_frames, rotated_dims = [], []
    for buf, (w, h) in zip(frames, dims):
        rbuf, nw, nh = rotate_grid(buf, w, h, orient)
        rotated_frames.append(rbuf)
        rotated_dims.append((nw, nh))
    rotated_origins = [rotate_origin(ox, oy, w, h, orient)
                       for (ox, oy), (w, h) in zip(origins, dims)]

    for i, (mode, kind, extra) in list(collision.items()):
        w, h = dims[i]
        if mode == COLLISION_SCALED:
            rx, ry, rw, rh = extra
            collision[i] = (mode, kind, rotate_rect(rx, ry, rw, rh, w, h, orient))
        elif mode == COLLISION_COMPLEX and extra is not None:
            unpacked = bytearray(w * h)
            for j, row in enumerate(unpack_collision_mask(extra, w, h)):
                for c, ch in enumerate(row):
                    unpacked[j * w + c] = OPAQUE if ch == "#" else TRANSPARENT
            rbuf, nw, nh = rotate_grid(bytes(unpacked), w, h, orient)
            collision[i] = (mode, kind, pack_collision_mask(rbuf, nw, nh))

    frames = rotated_frames
    repaired = 0
    fixed = []
    for f in frames:
        f2, merged = reduce_colours(f)
        if merged:
            repaired += 1
        fixed.append(f2)

    sizes = sorted(set(rotated_dims))
    size_note = f"{sizes[0][0]}x{sizes[0][1]}" if len(sizes) == 1 else \
        f"{len(sizes)} distinct sizes"
    print(f"  sprite {name}: {len(fixed)} frames, {size_note}")
    if repaired:
        print(f"    NOTE {repaired} frame(s) exceeded {PALETTE_USABLE} colours and were "
              f"reduced -- edit the art to avoid this")

    # Palette-swapped variants. The same art in different colours costs a palette rather
    # than a second copy of every frame, which is the whole reason to declare them.
    variants = []
    for vpath in spec.get("variants", []):
        vname = os.path.splitext(os.path.basename(vpath))[0]
        vim = load_sheet(root, vpath)
        vpx = vim.load()
        if vim.size != im.size:
            raise BuildError(f"sprite {name!r}: variant {vpath!r} is {vim.size[0]}x"
                             f"{vim.size[1]} but the base sheet is {sheet_w}x{sheet_h} -- "
                             f"variants must share the base's layout exactly")
        vframes = []
        for entry in spec["frames"]:
            x, y, w, h = entry[:4]
            buf = bytearray(w * h)
            for j in range(h):
                for i in range(w):
                    buf[j * w + i] = to_gcolor8(vpx[x + i, y + j], key)
            vframes.append(reduce_colours(rotate_grid(buf, w, h, orient)[0])[0])

        # The check that makes sharing safe: same shape, any colours.
        for idx, (base_f, var_f) in enumerate(zip(fixed, vframes)):
            if shape_signature(base_f) != shape_signature(var_f):
                raise BuildError(
                    f"sprite {name!r}: variant {vpath!r} frame {idx} is not a recolour of "
                    f"the base -- its pixel layout differs. A variant may change any colour "
                    f"but must not move, add or remove a pixel, and transparency must match. "
                    f"Drop it from `variants` and declare it as its own sprite instead.")
        variants.append({"name": vname, "path": vpath, "frames": vframes})

    # Which source is canonical for this sprite's ~bw bake (finish_sprite_with_variants):
    # a colour recolour has nothing to distinguish on a screen with no colour, so BW does
    # not attempt to preserve "variant selection" at all -- there is exactly one 1-bit
    # rendering of a sprite, chosen here rather than guessed. Unset means the base.
    bw_variant = spec.get("bw_variant")
    if bw_variant is not None and bw_variant not in {v["name"] for v in variants}:
        raise BuildError(
            f"sprite {name!r}: bw_variant = {bw_variant!r} is not one of its variants "
            f"({', '.join(v['name'] for v in variants) or '(none declared)'})")

    return {"name": name, "frames": fixed, "dims": rotated_dims, "origins": rotated_origins,
            "variants": variants, "bw_variant": bw_variant, "out": spec["out"],
            "anim": anim, "collision": collision, "repaired": repaired}


def build_sprite_frame_meta(dims, origins, collision, frame_packed):
    """The frame_meta table PnxSprite.frame_meta loads (PNX_SPRITE_FRAME_BYTES=8 per
    frame: u16 offset, u8 w, u8 h, u8 origin_x, u8 origin_y, u8 flags, u8 pad) -- the same
    role pack_font's glyph-offset loop plays for PnxGlyph, including its dedup: two
    frames whose PACKED bytes are byte-identical (same w/h and the same palette-relative
    indices -- palette-relative because pack_unit_4bpp/2bpp already baked the palette
    assignment in by the time this runs) share one `offset` instead of a second copy.
    `frame_packed` is the caller's own per-frame packed pixel bytes, which differ between
    the 4bpp blob and the 2bpp ~bw one (see PnxSprite's own comment), so this is called
    once per encoding. Returns (meta_bytes, deduped_pixels_bytes).
    """
    meta = bytearray()
    pixels = bytearray()
    dedup = {}
    for i, ((w, h), (ox, oy)) in enumerate(zip(dims, origins)):
        mode, kind, extra = collision.get(i, (COLLISION_NONE, COLLISION_KIND_WALL, None))
        flags = (kind << 2) | mode
        packed = frame_packed[i]
        key = (w, h, packed)
        if key in dedup:
            offset = dedup[key]
        else:
            offset = len(pixels)
            dedup[key] = offset
            pixels += packed
        meta += offset.to_bytes(2, "little") + bytes([w, h, ox, oy, flags, 0])
    return bytes(meta), bytes(pixels)


def compress_sprite_pixels_bitplane(frame_meta, dims, pixels):
    """The bitplane-compressed alternative to compress_pixel_body's LZSS form, for a
    sprite's pixel region specifically -- flags bit 1 (LZSS already owns bit 0), body is
    a per-DEDUPED-UNIT offset table + concatenated encode_bitplane_unit() blobs, one per
    distinct frame. `frame_meta`/`pixels` are exactly what build_sprite_frame_meta
    already produced (unmodified) -- its offsets are logical/post-decode, assigned in
    first-seen order, so walking the DISTINCT offsets in ascending order recovers the
    same deduped unit list a second dedup pass would, without one. This is deliberate,
    not incidental: pnx_sprite_load's own bitplane path does the identical walk over the
    frame_meta it already loaded, rather than this function stashing a redundant unit
    count/index anywhere -- one dedup computation, arrived at twice, from the one table
    that already has to exist either way.

    Never returned if it would not actually be smaller than shipping `pixels` unchanged
    -- same guarantee compress_pixel_body gives LZSS.
    """
    seen = {}
    for i, (w, h) in enumerate(dims):
        off = int.from_bytes(frame_meta[i * 8:i * 8 + 2], "little")
        if off not in seen:
            seen[off] = (w, h)

    units = []
    for off in sorted(seen):
        w, h = seen[off]
        n = w * h
        length = n // 2
        indices = unpack_4bpp_to_indices(pixels[off:off + length], n)
        units.append(encode_bitplane_unit(indices))

    body = encode_bitplane_units(units)

    if len(body) >= len(pixels):
        return 0, pixels
    # flag 2 means bitplane compression (M13)
    return 2, body


def compress_sprite_pixels_huffman(frame_meta, dims, pixels, sprite_name):
    """compress_sprite_pixels_bitplane's global-table-Huffman sibling -- same per-
    DEDUPED-UNIT walk over frame_meta's own offsets, routed through
    huffman_collect_or_encode instead of encode_bitplane_unit so this sprite's units
    contribute to (collect pass) or draw from (final pass) the one project-wide table.
    """
    seen = {}
    for i, (w, h) in enumerate(dims):
        off = int.from_bytes(frame_meta[i * 8:i * 8 + 2], "little")
        if off not in seen:
            seen[off] = (w, h)

    indices_list = []
    for off in sorted(seen):
        w, h = seen[off]
        n = w * h
        length = n // 2
        indices_list.append(unpack_4bpp_to_indices(pixels[off:off + length], n))

    body = huffman_collect_or_encode(sprite_name, indices_list)

    if _huffman_phase == "collect" or len(body) >= len(pixels):
        return 0, pixels
    # flag 4 means global-table Huffman (see finish_atlas's own atlas_huffman branches)
    return 4, body


def compress_sprite_pixels_bitplane_bw(frame_meta, dims, pixels):
    """compress_sprite_pixels_bitplane's `~bw` counterpart -- same per-deduped-unit walk
    over `frame_meta`'s own offsets, but against a 2bpp-packed (pack_unit_2bpp) `pixels`
    buffer and ink-state (bw_pixel_states) units rather than palette indices. `frame_meta`
    here is the ~bw call's OWN build_sprite_frame_meta output, whose offsets are already
    byte offsets into THIS `pixels` buffer's own 2bpp packing -- not the colour blob's.
    """
    seen = {}
    for i, (w, h) in enumerate(dims):
        off = int.from_bytes(frame_meta[i * 8:i * 8 + 2], "little")
        if off not in seen:
            seen[off] = (w, h)

    units = []
    for off in sorted(seen):
        w, h = seen[off]
        n = w * h
        length = (n + 3) // 4
        states = unpack_2bpp_to_states(pixels[off:off + length], n)
        units.append(encode_bitplane_unit(states, bpp=2))

    body = encode_bitplane_units(units)

    if len(body) >= len(pixels):
        return 0, pixels
    return 2, body


def compress_sprite_pixels_huffman_bw(frame_meta, dims, pixels, sprite_name):
    """compress_sprite_pixels_huffman's `~bw` counterpart, mirroring
    compress_sprite_pixels_bitplane_bw's own relationship to compress_sprite_pixels_bitplane
    -- same per-deduped-unit walk over frame_meta's own offsets, against a 2bpp-packed
    `pixels` buffer and ink-state units, routed through huffman_collect_or_encode_bw so
    this sprite's ~bw units contribute to (collect pass) or draw from (final pass) the
    project's OWN bw table (huffman_finalize_table_bw), separate from the colour one.
    """
    seen = {}
    for i, (w, h) in enumerate(dims):
        off = int.from_bytes(frame_meta[i * 8:i * 8 + 2], "little")
        if off not in seen:
            seen[off] = (w, h)

    indices_list = []
    for off in sorted(seen):
        w, h = seen[off]
        n = w * h
        length = (n + 3) // 4
        indices_list.append(unpack_2bpp_to_states(pixels[off:off + length], n))

    body = huffman_collect_or_encode_bw(sprite_name, indices_list)

    if _huffman_phase == "collect" or len(body) >= len(pixels):
        return 0, pixels
    return 4, body


def compress_pixel_body(pixels, compress, tag_len=False):
    """Returns (flags, body) for a sprite or atlas blob's pixel region -- shared by
    `compress_sprites` and `compress_atlases`, the counterpart to compress_maps' bank-body
    compression (see the bank-writing loop below), simpler because both sprites and
    atlases load whole rather than banked, so there is one stream, not one per bank.
    `flags` bit 0 set means `body` is a 2-byte COMPRESSED length followed by the LZSS
    stream of that length (pnx_sprite_load's and atlas_load_into's own comments document
    the convention); otherwise `body` is `pixels` unchanged. The length has to be the
    compressed byte count, not the uncompressed one: the uncompressed size is already
    derivable from frame_meta/tile_count (that is what sizes the decode buffer), but
    nothing else says where the stream ENDS and the shape tables begin -- pnx_lzss_decode
    reports bytes written, not bytes consumed, so the loader has no other way to find
    that boundary. Never emits the compressed form if it would not actually be smaller --
    a project turning compression on should never pay a decode cost for content too small
    to benefit.

    `tag_len` -- atlas call sites only (finish_atlas passes True; sprites don't) -- also
    inserts a 2-byte TRUE uncompressed pixel length ahead of the stream, between the
    compressed length and the stream itself. An atlas is the one place two on-disk pixel
    depths of the SAME content coexist (`pack_2bit`'s colour blob and ~bw blob), and
    "derivable from tile_count" above stops being true across that split: tile_count is
    bit-depth independent, so a colour blob loaded by a PNX_DISPLAY_BW build's reader (or
    the reverse) still looks self-consistent to it -- same tile_count, same tables -- and
    decodes "successfully" into half the real pixel data rather than being refused. This
    field is what atlas_load_into checks to catch that; see its own comment.
    """
    if not compress:
        return 0, pixels
    packed = lzss_compress(pixels)
    tag = len(pixels).to_bytes(2, "little") if tag_len else b""
    if len(packed) + 2 + len(tag) >= len(pixels):
        return 0, pixels
    return 1, len(packed).to_bytes(2, "little") + tag + packed


def build_sprite_shapes(collision, dims, fixed):
    """SCALED rects / COMPLEX masks, sparse, keyed by FRAME -- identical shape to
    finish_atlas's own tail tables, keyed by tile there. A COMPLEX record's mask_bytes is
    computed from THAT frame's own (w, h), not one shared tile_px: frames are not uniform
    size. A COMPLEX frame with no explicit mask defaults to its own opacity, the same
    `pack_collision_mask` finish_atlas already falls back to -- computed here, against
    `fixed[i]`, because this is the first point a frame's pixels are fully settled
    (rotated, colour-reduced).
    """
    scaled = bytearray()
    scaled_count = 0
    complex_masks = bytearray()
    complex_count = 0
    for i, (mode, kind, extra) in sorted(collision.items()):
        w, h = dims[i]
        if mode == COLLISION_SCALED:
            rx, ry, rw, rh = extra
            scaled += i.to_bytes(2, "little") + bytes([rx, ry, rw, rh])
            scaled_count += 1
        elif mode == COLLISION_COMPLEX:
            mask = extra if extra is not None else pack_collision_mask(fixed[i], w, h)
            complex_masks += i.to_bytes(2, "little") + mask
            complex_count += 1
    return (scaled_count.to_bytes(2, "little") + bytes(scaled)
           + complex_count.to_bytes(2, "little") + bytes(complex_masks))


def finish_sprite_with_variants(sprite, shared, orient=ORIENT_BUTTONS_RIGHT, pack_2bit=False,
                                compress_sprites=False):
    """One bitmap, one palette per variant, on colour. On a 1-bit build (`pack_2bit`) this
    still emits exactly one ~bw blob -- see the comment above `sprite["blob_bw"]` below for
    why colour's variant-sharing trick has no BW equivalent and what replaces it.

    Every frame packs against a single ORDERED palette rather than per-frame merged ones. It
    has to be one palette: the pixel data is shared across variants, so the index of a colour
    must mean the same thing in every frame and every recolour. And it has to be ordered,
    because two recolours sort into different colour orders and would otherwise pack to
    different indices -- which would defeat the sharing entirely.
    """
    name = sprite["name"]
    frames = sprite["frames"]
    dims = sprite["dims"]
    origins = sprite["origins"]
    collision = sprite["collision"]
    base_order = colour_order(frames)

    if len(base_order) > PALETTE_USABLE:
        raise BuildError(
            f"sprite {name!r}: its frames use {len(base_order)} colours together, over the "
            f"{PALETTE_USABLE} a palette holds. A sprite with variants shares one palette "
            f"across all frames, because the frames share one bitmap.")

    def slot(pal):
        """Append an ordered palette, reusing an identical one already present."""
        for i, p in enumerate(shared):
            if isinstance(p, Ordered) and tuple(p) == tuple(pal):
                return i
        shared.append(Ordered(pal))
        return len(shared) - 1

    base_slot = slot(base_order)
    frame_packed = [pack_unit_4bpp(f, shared[base_slot]) for f in frames]
    frame_meta, pixels = build_sprite_frame_meta(dims, origins, collision, frame_packed)
    # Neither `compress = "bitplane"` nor `"huffman"` extends to a variant sprite: the
    # whole point of variants is one shared bitmap redrawn per palette, and both formats'
    # per-unit ROM fetch has nothing to key units by beyond frame_meta's own dedup --
    # which a variant sprite already uses for something else (recolour sharing, not
    # decode-on-demand). Falls back to LZSS (still real compression, just not
    # decode-on-demand) rather than silently doing nothing.
    if compress_sprites in ("bitplane", "huffman"):
        print(f"    {name}: has variants, compress = {compress_sprites!r} falls back to "
              f"LZSS for this sprite's pixel data (needs a single deduped unit table, "
              f"which variant recolouring already spends on something else)")
    per_unit_mode = compress_sprites in ("bitplane", "huffman")
    flags, pixel_body = compress_pixel_body(pixels, "lzss" if per_unit_mode
                                            else compress_sprites)
    assign = [base_slot] * len(frames)
    shapes = build_sprite_shapes(collision, dims, frames)

    variant_slots = {}
    for v in sprite["variants"]:
        order = colour_order(v["frames"])
        if len(order) != len(base_order):
            raise BuildError(
                f"sprite {name!r}: variant {v['path']!r} resolves to {len(order)} colours "
                f"against the base's {len(base_order)}. Shapes match, so this means two "
                f"colours in the base were flattened to one in the variant -- which cannot "
                f"share a bitmap. Recolour without merging colours.")
        variant_slots[v["name"]] = slot(order)

    sprite["blob"] = (blob_header(MAGIC_SPRITE, len(frames), flags, orient=orient)
                      + frame_meta + pad4(bytes(assign)) + pixel_body + shapes)
    sprite["palettes"] = [shared[base_slot]]
    sprite["assign"] = assign
    sprite["variant_slots"] = variant_slots

    if pack_2bit:
        # A 1-bit screen has nothing to distinguish a recolour by, so BW does not try to
        # preserve variant selection the way colour does -- there is exactly one 1-bit
        # rendering of this sprite, baked from whichever source `bw_variant` names (the
        # base, if unset). `assign` is carried along unchanged only to keep the blob's
        # shape identical to the plain (no-variants) ~bw sprite -- pnx_sprite_load expects
        # an assign array of this length regardless of format, even though nothing reads
        # its values on a 1-bit build (PnxSprite's own comment, pnx_assets.h).
        bw_frames = frames
        if sprite.get("bw_variant"):
            bw_frames = next(v["frames"] for v in sprite["variants"]
                             if v["name"] == sprite["bw_variant"])
        frame_packed_bw = [pack_unit_2bpp(f) for f in bw_frames]
        frame_meta_bw, pixels_bw = build_sprite_frame_meta(dims, origins, collision,
                                                            frame_packed_bw)
        # Unlike the colour blob above, the `~bw` blob is never a shared-bitmap-across-
        # variants thing -- bw_frames is one flat rendering (a 1-bit screen has no runtime
        # recolour to preserve, see pack_2bit's own comment), so it dedupes and bitplane-
        # encodes exactly like a plain non-variant sprite's bw blob does.
        if compress_sprites == "bitplane":
            flags_bw, pixel_body_bw = compress_sprite_pixels_bitplane_bw(
                frame_meta_bw, dims, pixels_bw)
        elif compress_sprites == "huffman":
            flags_bw, pixel_body_bw = compress_sprite_pixels_huffman_bw(
                frame_meta_bw, dims, pixels_bw, sprite["name"])
        else:
            flags_bw, pixel_body_bw = compress_pixel_body(pixels_bw, compress_sprites)
        sprite["blob_bw"] = (blob_header(MAGIC_SPRITE, len(frames), flags_bw, orient=orient)
                             + frame_meta_bw + pad4(bytes(assign)) + pixel_body_bw + shapes)

    frame_bytes = sum(w * h // 2 for (w, h) in dims)
    saved = frame_bytes * len(sprite["variants"])
    print(f"    {name}: 1 shared palette, {len(sprite['variants'])} variant(s) collapsed "
          f"({saved:,} B saved, {len(variant_slots) * PALETTE_BYTES} B of palettes)")
    return sprite


def finish_sprite(sprite, shared, orient=ORIENT_BUTTONS_RIGHT, pack_2bit=False,
                  compress_sprites=False):
    frames = sprite["frames"]
    dims = sprite["dims"]
    origins = sprite["origins"]
    collision = sprite["collision"]

    if sprite.get("variants"):
        return finish_sprite_with_variants(sprite, shared, orient, pack_2bit, compress_sprites)

    sets = sprite_colour_sets(sprite)

    before = len(shared)
    palettes, assign = merge_palettes(sets, shared, frozen=True)
    shared[:] = palettes

    if any(a is None for a in assign):
        raise BuildError(f"sprite {sprite['name']!r}: a frame still exceeds "
                         f"{PALETTE_USABLE} colours after reduction")

    frame_packed = [pack_unit_4bpp(f, palettes[a]) for f, a in zip(frames, assign)]
    frame_meta, pixels = build_sprite_frame_meta(dims, origins, collision, frame_packed)
    if compress_sprites == "bitplane":
        flags, pixel_body = compress_sprite_pixels_bitplane(frame_meta, dims, pixels)
    elif compress_sprites == "huffman":
        flags, pixel_body = compress_sprite_pixels_huffman(frame_meta, dims, pixels,
                                                            sprite["name"])
    else:
        flags, pixel_body = compress_pixel_body(pixels, compress_sprites)
    shapes = build_sprite_shapes(collision, dims, frames)
    body = frame_meta + pad4(bytes(assign)) + pixel_body + shapes

    sprite["blob"] = blob_header(MAGIC_SPRITE, len(frames), flags, orient=orient) + body
    sprite["palettes"] = palettes
    sprite["assign"] = assign

    if pack_2bit:
        frame_packed_bw = [pack_unit_2bpp(f) for f in frames]
        frame_meta_bw, pixels_bw = build_sprite_frame_meta(dims, origins, collision,
                                                            frame_packed_bw)
        if compress_sprites == "bitplane":
            flags_bw, pixel_body_bw = compress_sprite_pixels_bitplane_bw(
                frame_meta_bw, dims, pixels_bw)
        elif compress_sprites == "huffman":
            flags_bw, pixel_body_bw = compress_sprite_pixels_huffman_bw(
                frame_meta_bw, dims, pixels_bw, sprite["name"])
        else:
            flags_bw, pixel_body_bw = compress_pixel_body(pixels_bw, compress_sprites)
        sprite["blob_bw"] = (blob_header(MAGIC_SPRITE, len(frames), flags_bw, orient=orient)
                             + frame_meta_bw + pad4(bytes(assign)) + pixel_body_bw + shapes)

    print(f"    {sprite['name']}: uses {len(set(assign))} palette(s), "
          f"{len(palettes) - before} new to the project")
    return sprite


# ---------------------------------------------------------------------- nine_slice

def pack_nine_slice(root, spec, orient=ORIENT_BUTTONS_RIGHT):
    """A 9-slice panel: one packed image plus four border insets, sliced from a sheet
    exactly like a sprite frame. Shares pack_sprite's image loading, colour key and
    quantisation -- a panel IS a sprite with exactly one frame, no anim, no variants, plus
    the border metadata a sprite has no use for. `frames` is a one-element list (rather
    than a bare `frame`) purely so settle_palettes/sprite_colour_sets can treat a
    nine_slice exactly like a variant-free sprite, with no parallel copy of either.
    """
    name = spec["name"]
    im = load_sheet(root, spec["sheet"])
    px = im.load()
    key = parse_colorkey(spec, f"nine_slice {name!r}")
    sheet_w, sheet_h = im.size

    x, y, w, h = spec.get("rect", (0, 0, sheet_w, sheet_h))
    if x + w > sheet_w or y + h > sheet_h:
        raise BuildError(f"nine_slice {name!r}: rect {x},{y} {w}x{h} runs past the sheet "
                         f"({sheet_w}x{sheet_h})")

    border = spec.get("border")
    if not (isinstance(border, (list, tuple)) and len(border) == 4):
        raise BuildError(f"nine_slice {name!r}: border must be [left, top, right, bottom]")
    bl, bt, br, bb = (int(v) for v in border)
    if any(v < 0 for v in (bl, bt, br, bb)):
        raise BuildError(f"nine_slice {name!r}: border values must not be negative")
    if bl + br > w or bt + bb > h:
        raise BuildError(
            f"nine_slice {name!r}: border {bl}/{bt}/{br}/{bb} does not fit a {w}x{h} panel")

    buf = bytearray(w * h)
    for j in range(h):
        for i in range(w):
            buf[j * w + i] = to_gcolor8(px[x + i, y + j], key)

    # Same reasoning as pack_sprite's own version of this check: a flat two-per-byte
    # stream with no per-row padding needs an even total, not an even width.
    if (w * h) % 2:
        raise BuildError(f"nine_slice {name!r}: {w}x{h} has an odd pixel count, which "
                         f"cannot pack two-per-byte at 4bpp")

    frame, sw, sh = rotate_grid(bytes(buf), w, h, orient)
    bl, bt, br, bb = rotate_border(bl, bt, br, bb, w, h, orient)
    fixed, merged = reduce_colours(frame)

    print(f"  nine_slice {name}: {w}x{h} panel"
          + (f", stored {sw}x{sh}" if (sw, sh) != (w, h) else "")
          + f", border {bl}/{bt}/{br}/{bb}")
    if merged:
        print(f"    NOTE exceeded {PALETTE_USABLE} colours and was reduced -- edit the "
              f"art to avoid this")

    return {"name": name, "w": sw, "h": sh, "frames": [fixed],
            "border": (bl, bt, br, bb), "out": spec["out"]}


def finish_nine_slice(ns, shared, orient=ORIENT_BUTTONS_RIGHT, pack_2bit=False):
    sets = sprite_colour_sets(ns)  # `ns["frames"]` is the one-element list pack_nine_slice
                                   # built for exactly this reuse.

    before = len(shared)
    palettes, assign = merge_palettes(sets, shared, frozen=True)
    shared[:] = palettes

    if assign[0] is None:
        raise BuildError(f"nine_slice {ns['name']!r}: still exceeds {PALETTE_USABLE} "
                         f"colours after reduction")

    frame = ns["frames"][0]
    pixels = pack_unit_4bpp(frame, palettes[assign[0]])
    border_bytes = bytes(ns["border"])

    ns["blob"] = (blob_header(MAGIC_NINE_SLICE, ns["w"], ns["h"], orient=orient)
                 + border_bytes + pixels)
    ns["palettes"] = palettes
    ns["assign"] = assign

    if pack_2bit:
        pixels_bw = pack_unit_2bpp(frame)
        ns["blob_bw"] = (blob_header(MAGIC_NINE_SLICE, ns["w"], ns["h"], orient=orient)
                         + border_bytes + pixels_bw)

    print(f"    {ns['name']}: uses 1 palette(s), {len(palettes) - before} new to the project")
    return ns


# ------------------------------------------------------------------------------ map

def parse_flag_names(raw):
    """[tile_flags] is retired, mid-redesign -- see MAP_EXTENDED's comment above.

    Custom per-tile flag names used to live in the free bits of the old tile_flags[]-by-id
    byte. That byte is gone; its replacement is a sparse per-cell table keyed off the
    MAP_EXTENDED bit, not built yet. No project's manifest actually declares [tile_flags]
    today (checked before starting this), so refusing it here is a build-time nudge
    toward the new mechanism once it exists, not a break of anything real.
    """
    if raw:
        raise BuildError(
            "[tile_flags] is retired: custom per-tile flags are being redone as a sparse "
            "per-cell table (the MAP_EXTENDED bit), not yet built. 'warp' is the only "
            "flag name available right now -- collision moved to being a tile property, "
            "see [[atlas.collision]] on the atlas that owns the tile.")
    return {"warp": TILE_WARP}


def parse_legend(raw, flag_names=None):
    """legend char -> (tile, flag byte, atlas name or None, flip|rotate bits, extended tag).

    `tile` is either a role name the atlas defines or a raw index into it. Roles are the
    better thing to write -- they survive re-importing a sheet, and game code can name
    them -- but an atlas has hundreds of tiles and only a handful are worth naming, so an
    index is how the rest get painted at all.

    The optional `atlas` key is what lets one map draw from several tilesets: without it
    a role resolves against the map's first atlas, which is what every single-tileset
    manifest means and keeps writing.

    `extended` is an optional u8 (0..255) the game defines the meaning of -- a door's
    state, a spawn id, whatever a cell needs to carry that is not collision (a tile
    property, see [[atlas.collision]]) and is not common enough to want its own bit. 0
    means "no tag" and costs nothing; a nonzero value sets MAP_EXTENDED on every cell that
    uses this character and lands in that WorldTile's sparse table (slice_worldtiles).
    """
    flag_names = flag_names or {"warp": TILE_WARP}
    legend = {}
    for ch, entry in raw.items():
        if len(ch) != 1:
            raise BuildError(f"legend key {ch!r} must be exactly one character")
        flags = 0
        for f in entry.get("flags", []):
            if f not in flag_names:
                raise BuildError(f"legend {ch!r}: unknown flag {f!r} "
                                 f"(known: {', '.join(sorted(flag_names))}; collision is "
                                 f"a tile property now -- see [[atlas.collision]])")
            flags |= flag_names[f]

        tile = entry["tile"]
        if isinstance(tile, bool) or not isinstance(tile, (str, int)):
            raise BuildError(f"legend {ch!r}: tile must be a role name or a tile index, "
                             f"not {tile!r}")
        if isinstance(tile, int) and tile < 0:
            raise BuildError(f"legend {ch!r}: tile index {tile} is negative")

        flip = 0
        want = entry.get("flip", [])
        for axis in ([want] if isinstance(want, str) else want):
            if axis not in ("x", "y"):
                raise BuildError(f"legend {ch!r}: flip must be \"x\", \"y\" or both, "
                                 f"not {axis!r}")
            flip |= MAP_FLIP_X if axis == "x" else MAP_FLIP_Y

        rotate = MAP_ROTATE if entry.get("rotate", False) else 0
        if not isinstance(entry.get("rotate", False), bool):
            raise BuildError(f"legend {ch!r}: rotate must be true or false, "
                             f"not {entry['rotate']!r}")

        extended = entry.get("extended", 0)
        if isinstance(extended, bool) or not isinstance(extended, int) or not 0 <= extended <= 255:
            raise BuildError(f"legend {ch!r}: extended must be an integer 0..255, "
                             f"not {extended!r}")

        legend[ch] = (tile, flags, entry.get("atlas"), flip | rotate, extended)
    return legend


def map_atlas_names(spec, known, default):
    """The atlases a map draws from, in the order that fixes its tile id space.

    `atlas = "x"` and `atlases = ["x", "y"]` are the same key spelled for one or many;
    accepting both means a single-tileset manifest never learns a plural it does not need.
    """
    name = spec["name"]
    if "atlas" in spec and "atlases" in spec:
        raise BuildError(f"map {name!r}: give either `atlas` or `atlases`, not both")

    wanted = spec.get("atlases", [spec["atlas"]] if "atlas" in spec else
                      ([default] if default else []))
    if isinstance(wanted, str):
        wanted = [wanted]
    if not wanted:
        raise BuildError(f"map {name!r}: no atlas to draw with")

    seen = []
    for which in wanted:
        if which not in known:
            raise BuildError(f"map {name!r}: atlas {which!r} is not defined "
                             f"(known: {', '.join(sorted(known))})")
        if which in seen:
            raise BuildError(f"map {name!r}: atlas {which!r} is listed twice")
        seen.append(which)
    return seen


def map_tile_bases(name, atlas_names, tile_counts, tile_px):
    """Partition the 10-bit cell index into one contiguous slice per atlas.

    A cell names a tile by a MAP-GLOBAL id, and the map's atlas table says where each
    atlas's slice begins. That spends none of the four per-cell bits M4b reserved for a
    future per-cell palette, and it costs the draw loop one walk of a table with at most a
    handful of entries.

    Returns [(atlas_name, first_tile, tile_count)], and the total.
    """
    table, base = [], 0
    for which in atlas_names:
        px = tile_px[which]
        if px != tile_px[atlas_names[0]]:
            raise BuildError(
                f"map {name!r}: atlas {which!r} has {px}px tiles but {atlas_names[0]!r} "
                f"has {tile_px[atlas_names[0]]}px -- one map draws on one grid, so every "
                f"atlas it uses must share a tile size")
        table.append((which, base, tile_counts[which]))
        base += tile_counts[which]

    if base > MAP_TILE_IDS:
        detail = ", ".join(f"{n} {tile_counts[n]}" for n in atlas_names)
        raise BuildError(
            f"map {name!r}: its atlases hold {base} tiles between them ({detail}), but a "
            f"map cell has {MAP_TILE_IDS} tile ids to spend. Carve less, or split the map.")
    return table, base


def compile_map(spec, legend, roles_by_atlas, atlas_table, map_names, collision_by_atlas,
                primary=True):
    """ASCII rows -> a compiled map, ready for finish_map to slice and pack.

    `atlas_table` is this map's [(atlas, first_tile, tile_count)] partition, so a legend
    char resolves to a map-global tile id here and nothing downstream has to know which
    tileset a cell came from.

    `primary=False` compiles a non-primary `[[map.layer]]`: pure decorative tile art with
    no `start`, no `warps`, and none of finish_compile's gameplay checks -- see
    finish_compile_layer's own comment for why those are a primary-layer-only concept.
    """
    name = spec["name"]
    rows = [r for r in spec["rows"].strip("\n").split("\n") if r.strip()]
    if not rows:
        raise BuildError(f"map {name!r}: no rows")

    h = len(rows)
    w = len(rows[0])
    for i, r in enumerate(rows):
        if len(r) != w:
            raise BuildError(f"map {name!r}: row {i} is {len(r)} chars, row 0 is {w} "
                             f"-- ragged maps compile to garbage")
    if w > 255 or h > 255:
        raise BuildError(f"map {name!r}: {w}x{h} exceeds the u8 dimension limit")

    # Resolving a legend char is the same work for every cell that uses it, and a map is
    # thousands of cells over a few dozen characters. Doing it once per character also
    # means an unusable legend entry is reported whether or not the map happens to use it.
    bases = {a: first for a, first, _ in atlas_table}
    counts = {a: n for a, _, n in atlas_table}
    default_atlas = atlas_table[0][0]
    resolved, unusable, flipped = {}, {}, {}
    for ch, (tile, flag, want_atlas, flip, ext_val) in legend.items():
        which = want_atlas or default_atlas
        # The legend is project-wide but an atlas set is per map, so a character this map
        # cannot draw is only an error if this map USES it. Reported at the cell rather
        # than here, where it would fail every map that merely shares the legend.
        if which not in bases:
            unusable[ch] = (f"legend {ch!r} draws from atlas {which!r}, which this map "
                            f"does not use. It draws from: "
                            f"{', '.join(a for a, _, _ in atlas_table)}")
            continue

        roles = roles_by_atlas[which]
        if isinstance(tile, int):
            # An index is checked against the atlas as it was actually packed, so a carve
            # that shrank -- a tighter region, a lower max_tiles -- fails the build with
            # the character that no longer resolves rather than drawing the wrong picture.
            if tile >= counts[which]:
                unusable[ch] = (f"legend {ch!r} names tile {tile} of atlas {which!r}, "
                                f"which packed {counts[which]} tiles (0-"
                                f"{counts[which] - 1})")
                continue
            local = tile
        elif tile not in roles:
            unusable[ch] = (f"legend {ch!r} names tile role {tile!r}, which atlas "
                            f"{which!r} does not define. That atlas provides: "
                            f"{', '.join(sorted(roles)) or '(no roles -- give it an '
                                                           'autopick or [atlas.semantic] '
                                                           'table)'}")
            continue
        else:
            local = roles[tile]

        # Whether an atlas ends up metatiled is not known until finish_atlas has weighed
        # the saving, which is long after this. So the conflict is only RECORDED here,
        # where the legend character is still in hand to name, and check_flip_metatiles
        # rules on it once both halves are known.
        if flip:
            flipped.setdefault(which, []).append(ch)

        # u16 little-endian: 10 bits of MAP-GLOBAL index, then PNX_MAP_FLIP_X/_Y/_ROTATE.
        # WARP and EXTENDED are folded in below, per cell, since both are folded from a
        # pre-shift byte/value rather than living in `flip` itself.
        resolved[ch] = (((bases[which] + local) & MAP_INDEX_MASK) | flip, flag, ext_val)

    tiles = bytearray(w * h * 2)   # u16 per cell
    flags = bytearray(w * h)   # one byte per cell; tiles are u16, see below
    extended = bytearray(w * h)   # one byte per cell; 0 means untagged
    painted = set()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch not in resolved:
                if ch in unusable:
                    raise BuildError(f"map {name!r} at {x},{y}: {unusable[ch]}")
                raise BuildError(f"map {name!r}: unknown legend char {ch!r} at {x},{y} "
                                 f"(known: {' '.join(sorted(legend))})")
            painted.add(ch)
            entry, flag, ext_val = resolved[ch]
            entry = fold_flag_into_entry(entry, flag)
            if ext_val:
                entry |= MAP_EXTENDED
            tiles[(y * w + x) * 2] = entry & 0xFF
            tiles[(y * w + x) * 2 + 1] = entry >> 8
            flags[y * w + x] = flag
            extended[y * w + x] = ext_val

    flipped = {a: [c for c in chars if c in painted]
              for a, chars in flipped.items() if any(c in painted for c in chars)}
    if not primary:
        return finish_compile_layer(name, w, h, tiles, flags, extended, atlas_table,
                                    flipped=flipped)
    return finish_compile(name, spec, w, h, tiles, flags, extended, atlas_table, map_names,
                          spec.get("warps", []), collision_by_atlas, flipped=flipped)


def compile_source_map(spec, root, roles_by_atlas, atlas_table, map_names, collision_by_atlas,
                       primary=True):
    """Compile a map whose cells live in a `.pnxmap` file rather than in the manifest.

    The tile table is the legend, indexed by number instead of by character -- so this
    resolves each entry exactly as compile_map resolves a legend char, and then hands the
    same cells and flags to the same checks. Nothing about reachability, warps or the
    blob layout knows which authoring format a map came from.

    Why a file at all is argued in tools/pnx_mapfile.py; the short version is that one
    character per cell capped a map at ~90 distinct tiles against the runtime's 1024, and
    a 255x255 map is 65KB of text sitting in the middle of a manifest.

    `primary=False` compiles a non-primary `[[map.layer]]` -- see compile_map's own
    `primary` for what that skips. `spec["source"]` is always read as ONE WorldTile plane
    here: `doc["w"]`/`["h"]`/`["cells"]` already mirror the file's own primary layer
    (`mapfile.loads`'s back-compat shape) whether the file is single-layer or declares
    several of its own -- if it declares several, only its own primary is used. A
    `[[map.layer]]` nesting layers-of-layers inside its own source file is not a
    combination this format needs to support; point each manifest layer at its own file.
    """
    name = spec["name"]
    path = os.path.join(root, spec["source"])
    if not os.path.exists(path):
        raise BuildError(f"map {name!r}: no such source file {spec['source']!r}")
    try:
        doc = mapfile.read(path)
    except mapfile.MapFileError as e:
        raise BuildError(f"map {name!r}: {e}") from None

    w, h = doc["w"], doc["h"]
    bases = {a: first for a, first, _ in atlas_table}
    counts = {a: n for a, _, n in atlas_table}

    # Resolved once per TABLE ENTRY, not per cell: a map is thousands of cells over a few
    # dozen entries, and an entry that cannot resolve is worth reporting whether or not
    # some cell happens to use it.
    resolved, flipped = [], {}
    for i, t in enumerate(doc["tiles"]):
        which = t["atlas"]
        if which not in bases:
            raise BuildError(
                f"map {name!r}: tile {i} draws from atlas {which!r}, which this map does "
                f"not use. It draws from: {', '.join(a for a, _, _ in atlas_table)}")
        idx = t["index"]
        if isinstance(idx, str):
            roles = roles_by_atlas[which]
            if idx not in roles:
                raise BuildError(
                    f"map {name!r}: tile {i} names role {idx!r}, which atlas {which!r} "
                    f"does not define. That atlas provides: "
                    f"{', '.join(sorted(roles)) or '(none)'}")
            idx = roles[idx]
        if idx >= counts[which]:
            raise BuildError(
                f"map {name!r}: tile {i} names index {idx} of atlas {which!r}, which "
                f"packed {counts[which]} tiles (0-{counts[which] - 1})")

        flip = 0
        for axis in t.get("flip", ""):
            flip |= MAP_FLIP_X if axis == "x" else MAP_FLIP_Y
        if t.get("rotate", False):
            flip |= MAP_ROTATE
        if flip:
            flipped.setdefault(which, []).append(str(i))
        ext_val = t.get("extended", 0)
        if isinstance(ext_val, bool) or not isinstance(ext_val, int) or not 0 <= ext_val <= 255:
            raise BuildError(f"map {name!r}: tile {i}'s extended value must be an "
                             f"integer 0..255, not {ext_val!r}")
        resolved.append((((bases[which] + idx) & MAP_INDEX_MASK) | flip,
                         t.get("flags", 0), ext_val))

    cells = doc["cells"]
    tiles = bytearray(w * h * 2)
    flags = bytearray(w * h)
    extended = bytearray(w * h)
    used = set()
    for i, c in enumerate(cells):
        used.add(c)
        entry, flag, ext_val = resolved[c]
        entry = fold_flag_into_entry(entry, flag)
        if ext_val:
            entry |= MAP_EXTENDED
        tiles[i * 2] = entry & 0xFF
        tiles[i * 2 + 1] = entry >> 8
        flags[i] = flag
        extended[i] = ext_val

    flipped = {a: [c for c in labels if int(c) in used]
              for a, labels in flipped.items() if any(int(c) in used for c in labels)}
    if not primary:
        return finish_compile_layer(name, w, h, tiles, flags, extended, atlas_table,
                                    flipped=flipped)

    # `start` and `warps` come from the FILE, not the manifest: they are positions in a
    # grid the file owns, and a manifest that could disagree with it would be a second
    # place to look when a warp lands in the wrong room.
    spec = dict(spec)
    spec["start"] = doc["start"]

    return finish_compile(name, spec, w, h, tiles, flags, extended, atlas_table, map_names,
                          doc["warps"], collision_by_atlas, flipped=flipped)


def tile_collision_mode(atlas_table, collision_by_atlas, tile):
    """A map-global tile id -> its art tile's COLLISION_* mode.

    Walks atlas_table the same way the runtime resolves a drawn cell's tileset -- a
    handful of entries, linear is fine. NONE for a tile no [[atlas.collision]] entry
    named, which is the correct default: most tiles are plain floor/scenery.
    """
    for name, first, count in atlas_table:
        if first <= tile < first + count:
            return collision_by_atlas.get(name, {}).get(
                tile - first, (COLLISION_NONE, COLLISION_KIND_WALL, None))[0]
    return COLLISION_NONE


def solid_cells_for(tiles, atlas_table, collision_by_atlas):
    """Per-cell exact-SOLID boolean plane, for the reachability checks below only.

    Deliberately exact-SOLID, not SCALED or COMPLEX: those are only PARTIALLY solid (a
    fence's bottom half; whatever a mask covers), and a flood fill has no way to know how
    much of a cell a player could still slip through. Treating a mostly-solid COMPLEX
    tile as open is the safe direction -- it costs a build that could theoretically have
    caught a real seal-off and did not, not a build that fails over content that was
    fine. Matches exactly what plain SOLID already did before any other mode existed.
    """
    out = bytearray(len(tiles) // 2)
    for i in range(len(out)):
        tile = (tiles[i * 2] | (tiles[i * 2 + 1] << 8)) & MAP_INDEX_MASK
        out[i] = tile_collision_mode(atlas_table, collision_by_atlas, tile) == COLLISION_SOLID
    return out


def finish_compile(name, spec, w, h, tiles, flags, extended, atlas_table, map_names,
                   warp_specs, collision_by_atlas, flipped=None):
    """Everything a compiled map is checked for, whatever it was authored in.

    Split out when maps gained a binary source format. The checks below -- a start inside
    a wall, a warp on a tile with no warp flag, a warp sealed off from the start -- are
    the pipeline's whole reason for existing, and a second authoring path that quietly
    skipped them would be worse than no second path at all. So `rows` and `.pnxmap` both
    resolve to cells, flags and extended tags and then arrive here.

    `extended` has nothing to validate -- unlike warp, a tag has no invariant a build
    could catch (any value is legal on any cell) -- so it just rides along to the
    returned dict for rotate_maps and slice_worldtiles to carry into the blob.
    """
    flipped = flipped or {}
    solid = solid_cells_for(tiles, atlas_table, collision_by_atlas)
    sx, sy = spec["start"]
    if not (0 <= sx < w and 0 <= sy < h):
        raise BuildError(f"map {name!r}: start {(sx, sy)} is outside the {w}x{h} map")
    if solid[sy * w + sx]:
        raise BuildError(f"map {name!r}: start {(sx, sy)} is inside a solid tile")

    # Flood fill from the start over walkable tiles.
    #
    # An unreachable warp fails the build, but `gated = true` on the warp declares that it is
    # reachable only through game state -- a button-operated door, a destructible wall, a bridge
    # that appears -- and silences it permanently.
    #
    # The declaration is what makes strictness correct. A static flood fill cannot see a button,
    # so without an escape hatch the check calls correct content broken, and a check that cries
    # wolf gets silenced wholesale. With one, the author states intent once and it is recorded in
    # the content, in git, where a reviewer sees it.
    #
    # And because the acknowledgement is attached to the warp declaration rather than kept in a
    # side file, it cannot outlive what it describes: move the warp and it travels along, delete
    # the warp and it is gone. There is no fingerprint to maintain and nothing to go stale
    # silently -- the one stale case, `gated` on a warp that turns out to be reachable, is
    # reported, because it usually means the gate was removed.
    reachable = set()
    stack = [(sx, sy)]
    while stack:
        x, y = stack.pop()
        if (x, y) in reachable or not (0 <= x < w and 0 <= y < h):
            continue
        if solid[y * w + x]:
            continue
        reachable.add((x, y))
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]

    walkable = sum(1 for i in range(w * h) if not solid[i])
    sealed = walkable - len(reachable)

    warps = []
    for wp in warp_specs:
        tx, ty = wp["at"]
        dest_name, dtx, dty = wp["to"]
        if not (0 <= tx < w and 0 <= ty < h):
            raise BuildError(f"map {name!r}: warp at {(tx, ty)} is outside the map")
        if not flags[ty * w + tx] & TILE_WARP:
            raise BuildError(f"map {name!r}: warp at {(tx, ty)} sits on a tile with no "
                             f"'warp' flag -- the player would walk over it and nothing "
                             f"would happen")
        if solid[ty * w + tx]:
            raise BuildError(f"map {name!r}: warp at {(tx, ty)} is on a SOLID tile -- the "
                             f"player can never stand on it, so it can never fire. No runtime "
                             f"state fixes this; move the warp to a walkable tile.")

        gated = bool(wp.get("gated", False))
        if (tx, ty) not in reachable and not gated:
            raise BuildError(
                f"map {name!r}: warp at {(tx, ty)} is UNREACHABLE from start {(sx, sy)} -- it is "
                f"sealed off by solid tiles, so it can never fire and the build will look fine. "
                f"If that is deliberate -- a gate, a secret door, a wall the game opens -- add "
                f"`gated = true` to the warp and this will not be raised again.")
        if (tx, ty) in reachable and gated:
            print(f"    NOTE map {name}: warp at {(tx, ty)} is marked `gated` but IS reachable "
                  f"from the start. Harmless, but the gate may have been removed.")
        if dest_name not in map_names:
            raise BuildError(f"map {name!r}: warp targets unknown map {dest_name!r} "
                             f"(known: {', '.join(map_names)})")
        warps.append((tx, ty, map_names.index(dest_name), dtx, dty))

    print(f"  map {name}: {w}x{h}, {len(warps)} warps, "
          f"{len(reachable)}/{walkable} tiles reachable"
          + (f", {sealed} sealed off" if sealed else "")
          + (f", {len(atlas_table)} atlases" if len(atlas_table) > 1 else ""))



    # The blob is built later by finish_map, once the atlases are packed and the map can
    # be sliced into WorldTiles.
    return {"name": name, "w": w, "h": h, "start": (sx, sy), "tiles": bytes(tiles),
            "out": spec["out"], "warps": warps, "reachable": reachable,
            "flags": flags, "extended": extended, "solid": solid, "atlas_table": atlas_table,
            # {atlas: [label]} for the tiles this map PAINTED flipped, kept only so
            # check_flip_metatiles can name them once the atlases are packed. A flipped
            # entry nobody used is nobody's problem, the same view taken of an entry that
            # does not resolve. Labels are legend characters for a `rows` map and tile
            # table indices for a `.pnxmap` -- whatever names the thing to go and fix.
            "flipped": flipped,
            "atlases": [a for a, _, _ in atlas_table]}


def finish_compile_layer(name, w, h, tiles, flags, extended, atlas_table, flipped=None):
    """A non-primary map layer: background/overlay art with no gameplay checks.

    Warps land on the PRIMARY layer only (PnxMapLayer's own comment, pnx_assets.h), so a
    layer that is not primary skips the flood fill, the start-inside-a-wall check, and
    every warp check finish_compile runs for whichever layer IS primary -- there is no
    start here and nothing for a warp to test. `solid`/`reachable` are likewise never
    computed: the only consumer of either is check_warp_destinations, which only ever asks
    about a warp's PRIMARY-layer landing tile.
    """
    flipped = flipped or {}
    print(f"    layer {name!r}: {w}x{h}"
          + (f", {len(atlas_table)} atlases" if len(atlas_table) > 1 else ""))
    return {"name": name, "w": w, "h": h, "tiles": bytes(tiles),
            "flags": flags, "extended": extended, "atlas_table": atlas_table,
            "flipped": flipped, "atlases": [a for a, _, _ in atlas_table]}


def check_flip_metatiles(maps, atlases):
    """Refuse a flipped OR rotated legend character drawn from a metatiled atlas.

    Mirroring or transposing a composed tile means reordering the quadrants as well as
    transforming each one, which pnx_tilemap_draw does not do -- it skips both for
    metatiles entirely. So the watch would draw the tile untransformed, which reads as
    art that is subtly wrong rather than as an error, and is the kind of thing nobody
    finds for a week.

    Named for flip because that came first, but `flipped` (built in compile_map /
    compile_source_map) is really "carries any per-cell transform" -- rotate folds into
    the same `flip` byte those functions test with `if flip:`, so a rotate-only entry
    lands in this set exactly like a mirrored one and is refused the same way.

    Deliberately not in compile_map: whether an atlas is metatiled is decided in
    finish_atlas by weighing the saving, long after the maps are compiled. Checking here
    is the first moment both halves are known, and the legend characters were carried
    along so the message can still name them.
    """
    meta = {a["name"] for a in atlases if a.get("subtiles")}
    for m in maps:
        # Every layer, not just the primary (which `m` itself mirrors) -- a background or
        # overlay layer paints from the same shared atlas table and is exactly as subject
        # to this as the layer gameplay happens on.
        for layer in m.get("layers", [m]):
            for atlas_name, chars in sorted(layer.get("flipped", {}).items()):
                if atlas_name not in meta:
                    continue
                raise BuildError(
                    f"map {m['name']!r} layer {layer['name']!r}: legend "
                    f"{', '.join(repr(c) for c in sorted(chars))} paints a FLIPPED or "
                    f"ROTATED tile from atlas {atlas_name!r}, which was packed as "
                    f"metatiles -- and the runtime does not transform those, so it would "
                    f"draw untransformed. Either drop the flip/rotate, or put "
                    f"`metatiles = false` on that atlas and pay the flat tile cost.")


def swap_flip_bits(cells):
    """Exchange PNX_MAP_FLIP_X and _Y in every u16 cell of a tile plane."""
    out = bytearray(cells)
    for i in range(0, len(out), 2):
        hi = out[i + 1]
        x, y = hi & (MAP_FLIP_X >> 8), hi & (MAP_FLIP_Y >> 8)
        if bool(x) != bool(y):
            out[i + 1] = hi ^ ((MAP_FLIP_X | MAP_FLIP_Y) >> 8)
    return bytes(out)


def rotate_maps(maps, orient):
    """Turn every compiled map from the author's frame into the framebuffer's.

    Runs AFTER all validation, which is the point: the flood fill, the warp checks and
    every message they produce speak in the coordinates the author typed into `rows`. A
    map rotated before validation would report a sealed door at a position that appears
    nowhere in the manifest.

    A tilemap rotates like any other grid -- and because each tile's art was rotated as it
    was carved, rotating the grid is the whole job. A landscape orientation swaps the
    dimensions, so a 32x24 map becomes 24x32; buttons_left does not, since a half-turn
    leaves width and height alone. Either way the runtime, which only ever reads w and h
    from the blob, is none the wiser.

    Runs on every LAYER a map declares, not just the primary -- a background or overlay
    layer is drawn in the framebuffer's frame exactly as much as the one gameplay happens
    on. `m` itself IS `m["layers"][0]` (the primary, always layer 0 -- see the manifest
    driver's own comment for why), so rotating `m["tiles"]`/`w`/`h` inside the loop below
    already rotates the primary layer's own entry; only `start`/`warps`/`reachable`, which
    exist on the primary alone, need handling outside it.
    """
    if orient == ORIENT_BUTTONS_RIGHT:
        return

    # Captured before anything moves: a warp's destination is a coordinate in ANOTHER
    # map, and it has to be rotated by that map's PRIMARY layer's dimensions, not by this
    # one's -- a warp always lands on a primary layer (PnxMapLayer's own comment).
    author_dims = [(m["w"], m["h"]) for m in maps]

    for m in maps:
        # The primary's PRE-rotation dims: captured before the per-layer loop below
        # rotates `m` itself (== layers[0]) in place and overwrites m["w"]/m["h"].
        pw, ph = m["w"], m["h"]

        for layer in m["layers"]:
            w, h = layer["w"], layer["h"]
            layer["tiles"], nw, nh = rotate_grid(layer["tiles"], w, h, orient, stride=2)
            # Moving the cell is not the whole job for a FLIPPED cell. Each tile's art was
            # rotated as it was carved, so the axes turned with it: a quarter turn makes
            # the author's horizontal mirror the framebuffer's vertical one, so both
            # landscape orientations swap the pair. A half-turn (buttons_left) does not --
            # a horizontal mirror composed with a point reflection is still a horizontal
            # mirror -- so this would silently cross a layer's flip bits for that
            # orientation if it ran unguarded.
            if orient in LANDSCAPE_ORIENTS:
                layer["tiles"] = swap_flip_bits(layer["tiles"])
            layer["flags"], _, _ = rotate_grid(layer["flags"], w, h, orient)
            layer["extended"], _, _ = rotate_grid(layer["extended"], w, h, orient)
            layer["w"], layer["h"] = nw, nh

        m["start"] = rotate_point(*m["start"], pw, ph, orient)
        m["reachable"] = {rotate_point(x, y, pw, ph, orient) for x, y in m["reachable"]}
        m["warps"] = [rotate_point(tx, ty, pw, ph, orient)
                      + (dest,)
                      + rotate_point(dtx, dty, *author_dims[dest], orient)
                      for (tx, ty, dest, dtx, dty) in m["warps"]]


class _BitWriter:
    """MSB-first bit accumulator -- matches src/pnx/assets/pnx_bitplane.c's BitReader
    exactly (bit 7 of byte 0 first). Simplicity over speed: build-time only, same posture
    as lzss_compress below."""

    def __init__(self):
        self.bits = []

    def write_bit(self, b):
        self.bits.append(b & 1)

    def write_bits_msb(self, value, n):
        for i in range(n - 1, -1, -1):
            self.write_bit((value >> i) & 1)

    def to_bytes(self):
        out = bytearray()
        for i in range(0, len(self.bits), 8):
            chunk = self.bits[i:i + 8] + [0] * (8 - len(self.bits[i:i + 8]))
            byte = 0
            for b in chunk:
                byte = (byte << 1) | b
            out.append(byte)
        return bytes(out)


def _elias_gamma_n_bits(v):
    n_bits = 1
    while not ((1 << n_bits) - 1 <= v <= (1 << (n_bits + 1)) - 2):
        n_bits += 1
    return n_bits


def _write_elias_gamma(bw, v):
    n_bits = _elias_gamma_n_bits(v)
    for _ in range(n_bits - 1):
        bw.write_bit(1)
    bw.write_bit(0)
    bw.write_bits_msb(v + 1 - (1 << n_bits), n_bits)


def encode_bitplane_units(units):
    """Concatenates bitplane-encoded units with a leading u16 offset table."""
    offsets = [0]
    for u in units:
        offsets.append(offsets[-1] + len(u))
    offset_table = b"".join(o.to_bytes(2, "little") for o in offsets)
    return offset_table + b"".join(units)


def encode_bitplane_unit(pixels, bpp=4):
    """Bitplane-separated, Elias-gamma-RLE-coded, frequency-sorted-local-palette
    encoding of one sprite frame or atlas tile's pixel indices (flat, row-major) --
    src/pnx/assets/pnx_bitplane.c's decoder reads exactly this. Ported from this
    session's bpeg_encode.py prototype (2000/2000 synthetic + 48/48 real-tile round-trips
    verified there and again by test_bitplane_compress.c/test_bitplane_atlas.c on the C
    side) rather than re-derived, so the format is proven before this ever touches a real
    project's build. See src/pnx/assets/pnx_bitplane.h for the on-disk layout and why it
    trades against LZSS the way it does (random per-unit access vs raw size).

    The bitplane/Elias-gamma stream's own cost is ceil(log2(k)) bits/pixel for this unit's
    own real colour count `k`, independent of `bpp` -- a 2-colour BW tile costs the same
    ~1 bit/pixel here a 2-colour 4bpp tile would. `bpp` (4 for a colour build's own
    pixels, 2 for a `~bw` build's ink states -- see bw_pixel_states) only selects which
    packed layout the RAW fallback escape hatch below falls back to, so it stays pixel-
    identical to what an uncompressed build of the SAME bpp would have shipped (the same
    round-trip guarantee the 4bpp path always had).

    Returns the complete on-disk bytes for this one unit: 1-byte header (bit 7 = raw
    fallback flag, bits 3-0 = local colour count - 1) then either the packed escape hatch
    (bpp-packed, matching bp_pack/pnx_bitplane_decode's PNX_DISPLAY_BW branch on the C
    side) or a nibble-packed local-palette offset table + the bitplane/Elias-gamma stream,
    whichever is smaller -- same "never worse than raw" guarantee compress_pixel_body
    already gives LZSS.
    """
    n = len(pixels)
    colors = sorted(set(pixels))
    k = len(colors)
    limit = 1 << bpp
    if k > 16:
        raise BuildError(f"encode_bitplane_unit: {k} distinct colours, 16 is the most "
                         f"the on-disk local-palette offset table can index")
    if k > limit:
        raise BuildError(f"encode_bitplane_unit: {k} distinct values, {bpp}bpp allows at "
                         f"most {limit}")

    raw_body = (pack_unit_4bpp_raw_indices(pixels) if bpp == 4
               else pack_2bpp_raw_states(pixels))

    freq = {}
    for p in pixels:
        freq[p] = freq.get(p, 0) + 1
    order = {c: i for i, (c, _) in enumerate(sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])))}
    local = [order[v] for v in pixels]

    off_vals = [c for c, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))]
    off_table = bytearray()
    for i in range(0, k, 2):
        hi = off_vals[i]
        lo = off_vals[i + 1] if i + 1 < k else 0
        off_table.append((hi << 4) | lo)

    bits = 0 if k <= 1 else (1 if k <= 2 else 2 if k <= 4 else 3 if k <= 8 else 4)

    bw = _BitWriter()
    for p in range(bits):
        plane = [(v >> p) & 1 for v in local]
        bw.write_bit(plane[0])
        i = 0
        while i < n:
            j = i
            while j < n and plane[j] == plane[i]:
                j += 1
            _write_elias_gamma(bw, j - i)
            i = j
    bitstream = bw.to_bytes()

    coded_body = bytes(off_table) + bitstream
    coded_total = 1 + len(coded_body)
    raw_total = 1 + len(raw_body)

    if coded_total <= raw_total:
        header = (k - 1) & 0xF
        return bytes([header]) + coded_body
    return bytes([0x80]) + raw_body


def pack_unit_4bpp_raw_indices(pixels):
    """2 pixels/byte, high nibble first -- pack_unit_4bpp's own layout, but from already-
    palette-relative indices (0-15) rather than raw GColor8 values, since encode_bitplane_unit
    is called AFTER a frame's own pack_unit_4bpp/palette assignment has already run (it
    needs the on-disk raw-fallback escape hatch to be pixel-identical to what an
    uncompressed build would have shipped, so a build turning compress OFF and back ON
    round-trips through the exact same bytes)."""
    px = list(pixels)
    if len(px) % 2:
        px.append(0)
    return bytes((px[i] << 4) | px[i + 1] for i in range(0, len(px), 2))


def unpack_4bpp_to_indices(data, n):
    """Inverse of pack_unit_4bpp/pack_unit_4bpp_raw_indices -- reads `n` palette-relative
    indices back out of `data`'s 2-per-byte packing. Needed because finish_sprite already
    has frame pixels as PACKED bytes (frame_packed, from pack_unit_4bpp) by the time the
    bitplane path needs per-pixel indices to feed encode_bitplane_unit -- unpacking is
    cheaper than threading a second, unpacked copy of every frame through the function
    just for the one caller that wants it."""
    out = []
    for i in range(n // 2 + (n % 2)):
        b = data[i]
        out.append(b >> 4)
        if len(out) < n:
            out.append(b & 0x0F)
    return out[:n]


def unpack_2bpp_to_states(data, n):
    """Inverse of pack_unit_2bpp/pack_2bpp_raw_states -- reads `n` ink states (0-3) back
    out of `data`'s 4-per-byte, high-bits-first packing. compress_sprite_pixels_bitplane_bw's
    own use, mirroring unpack_4bpp_to_indices for the colour path."""
    out = []
    for i in range(0, n, 4):
        b = data[i // 4]
        for k in range(4):
            if len(out) >= n:
                break
            out.append((b >> (6 - 2 * k)) & 3)
    return out[:n]


def lzss_compress(data, window=4096, min_match=3, max_match=18):
    """Encodes `data` for pnx_lzss_decode (src/pnx/assets/pnx_lzss.c) to read back -- a
    classic (12,4) token: a control byte's 8 bits (LSB first) each say literal (1, one
    byte follows) or match (0, a 2-byte back-reference follows: (distance-1) low byte,
    then (distance-1)'s high nibble packed with (length-3) in the other nibble).

    Build-time only, so a plain greedy longest-match search is fine -- encoder speed and
    optimality never matter here, only the decoder's simplicity does, and that is what the
    token format buys: a copy/literal loop, no entropy stage.
    """
    n = len(data)
    i = 0
    out = bytearray()
    control_pos = None
    bit_index = 8  # forces a fresh control byte before the first token

    while i < n:
        if bit_index == 8:
            control_pos = len(out)
            out.append(0)
            bit_index = 0

        start = max(0, i - window)
        best_len, best_off = 0, 0
        limit = min(max_match, n - i)
        for j in range(start, i):
            length = 0
            while length < limit and data[j + length] == data[i + length]:
                length += 1
            if length > best_len:
                best_len, best_off = length, i - j

        if best_len >= min_match:
            off = best_off - 1          # 0..4095, fits 12 bits
            ln = best_len - min_match   # 0..15, fits 4 bits
            out.append(off & 0xFF)
            out.append(((off >> 8) & 0x0F) | (ln << 4))
            i += best_len
        else:
            out[control_pos] |= (1 << bit_index)
            out.append(data[i])
            i += 1
        bit_index += 1

    return bytes(out)


def lzss_decompress(data, out_len):
    """The inverse of lzss_compress -- a pure-Python mirror of pnx_lzss_decode
    (src/pnx/assets/pnx_lzss.c), so parse_map can read a compressed bank back without a C
    dependency. Keep this in lockstep with that file if the token format ever changes; the
    editor round-tripping a compressed map depends on the two agreeing exactly.
    """
    out = bytearray()
    si = 0
    n = len(data)
    while len(out) < out_len and si < n:
        control = data[si]
        si += 1
        for bit in range(8):
            if len(out) >= out_len:
                break
            if control & (1 << bit):
                if si >= n:
                    return bytes(out)
                out.append(data[si])
                si += 1
            else:
                if si + 2 > n:
                    return bytes(out)
                lo, hi = data[si], data[si + 1]
                si += 2
                dist = (lo | ((hi & 0x0F) << 8)) + 1
                length = (hi >> 4) + 3
                if dist > len(out):
                    return bytes(out)
                start = len(out) - dist
                for k in range(length):
                    if len(out) >= out_len:
                        break
                    out.append(out[start + k])
    return bytes(out)


def build_cell_dictionary(tiles):
    """Every distinct cell entry word (tile id + flip/rotate/warp/extended bits) a map's
    cell plane actually uses, in first-seen order, plus the index width that fits the
    count.

    Real maps use only a handful of distinct entries -- a legend only ever paints from a
    small palette, and it was measured directly (not assumed) against every map already in
    this repo: never more than 9. A WorldTile stores a 1-byte (or, past 256 entries,
    2-byte) index into this table instead of the raw 2-byte word -- see PNX_BLOB_VERSION's
    v13 comment (pnx_assets.h) for the numbers this earns. `idx_width` degrades to 2 on a
    map with more than 256 distinct entries rather than failing the build: no worse than
    today's cost, never a build error over it.
    """
    order = []
    index_of = {}
    for i in range(0, len(tiles), 2):
        entry = tiles[i] | (tiles[i + 1] << 8)
        if entry not in index_of:
            index_of[entry] = len(order)
            order.append(entry)
    idx_width = 1 if len(order) <= 256 else 2
    return order, index_of, idx_width


def slice_worldtiles(m, worldtile, index_of, idx_width):
    """Cut the cell plane into WorldTiles.

    Used to also carry each WorldTile's own COLLISION flag overrides -- retired along with
    tile_flags[]-by-id (see MAP_ROTATE's comment): collision/warp are baked straight into
    every cell's own u16 now (fold_flag_into_entry, at both places `m["tiles"]` gets
    built), so there is no per-tile collision default for a cell to diverge from any more.

    A DIFFERENT sparse table is built here instead, for a different reason: EXTENDED. A
    cell's tag is arbitrary game data, not something with a tile-owned default to diverge
    from in the first place, so it was never going to fold into a byte the way warp did --
    it needs its own (x, y, value) triple, and this is exactly the shape the old override
    table already had. So the mechanism that collision retired is reused, unchanged, for a
    purpose collision never actually needed.

    Edge WorldTiles are stored clipped rather than padded out to a full square. The pool
    slot is still full size, so nothing downstream cares, and a 32x24 map does not pay for
    the 8 rows of nothing that padding to 32x32 would invent.
    """
    w, h = m["w"], m["h"]
    cols = (w + worldtile - 1) // worldtile
    rows = (h + worldtile - 1) // worldtile

    tiles = []
    for wy in range(rows):
        for wx in range(cols):
            x0, y0 = wx * worldtile, wy * worldtile
            cw = min(worldtile, w - x0)
            ch = min(worldtile, h - y0)

            cells = bytearray()
            ext = bytearray()
            ext_count = 0
            used = set()
            for ly in range(ch):
                for lx in range(cw):
                    i = (y0 + ly) * w + (x0 + lx)
                    entry = m["tiles"][i * 2] | (m["tiles"][i * 2 + 1] << 8)
                    cells += index_of[entry].to_bytes(idx_width, "little")
                    tile = entry & 0x03FF
                    used.add(tile)
                    val = m["extended"][i]
                    if val:
                        ext += bytes([lx, ly, val])
                        ext_count += 1

            # Which atlases this WorldTile draws from, as a bit per entry in the map's
            # atlas table. The streamer reads it to pin those atlases BEFORE it reads the
            # cells, so a resident WorldTile can never name a tile whose art is gone.
            mask = 0
            for tile in used:
                for bit, (_, first, n) in enumerate(m["atlas_table"]):
                    if first <= tile < first + n:
                        mask |= 1 << bit
                        break

            payload = (bytes([cw, ch]) + bytes(cells)
                      + ext_count.to_bytes(2, "little") + bytes(ext))
            tiles.append({"x": wx, "y": wy, "w": cw, "h": ch, "mask": mask,
                          "cells": bytes(cells), "ext_count": ext_count,
                          "payload": payload})
    return cols, rows, tiles


def check_worldtile_windows(m, cols, rows, tiles, window, atlas_slots):
    """Every window of WorldTiles that can be resident at once must fit the atlas pool.

    This is the check that turns a map which would thrash -- evicting an atlas and reading
    it back every few steps -- into a build error naming the corner of the map that does
    it. The streamer cannot recover from it at runtime: if one screenful needs five atlases
    and there are four slots, something has to be evicted while it is still on screen.
    """
    by_xy = {(t["x"], t["y"]): t for t in tiles}
    worst, worst_at = 0, None
    for wy in range(max(1, rows - window[1] + 1)):
        for wx in range(max(1, cols - window[0] + 1)):
            mask = 0
            for dy in range(min(window[1], rows)):
                for dx in range(min(window[0], cols)):
                    t = by_xy.get((wx + dx, wy + dy))
                    if t:
                        mask |= t["mask"]
            if bin(mask).count("1") > worst:
                worst, worst_at = bin(mask).count("1"), (wx, wy, mask)

    if worst > atlas_slots:
        wx, wy, mask = worst_at
        names = [n for bit, (n, _, _) in enumerate(m["atlas_table"]) if mask >> bit & 1]
        raise BuildError(
            f"map {m['name']!r}: the {window[0]}x{window[1]} WorldTiles resident around "
            f"{wx},{wy} draw from {worst} atlases ({', '.join(names)}), but the map "
            f"declares atlas_slots = {atlas_slots}. One of those atlases would be evicted "
            f"while it is still on screen. Raise atlas_slots to {worst}, or keep that part "
            f"of the map to fewer tilesets.")
    return worst


def atlas_pool_layout(sizes, slots):
    """Where each atlas pool slot begins, as offsets with a sentinel past the last.

    Two cases, one table. When there is a slot per atlas nothing is ever evicted, so slot
    i is atlas i and each gets EXACTLY its own size -- which for the ship's 29,596-byte
    tileset beside water's 8,116 is 37 KB rather than the 59 KB that two slots of the
    larger would cost, and the difference between fitting on a 128 KB watch and not.

    When there are fewer slots than atlases any atlas can land in any slot, so every slot
    has to hold the largest. That is the price of eviction, and it is paid only by the maps
    that need it.
    """
    if slots >= len(sizes):
        at, offsets = 0, []
        for n in sizes:
            offsets.append(at)
            at += (n + 3) & ~3
        return offsets + [at]
    stride = (max(sizes) + 3) & ~3
    return [i * stride for i in range(slots + 1)]


def bank_shift_for(slot_bytes, bank_bytes):
    """Tiles per bank, as a shift, so the runtime finds a bank by shifting an index.

    The largest power of two whose bank still fits the cap, and never zero: one WorldTile
    that overruns the cap on its own gets a bank to itself rather than an error, because
    the cap is a target for seek cost and not a format limit.
    """
    shift = 0
    while (2 << shift) * slot_bytes <= bank_bytes and shift < 8:
        shift += 1
    return shift


def prepare_map(m, worldtile=WORLDTILE_DEFAULT, bank_bytes=WORLDTILE_BANK_BYTES):
    """Slice every one of a map's layers into WorldTiles and work out how many bank
    resources each needs.

    Split out of finish_map because of an ordering knot: a map's banks are resources, so
    they need asset ids, so they have to exist before the id table is built -- and the id
    table has to exist before finish_map, which writes atlas ids into the blob. Slicing is
    the only part that answers "how many banks", and it depends on nothing but the map, so
    it moves here and runs first.

    The cell dictionary is built ONCE, from every layer's tiles together -- a map's tile id
    space is one shared thing (PnxMap's own comment), so its distinct cell entries are one
    shared table too, the same way the atlas table itself is shared rather than duplicated
    per layer. `worldtile`/`bank_bytes` are the map's defaults; a layer whose own spec named
    either overrides it (`m["layers"][i]["worldtile"]`/`["bank_bytes"]`, set by the manifest
    driver before this runs), which is what lets a screen-sized parallax background pick a
    smaller WorldTile than the ground layer beneath it without every layer paying for one
    setting that only suits the biggest.
    """
    combined = b"".join(layer["tiles"] for layer in m["layers"])
    cell_dict, index_of, idx_width = build_cell_dictionary(combined)
    m["cell_dict"] = cell_dict
    m["idx_width"] = idx_width

    for layer in m["layers"]:
        lw = layer.get("worldtile", worldtile)
        lb = layer.get("bank_bytes", bank_bytes)
        cols, rows, tiles = slice_worldtiles(layer, lw, index_of, idx_width)
        slot_bytes = (max(len(t["payload"]) for t in tiles) + 3) & ~3
        shift = bank_shift_for(slot_bytes, lb)

        layer["wt"] = (cols, rows, tiles)
        layer["worldtile"] = lw
        layer["slot_bytes"] = slot_bytes
        layer["bank_shift"] = shift
        layer["bank_count"] = (cols * rows + (1 << shift) - 1) >> shift

    m["bank_count"] = sum(layer["bank_count"] for layer in m["layers"])
    return m


def finish_map(m, atlas_assets, atlas_bytes, pal_table=b"",
               worldtile=WORLDTILE_DEFAULT, atlas_slots=None, resident=False,
               bank_bytes=WORLDTILE_BANK_BYTES, first_bank_asset=0,
               orient=ORIENT_BUTTONS_RIGHT, compress=False):
    """Build the map blob: a resident preamble, then WorldTile payloads to stream.

    `atlas_assets` maps each of the map's atlas names to its asset id, which is how the
    runtime finds the tilesets a map was authored against without a scene having to guess.
    Guessing wrong draws a map in another tileset's tiles -- a failure that looks like
    corrupted art rather than like a pairing mistake.

    No flag table any more -- collision/warp are read straight out of each cell's own
    u16 (see MAP_ROTATE's comment). That answers the same "collision for a cell whose
    atlas is not resident" case the flag table existed for, since the cell plane itself
    is exactly as resident as it always was; nothing about that changed. EXTENDED does
    still have a side table (see MAP_EXTENDED's comment) -- but it lives INSIDE each
    WorldTile's own payload, not resident here, since a tag is only ever asked about for
    a cell whose WorldTile (and so whose tag table) is already resident.

    The map's own resource holds only what stays resident. WorldTile payloads live in
    separate BANK resources -- see WORLDTILE_BANK_BYTES for why -- whose asset ids run
    consecutively from `first_bank_asset`, so bank i is simply that plus i.

    **Payloads are padded to the pool's slot stride inside a bank**, which buys two things
    for the cost of a few bytes at the map's edge. A WorldTile's position needs no lookup
    at all -- bank `i >> bank_shift`, offset `(i & mask) * slot_bytes` -- so the per-tile
    offset table leaves the resident preamble entirely. And a run of consecutive WorldTiles
    is then a contiguous read that lands in consecutive pool slots, which is what lets the
    runtime fetch a whole row, or a whole bank, in one call instead of one per tile.

    Layout after the 8-byte header (v14, M13: layer_count, primary_layer, warp_count,
    pad in bytes 3..6 -- a single grid's own w/h/worldtile do not mean anything once a
    map is 1..PNX_MAP_MAX_LAYERS of them):

        SHARED, once per map:
          u8  atlas_count, tile_px, flags (bit 0: palette remap, bit 1: compressed),
              atlas_slots
          u16 tile_total;  u16 dict_count;  u32 atlas_pool_bytes
          atlas table    atlas_count * (u16 asset id, u16 first_tile)
          atlas slots    (atlas_slots + 1) * u32, offsets into the atlas pool
        PER LAYER, layer_count times:
          u8  w, h, worldtile, wt_cols, wt_rows, bank_shift, want_slots
          u16 slot_bytes;  u16 first_bank_asset
          u8  parallax_pct_x, parallax_pct_y, wrap
          wt mask        wt_cols * wt_rows bytes, padded to 4
        SHARED, once more, after every layer:
          palette remap  tile_total bytes when present, padded to 4
          warps          warp_count * 5, padded to 4
          cell dict      dict_count * 2 bytes, padded to 4

    Every layer's fixed 14 bytes are written before ANY layer's wt_mask -- two
    contiguous zones, not interleaved -- because the runtime sizes its one read for the
    whole preamble from a worst-case-bounded probe before it knows any layer's own
    cols/rows; interleaving would mean it could not compute that size without already
    having read the very thing it is trying to size. See pnx_map_load's own comment
    (pnx_assets.c) for the reader this has to match exactly.

    `worldtile`/`atlas_slots`/`resident`/`bank_bytes` are the map's defaults, applied to
    every layer unless that layer's own spec overrode one (prepare_map already resolved
    `worldtile`/`bank_bytes` per layer; `atlas_slots`/`resident` are resolved here the
    same way). `first_bank_asset` is layer 0's; each later layer's own run starts right
    after the previous layer's own banks, so the whole map's bank asset ids stay one
    contiguous range even though each layer addresses its own with a fresh
    `first_bank_asset` (PnxMapLayer's own field) -- see the manifest driver's asset
    ordering loop, which allocates them in this same layer-then-bank-index order.

    Returns the map with `banks` set to the FLAT, layer-then-bank-index-ordered list of
    every layer's payload blobs, which is the order the caller must write them out in for
    `first_bank_asset` to mean what each layer's directory entry says it does.
    """
    tile_total = sum(count for _, _, count in m["atlas_table"])
    atlas_pool_slots = len(m["atlas_table"])

    # The atlas pool is shared by the WHOLE map (PnxMap's own comment) -- every layer draws
    # from the same table and the same pool -- so the auto-picked budget has to cover
    # whichever layer needs the most of it. This checks each layer's own worst window
    # independently and takes the largest; it does not attempt the joint "two different
    # layers on screen at once, needing two different atlases each" case, which needs
    # `atlas_slots` set explicitly if a project's layers are that far apart.
    needed = 0
    for layer in m["layers"]:
        cols, rows, tiles = layer["wt"]
        window = worldtile_window(m["tile_px"], layer["worldtile"])
        needed = max(needed, check_worldtile_windows(
            layer, cols, rows, tiles, window,
            atlas_slots if atlas_slots else atlas_pool_slots))
    if atlas_slots is None:
        atlas_slots = needed

    pool = atlas_pool_layout([atlas_bytes[n] for n, _, _ in m["atlas_table"]], atlas_slots)

    banks = []
    layer_dirs = bytearray()
    layer_masks = bytearray()
    bank_asset = first_bank_asset
    worldtiles_total = 0
    for layer in m["layers"]:
        cols, rows, tiles = layer["wt"]
        n = cols * rows
        worldtiles_total += n

        # The window is what the SCREEN can reach, so a layer smaller than one window is
        # fully resident and never streams -- the adaptive half of "one format, one code
        # path". `resident` defaults from the map; a layer may force its own.
        window = worldtile_window(m["tile_px"], layer["worldtile"])
        layer_resident = layer.get("resident", resident)
        wt_slots = n if layer_resident else min(n, window[0] * window[1])
        if wt_slots > 255:
            raise BuildError(
                f"map {m['name']!r}: `resident = true` wants a slot for each of its {n} "
                f"WorldTiles, past the 255 the format can address. A layer this size is "
                f"one that has to stream -- which is the answer the comparison was going "
                f"to give.")

        slot_bytes = layer["slot_bytes"]
        shift = layer["bank_shift"]
        per_bank = 1 << shift
        layer_banks = []
        for start in range(0, n, per_bank):
            body = bytearray()
            for t in tiles[start:start + per_bank]:
                body += t["payload"].ljust(slot_bytes, b"\0")
            # LZSS-compressed under `compress_maps` (M12): the BODY only, never the header
            # -- every bank is still validated by a header-only read before any body is
            # touched (pnx_map_load), and that check has to work whether or not the body
            # past it is compressed. Runtime decode reads the whole compressed body in one
            # call and treats the bank as an atomic streaming unit -- see pnx_lzss.h's own
            # comment for why that is the accepted trade.
            stored = lzss_compress(bytes(body)) if compress else bytes(body)
            # Stamped like every other blob, so a bank left over from a build in the other
            # orientation is refused rather than drawn sideways. The header is why a
            # tile's offset within its bank starts at HEADER_BYTES rather than at zero.
            layer_banks.append(blob_header(MAGIC_BANK, len(tiles[start:start + per_bank]),
                                           slot_bytes & 0xFF, slot_bytes >> 8, 0,
                                           orient=orient) + stored)

        layer["first_bank_asset"] = bank_asset
        layer["wt_slots"] = wt_slots
        layer["bank_shift"] = shift
        bank_asset += len(layer_banks)
        banks += layer_banks

        layer_dirs += bytes([layer["w"], layer["h"], layer["worldtile"], cols, rows,
                             shift, wt_slots])
        layer_dirs += slot_bytes.to_bytes(2, "little")
        layer_dirs += layer["first_bank_asset"].to_bytes(2, "little")
        layer_dirs += bytes([layer.get("parallax_pct_x", 255),
                             layer.get("parallax_pct_y", 255),
                             1 if layer.get("wrap") else 0])
        layer_masks += bytes(t["mask"] for t in tiles)

    cell_dict = m["cell_dict"]

    # Shared, once per map: see finish_map's own layout comment for the field order this
    # has to match exactly (pnx_map_load's shared-fixed-section read, pnx_assets.c).
    preamble = bytearray()
    # Bit 0: palette remap present. Bit 1 (M12): this map's banks are LZSS-compressed.
    preamble += bytes([len(m["atlas_table"]), m["tile_px"],
                       (1 if pal_table else 0) | (2 if compress else 0), atlas_slots])
    preamble += tile_total.to_bytes(2, "little")
    preamble += len(cell_dict).to_bytes(2, "little")
    preamble += pool[-1].to_bytes(4, "little")
    for name, first, _ in m["atlas_table"]:
        preamble += atlas_assets[name].to_bytes(2, "little") + first.to_bytes(2, "little")
    preamble += b"".join(o.to_bytes(4, "little") for o in pool)

    # Every layer's fixed 13 bytes, contiguously, THEN every layer's wt_mask,
    # contiguously -- see the layout comment above for why these are two zones rather
    # than interleaved even when layer_count is 1.
    preamble += layer_dirs
    preamble += pad4(bytes(layer_masks))

    # Shared again, after every layer.
    preamble += pad4(bytes(pal_table)) if pal_table else b""
    preamble += pad4(b"".join(bytes(x) for x in m["warps"]))
    preamble += pad4(b"".join(v.to_bytes(2, "little") for v in cell_dict))

    base = HEADER_BYTES + len(preamble)
    layer_count = len(m["layers"])
    # Header's four generic bytes (v14): layer_count, primary_layer, warp_count, pad.
    # primary_layer is always 0 -- compile_map_layers already reorders so the primary is
    # first, which is what lets every other layer-aware reader (this function, parse_map,
    # the editor) treat "layers[0]" and "the primary" as the same thing unconditionally.
    m["blob"] = blob_header(MAGIC_MAP, layer_count, 0, len(m["warps"]), 0,
                            orient=orient) + bytes(preamble)
    m["banks"] = banks
    m["resident_bytes"] = (base + sum(layer["wt_slots"] * layer["slot_bytes"]
                                      for layer in m["layers"]) + pool[-1])
    m["worldtiles"] = worldtiles_total
    m["wt_slots"] = sum(layer["wt_slots"] for layer in m["layers"])
    m["atlas_slots"] = atlas_slots
    m["atlas_pool_bytes"] = pool[-1]

    primary = m["layers"][0]
    pcols, prows, ptiles = primary["wt"]
    held = ("all resident" if primary["wt_slots"] == pcols * prows
            else f"{primary['wt_slots']} of {pcols * prows} resident")
    print(f"  map {m['name']}: {pcols}x{prows} WorldTiles of {primary['worldtile']} "
          f"({held})" + (f", {layer_count} layers" if layer_count > 1 else ""))
    if m.get("worldtile_note"):
        print(f"    worldtile {m['worldtile_note']}")
    if len(banks) > 1:
        biggest = max(len(b) for b in banks)
        print(f"    {len(banks)} banks total, largest {biggest:,} B -- a ranged read "
              f"seeks at most that, not {sum(len(b) for b in banks):,}")
    if len(m["atlas_table"]) > 1:
        whole = sum(atlas_bytes[n_] for n_, _, _ in m["atlas_table"])
        note = ("every atlas resident" if atlas_slots >= len(m["atlas_table"])
                else f"{whole - pool[-1]:,} B saved by streaming them")
        print(f"    {len(m['atlas_table'])} atlases, {tile_total} tile ids, "
              f"{atlas_slots} slot(s) in {pool[-1]:,} B -- {note}")

    # The counterfactual, whenever the map actually streams something. Streaming's whole
    # claim is a RAM number, and a claim of that shape is worth nothing without the figure
    # it is being compared against -- so the pipeline states both rather than leaving
    # "smaller than what?" to the reader. A map that holds everything anyway has no
    # counterfactual and gets no line.
    if m["wt_slots"] < worldtiles_total or atlas_slots < len(m["atlas_table"]):
        whole = (base + sum((cols * rows) * layer["slot_bytes"]
                            for layer in m["layers"] for cols, rows, _ in [layer["wt"]])
                 + sum(atlas_bytes[a] for a, _, _ in m["atlas_table"]))
        print(f"    resident {m['resident_bytes']:,} B against {whole:,} B held whole "
              f"-- {100 - 100 * m['resident_bytes'] // whole}% less")
    return m


def parse_map(blob, banks=()):
    """Read a packed map back into its parts, including a reassembled cell plane per layer.

    The inverse of finish_map, and the only place outside it that knows the layout. Tests
    assert on the plane rather than on byte offsets, and the editor reads a built map
    without re-deriving the slicing -- both of which used to mean a second copy of the
    format that could drift from this one.

    `banks` is the map's WorldTile bank blobs, ALL layers, in the flat order finish_map
    wrote them (layer 0's banks, then layer 1's, ...) -- the same order `build`'s asset
    ids were handed out in, so a caller gathering them by `PNX_ASSET_BANK_<map>_<i>` needs
    no layer-awareness of its own. Without them everything resident is still readable --
    dimensions, atlases, flags, warps -- and every layer's `cells`/`extended` come back
    empty, which is the honest answer for a caller that only has the map's own resource:
    EXTENDED tags live inside each WorldTile's own payload (see slice_worldtiles), same as
    cells, so both need the banks to be readable at all.

    Returns a dict with `layers` (one entry per declared layer, each shaped the way this
    function's single-layer return always was: w/h/worldtile/cols/rows/bank_shift/
    bank_count/first_bank_asset/parallax_pct_x/parallax_pct_y/wrap/masks/cells/extended/
    wt_slots/wt_slot_bytes) plus every one of the PRIMARY layer's own fields mirrored at the TOP
    level too, so `mp["cells"]`/`mp["w"]` etc. keep meaning exactly what they meant before
    multi-layer maps existed -- every caller that predates this (tests, the editor,
    cell_warp/cell_extended below) reads a single-layer map without any changes of its own.
    """
    if blob[:2] != MAGIC_MAP:
        raise BuildError(f"not a map blob: magic {blob[:2]!r}")
    if blob[2] != BLOB_VERSION:
        raise BuildError(f"map blob is v{blob[2]}, this pipeline writes v{BLOB_VERSION}")

    # v14 (M13): header's four generic bytes are layer_count/primary_layer/warp_count/pad.
    layer_count, primary_layer, warp_count = blob[3], blob[4], blob[5]
    if layer_count == 0:
        raise BuildError("map blob declares 0 layers")

    p = HEADER_BYTES
    atlas_count, tile_px, flags, atlas_slots = blob[p:p + 4]
    tile_total = int.from_bytes(blob[p + 4:p + 6], "little")
    dict_count = int.from_bytes(blob[p + 6:p + 8], "little")
    atlas_pool_bytes = int.from_bytes(blob[p + 8:p + 12], "little")
    idx_width = 1 if dict_count <= 256 else 2

    at = p + 12
    atlas_table = []
    for _ in range(atlas_count):
        atlas_table.append((int.from_bytes(blob[at:at + 2], "little"),
                            int.from_bytes(blob[at + 2:at + 4], "little")))
        at += 4

    pool = [int.from_bytes(blob[at + i * 4:at + i * 4 + 4], "little")
            for i in range(atlas_slots + 1)]
    at += (atlas_slots + 1) * 4

    # Every layer's own fixed 14 bytes, contiguously, THEN every layer's wt_mask,
    # contiguously -- see finish_map's own layout comment for why those are two separate
    # zones rather than interleaved, even when layer_count is 1.
    dirs = []
    for _ in range(layer_count):
        w, h, worldtile, cols, rows, bank_shift, wt_slots = blob[at:at + 7]
        slot_bytes = int.from_bytes(blob[at + 7:at + 9], "little")
        first_bank_asset = int.from_bytes(blob[at + 9:at + 11], "little")
        parallax_pct_x, parallax_pct_y, wrap = blob[at + 11], blob[at + 12], blob[at + 13]
        at += 14
        dirs.append({"w": w, "h": h, "worldtile": worldtile, "cols": cols, "rows": rows,
                     "bank_shift": bank_shift, "wt_slots": wt_slots,
                     "slot_bytes": slot_bytes, "first_bank_asset": first_bank_asset,
                     "parallax_pct_x": parallax_pct_x, "parallax_pct_y": parallax_pct_y,
                     "wrap": bool(wrap)})

    def take(n):
        nonlocal at
        chunk = blob[at:at + n]
        at += (n + 3) & ~3
        return chunk

    masks_all = take(sum(d["cols"] * d["rows"] for d in dirs))
    remap = take(tile_total) if flags & 1 else b""
    warps = take(warp_count * 5)
    dict_bytes = take(dict_count * 2)
    cell_dict = [int.from_bytes(dict_bytes[i * 2:i * 2 + 2], "little")
                for i in range(dict_count)]

    compressed = bool(flags & 2)

    mo = 0
    bank_offset = 0
    layers = []
    for d in dirs:
        w, h, worldtile = d["w"], d["h"], d["worldtile"]
        cols, rows, bank_shift, slot_bytes = d["cols"], d["rows"], d["bank_shift"], d["slot_bytes"]
        n = cols * rows
        per_bank = 1 << bank_shift
        bank_count = (n + per_bank - 1) >> bank_shift if n else 0
        masks = masks_all[mo:mo + n]
        mo += n
        layer_banks = banks[bank_offset:bank_offset + bank_count] if banks else ()
        bank_offset += bank_count

        # A WorldTile's home is arithmetic, not a lookup: payloads are padded to the slot
        # stride, so bank and offset both fall out of the index. A compressed bank (M12)
        # is decoded once per bank and cached here rather than per WorldTile -- the
        # atomic-unit cost the runtime accepts too (pnx_lzss.h's own comment).
        decoded_banks = {}

        def bank_body(bank_index, _banks=layer_banks, _per_bank=per_bank, _n=n,
                      _slot_bytes=slot_bytes, _decoded=decoded_banks):
            raw = _banks[bank_index]
            if not compressed:
                return raw
            if bank_index not in _decoded:
                tiles_in_bank = min(_per_bank, _n - bank_index * _per_bank)
                _decoded[bank_index] = lzss_decompress(
                    raw[HEADER_BYTES:], tiles_in_bank * _slot_bytes)
            return _decoded[bank_index]

        cells = bytearray(w * h * 2) if layer_banks else bytearray()
        extended = {}
        for i in range(n if layer_banks else 0):
            wx, wy = i % cols, i // cols
            bank_index = i >> bank_shift
            body_all = bank_body(bank_index)
            off = (0 if compressed else HEADER_BYTES) + (i & (per_bank - 1)) * slot_bytes
            body = body_all[off:off + slot_bytes]
            cw, ch = body[0], body[1]
            # Stored as dictionary indices (v13), decoded back to the raw entry word here
            # so every reader downstream of parse_map (cell_warp/cell_extended, tests, the
            # editor) keeps seeing the same `cells` shape it always has -- the index is
            # this format's concern, not theirs.
            for ly in range(ch):
                src = 2 + ly * cw * idx_width
                dst = ((wy * worldtile + ly) * w + wx * worldtile) * 2
                for lx in range(cw):
                    if idx_width == 1:
                        index = body[src + lx]
                    else:
                        index = int.from_bytes(body[src + lx * 2:src + lx * 2 + 2],
                                              "little")
                    entry = cell_dict[index]
                    cells[dst + lx * 2] = entry & 0xFF
                    cells[dst + lx * 2 + 1] = entry >> 8

            ext_at = 2 + cw * ch * idx_width
            ext_count = int.from_bytes(body[ext_at:ext_at + 2], "little")
            ext_at += 2
            for _ in range(ext_count):
                lx, ly, val = body[ext_at], body[ext_at + 1], body[ext_at + 2]
                extended[(wx * worldtile + lx, wy * worldtile + ly)] = val
                ext_at += 3

        layers.append({"w": w, "h": h, "worldtile": worldtile,
                       "cols": cols, "rows": rows, "bank_shift": bank_shift,
                       "bank_count": bank_count, "first_bank_asset": d["first_bank_asset"],
                       "parallax_pct_x": d["parallax_pct_x"],
                       "parallax_pct_y": d["parallax_pct_y"], "wrap": d["wrap"],
                       "masks": masks, "cells": bytes(cells), "extended": extended,
                       "wt_slots": d["wt_slots"], "wt_slot_bytes": slot_bytes})

    primary = layers[primary_layer] if primary_layer < len(layers) else layers[0]
    return {**primary,
            "tile_px": tile_px, "atlas_table": atlas_table, "palette": remap,
            "warps": [tuple(warps[i * 5:i * 5 + 5]) for i in range(warp_count)],
            "cell_dict": cell_dict, "dict_count": dict_count, "idx_width": idx_width,
            "atlas_slots": atlas_slots, "atlas_pool": pool,
            "atlas_pool_bytes": atlas_pool_bytes,
            "layers": layers, "layer_count": layer_count, "primary_layer": primary_layer}


def cell_warp(mp, x, y):
    """A parsed map's cell (x, y) -> whether it carries the warp bit.

    No cell_collision here any more: collision is a TILE property (see MAP_ROTATE's
    comment), read from the OWNING ATLAS's tile_flags (finish_atlas), not from a map's
    cells at all -- there is no per-cell value left to read.
    """
    i = (y * mp["w"] + x) * 2
    entry = mp["cells"][i] | (mp["cells"][i + 1] << 8)
    return bool(entry & MAP_WARP)


def cell_extended(mp, x, y):
    """A parsed map's cell (x, y) -> its EXTENDED tag value, or None if it carries none.

    `mp["extended"]` is already a sparse {(x, y): value} dict (parse_map), built from
    every WorldTile's own tag table -- so this is a lookup, not a scan, and empty for a
    map parsed without its banks (nothing resident to look inside).
    """
    return mp["extended"].get((x, y))


def check_warp_destinations(maps):
    """A warp landing inside a wall strands the player. Cross-map, so it runs last."""
    by_name = {m["name"]: m for m in maps}
    order = [m["name"] for m in maps]

    for m in maps:
        for (tx, ty, dest_idx, dtx, dty) in m["warps"]:
            dest = by_name[order[dest_idx]]
            if not (0 <= dtx < dest["w"] and 0 <= dty < dest["h"]):
                raise BuildError(
                    f"map {m['name']!r}: warp at {(tx, ty)} lands at {(dtx, dty)} in "
                    f"{dest['name']!r}, which is outside that {dest['w']}x{dest['h']} map")
            if dest["solid"][dty * dest["w"] + dtx]:
                raise BuildError(
                    f"map {m['name']!r}: warp at {(tx, ty)} lands at {(dtx, dty)} in "
                    f"{dest['name']!r}, which is a SOLID tile -- the player arrives "
                    f"inside a wall")
            if (dtx, dty) not in dest["reachable"]:
                raise BuildError(
                    f"map {m['name']!r}: warp at {(tx, ty)} lands at {(dtx, dty)} in "
                    f"{dest['name']!r}, which is walkable but sealed off from that "
                    f"map's start -- the player arrives in a closed pocket")


# --------------------------------------------------------------------------- dialog

def pack_dialog(dialogs, orient=ORIENT_BUTTONS_RIGHT):
    """All dialog in one blob: an offset index, then NUL-terminated pages.

    One resource rather than one per conversation, because a read costs ~29 us per CALL
    regardless of size. Text is content, so it belongs in the manifest rather than as
    string literals in C.

    Format after the header (entry count in byte 3):
        count * (u16 first_page, u16 page_count)
        total_pages * u16 offset into the text block
        text block
    """
    names = sorted(dialogs)
    pages, index = [], []
    for name in names:
        entry = dialogs[name]["pages"]
        if not entry:
            raise BuildError(f"dialog {name!r}: no pages")
        index.append((len(pages), len(entry)))
        pages.extend(entry)

    text, offsets = bytearray(), []
    for page in pages:
        offsets.append(len(text))
        text += page.encode("ascii", errors="replace") + b"\0"

    if len(text) > 0xFFFF:
        raise BuildError(f"dialog: {len(text)} bytes of text exceeds the u16 offset "
                         f"limit; split into multiple dialog assets")

    body = bytearray()
    for first, count in index:
        body += first.to_bytes(2, "little") + count.to_bytes(2, "little")
    for off in offsets:
        body += off.to_bytes(2, "little")
    body += text

    print(f"  dialog: {len(names)} entries, {len(pages)} pages, {len(text)} bytes text")
    blob = blob_header(MAGIC_DIALOG, len(names), orient=orient) + bytes(body)
    return {"names": names, "index": index, "blob": blob}


# -------------------------------------------------------------------------- samples

# One second of 16 kHz 8-bit PCM is 16,000 bytes. With ~70KB left after art, four seconds
# of recorded audio would consume the entire remaining content budget -- so samples are
# for short effects and nothing else. The cap is enforced rather than advised, because
# the failure is otherwise discovered as a bundle that will not ship.
SAMPLE_MAX_MS = 1500
SAMPLE_RATE = 16000


def pack_samples(root, specs, orient=ORIENT_BUTTONS_RIGHT):
    """Import small PCM effects: raw signed 8-bit mono, or a WAV we can read directly.

    Deliberately not a general audio importer. Long-form audio -- music beds, voice --
    does not fit this platform's budget at any quality, so the sequencer covers music and
    this covers footsteps and menu blips.
    """
    out = []
    for name in sorted(specs):
        spec = specs[name]
        path = os.path.join(root, spec["file"])
        if not os.path.exists(path):
            raise BuildError(f"sample {name!r}: missing file {path}")

        with open(path, "rb") as f:
            data = f.read()

        if data[:4] == b"RIFF":
            pcm, rate, bits, channels = parse_wav(data, name)
        else:
            pcm, rate = data, int(spec.get("rate", SAMPLE_RATE))
            bits, channels = 8, 1

        if channels != 1:
            raise BuildError(f"sample {name!r}: {channels} channels -- mono only, the "
                             f"device has one speaker")
        if bits == 16:
            # Down to 8-bit signed: the mixer accumulates in 8-bit and the speaker cannot
            # resolve more, so keeping 16 would double the cost for nothing.
            pcm = bytes(((int.from_bytes(pcm[i:i+2], "little", signed=True) >> 8) & 0xFF)
                        for i in range(0, len(pcm) - 1, 2))
        elif bits != 8:
            raise BuildError(f"sample {name!r}: {bits}-bit is not supported (8 or 16)")

        ms = len(pcm) * 1000 // max(rate, 1)
        if ms > SAMPLE_MAX_MS:
            raise BuildError(
                f"sample {name!r}: {ms}ms is longer than the {SAMPLE_MAX_MS}ms cap "
                f"({len(pcm):,} bytes). One second costs 16KB and roughly 70KB remains "
                f"after art -- use the sequencer for anything sustained.")

        loop = int(spec.get("loop_start", 0xFFFFFFFF))
        body = (rate.to_bytes(4, "little") + loop.to_bytes(4, "little") + pcm)
        blob = blob_header(MAGIC_SAMPLE, 0, 0, 0, 0, orient=orient) + body
        print(f"  sample {name}: {ms}ms, {rate}Hz, {len(pcm):,} bytes"
              + (f", loops at {loop}" if loop != 0xFFFFFFFF else ""))
        out.append({"name": name, "blob": blob, "out": f"sfx_{name}.bin", "ms": ms})
    return out


def parse_wav(data, name):
    """Enough WAV to read what an effects editor exports. Not a general parser."""
    if data[8:12] != b"WAVE":
        raise BuildError(f"sample {name!r}: RIFF file is not WAVE")
    pos, rate, bits, channels, pcm = 12, 0, 0, 0, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = int.from_bytes(body[0:2], "little")
            if fmt != 1:
                raise BuildError(f"sample {name!r}: only uncompressed PCM WAV is "
                                 f"supported (format {fmt})")
            channels = int.from_bytes(body[2:4], "little")
            rate = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little")
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if pcm is None:
        raise BuildError(f"sample {name!r}: WAV has no data chunk")
    # 8-bit WAV is UNSIGNED by spec; the mixer wants signed.
    if bits == 8:
        pcm = bytes((b - 128) & 0xFF for b in pcm)
    return pcm, rate, bits, channels


# ---------------------------------------------------------------------------- music

WAVEFORMS = ["square", "saw", "triangle", "noise"]
SEMITONES = {"c": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3, "e": 4, "f": 5,
             "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10,
             "b": 11}

MUSIC_NO_NOTE = 0
MUSIC_NOTE_OFF = 1


def parse_note(token, where):
    """'C4' -> MIDI 60. '.' holds, '-' releases.

    Note names rather than numbers, because a manifest is read by people. MIDI 60 tells
    you nothing; C4 tells you where it sits.
    """
    token = token.strip()
    if token in (".", "", "..."):
        return MUSIC_NO_NOTE
    if token in ("-", "off"):
        return MUSIC_NOTE_OFF

    m = re.fullmatch(r"([A-Ga-g][#b]?)(-?\d)", token)
    if not m:
        raise BuildError(f"{where}: {token!r} is not a note (expected e.g. C4, F#3, "
                         f"'.' to hold, '-' to release)")
    semi = SEMITONES[m.group(1).lower()]
    midi = (int(m.group(2)) + 1) * 12 + semi
    if not 2 <= midi <= 127:
        raise BuildError(f"{where}: {token!r} is outside the playable range")
    return midi


# One packed synth instrument. MIRRORED in src/pnx/audio/pnx_music.h
# (PNX_SYNTH_RECORD_BYTES) and decoded field by field in pnx_music_decode_instrument.
#
# Fixed width so the sequencer indexes instruments by number without a scan, and written
# out longhand at both ends rather than struct-copied: the packed form has no padding and
# a fixed endianness, while the C struct has whatever the compiler chose.
SYNTH_RECORD_BYTES = 48

LFO_TARGETS = ["off", "pitch", "volume", "duty", "cutoff"]
FILTER_MODES = ["off", "lowpass", "highpass", "bandpass"]


def pack_synth_instrument(spec, where):
    """One [[music.X.synth]] entry -> SYNTH_RECORD_BYTES of blob."""
    oscs = spec.get("osc", [])
    if not oscs:
        raise BuildError(f"{where}: a synth instrument needs at least one oscillator")
    if len(oscs) > 3:
        raise BuildError(f"{where}: {len(oscs)} oscillators, the voice has 3")

    lfo = str(spec.get("lfo_target", "off"))
    if lfo not in LFO_TARGETS:
        raise BuildError(f"{where}: unknown lfo_target {lfo!r} "
                         f"(known: {', '.join(LFO_TARGETS)})")
    mode = str(spec.get("filter", "off"))
    if mode not in FILTER_MODES:
        raise BuildError(f"{where}: unknown filter {mode!r} "
                         f"(known: {', '.join(FILTER_MODES)})")

    amp = spec.get("amp", {})
    cut = spec.get("cutoff", {})

    def u8(v, lo=0, hi=255):
        v = int(v)
        if not lo <= v <= hi:
            raise BuildError(f"{where}: {v} is outside {lo}..{hi}")
        return v

    r = bytearray(SYNTH_RECORD_BYTES)
    r[0] = len(oscs)
    r[1] = FILTER_MODES.index(mode)
    r[2] = u8(spec.get("cutoff_base", 128))
    r[3] = u8(spec.get("resonance", 0))
    r[4] = u8(spec.get("cutoff_env", 0))
    r[5] = LFO_TARGETS.index(lfo)
    r[6] = u8(spec.get("lfo_rate", 0))
    r[7] = u8(spec.get("lfo_depth", 0))
    pe = int(spec.get("pitch_env", 0))
    if not -1200 <= pe <= 1200:
        raise BuildError(f"{where}: pitch_env {pe} is outside -1200..1200 cents")
    r[8:10] = (pe & 0xFFFF).to_bytes(2, "little")
    r[10] = u8(spec.get("pitch_env_decay", 0))
    r[11] = u8(spec.get("reverb", 0))
    r[12] = u8(spec.get("chorus", 0))

    for base, env, default_sustain in ((14, amp, 180), (22, cut, 128)):
        r[base:base + 2] = int(env.get("attack", 5)).to_bytes(2, "little")
        r[base + 2:base + 4] = int(env.get("decay", 80)).to_bytes(2, "little")
        r[base + 4] = u8(env.get("sustain", default_sustain))
        r[base + 6:base + 8] = int(env.get("release", 120)).to_bytes(2, "little")

    for i, o in enumerate(oscs):
        wave = o.get("wave", "square")
        if wave not in WAVEFORMS:
            raise BuildError(f"{where}: oscillator {i} has unknown waveform {wave!r} "
                             f"(known: {', '.join(WAVEFORMS)})")
        det = int(o.get("detune", 0))
        if not -1200 <= det <= 1200:
            raise BuildError(f"{where}: oscillator {i} detune {det} is outside "
                             f"-1200..1200 cents")
        octv = int(o.get("octave", 0))
        if not -4 <= octv <= 4:
            raise BuildError(f"{where}: oscillator {i} octave {octv} is outside -4..4")
        at = 30 + i * 6
        r[at] = WAVEFORMS.index(wave)
        r[at + 1] = u8(o.get("volume", 200))
        r[at + 2:at + 4] = (det & 0xFFFF).to_bytes(2, "little")
        r[at + 4] = octv & 0xFF
        r[at + 5] = u8(o.get("duty", 128))
    return bytes(r)


def pack_music_names(man):
    """Song names only, for the id ordering, without recompiling them."""
    return [{"name": n} for n in sorted(man.get("music", {}))]


def pack_music(specs, orient=ORIENT_BUTTONS_RIGHT):
    """Compile [music.*] into pattern blobs.

    A row is two bytes per channel -- note and instrument -- so a 16-row four-channel
    pattern is 128 bytes. A whole song is a few hundred, against the tens of kilobytes a
    recorded loop would cost. That ratio is why the sequencer exists at all.
    """
    songs = []
    for name in sorted(specs):
        spec = specs[name]
        channels = int(spec.get("channels", 4))
        if channels != 4:
            raise BuildError(f"music {name!r}: the sequencer has exactly 4 channels")

        instruments = spec.get("instrument", [])
        if not instruments:
            raise BuildError(f"music {name!r}: no instruments defined")
        if len(instruments) > 255:
            raise BuildError(f"music {name!r}: too many instruments")

        inst_bytes = bytearray()
        for i, ins in enumerate(instruments):
            wave = ins.get("wave", "square")
            if wave not in WAVEFORMS:
                raise BuildError(f"music {name!r}: instrument {i} has unknown waveform "
                                 f"{wave!r} (known: {', '.join(WAVEFORMS)})")
            inst_bytes += bytes([WAVEFORMS.index(wave)])
            inst_bytes += int(ins.get("attack", 5)).to_bytes(2, "little")
            inst_bytes += int(ins.get("decay", 50)).to_bytes(2, "little")
            inst_bytes += bytes([int(ins.get("sustain", 180)) & 0xFF])
            inst_bytes += int(ins.get("release", 100)).to_bytes(2, "little")

        patterns = spec.get("pattern", [])
        if not patterns:
            raise BuildError(f"music {name!r}: no patterns defined")

        rows_per = None
        pattern_bytes = bytearray()
        for pi, pat in enumerate(patterns):
            rows = pat.get("rows", [])
            if rows_per is None:
                rows_per = len(rows)
            elif len(rows) != rows_per:
                raise BuildError(f"music {name!r}: pattern {pi} has {len(rows)} rows, "
                                 f"pattern 0 has {rows_per} -- all must match")
            for ri, row in enumerate(rows):
                where = f"music {name!r} pattern {pi} row {ri}"
                cells = row.split()
                if len(cells) != channels:
                    raise BuildError(f"{where}: {len(cells)} cells for {channels} "
                                     f"channels")
                for cell in cells:
                    if ":" in cell:
                        note_tok, inst_tok = cell.split(":", 1)
                        inst = int(inst_tok)
                    else:
                        note_tok, inst = cell, 0
                    if inst >= len(instruments):
                        raise BuildError(f"{where}: instrument {inst} does not exist")
                    pattern_bytes += bytes([parse_note(note_tok, where), inst])

        order = spec.get("order", list(range(len(patterns))))
        for o in order:
            if o >= len(patterns):
                raise BuildError(f"music {name!r}: order references pattern {o}, but "
                                 f"only {len(patterns)} exist")

        tempo = int(spec.get("tempo", 120))
        body = (tempo.to_bytes(2, "little") + bytes([channels, 0])
                + bytes(inst_bytes) + pad4(bytes(order)) + bytes(pattern_bytes))

        # Optional synth table, appended after the patterns. Additive: a song without one
        # is byte-identical to what this pipeline produced before synth instruments
        # existed, so nothing already built had to be rebuilt.
        synth = spec.get("synth", [])
        if synth:
            if len(synth) > 255:
                raise BuildError(f"music {name!r}: too many synth instruments")
            if len(synth) != len(instruments):
                raise BuildError(
                    f"music {name!r}: {len(synth)} synth instruments against "
                    f"{len(instruments)} plain ones -- a row names ONE instrument index, "
                    f"so the two tables have to line up or a note would play a different "
                    f"sound depending on which table it resolved through")
            body += bytes([len(synth), SYNTH_RECORD_BYTES])
            for i, ins in enumerate(synth):
                body += pack_synth_instrument(ins, f"music {name!r} synth {i}")

        blob = blob_header(MAGIC_MUSIC, len(patterns), len(order), rows_per,
                           len(instruments), orient=orient) + body
        print(f"  music {name}: {len(patterns)} patterns x {rows_per} rows, "
              f"{len(instruments)} instruments"
              + (f" (+{len(synth)} synth)" if synth else "")
              + f", {tempo}bpm, {len(blob)} bytes")
        songs.append({"name": name, "blob": blob, "out": f"music_{name}.bin",
                      "synth": len(synth),
                      # Named instruments reach the header the way tile roles do, so C can
                      # say MUSIC_THEME_INST_BASS instead of 1. Unnamed ones emit nothing;
                      # the index still works and nothing is invented.
                      "instrument_names": [(i, ins["name"])
                                           for i, ins in enumerate(instruments)
                                           if ins.get("name")]})
    return songs


# ----------------------------------------------------------------------------- font
#
# A font is the one asset nobody can author by hand at this scale, and it is also the one
# most likely to be illegible after import: at 12px hinting dominates, and a typeface that
# reads beautifully on a page turns to mush. So the pipeline's job here is not merely to
# convert a TTF -- it is to produce metrics the editor can show at the target size, and to
# fail loudly on the things that are silently wrong.
#
# Glyphs are 1bpp or 2bpp, NOT the 4bpp every other asset uses. Text has one colour, so
# the 4 bits an atlas spends naming a palette entry would be 3 bits of waste per pixel.
# 1bpp is `ink or nothing`; 2bpp adds two intermediate coverage levels the blitter blends
# against whatever is already on screen.

FONT_ASCII_FIRST = 32       # space
FONT_ASCII_LAST = 126       # tilde
FONT_GLYPH_ENTRY = 8        # bytes per glyph index entry
FONT_ABSENT = 0xFF          # codepoint map entry for a glyph the font does not carry

# The map is byte-indexed and 0xFF means absent, so 255 is the ceiling on glyph count.
# ASCII needs 95, so this only binds if someone tries to smuggle in a larger script.
FONT_MAX_GLYPHS = 255


def derive_charset(man, spec, name):
    """The set of characters a font must carry.

    `charset = "auto"` takes it from the content the pipeline already reads -- every
    dialog page -- rather than making the author restate it. That matters because the
    alternative is shipping all 95 printable ASCII glyphs whether or not the game uses
    them, and a dialogue-sized face wastes real budget on characters no page contains.

    `extra` exists because not all text is content: damage numbers, "HP", a percent sign
    in a menu appear in no dialog page and would otherwise be missing at runtime, which
    presents as gaps in a string rather than as an error.
    """
    want = spec.get("charset", "auto")
    origin = {}

    if want == "auto":
        chars = set()
        for entry_name, entry in sorted(man.get("dialog", {}).items()):
            for i, page in enumerate(entry.get("pages", [])):
                for ch in page:
                    chars.add(ch)
                    origin.setdefault(ch, f"dialog {entry_name!r} page {i}")
    elif want == "ascii":
        chars = {chr(c) for c in range(FONT_ASCII_FIRST, FONT_ASCII_LAST + 1)}
    elif isinstance(want, str):
        chars = set(want)
        for ch in chars:
            origin.setdefault(ch, f"font {name!r} charset")
    else:
        raise BuildError(f"font {name!r}: charset must be \"auto\", \"ascii\", or a "
                         f"string of characters, not {want!r}")

    for ch in spec.get("extra", ""):
        chars.add(ch)
        origin.setdefault(ch, f"font {name!r} extra")

    # Space is always carried. Its advance is what separates words, and a font without it
    # renders every line as one run.
    chars.add(" ")
    chars.discard("\n")

    bad = sorted(c for c in chars if not FONT_ASCII_FIRST <= ord(c) <= FONT_ASCII_LAST)
    if bad:
        where = "; ".join(f"{c!r} (U+{ord(c):04X}) from {origin.get(c, 'the manifest')}"
                          for c in bad[:4])
        raise BuildError(
            f"font {name!r}: {len(bad)} character(s) outside printable ASCII: {where}"
            + ("; ..." if len(bad) > 4 else "")
            + ". Only ASCII 32-126 is supported -- a non-Latin script needs a wider "
              "codepoint map than the byte-indexed one this format uses. Replace typographic "
              "quotes and dashes with their ASCII equivalents.")

    if len(chars) > FONT_MAX_GLYPHS:
        raise BuildError(f"font {name!r}: {len(chars)} glyphs exceeds the {FONT_MAX_GLYPHS} "
                         f"the codepoint map can index")

    return sorted(chars), origin


def quantise_coverage(value, threshold):
    """One greyscale sample to ink or not.

    A plain cutoff, and `threshold` is the whole story -- which is why the editor puts a
    slider on it. Move it 20 either way at 12px and stems appear or vanish; there is no
    correct value, only a legible one for a given typeface.

    Used to also spread a depth=2 sample over 4 antialiased levels -- that meaning of
    depth=2 is gone, replaced entirely by baked outline/fill glyphs (see pack_font's own
    comment on `rasterise_glyph_styled`). No font in pebblnyx ever shipped with the old
    depth=2 antialiasing, so this dropped the branch rather than carry it as unreachable.
    """
    return 1 if value >= threshold else 0


def _trim_to_ink(levels, pad):
    """Shared by rasterise_glyph and rasterise_glyph_styled: trim a full padded level
    grid down to the bounding box of its own non-zero cells, and report the bearing
    (offset from the anchor point every glyph is drawn at) that trim implies.

    Quantisation/level-assignment has to happen before this runs, deliberately -- trimming
    the raw greyscale bbox first would keep rows whose every sample falls below whatever
    threshold applies, padding the glyph with blank lines that cost bytes and shift the
    bearing.
    """
    h = len(levels)
    w = len(levels[0]) if h else 0
    rows = [y for y in range(h) if any(levels[y])]
    cols = [x for x in range(w) if any(levels[y][x] for y in range(h))]
    if not rows or not cols:
        return None, 0, 0            # no ink: a space, or a glyph the face lacks

    top, bottom = rows[0], rows[-1]
    left, right = cols[0], cols[-1]
    trimmed = [row[left:right + 1] for row in levels[top:bottom + 1]]
    return trimmed, left - pad, pad - top


def rasterise_glyph(pil_font, ch, threshold, pad):
    """Render one character crisp (ink or not) and trim it to its inked box.

    Returns levels as a list of rows, plus metrics measured FROM THE BASELINE, which is
    the only origin that keeps two different fonts aligned on one line -- the same
    contract rasterise_glyph_styled keeps, so pack_font's own packing/dedup code never
    needs to know which one produced a given glyph's rows.
    """
    img = Image.new("L", (pad * 3, pad * 3), 0)
    ImageDraw.Draw(img).text((pad, pad), ch, fill=255, font=pil_font, anchor="ls")

    px = img.load()
    w, h = img.size
    levels = [[quantise_coverage(px[x, y], threshold) for x in range(w)]
              for y in range(h)]
    return _trim_to_ink(levels, pad)


def rasterise_glyph_styled(pil_font, ch, threshold, outline_width, pad):
    """Crisp fill plus a dilated outline ring, baked as index levels -- 0 transparent, 1
    outline, 2 fill A -- rather than antialiased coverage. This is font depth=2's whole
    meaning now: a small indexed palette per glyph (outline / fill A / fill B), drawn with
    a caller-supplied 3-colour palette at runtime (pnx_text_draw_outlined/
    pnx_text_draw_gradient_outlined), not one ink colour blended at partial coverage.

    Level 3 (fill B) is deliberately never produced here -- auto-baking only ever
    generates a solid single-colour fill plus its outline. A second fill colour (a
    two-tone outline, a split gradient across a letter, whatever) is paint-only: a human
    editing the glyph by hand (`glyph_overrides`, pack_font's own handling of it) is a
    better tool for "does this look good" than a pipeline heuristic guessing a split
    point, per direct ask ("letting the user edit the glyphs... will be easier than
    trying to set up tools to auto generate colorations").

    Dilation is 4-connected (Manhattan distance <= outline_width), not 8-connected/
    circular -- plenty at the glyph sizes this ships at (a handful of px), and cheaper.
    """
    img = Image.new("L", (pad * 3, pad * 3), 0)
    ImageDraw.Draw(img).text((pad, pad), ch, fill=255, font=pil_font, anchor="ls")

    px = img.load()
    w, h = img.size
    ink = [[px[x, y] >= threshold for x in range(w)] for y in range(h)]

    outline = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if ink[y][x]:
                continue
            for dy in range(-outline_width, outline_width + 1):
                span = outline_width - abs(dy)
                ny = y + dy
                if not 0 <= ny < h:
                    continue
                for dx in range(-span, span + 1):
                    nx = x + dx
                    if 0 <= nx < w and ink[ny][nx]:
                        outline[y][x] = True
                        break
                if outline[y][x]:
                    break

    levels = [[2 if ink[y][x] else (1 if outline[y][x] else 0) for x in range(w)]
              for y in range(h)]
    return _trim_to_ink(levels, pad)


def parse_glyph_override_ascii(text, where, ch):
    """A hand-painted glyph override -- one character per cell, the same ASCII-art
    convention parse_collision_mask_ascii already uses ('.'/'#'), extended to the four
    levels a styled glyph carries: '.' transparent, 'o' outline, '#' fill A, '%' fill B.

    A full replacement of the glyph's level grid, not a diff against the auto-bake --
    simpler to reason about (what you see in the editor's paint canvas is exactly what
    ships), and it is the editor's own job to seed a fresh override from the current
    auto-bake before the user starts painting, not this function's.
    """
    rows = text.strip("\n").split("\n")
    if not rows or not rows[0]:
        raise BuildError(f"{where}: glyph {ch!r}'s glyph_overrides entry is empty")
    w = len(rows[0])
    if any(len(r) != w for r in rows):
        raise BuildError(f"{where}: glyph {ch!r}'s glyph_overrides rows are not all the same "
                         f"width ({[len(r) for r in rows]})")
    sym = {".": 0, "o": 1, "#": 2, "%": 3}
    levels = []
    for j, row in enumerate(rows):
        line = []
        for i, c in enumerate(row):
            if c not in sym:
                raise BuildError(f"{where}: glyph {ch!r}'s glyph_overrides has {c!r} at "
                                 f"{i},{j} -- only '.' (transparent), 'o' (outline), "
                                 f"'#' (fill A) and '%' (fill B) are allowed")
            line.append(sym[c])
        levels.append(line)
    return levels


def pack_glyph_rows(rows, depth):
    """Levels to packed bytes, MSB-first, one row at a time.

    Row-aligned rather than a continuous bit stream, so the blitter can index a row by
    multiply-and-add instead of tracking a bit offset across rows. Costs at most 7 bits
    per row and makes the inner loop a great deal simpler.
    """
    out = bytearray()
    for row in rows:
        acc, bits = 0, 0
        for level in row:
            acc = (acc << depth) | level
            bits += depth
            if bits == 8:
                out.append(acc)
                acc, bits = 0, 0
        if bits:
            out.append((acc << (8 - bits)) & 0xFF)
    return bytes(out)


def pack_font(root, spec, man, orient=ORIENT_BUTTONS_RIGHT):
    """Rasterise a TTF/OTF at a pixel size and pack it as a PF blob.

    Glyph bitmaps rotate with the rest of the content, so `w` and `h` in a glyph entry are
    the dimensions AS STORED. The metrics beside them -- advance, bearing_x, bearing_y --
    stay typographic: along the baseline, and up from it. Keeping those two frames apart
    is what lets pnx_text lay out a line with one set of arithmetic and blit the result
    with another, and it is why a vertical script would need no new fields.

    Format after the header (depth, line_height, baseline, advance axis in bytes 3-6):
        u16 glyph_count, u16 bitmap_bytes
        u8  first_cp, last_cp, fallback_index, space_advance
        glyph_count * 8: u16 offset, u8 w, u8 h, u8 advance, s8 bearing_x, s8 bearing_y, pad
        (last_cp - first_cp + 1) bytes: codepoint -> glyph index, 0xFF absent
        bitmap block
    """
    if Image is None:
        raise BuildError("Pillow is required for fonts: pip install pillow")

    name = spec.get("name")
    if not name:
        raise BuildError("a [[font]] block has no name")
    if "source" not in spec:
        raise BuildError(f"font {name!r}: no source -- point it at a .ttf or .otf")

    path = os.path.join(root, spec["source"])
    if not os.path.exists(path):
        raise BuildError(f"font {name!r}: missing source: {path}")

    # Rasterised glyphs are a redistribution of the typeface even though the outlines
    # stay behind, and a bundle that ships them without a licence is a problem the author
    # discovers from a rights holder rather than from a build. Cheap to record, so it is
    # required rather than suggested.
    if not spec.get("license"):
        raise BuildError(
            f"font {name!r}: no license. Rasterising {os.path.basename(path)} into the "
            f"bundle redistributes the typeface, so the manifest has to record the terms "
            f'-- e.g. license = "SIL OFL 1.1", or "public domain". This is checked because '
            f"the alternative is finding out later.")

    depth = int(spec.get("depth", 1))
    if depth not in (1, 2):
        raise BuildError(f"font {name!r}: depth must be 1 (crisp) or 2 (baked outline + "
                         f"fill, see outline_width/glyph_overrides), not {depth}")

    # outline_width/glyph_overrides only mean something at depth=2 -- reject them at
    # depth=1 rather than silently ignore, same posture every other manifest field this
    # pipeline validates takes (a typo that goes silently unused is a bug report from
    # someone who assumed it was working).
    if depth == 2:
        outline_width = int(spec.get("outline_width", 1))
        if outline_width < 1:
            raise BuildError(f"font {name!r}: outline_width must be >= 1, not "
                             f"{outline_width}")
        glyph_overrides = spec.get("glyph_overrides", {})
        if not isinstance(glyph_overrides, dict):
            raise BuildError(f"font {name!r}: glyph_overrides must be a table of "
                             f"character -> mask string, e.g. [font.glyph_overrides]")
    else:
        outline_width = None
        glyph_overrides = {}
        if "outline_width" in spec:
            raise BuildError(f"font {name!r}: outline_width only means something at "
                             f"depth=2 (baked outline glyphs) -- this font is depth=1")
        if "glyph_overrides" in spec:
            raise BuildError(f"font {name!r}: glyph_overrides only means something at "
                             f"depth=2 (baked outline glyphs) -- this font is depth=1")

    size = int(spec.get("size", 12))
    if not 4 <= size <= 64:
        raise BuildError(f"font {name!r}: size {size} is outside 4-64 px")

    # Default black point depends on depth: 128 is the natural cutoff for a hard
    # threshold, but it would discard every antialiased sample at depth 2 and produce a
    # font indistinguishable from depth 1 at twice the bytes.
    threshold = int(spec.get("threshold", 128 if depth == 1 else 24))
    if not 1 <= threshold <= 254:
        raise BuildError(f"font {name!r}: threshold {threshold} is outside 1-254")

    tracking = int(spec.get("tracking", 0))
    overrides = {str(k): int(v) for k, v in spec.get("advance", {}).items()}

    try:
        pil_font = ImageFont.truetype(path, size)
    except OSError as e:
        raise BuildError(f"font {name!r}: cannot open {path} at {size}px: {e}") from None

    # A second source font for a subset of the charset -- one packed font whose glyphs
    # come from two different typefaces (e.g. digits from one face, letters from
    # another), rather than a game splitting strings at draw time to switch fonts
    # mid-string. "Just combine all the rendered glyphs into a single font" (the ask,
    # after an earlier draft did exactly that render-time split and it worked but read
    # as more machinery than the problem needed). Each character rasterises from
    # `overlay_source` if it's in `overlay_charset`, `source` otherwise -- no other
    # difference between the two: same size/depth/threshold/tracking apply to both, so
    # the two faces still sit on one consistent baseline/line-height (this font's own
    # `ascent`/`descent`, read from `pil_font` -- the PRIMARY source -- alone, same as
    # always; a glyph from the overlay face just has to fit within that box, same as any
    # other glyph here already does via rasterise_glyph's own bearing/clipping).
    overlay_path = spec.get("overlay_source")
    overlay_charset = set(spec.get("overlay_charset", ""))
    overlay_pil_font = None
    if overlay_path:
        full_overlay_path = os.path.join(root, overlay_path)
        if not os.path.exists(full_overlay_path):
            raise BuildError(f"font {name!r}: missing overlay_source: {full_overlay_path}")
        if not spec.get("overlay_license"):
            raise BuildError(
                f"font {name!r}: overlay_source given but no overlay_license -- same "
                f"redistribution rule license's own check enforces for `source`, applies "
                f"here too: rasterising {os.path.basename(full_overlay_path)} into the "
                f"bundle redistributes THAT typeface as well.")
        try:
            overlay_pil_font = ImageFont.truetype(full_overlay_path, size)
        except OSError as e:
            raise BuildError(f"font {name!r}: cannot open {full_overlay_path} at "
                             f"{size}px: {e}") from None

    ascent, descent = pil_font.getmetrics()
    chars, origin = derive_charset(man, spec, name)

    glyphs, bitmaps, dedup = [], bytearray(), {}
    for ch in chars:
        src_font = overlay_pil_font if (overlay_pil_font and ch in overlay_charset) else pil_font
        pad = max(size, 8) * 2

        if depth == 2:
            rows, bearing_x, bearing_y = rasterise_glyph_styled(src_font, ch, threshold,
                                                                outline_width, pad)
            if ch in glyph_overrides:
                override_levels = parse_glyph_override_ascii(glyph_overrides[ch],
                                                              f"font {name!r}", ch)
                auto_h = len(rows) if rows else 0
                auto_w = len(rows[0]) if rows else 0
                ov_h = len(override_levels)
                ov_w = len(override_levels[0]) if ov_h else 0
                if (ov_w, ov_h) != (auto_w, auto_h):
                    raise BuildError(
                        f"font {name!r}: glyph {ch!r}'s glyph_overrides is {ov_w}x{ov_h}, but "
                        f"the current auto-bake (source/size/outline_width) is "
                        f"{auto_w}x{auto_h} -- the override REPLACES the auto-baked "
                        f"level grid, not a diff against it, so it has to match "
                        f"whatever the auto-bake currently produces. Re-paint from the "
                        f"editor, which always starts a fresh override from the current "
                        f"auto-bake, rather than hand-editing saved text after a font "
                        f"setting changed underneath it.")
                rows = override_levels
        else:
            rows, bearing_x, bearing_y = rasterise_glyph(src_font, ch, threshold, pad)

        advance = overrides.get(ch, int(round(src_font.getlength(ch)))) + tracking
        if advance < 0:
            raise BuildError(f"font {name!r}: advance for {ch!r} is negative ({advance}) "
                             f"-- tracking = {tracking} is too small")

        if rows is None:
            glyphs.append({"ch": ch, "offset": 0, "w": 0, "h": 0, "advance": advance,
                           "bx": 0, "by": 0})
            continue

        rows = rotate_levels(rows, orient)
        w, h = len(rows[0]), len(rows)
        packed = pack_glyph_rows(rows, depth)

        # Identical bitmaps share one copy. Cheap, and it fires more than it looks like it
        # would: at small sizes 'l' and 'I', 'O' and '0', or a comma and an apostrophe
        # frequently rasterise to the same pixels even when the outlines differ.
        key = (w, h, packed)
        if key in dedup:
            offset = dedup[key]
        else:
            offset = len(bitmaps)
            dedup[key] = offset
            bitmaps += packed

        for field, value in (("bearing_x", bearing_x), ("bearing_y", bearing_y)):
            if not -128 <= value <= 127:
                raise BuildError(f"font {name!r}: {field} for {ch!r} is {value}, outside "
                                 f"a signed byte -- the face is not usable at {size}px")
        if w > 255 or h > 255:
            raise BuildError(f"font {name!r}: glyph {ch!r} rasterises to {w}x{h}, "
                             f"which exceeds the 255px a byte can hold")

        glyphs.append({"ch": ch, "offset": offset, "w": w, "h": h, "advance": advance,
                       "bx": bearing_x, "by": bearing_y})

    if len(bitmaps) > 0xFFFF:
        raise BuildError(f"font {name!r}: {len(bitmaps)} bytes of glyph bitmaps exceeds "
                         f"the u16 offset limit; use a smaller size or a narrower charset")

    inked = [g for g in glyphs if g["w"]]
    if not inked:
        raise BuildError(f"font {name!r}: every glyph rasterised blank at {size}px with "
                         f"threshold {threshold}. A bitmap-only face renders nothing "
                         f"except at its designed size; try that size, or lower the "
                         f"threshold.")

    first_cp, last_cp = ord(chars[0]), ord(chars[-1])
    index_of = {g["ch"]: i for i, g in enumerate(glyphs)}

    # The fallback is what an unmapped codepoint draws. '?' if the font carries one,
    # otherwise the first inked glyph -- anything visible beats a silent gap, because a
    # gap reads as a layout bug rather than as a missing character.
    fallback = index_of.get("?", index_of[inked[0]["ch"]])
    space_advance = glyphs[index_of[" "]]["advance"]

    body = bytearray()
    body += len(glyphs).to_bytes(2, "little") + len(bitmaps).to_bytes(2, "little")
    body += bytes([first_cp, last_cp, fallback, space_advance & 0xFF])
    for g in glyphs:
        body += g["offset"].to_bytes(2, "little")
        body += bytes([g["w"], g["h"], g["advance"] & 0xFF,
                       g["bx"] & 0xFF, g["by"] & 0xFF, 0])
    body += bytes(index_of.get(chr(cp), FONT_ABSENT)
                  for cp in range(first_cp, last_cp + 1))
    body += bitmaps

    line_height = ascent + descent
    advance_axis = ORIENT_ADVANCE[orient]
    blob = (blob_header(MAGIC_FONT, depth, line_height, ascent, advance_axis,
                        orient=orient) + bytes(body))

    saved = sum(g["w"] for g in inked) and (
        len(glyphs) - len({(g["w"], g["h"], g["offset"]) for g in inked}))
    print(f"  font {name}: {len(glyphs)} glyphs at {size}px depth {depth}, "
          f"{line_height}px line / {ascent}px baseline, {len(bitmaps)} B bitmaps"
          + (f", {saved} shared" if saved else "")
          + f", {len(blob)} bytes total")

    return {"name": name, "blob": blob, "out": spec.get("out", f"font_{name}.bin"),
            "glyphs": glyphs, "chars": chars, "origin": origin,
            "depth": depth, "size": size, "threshold": threshold,
            "tracking": tracking, "line_height": line_height, "baseline": ascent,
            "advance_axis": advance_axis,
            "license": spec["license"], "source": spec["source"],
            "overlay_source": overlay_path, "overlay_license": spec.get("overlay_license"),
            "overlay_charset": "".join(sorted(overlay_charset)),
            "outline_width": outline_width, "glyph_overrides": sorted(glyph_overrides),
            "bitmap_bytes": len(bitmaps)}


# --------------------------------------------------------------------------- scenes

def build_scenes(man, asset_index, maps=(), orient=ORIENT_BUTTONS_RIGHT):
    """Compile [scene.*] into a table of asset-id lists.

    A scene is the only load point the framework has, so it is the right unit to declare
    and the right unit to budget. Without this the set of assets a scene needs lives in
    C, which means the pipeline cannot check it, cannot report its cost, and cannot warn
    that it will not fit.

    Format after the header (scene count in byte 3):
        count * (u16 first_entry, u8 entry_count, u8 map_or_255)
        entries: u16 asset id each
    """
    specs = man.get("scene", {})
    if not specs:
        return None

    names = sorted(specs)
    index, entries = [], []

    for name in names:
        spec = specs[name]
        ids = []

        for kind, key in (("ATLAS", "atlases"), ("SPRITE", "sprites"),
                         ("NINE_SLICE", "nine_slices"), ("FONT", "fonts")):
            for ref in spec.get(key, []):
                handle = f"PNX_ASSET_{kind}_{c_ident(ref)}"
                if handle not in asset_index:
                    raise BuildError(f"scene {name!r}: no {kind.lower()} named {ref!r} "
                                     f"(known: {', '.join(sorted(asset_index))})")
                ids.append(asset_index[handle])

        if spec.get("dialog"):
            if "PNX_ASSET_DIALOG_DIALOG" not in asset_index:
                raise BuildError(f"scene {name!r}: asks for dialog, but the manifest "
                                 f"defines none")
            ids.append(asset_index["PNX_ASSET_DIALOG_DIALOG"])

        map_id = 255
        if "map" in spec:
            handle = f"PNX_ASSET_MAP_{c_ident(spec['map'])}"
            if handle not in asset_index:
                raise BuildError(f"scene {name!r}: no map named {spec['map']!r}")
            map_id = asset_index[handle]
            ids.append(map_id)

        if not ids:
            raise BuildError(f"scene {name!r}: loads nothing")

        # A map owns the tilesets it draws with and streams them itself, so a scene must
        # not also load them: the scene's copy is resident for the whole scene, and the
        # map's pool holds a second one. That is not a mismatch the runtime can see -- it
        # just quietly costs twice the atlas -- so it is caught here.
        if "map" in spec:
            for m in maps:
                if m["name"] != spec["map"]:
                    continue
                clash = [a for a in m["atlases"]
                         if asset_index.get(f"PNX_ASSET_ATLAS_{c_ident(a)}") in ids]
                if clash:
                    keep = [a for a in spec.get("atlases", []) if a not in clash]
                    raise BuildError(
                        f"scene {name!r}: map {m['name']!r} already streams "
                        f"{', '.join(clash)}, so listing it in `atlases` loads a second "
                        f"resident copy. Drop it: "
                        + (f"atlases = {keep!r}" if keep else "remove the `atlases` line"))

        index.append((len(entries), len(ids), map_id))
        entries.extend(ids)

    body = bytearray()
    for first, count, map_id in index:
        body += first.to_bytes(2, "little") + bytes([count, map_id])
    for asset_id in entries:
        body += asset_id.to_bytes(2, "little")

    print(f"  scenes: {len(names)} declared, {len(entries)} asset references")
    return {"names": names, "index": index, "entries": entries,
            "blob": blob_header(MAGIC_SCENES, len(names), orient=orient) + bytes(body)}


def report_scene_budgets(scenes, sizes, palette_bytes_total, map_resident=None):
    """Per-scene resident cost -- the number that decides the scene arena size.

    Total resource size says what ships; this says what has to be in RAM at once, which
    is the constraint that actually bites. Palettes are counted into every scene because
    they load before anything else does.

    A map costs its resident preamble plus its two pools, NOT its blob size: the whole
    point of WorldTiles is that a map on disk and a map in RAM are different numbers.
    """
    if not scenes:
        return
    map_resident = map_resident or {}
    print("\nscene residency (what must fit in the scene arena at once)")
    worst, worst_name = 0, ""
    for i, name in enumerate(scenes["names"]):
        first, count, _ = scenes["index"][i]
        total = palette_bytes_total + sum(map_resident.get(a, sizes.get(a, 0))
                                          for a in scenes["entries"][first:first + count])
        if total > worst:
            worst, worst_name = total, name
        print(f"  {name:<16} {count:>2} assets  {total:>7,} B")
    print(f"  {'WORST':<16} {worst_name:<11} {worst:>7,} B "
          f"<- minimum scene arena")


# --------------------------------------------------------------------------- output

def c_ident(name):
    return "".join(ch if ch.isalnum() else "_" for ch in name).upper()


def write_blob(path, blob):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


def bw_variant_path(out):
    """`tiles.bin` -> `tiles~bw.bin`. The SDK resolves tags by globbing the declared
    file's own directory for `<stem>*<ext>` (docs/PORTING.md), so this is the entire
    naming contract -- same directory, same base name, `~bw` before the extension."""
    stem, ext = os.path.splitext(out)
    return f"{stem}~bw{ext}"


def parse_hud_vars(specs):
    """Validate `[[hud_var]]` and return it sorted by name -- the PNX_HUD_VAR_* index
    order, deterministic the same way tile roles' own codegen already is, so reordering
    entries in the manifest never renumbers a variable a game already refers to by name.
    """
    names = [hv.get("name") for hv in specs]
    if len(set(names)) != len(names):
        raise BuildError("duplicate hud_var names")
    for hv in specs:
        name = hv.get("name")
        if not name or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise BuildError(
                f"hud_var {name!r}: name must be lowercase, start with a letter, and "
                f"contain only letters, digits, and underscores")
        if hv.get("type") not in ("int", "text"):
            raise BuildError(
                f"hud_var {name!r}: type must be \"int\" or \"text\", got "
                f"{hv.get('type')!r}")
    return sorted(specs, key=lambda hv: hv["name"])


# HUD window elements: an ordered id table, same shape WAVEFORMS/LFO_TARGETS/FILTER_MODES
# already use for a manifest string resolved to a small fixed byte -- the order IS the id,
# on both the Python and the pnx_hud_window.c side (that file's own EASE_TABLE/switch
# statements list them in this exact order).
HUD_ELEMENT_KINDS = ["panel", "sprite", "bar", "text"]
HUD_ANCHORS = ["top_left", "top", "top_right", "left", "center", "right", "bottom_left",
              "bottom", "bottom_right"]
HUD_EASES = ["linear", "in_quad", "out_quad", "in_out_quad", "in_cubic", "out_cubic",
            "in_out_cubic"]


def _hud_colour_byte(spec, key, window_name, default=0xC0):
    v = spec.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 255:
        raise BuildError(f"hud_window {window_name!r}: {key} must be a GColor8 byte "
                         f"0-255 (e.g. 0xC0), got {v!r}")
    return v


def parse_hud_windows(specs, sprite_names, nine_slice_names, font_names, hud_var_types):
    """Validate `[[hud_window]]` and its nested `[[hud_window.element]]` entries --
    nested array-of-tables, the same shape `[[atlas.collision]]` already uses (confirmed
    tomllib attaches it to the most recently opened `[[hud_window]]` the same way).

    Returns windows unchanged (declaration order -- unlike hud_var, a window's PNX_ASSET_*
    id comes from the shared `ordered` asset list, not from sorting here) but with every
    element normalised: kind/anchor/ease as their table INDEX, not the manifest string.
    """
    names = [w.get("name") for w in specs]
    if len(set(names)) != len(names):
        raise BuildError("duplicate hud_window names")

    out = []
    for w in specs:
        name = w.get("name")
        if not name or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise BuildError(
                f"hud_window {name!r}: name must be lowercase, start with a letter, and "
                f"contain only letters, digits, and underscores")

        ease = w.get("ease", "linear")
        if ease not in HUD_EASES:
            raise BuildError(f"hud_window {name!r}: ease {ease!r} unknown "
                             f"(known: {', '.join(HUD_EASES)})")
        show_ms, hide_ms = w.get("show_ms", 200), w.get("hide_ms", 200)
        for label, ms in (("show_ms", show_ms), ("hide_ms", hide_ms)):
            if not isinstance(ms, int) or isinstance(ms, bool) or not 0 <= ms <= 0xFFFF:
                raise BuildError(f"hud_window {name!r}: {label} must be 0-65535, "
                                 f"got {ms!r}")
        slide = w.get("slide", [0, 0])
        if not (isinstance(slide, list) and len(slide) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in slide)):
            raise BuildError(f"hud_window {name!r}: slide must be [dx, dy]")

        elements = []
        for i, e in enumerate(w.get("element", [])):
            label = f"hud_window {name!r} element {i}"
            kind = e.get("kind")
            if kind not in HUD_ELEMENT_KINDS:
                raise BuildError(f"{label}: kind {kind!r} unknown "
                                 f"(known: {', '.join(HUD_ELEMENT_KINDS)})")
            anchor = e.get("anchor", "top_left")
            if anchor not in HUD_ANCHORS:
                raise BuildError(f"{label}: anchor {anchor!r} unknown "
                                 f"(known: {', '.join(HUD_ANCHORS)})")
            offset = e.get("offset", [0, 0])
            if not (isinstance(offset, list) and len(offset) == 2
                    and all(isinstance(v, int) and not isinstance(v, bool)
                            for v in offset)):
                raise BuildError(f"{label}: offset must be [dx, dy]")

            out_e = {"kind": HUD_ELEMENT_KINDS.index(kind),
                     "anchor": HUD_ANCHORS.index(anchor),
                     "offset": offset, "hud_var": 0xFF, "asset": None, "w": 0, "h": 0,
                     "p0": 0, "p1": 0, "p2": 0, "max": 0}

            if kind == "panel":
                panel = e.get("panel")
                if panel not in nine_slice_names:
                    raise BuildError(f"{label}: no nine_slice named {panel!r} "
                                     f"(known: {', '.join(sorted(nine_slice_names)) or '(none)'})")
                out_e["asset"] = ("NINE_SLICE", panel)
                out_e["w"], out_e["h"] = _hud_wh(e, label)
            elif kind == "sprite":
                sprite = e.get("sprite")
                if sprite not in sprite_names:
                    raise BuildError(f"{label}: no sprite named {sprite!r} "
                                     f"(known: {', '.join(sorted(sprite_names)) or '(none)'})")
                out_e["asset"] = ("SPRITE", sprite)
                frame = e.get("frame", 0)
                if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
                    raise BuildError(f"{label}: frame must be a non-negative int, "
                                     f"got {frame!r}")
                out_e["p0"] = frame
            elif kind == "bar":
                hud_var = e.get("hud_var")
                if hud_var_types.get(hud_var) != "int":
                    raise BuildError(f"{label}: hud_var {hud_var!r} must name a "
                                     f"declared [[hud_var]] of type \"int\"")
                out_e["hud_var"] = hud_var  # resolved to an id by the caller
                out_e["w"], out_e["h"] = _hud_wh(e, label)
                bar_max = e.get("max")
                if not isinstance(bar_max, int) or isinstance(bar_max, bool) or bar_max <= 0:
                    raise BuildError(f"{label}: max must be a positive int, got {bar_max!r}")
                out_e["max"] = bar_max
                out_e["p0"] = _hud_colour_byte(e, "border", name)
                out_e["p1"] = _hud_colour_byte(e, "track", name, default=0x00)
                out_e["p2"] = _hud_colour_byte(e, "fill", name)
            else:  # text
                hud_var = e.get("hud_var")
                if hud_var_types.get(hud_var) != "text":
                    raise BuildError(f"{label}: hud_var {hud_var!r} must name a "
                                     f"declared [[hud_var]] of type \"text\"")
                out_e["hud_var"] = hud_var
                font = e.get("font")
                if font not in font_names:
                    raise BuildError(f"{label}: no font named {font!r} "
                                     f"(known: {', '.join(sorted(font_names)) or '(none)'})")
                out_e["asset"] = ("FONT", font)
                out_e["p0"] = _hud_colour_byte(e, "colour", name)

            elements.append(out_e)

        if not elements:
            raise BuildError(f"hud_window {name!r}: has no elements")

        out.append({"name": name, "show_ms": show_ms, "hide_ms": hide_ms,
                   "ease": HUD_EASES.index(ease), "slide": slide, "elements": elements})

    return out


def _hud_wh(e, label):
    w, h = e.get("w"), e.get("h")
    for key, v in (("w", w), ("h", h)):
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise BuildError(f"{label}: {key} must be a positive int, got {v!r}")
    return w, h


def build_hud_windows(windows, asset_index, hud_var_index, orient=ORIENT_BUTTONS_RIGHT):
    """[[hud_window]] -> one "HW" blob per window, elements referencing OTHER assets by
    their already-resolved global id -- no scene involvement (pnx_hud_window.h's own top
    comment explains why). Called after `asset_index` is complete, the same timing
    build_scenes already relies on for its own cross-references.
    """
    blobs = []
    for w in windows:
        body = bytearray()
        body += w["show_ms"].to_bytes(2, "little")
        body += w["hide_ms"].to_bytes(2, "little")
        body += int(w["slide"][0]).to_bytes(2, "little", signed=True)
        body += int(w["slide"][1]).to_bytes(2, "little", signed=True)

        for e in w["elements"]:
            asset_id = 0xFFFF
            if e["asset"] is not None:
                kind, ref_name = e["asset"]
                handle = f"PNX_ASSET_{kind}_{c_ident(ref_name)}"
                asset_id = asset_index[handle]
            hud_var_id = (hud_var_index[e["hud_var"]] if e["hud_var"] != 0xFF else 0xFF)

            body += bytes([e["kind"], e["anchor"]])
            body += int(e["offset"][0]).to_bytes(2, "little", signed=True)
            body += int(e["offset"][1]).to_bytes(2, "little", signed=True)
            body += asset_id.to_bytes(2, "little")
            body += bytes([hud_var_id, e["p0"], e["p1"], e["p2"]])
            body += e["w"].to_bytes(2, "little")
            body += e["h"].to_bytes(2, "little")
            body += int(e["max"]).to_bytes(2, "little", signed=True)

        header = blob_header(MAGIC_HUD_WINDOW, len(w["elements"]), w["ease"], orient=orient)
        blobs.append((w["name"], bytes(header) + bytes(body)))

    print(f"  hud_window: {len(windows)} declared, "
          f"{sum(len(w['elements']) for w in windows)} elements")
    return blobs


def generate_header(path, atlases, sprites, maps, dialog, roles, palette_count=0,
                    scenes=None, songs=None, samples=None, fonts=None,
                    blob_files=None, orient=ORIENT_BUTTONS_RIGHT, flag_names=None,
                    nine_slices=None, hud_vars=None, hud_windows=None,
                    huffman_table=False):
    L = [
        "// GENERATED by tools/pnx_assets.py -- do not edit.",
        "//",
        "// Regenerate with the manifest, never by hand. Nothing here should be typed",
        "// into game code by name: use the symbols, not the numbers.",
        "",
        "#pragma once",
        "",
        "#include <stdint.h>",
        "#include <stddef.h>  // NULL -- a sprite clip with no authored per-frame",
        "                    // durations emits its _DURATIONS symbol as this, not an",
        "                    // array (see the sprite loop below)",
        "",
        f"// Built for orientation {ORIENT_NAMES[orient]}. Every dimension and coordinate",
        "// below is in the FRAMEBUFFER's frame, already rotated -- a map that reads 32",
        "// wide in the manifest reports 24 here if that is what the display sees. Pass",
        "// this to pnx_assets_expect_orientation() at start-up and a blob left over from",
        "// a build in the other orientation is a clean refusal rather than scrambled art.",
        f"#define PNX_ORIENTATION {orient}",
        "",
    ]

    assets = ([("PALETTES", "palettes")]
              + ([("HUFFMAN_TABLE", "huffman_table")] if huffman_table else [])
              + [("ATLAS", a["name"]) for a in atlases]
              + [("SPRITE", s["name"]) for s in sprites]
              + [("NINE_SLICE", ns["name"]) for ns in (nine_slices or [])]
              # Each map is followed by its WorldTile banks, consecutively, which is what
              # the blob's `first_bank_asset` relies on. Must stay in step with `ordered`
              # in build() -- these two lists ARE the asset id order.
              + [h for m in maps
                 for h in ([("MAP", m["name"])]
                           + [("BANK", f"{m['name']}_{i}")
                              for i in range(m["bank_count"])])]
              + ([("DIALOG", "dialog")] if dialog else [])
              + [("MUSIC", sg["name"]) for sg in (songs or [])]
              + [("SAMPLE", sm["name"]) for sm in (samples or [])]
              + [("FONT", ft["name"]) for ft in (fonts or [])]
              # Must stay in step with `ordered` in build(), which also appends
              # hud_windows right after fonts and before scenes.
              + [("HUD_WINDOW", w["name"]) for w in (hud_windows or [])]
              + ([("SCENES", "scenes")] if scenes else []))

    L += ["// Stable asset handles. Index into the runtime registry.",
          "typedef enum {"]
    for kind, name in assets:
        L.append(f"  PNX_ASSET_{kind}_{c_ident(name)},")
    L += ["  PNX_ASSET_COUNT", "} PnxAssetId;", "",
          "// The runtime loads palettes by this fixed slot before anything indexes them.",
          "_Static_assert(PNX_ASSET_PALETTES_PALETTES == 0,",
          "               \"palettes must be asset 0\");", ""]

    # The Pebble resource ids come from the SDK's own generated header, which does not
    # exist on a host build. Guarded so host tests can still include this file.
    L += ["#ifndef PNX_PLATFORM_HOST",
          "// RESOURCE_ID_* constants, generated by the SDK from package.json. This is",
          "// pure integer #defines with no Pebble API in it, so including it here does",
          "// not breach the rule that only the platform layer may touch the SDK -- and",
          "// it is the only way game code can name a resource without <pebble.h>.",
          '#include "src/resource_ids.auto.h"',
          "",
          "// Maps each handle to its Pebble resource, in the same order as the enum.",
          "#define PNX_ASSET_RESOURCE_TABLE { \\"]
    for kind, name in assets:
        L.append(f"  RESOURCE_ID_{c_ident(name)}, \\")
    L += ["}", "#else",
          "// The host has no resource ids, only files. Emitted for the same reason the",
          "// table above is: a hand-written list of blob paths in a test goes stale the",
          "// moment the manifest gains an asset, and the symptom is a scene failing for",
          "// a reason that has nothing to do with scenes.",
          "#define PNX_ASSET_FILE_TABLE { \\"]
    for out in (blob_files or []):
        L.append(f'  "{out}", \\')
    L += ["}", "#endif", ""]

    for a in atlases:
        n = c_ident(a["name"])
        L += [f"#define {n}_TILE_PX {a['tile_px']}",
              f"#define {n}_TILE_BYTES {a['tile_px'] * a['tile_px'] // 2}",
              f"#define {n}_TILE_COUNT {len(a['tiles'])}",
              f"#define {n}_PALETTE_COUNT {len(a['palettes'])}"]
        for anim_name, value in sorted(a["anim"].items()):
            an = c_ident(anim_name)
            if isinstance(value, int):
                L.append(f"#define {n}_{an} {value}")
                continue
            # A clip: a generated TILE-INDEX array plus its playback constants, fed
            # straight to pnx_anim_play/pnx_anim_frame (core/pnx_anim.h) -- same shape
            # a sprite's own [sprite.anim] clip generates, "frames" here are atlas tile
            # ids instead of sprite frame indices. `durations` is a real array when
            # authored, else the bare literal NULL, same convention as sprites.
            frame_list = ", ".join(str(f) for f in value["frames"])
            L.append(f"static const uint8_t {n}_{an}_FRAMES[] = {{ {frame_list} }};")
            L.append(f"#define {n}_{an}_COUNT {len(value['frames'])}")
            L.append(f"#define {n}_{an}_FPS {value['fps']}")
            L.append(f"#define {n}_{an}_LOOP {1 if value['loop'] else 0}")
            if value["durations"] is not None:
                dur_list = ", ".join(str(d) for d in value["durations"])
                L.append(f"static const uint8_t {n}_{an}_DURATIONS[] = "
                         f"{{ {dur_list} }};")
            else:
                L.append(f"#define {n}_{an}_DURATIONS NULL")
        L.append("")

    swaps = [(s_["name"], s_["variant_slots"]) for s_ in sprites if s_.get("variant_slots")]
    if swaps:
        L += ["// Palette-swapped sprite variants. Same bitmap, different palette: assign one",
              "// to PnxSpriteInstance.palette and the sprite draws recoloured.",
              "// PNX_SPRITE_PALETTE_DEFAULT selects the base."]
        for sname, slots in swaps:
            for vname, slot in sorted(slots.items()):
                L.append(f"#define SPRITE_{c_ident(sname)}_PALETTE_{c_ident(vname)} {slot}")
        L.append("")

    L += [f"// Shared palette table. PNX_PALETTE_SLOTS must be at least this.",
          f"#define PNX_PALETTES_USED {palette_count}", ""]

    # Custom [tile_flags] names used to get a TILE_FLAG_* #define here. Retired along
    # with the byte they lived in -- parse_flag_names now refuses [tile_flags] outright,
    # so `flag_names` past this point is always exactly the four built-in names and this
    # block would never have fired. Comes back once MAP_EXTENDED's side table exists.

    named = [(sg["name"], sg.get("instrument_names") or []) for sg in (songs or [])]
    if any(n for _, n in named):
        L += ["// Named instruments. A pattern row names an instrument by INDEX, which is",
              "// right for the two bytes a row costs and useless for reading -- so a song",
              "// may name them, and game code says MUSIC_THEME_INST_BASS rather than 1."]
        for song_name, names in named:
            for index, label in names:
                L.append(f"#define MUSIC_{c_ident(song_name)}_INST_{c_ident(label)} "
                         f"{index}")
        L.append("")

    if roles:
        L += ["// Tile roles from the manifest legend, resolved per atlas. Prefixed by",
              "// atlas name because two tilesets may both define a role called 'wall'."]
        for atlas_name in sorted(roles):
            for role, idx in sorted(roles[atlas_name].items()):
                L.append(f"#define {c_ident(atlas_name)}_TILE_{c_ident(role)} {idx}")
        # Unprefixed aliases for a single-atlas project, so simple manifests stay simple.
        if len(roles) == 1:
            only = next(iter(roles))
            for role, idx in sorted(roles[only].items()):
                L.append(f"#define TILE_{c_ident(role)} {idx}")
        L.append("")

    for s in sprites:
        n = c_ident(s["name"])
        # No sprite-wide _W/_H/_FRAME_BYTES any more: frames are not uniform size (a
        # tightly packed sheet's poses rarely are), so there is no single width/height to
        # name. pnx_sprite_frame_get reads a frame's own w/h/origin at runtime instead.
        L.append(f"#define {n}_FRAME_COUNT {len(s['frames'])}")
        for anim_name, value in sorted(s["anim"].items()):
            an = c_ident(anim_name)
            if isinstance(value, int):
                L.append(f"#define {n}_{an} {value}")
                continue
            # A clip: a generated frame-index array plus its playback constants, fed
            # straight to pnx_anim_play/pnx_anim_frame (gfx/pnx_sprite.h). `durations`
            # is a real array when authored, else the bare literal NULL, so game code
            # always passes `{n}_{an}_DURATIONS` without needing to know which case it is.
            frame_list = ", ".join(str(f) for f in value["frames"])
            L.append(f"static const uint8_t {n}_{an}_FRAMES[] = {{ {frame_list} }};")
            L.append(f"#define {n}_{an}_COUNT {len(value['frames'])}")
            L.append(f"#define {n}_{an}_FPS {value['fps']}")
            L.append(f"#define {n}_{an}_LOOP {1 if value['loop'] else 0}")
            if value["durations"] is not None:
                dur_list = ", ".join(str(d) for d in value["durations"])
                L.append(f"static const uint8_t {n}_{an}_DURATIONS[] = "
                         f"{{ {dur_list} }};")
            else:
                L.append(f"#define {n}_{an}_DURATIONS NULL")
        L.append("")

    for ns in (nine_slices or []):
        n = c_ident(ns["name"])
        bl, bt, br, bb = ns["border"]
        L += [f"#define {n}_W {ns['w']}", f"#define {n}_H {ns['h']}",
              f"#define {n}_BORDER_L {bl}", f"#define {n}_BORDER_T {bt}",
              f"#define {n}_BORDER_R {br}", f"#define {n}_BORDER_B {bb}", ""]

    for m in maps:
        n = c_ident(m["name"])
        L += [f"#define MAP_{n}_W {m['w']}", f"#define MAP_{n}_H {m['h']}",
              f"#define MAP_{n}_START_X {m['start'][0]}",
              f"#define MAP_{n}_START_Y {m['start'][1]}", ""]

    if scenes:
        L += ["// Scenes: the only load point. pnx_scene_load() takes one of these.",
              "typedef enum {"]
        for name in scenes["names"]:
            L.append(f"  PNX_SCENE_{c_ident(name)},")
        L += ["  PNX_SCENE_COUNT", "} PnxSceneId;", ""]

    if dialog:
        L += ["// Dialog entries, alphabetical -- index into the dialog asset."]
        for i, name in enumerate(dialog["names"]):
            L.append(f"#define DIALOG_{c_ident(name)} {i}")
        L += [f"#define DIALOG_COUNT {len(dialog['names'])}", ""]

    if fonts:
        L += ["// Font metrics. LINE_HEIGHT paces successive lines; BASELINE is the",
              "// offset from a line's top to the baseline pnx_text_draw() takes as y."]
        for ft in fonts:
            n = c_ident(ft["name"])
            L += [f"#define FONT_{n}_LINE_HEIGHT {ft['line_height']}",
                  f"#define FONT_{n}_BASELINE {ft['baseline']}",
                  f"#define FONT_{n}_GLYPHS {len(ft['glyphs'])}",
                  f"#define FONT_{n}_DEPTH {ft['depth']}", ""]

    if hud_vars:
        L += ["// HUD variables: a named runtime table game code writes to each tick and",
              "// a HUD draw call reads back (pnx_hud_vars.h). Not a resource -- these",
              "// are ids into storage the game itself declares, sized by the COUNT",
              "// below, and hands to pnx_hud_vars_init() at start-up."]
        for i, hv in enumerate(hud_vars):
            L.append(f"#define PNX_HUD_VAR_{c_ident(hv['name'])} {i}")
        L += [f"#define PNX_HUD_VAR_COUNT {len(hud_vars)}", ""]

    if hud_windows:
        L += ["// HUD windows: each needs PnxHudElement storage the game declares and",
              "// sizes by this count, handed to pnx_hud_window_load() (pnx_hud_window.h)."]
        for w in hud_windows:
            L.append(f"#define PNX_HUD_WINDOW_{c_ident(w['name'])}_ELEMENTS "
                     f"{len(w['elements'])}")
        L.append("")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(L))
    return path


def sync_package_json(path, blobs):
    """Rewrite pebble.resources.media from the manifest.

    Without this the resource list has to be maintained by hand in package.json AND in
    the manifest, which is the duplication the manifest exists to remove -- and the
    failure mode is silent: a blob that is built but not declared simply is not in the
    bundle, and the app reports a missing resource at runtime with no build-time hint.

    Everything else in package.json is preserved; only the media array is replaced.
    """
    if not os.path.exists(path):
        raise BuildError(f"--package given but {path} does not exist")

    with open(path) as f:
        pkg = json.load(f)

    media = [{"type": "raw", "name": c_ident(name), "file": os.path.basename(out)}
             for name, out in blobs]

    # Resource names become #defines in the SDK's generated header, so two assets of
    # DIFFERENT kinds sharing a name -- an atlas called `ship` and a map called `ship` --
    # emit RESOURCE_ID_SHIP twice and the app fails to compile with a redefinition warning
    # that names neither asset and points at a generated file. The manifest's own handles
    # are prefixed by kind and never collide, so this is the only place it can bite, and
    # it is invisible until an `arm-none-eabi-gcc` that nothing in the pipeline runs.
    seen = {}
    for entry, (name, _out) in zip(media, blobs):
        if entry["name"] in seen:
            raise BuildError(
                f"assets {seen[entry['name']]!r} and {name!r} both become resource "
                f"{entry['name']}, which the SDK turns into one #define -- the app would "
                f"fail to compile with a redefinition of RESOURCE_ID_{entry['name']} and "
                f"name neither of them. Rename one; the kinds do not have to differ.")
        seen[entry["name"]] = name

    pkg.setdefault("pebble", {}).setdefault("resources", {})["media"] = media

    with open(path, "w") as f:
        json.dump(pkg, f, indent=2)
        f.write("\n")

    print(f"package.json: {len(media)} resources declared")


def report_font_licences(fonts):
    """Print what the bundle redistributes and under what terms.

    Rasterised glyphs are the typeface, minus the outlines. Someone shipping to the
    appstore needs this list to write their credits, and it should not require reading
    the manifest to assemble.
    """
    print("\nfont licences -- rasterised glyphs are redistribution; credit accordingly")
    width = max(len(f["name"]) for f in fonts)
    for ft in fonts:
        print(f"  {ft['name'].ljust(width)}  {os.path.basename(ft['source'])} "
              f"@{ft['size']}px  --  {ft['license']}")
        if ft.get("overlay_source"):
            print(f"  {' ' * width}  {os.path.basename(ft['overlay_source'])} "
                  f"@{ft['size']}px (glyphs: {ft['overlay_charset']!r})  --  "
                  f"{ft['overlay_license']}")


def report_budget(entries, budget):
    """Per-category breakdown against the appstore ceiling."""
    width = max([len(n) for _, n, _ in entries] + [8])
    by_kind = {}
    total = 0

    print("\nresource budget")
    for kind, name, size in entries:
        print(f"  {kind:<7} {name.ljust(width)}  {size:>7} B")
        by_kind[kind] = by_kind.get(kind, 0) + size
        total += size

    print("  " + "-" * (width + 20))
    for kind, size in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<7} {'':<{width}}  {size:>7} B  ({100.0*size/total:.0f}%)")

    pct = 100.0 * total / budget
    bar = min(40, int(40 * total / budget))
    print(f"\n  [{'#' * bar}{'.' * (40 - bar)}] {total} / {budget} B ({pct:.1f}%)")

    # The budget above is the APPSTORE threshold, which is what shipping is limited by --
    # but it is not the device's ceiling, and reporting only the tighter number makes the
    # real one look four times closer than it is. Both, so a decision to go past 256KB is
    # a decision rather than an accident.
    if budget <= RESOURCE_BYTES_APPSTORE < RESOURCE_BYTES_HARD:
        print(f"  {'':<8}{'':<{width}}   appstore {RESOURCE_BYTES_APPSTORE:,} B, "
              f"device ceiling {RESOURCE_BYTES_HARD:,} B "
              f"({100.0 * total / RESOURCE_BYTES_HARD:.0f}% of the latter)")

    # The other ceiling, and it is a different KIND of thing: a .pbpack holds at most 256
    # resource ENTRIES, whatever they weigh. That never mattered while an asset was a
    # tileset or a song, and started mattering the moment a map became one resource plus a
    # bank per few WorldTiles -- a project of large maps runs out of entries long before it
    # runs out of bytes. Exceeding it is a bare traceback out of the SDK's packer.
    print(f"  {len(entries)} of {PBPACK_MAX_RESOURCES} resource entries "
          f"({100.0 * len(entries) / PBPACK_MAX_RESOURCES:.0f}%)")
    if len(entries) > PBPACK_MAX_RESOURCES:
        banks = sum(1 for kind, _n, _s in entries if kind == "bank")
        raise BuildError(
            f"{len(entries)} resources exceeds the {PBPACK_MAX_RESOURCES} a .pbpack can "
            f"hold, {banks} of them WorldTile banks. Raise `bank_bytes` on the largest "
            f"maps -- it trades entries back for seek time -- or carve fewer maps.")

    if total > budget:
        raise BuildError(f"resources exceed the {budget} byte budget by "
                         f"{total - budget} bytes")
    return total


# ----------------------------------------------------------------------------- main

def build(manifest_path, out_dir, header_path, preview=False, package=None,
          orientation=None):
    root = os.path.dirname(os.path.abspath(manifest_path))
    with open(manifest_path, "rb") as f:
        man = tomllib.load(f)

    project = man.get("project", {})
    budget = int(project.get("budget_bytes", DEFAULT_BUDGET))

    # The override exists so one manifest can be built both ways. That is not a
    # convenience: "the same content compiles to either orientation" is a claim about the
    # pipeline, and a claim nobody can run is one that quietly stops being true.
    orient = parse_orientation(orientation or project.get("orientation"),
                               "--orientation" if orientation else "[project]")

    out_dir = out_dir or os.path.join(root, project.get("resources", "resources"))
    header_path = header_path or os.path.join(root, project.get("header",
                                                                "src/c/assets_gen.h"))

    entries = []
    blobs = []

    print("building assets"
          + (f" ({ORIENT_NAMES[orient]}: content rotated at build time, so the engine's "
             f"ordinary blit draws it)" if orient != ORIENT_BUTTONS_RIGHT else ""))
    atlases = [pack_atlas(root, a, orient) for a in man.get("atlas", [])]

    # Tile roles are PER ATLAS. They used to be one shared dict, which forced the
    # single-tileset restriction: two atlases both defining "wall" would collide, and a
    # map had no way to say which one it meant. Explicit `semantic` still wins over
    # `autopick`, so a manifest can start auto and be pinned down later.
    roles_by_atlas = {}
    collision_by_atlas = {}
    for spec, atlas in zip(man.get("atlas", []), atlases):
        r = {}
        if "autopick" in spec:
            r.update(autopick_tiles(atlas, spec["autopick"]))
        for role, idx in spec.get("semantic", {}).items():
            if not 0 <= idx < len(atlas["tiles"]):
                raise BuildError(f"atlas {atlas['name']!r}: semantic {role!r}={idx} is "
                                 f"out of range (0..{len(atlas['tiles'])-1})")
            r[role] = idx
        roles_by_atlas[atlas["name"]] = r
        collision_by_atlas[atlas["name"]] = parse_atlas_collision(spec, atlas, r)

    sprites = [pack_sprite(root, sp, orient) for sp in man.get("sprite", [])]
    nine_slices = [pack_nine_slice(root, ns, orient) for ns in man.get("nine_slice", [])]
    hud_vars = parse_hud_vars(man.get("hud_var", []))

    flag_names = parse_flag_names(man.get("tile_flags", {}))
    legend = parse_legend(man.get("legend", {}), flag_names)
    map_specs = man.get("map", [])
    map_names = [m["name"] for m in map_specs]
    if len(set(map_names)) != len(map_names):
        raise BuildError("duplicate map names")
    if not atlases and map_specs:
        raise BuildError("maps need at least one atlas to draw with")

    default_atlas = atlases[0]["name"] if atlases else None
    tile_counts = {a["name"]: len(a["tiles"]) for a in atlases}
    tile_px = {a["name"]: a["tile_px"] for a in atlases}
    maps = []
    for spec in map_specs:
        names = map_atlas_names(spec, roles_by_atlas, default_atlas)
        table, _ = map_tile_bases(spec["name"], names, tile_counts, tile_px)

        # A map may carry its own `[map.legend]`, overlaid on the project one. The legend
        # is a character per cell, so the printable set is the real ceiling on how many
        # DISTINCT tiles can be placed -- about ninety. Project-wide, that ninety was
        # shared by every map and every atlas in the game, which put most of a carved
        # tileset permanently out of reach. Per map it is ninety EACH, and the shared
        # legend still carries the characters that mean the same thing everywhere.
        #
        # Overlay rather than replacement so a map can pin one character without
        # restating the others, and so every existing manifest keeps working untouched.
        own = spec.get("legend")
        map_legend = legend
        if own:
            map_legend = dict(legend)
            map_legend.update(parse_legend(own, flag_names))

        def compile_layer(layer_spec, primary, _legend=map_legend, _table=table):
            """One WorldTile plane -- `rows` or `source`, same rule a bare `[[map]]`
            already enforces, applied identically to a `[[map.layer]]` sub-table."""
            own_l = layer_spec.get("legend")
            legend_l = _legend
            if own_l:
                legend_l = dict(_legend)
                legend_l.update(parse_legend(own_l, flag_names))

            has_source, has_rows = "source" in layer_spec, "rows" in layer_spec
            if has_source and has_rows:
                raise BuildError(
                    f"map {spec['name']!r}: has both `source` and `rows`. A layer is "
                    f"authored in one or the other -- delete whichever is not the real "
                    f"one.")
            if not (has_source or has_rows):
                raise BuildError(
                    f"map {spec['name']!r}: has neither `rows` nor `source`, so it has "
                    f"no cells")
            if has_source:
                return compile_source_map(layer_spec, root, roles_by_atlas, _table,
                                          map_names, collision_by_atlas, primary=primary)
            return compile_map(layer_spec, legend_l, roles_by_atlas, _table, map_names,
                               collision_by_atlas, primary=primary)

        # A map is either the ORIGINAL shape -- one `rows` or `source` right on the
        # `[[map]]` table, unchanged since before layers existed -- or declares its planes
        # explicitly as `[[map.layer]]` sub-tables, background-to-foreground in the order
        # written. One of those may set `primary = true` (default: the first) to say which
        # plane `start`/`warps`/`out` belong to; it is always moved to index 0 before
        # compiling, so the compiled blob's primary_layer byte is always 0 -- which is what
        # lets every other layer-aware reader (finish_map, parse_map, the editor) treat
        # "layers[0]" and "the primary" as the same thing unconditionally, without
        # threading an arbitrary index through all of them.
        layer_specs = spec.get("layer")
        if layer_specs and ("rows" in spec or "source" in spec):
            raise BuildError(
                f"map {spec['name']!r} declares `[[map.layer]]` AND a top-level "
                f"`rows`/`source`. Once a map's planes are explicit, cells belong on a "
                f"layer -- move the top-level one into its own `[[map.layer]]` entry.")
        if layer_specs:
            primary_i = next((i for i, ls in enumerate(layer_specs)
                              if ls.get("primary")), 0)
            ordered = [layer_specs[primary_i]] + [ls for i, ls in enumerate(layer_specs)
                                                  if i != primary_i]

            primary_spec = dict(ordered[0])
            primary_spec["out"] = spec["out"]
            primary_spec["name"] = spec["name"]
            m = compile_layer(primary_spec, primary=True)

            layers = [m]
            for i, ls in enumerate(ordered[1:], start=1):
                ls2 = dict(ls)
                ls2["name"] = f"{spec['name']}.{i}"
                layer_m = compile_layer(ls2, primary=False)
                # Consumed by finish_map's per-layer directory entry; not a general
                # per-layer worldtile/residency override (see prepare_map's own comment --
                # every layer of one map shares the map's worldtile/atlas_slots/resident/
                # bank_bytes, which is a project-level performance knob this format leaves
                # at one setting per map rather than one per layer).
                layer_m["parallax_pct_x"] = max(0, min(255, int(ls.get("parallax_pct_x", 255))))
                layer_m["parallax_pct_y"] = max(0, min(255, int(ls.get("parallax_pct_y", 255))))
                layer_m["wrap"] = bool(ls.get("wrap", False))
                layers.append(layer_m)
            m["layers"] = layers
        else:
            m = compile_layer(spec, primary=True)
            m["layers"] = [m]
        m["primary_layer"] = 0
        m["palette"] = spec.get("palette")
        m["tile_px"] = tile_px[names[0]]

        # "auto" is the default and picks by arithmetic; a number forces it. Same bargain
        # the atlas `metatiles` key offers, and for the same reason -- the cheapest size
        # depends on the screen, so a constant is right for one shape of content and
        # quietly wrong for the rest.
        want_wt = spec.get("worldtile", "auto")
        held = bool(spec.get("resident", False))
        if want_wt == "auto":
            wt, best, worst = pick_worldtile(spec["name"], m["w"], m["h"], m["tile_px"],
                                             held)
            m["worldtile_note"] = (f"auto {wt}: {best:,} B against {worst:,} B at the "
                                   f"worst size, {'held whole' if held else 'streaming'}"
                                   ) if worst > best else None
        else:
            wt = int(want_wt)
            m["worldtile_note"] = None
        if not WORLDTILE_MIN <= wt <= WORLDTILE_MAX or wt & (wt - 1):
            raise BuildError(
                f"map {spec['name']!r}: worldtile = {wt} must be a power of two between "
                f"{WORLDTILE_MIN} and {WORLDTILE_MAX}, or \"auto\". The runtime finds a "
                f"cell's WorldTile by shifting, not dividing, which is what makes it free "
                f"per drawn tile.")
        m["worldtile"] = wt

        slots = spec.get("atlas_slots")
        if slots is not None and not 1 <= int(slots) <= len(names):
            raise BuildError(
                f"map {spec['name']!r}: atlas_slots = {slots}, but the map declares "
                f"{len(names)} atlases -- a slot count outside 1..{len(names)} is either "
                f"a pool that cannot hold one atlas or one that can never fill.")
        m["atlas_slots"] = int(slots) if slots is not None else None

        # `resident = true` gives the map a slot per WorldTile, so all of it loads at map
        # load and none of it is ever evicted -- what a map cost before WorldTiles existed.
        # It is here to be MEASURED against, not because anything needs it: the per-map
        # report prints both numbers either way, and this is what lets a project put the
        # two side by side at runtime instead of taking the report's word for it.
        m["resident"] = bool(spec.get("resident", False))

        # How big a WorldTile bank resource may get. Lower it to cut seek cost further at
        # the price of more resources; the default is explained at WORLDTILE_BANK_BYTES.
        bank = int(spec.get("bank_bytes", WORLDTILE_BANK_BYTES))
        if bank < 512:
            raise BuildError(
                f"map {spec['name']!r}: bank_bytes = {bank} is below one WorldTile's worth "
                f"of cells, so every bank would hold a single tile and the map would cost "
                f"a resource each. Give it at least 512.")
        m["bank_bytes"] = bank
        maps.append(m)
    check_warp_destinations(maps)
    rotate_maps(maps, orient)

    # Slice every map before any asset id is handed out: a map's WorldTile banks are
    # resources of their own, so they need ids, and only slicing says how many there are.
    for m in maps:
        prepare_map(m, m["worldtile"], m["bank_bytes"])

    dialog_specs = man.get("dialog", {})

    # Computed here rather than after packing, because a map blob now records the asset
    # id of its atlas -- which is how the runtime pairs the two without depending on the
    # order a scene happens to load them in.
    ordered = (["PNX_ASSET_PALETTES_PALETTES"]
               + (["PNX_ASSET_HUFFMAN_TABLE_HUFFMAN_TABLE"]
                  if project.get("compress", "bitplane") == "huffman" else [])
               + [f"PNX_ASSET_ATLAS_{c_ident(a['name'])}" for a in atlases]
               + [f"PNX_ASSET_SPRITE_{c_ident(sp['name'])}" for sp in sprites]
               + [f"PNX_ASSET_NINE_SLICE_{c_ident(ns['name'])}" for ns in nine_slices]
               # A map, then its WorldTile banks, consecutively -- which is what lets the
               # blob store one `first_bank_asset` and find bank i at that plus i.
               + [h for m in maps
                  for h in ([f"PNX_ASSET_MAP_{c_ident(m['name'])}"]
                            + [f"PNX_ASSET_BANK_{c_ident(m['name'])}_{i}"
                               for i in range(m["bank_count"])])]
               + (["PNX_ASSET_DIALOG_DIALOG"] if dialog_specs else [])
               + [f"PNX_ASSET_MUSIC_{c_ident(sg['name'])}" for sg in
                  pack_music_names(man)]
               + [f"PNX_ASSET_SAMPLE_{c_ident(n)}" for n in
                  sorted(man.get("sample", {}))]
               + [f"PNX_ASSET_FONT_{c_ident(f['name'])}" for f in man.get("font", [])])
    asset_index = {h: i for i, h in enumerate(ordered)}

    # One running palette list across every asset, so a later atlas or sprite reuses or
    # extends what earlier ones built. Sharing is discovered, never declared.
    #
    # Atlases finish BEFORE maps now: a map that names a palette variant needs the table the
    # atlas builds while assigning palettes, so the order is a dependency rather than a habit.
    print("palettes:")
    # Two phases, and the order is a correctness requirement rather than a habit -- see
    # settle_palettes. Every palette is settled first; only then is a pixel packed.
    # `~bw` escape hatch (docs/PORTING.md): ON by default for every project, not opt-in --
    # a colour platform never receives one of these files regardless (the SDK's own `~`
    # tag resolution only matches a bw platform against `~bw`), so there is no cost to a
    # project that never ships one, and PNX_PACK_2BIT (pnx_config.h) defaults from the
    # same PBL_BW signal that decides whether the SDK ships them -- the two agree
    # automatically rather than needing PNX_DEFINES set by hand. `pack_2bit = false`
    # opts a project out entirely, which only has a reason to exist if a project ships
    # its own PNX_PACK_2BIT=0 override and wants the pipeline to match it.
    pack_2bit = bool(project.get("pack_2bit", True))
    if pack_2bit:
        print("  pack_2bit: emitting ~bw resource variants (default on -- see "
              "docs/PORTING.md; set pack_2bit = false in [project] to opt out)")

    # OFF by default, unlike pack_2bit: this trades a real CPU decode cost per bank load
    # for smaller map resources (M12), and that trade is a project's own call to make, not
    # a default every map pays for. Pairs with PNX_USE_MAP_COMPRESS (pnx_config.h) on the
    # runtime side -- a map built with this on refuses to load against a runtime built
    # without it, rather than reading compressed bytes as if they were plain cells.
    compress_maps = bool(project.get("compress_maps", False))
    if compress_maps:
        print("  compress_maps: LZSS-compressing WorldTile bank bodies -- set "
              "PNX_USE_MAP_COMPRESS=1 in the project's pnx_config.h to match")

    # Project-wide sprite/atlas pixel compression -- exactly one mode, applied uniformly
    # to every sprite and every atlas the project builds. Mirrors PNX_COMPRESS_MODE
    # (pnx_config.h): a project sets one, matches the other. Defaults to "bitplane" --
    # the only mode that keeps pixel data out of RAM until a frame or tile is actually
    # drawn (pnx_tile_cache.c/pnx_sprite_cache.c decode on demand); "lzss" or "none" opts
    # out. compress_pixel_body never emits the compressed form for content too small to
    # benefit, so "lzss" is safe to leave on project-wide rather than needing a per-asset
    # opt-in.
    for stale_key in ("compress_sprites", "compress_atlases"):
        if stale_key in project:
            raise BuildError(
                f"project: {stale_key!r} is no longer read -- sprite/atlas pixel "
                f"compression is project-wide now, one choice for both. Replace it (and "
                f"its sibling, if both are set) with a single `compress = \"none\" | "
                f"\"lzss\" | \"bitplane\"` in [project].")
    compress = project.get("compress", "bitplane")
    if compress not in ("none", "lzss", "bitplane", "huffman"):
        raise BuildError(
            f"project: compress = {compress!r} is not one of \"none\", \"lzss\", "
            f"\"bitplane\", \"huffman\"")
    compress_sprites = compress if compress != "none" else False
    compress_atlases = compress if compress != "none" else False

    if compress == "bitplane":
        print("  compress: bitplane/Elias-gamma-compressing sprite and atlas pixel data "
              "for on-demand decoding -- set PNX_COMPRESS_MODE=PNX_COMPRESS_BITPLANE in "
              "the project's pnx_config.h to match (the default either side)")
    elif compress == "lzss":
        print("  compress: LZSS-compressing sprite and atlas pixel data -- set "
              "PNX_COMPRESS_MODE=PNX_COMPRESS_LZSS in the project's pnx_config.h to match")
    elif compress == "huffman":
        print("  compress: bitplane pixel data, but run lengths are coded against ONE "
              "project-wide Huffman table (two-pass build) instead of per-unit "
              "Elias-gamma -- set PNX_COMPRESS_MODE=PNX_COMPRESS_HUFFMAN in the "
              "project's pnx_config.h to match")

    shared = []
    settle_palettes(atlases, sprites, shared, nine_slices)

    huffman_table_blob = None
    huffman_table_blob_bw = None
    if compress == "huffman":
        # Pass 1 ("collect"): run every atlas/sprite through their normal tile-
        # selection/palette logic (shared's palette set is already fully settled by
        # settle_palettes above, so this pass mutates nothing that pass 2 depends on) and
        # stash each unit's own indices instead of encoding -- see huffman_collect_or_encode
        # and huffman_finalize_table's own comments for the full design. pack_2bit's own
        # ~bw units (a separate pixel format, separate table -- see
        # _huffman_collect_bw's own comment) get pooled in the SAME collect pass, since
        # finish_atlas/finish_sprite build both blob and blob_bw together.
        global _huffman_phase
        _huffman_phase = "collect"
        for a in atlases:
            finish_atlas(a, collision_by_atlas[a["name"]], shared, orient, pack_2bit,
                        compress_atlases)
        for sp in sprites:
            finish_sprite(sp, shared, orient, pack_2bit, compress_sprites)
        huffman_table_blob = (blob_header(MAGIC_HUFFMAN_TABLE, orient=orient)
                              + huffman_finalize_table())
        if pack_2bit:
            huffman_table_blob_bw = (blob_header(MAGIC_HUFFMAN_TABLE, orient=orient)
                                     + huffman_finalize_table_bw())
        _huffman_phase = "final"

    for a in atlases:
        # [[atlas.collision]]'s mode byte bakes into PnxAtlas.tile_flags, revived rather
        # than removed: it was baked and loaded by the C reader but had no consumer
        # anywhere in the engine before this (checked before touching it). SCALED's rect
        # and COMPLEX's mask are too big for one byte each and get their own sparse
        # tail tables, built inside finish_atlas itself now that the tiles are settled.
        finish_atlas(a, collision_by_atlas[a["name"]], shared, orient, pack_2bit,
                    compress_atlases)
    # The first point at which both halves are known: which atlases chose metatiles, and
    # which legend characters were painted flipped.
    check_flip_metatiles(maps, atlases)
    for sp in sprites:
        finish_sprite(sp, shared, orient, pack_2bit, compress_sprites)
    for ns in nine_slices:
        finish_nine_slice(ns, shared, orient, pack_2bit)

    by_name = {a["name"]: a for a in atlases}
    for m in maps:
        assets = {n: asset_index[f"PNX_ASSET_ATLAS_{c_ident(n)}"] for n in m["atlases"]}

        # The palette remap runs over the map's whole tile id space, so an atlas without
        # the named variant keeps its own assignment. That is what lets one recoloured zone
        # span several tilesets without every one of them having to declare the variant.
        want = m.get("palette")
        table = b""
        if want:
            offered = {n: by_name[n].get("variant_tables", {}) for n in m["atlases"]}
            if not any(want in t for t in offered.values()):
                every = sorted({v for t in offered.values() for v in t})
                raise BuildError(
                    f"map {m['name']!r}: palette = {want!r}, but none of its atlases "
                    f"({', '.join(m['atlases'])}) declares such a variant. Between them "
                    f"they provide: {', '.join(every) or '(none -- add `variants` to an atlas)'}")
            table = b"".join(offered[n].get(want) or bytes(by_name[n]["assign"])
                             for n in m["atlases"])

        # Pool slots must fit the DECODED pixel data a compressed atlas expands to, not
        # the smaller on-disk blob -- see finish_atlas's `resident_bytes` comment.
        sizes = {n: by_name[n]["resident_bytes"] for n in m["atlases"]}
        first_bank = asset_index[f"PNX_ASSET_BANK_{c_ident(m['name'])}_0"]
        finish_map(m, assets, sizes, table,
                   m["worldtile"], m["atlas_slots"], m["resident"],
                   m["bank_bytes"], first_bank, orient, compress_maps)
    # Colour only, no ~bw variant: a 1-bit platform never loads this resource at all (see
    # PnxPalette's own comment, pnx_assets.h) -- there is nothing for a ~bw tag to shrink.
    palette_blob = (blob_header(MAGIC_PALETTES, len(shared), orient=orient)
                    + b"".join(palette_bytes(p) for p in shared))
    print(f"  {len(shared)} palettes, {len(shared) * PALETTE_BYTES} B shared across "
          f"every asset -- set PNX_PALETTE_SLOTS >= {len(shared)}")

    dialog = pack_dialog(dialog_specs, orient) if dialog_specs else None
    songs = pack_music(man.get("music", {}), orient) if man.get("music") else []
    samples = pack_samples(root, man.get("sample", {}), orient) if man.get("sample") else []

    # Fonts come after dialog is known, because `charset = "auto"` derives its glyph set
    # from the dialog pages -- a font cannot be sized until the text it must render is.
    font_specs = man.get("font", [])
    font_names = [f.get("name") for f in font_specs]
    if len(set(font_names)) != len(font_names):
        raise BuildError("duplicate font names")
    fonts = [pack_font(root, f, man, orient) for f in font_specs]

    # Order here defines the PnxAssetId enum and the resource table, so it must match
    # generate_header's. Both walk atlases, sprites, maps, dialog in that order.
    pal_out = project.get("palettes_out", "palettes.bin")
    entries.append(("palette", "palettes",
                    write_blob(os.path.join(out_dir, pal_out), palette_blob)))
    blobs.append(("palettes", pal_out))

    if compress == "huffman":
        huffman_out = project.get("huffman_table_out", "huffman_table.bin")
        entries.append(("huffman_table", "huffman_table",
                        write_blob(os.path.join(out_dir, huffman_out), huffman_table_blob)))
        blobs.append(("huffman_table", huffman_out))
        if huffman_table_blob_bw is not None:
            # Same file-tag convention as a[.get("blob_bw")] above -- same directory, same
            # base name, `~bw` before the extension -- not a second entries/blobs
            # registration, for the same reason: a bw platform never ships both files at
            # once, so counting this one too would price a byte twice for a total no
            # build pays.
            write_blob(os.path.join(out_dir, bw_variant_path(huffman_out)),
                      huffman_table_blob_bw)

    for a in atlases:
        entries.append(("atlas", a["name"],
                        write_blob(os.path.join(out_dir, a["out"]), a["blob"])))
        blobs.append((a["name"], a["out"]))
        if a.get("blob_bw"):
            # `~` tag beside the untagged file, same directory, same base name -- that is
            # the whole mechanism (docs/PORTING.md: "the build globs for tiles*.bin"). Not
            # added to entries/blobs: those drive ONE platform's budget total and
            # package.json's media list, and a bw platform never ships both files at once,
            # so counting this one too would price a byte twice for a total no build pays.
            write_blob(os.path.join(out_dir, bw_variant_path(a["out"])), a["blob_bw"])
    for sp in sprites:
        entries.append(("sprite", sp["name"],
                        write_blob(os.path.join(out_dir, sp["out"]), sp["blob"])))
        blobs.append((sp["name"], sp["out"]))
        if sp.get("blob_bw"):
            write_blob(os.path.join(out_dir, bw_variant_path(sp["out"])), sp["blob_bw"])
    for ns in nine_slices:
        entries.append(("nine_slice", ns["name"],
                        write_blob(os.path.join(out_dir, ns["out"]), ns["blob"])))
        blobs.append((ns["name"], ns["out"]))
        if ns.get("blob_bw"):
            write_blob(os.path.join(out_dir, bw_variant_path(ns["out"])), ns["blob_bw"])
    for m in maps:
        entries.append(("map", m["name"],
                        write_blob(os.path.join(out_dir, m["out"]), m["blob"])))
        blobs.append((m["name"], m["out"]))

        # Banks follow their map immediately, matching the id order in `ordered`. The name
        # carries the map's -- `field_0`, not `bank_0`, which two maps would collide on --
        # and it is the same name the handle is built from, since the SDK's resource id
        # comes from this and the handle from generate_header's parallel list.
        stem = m["out"][:-4] if m["out"].endswith(".bin") else m["out"]
        for i, bank in enumerate(m["banks"]):
            out = f"{stem}_b{i}.bin"
            entries.append(("bank", f"{m['name']}_{i}",
                            write_blob(os.path.join(out_dir, out), bank)))
            blobs.append((f"{m['name']}_{i}", out))
    if dialog:
        out = project.get("dialog_out", "dialog.bin")
        entries.append(("dialog", "dialog",
                        write_blob(os.path.join(out_dir, out), dialog["blob"])))
        blobs.append(("dialog", out))

    # After dialog, matching the id order in `ordered` -- package.json's media list and
    # the generated enum have to agree or every asset id shifts.
    for sg in songs:
        entries.append(("music", sg["name"],
                        write_blob(os.path.join(out_dir, sg["out"]), sg["blob"])))
        blobs.append((sg["name"], sg["out"]))

    for sm in samples:
        entries.append(("sample", sm["name"],
                        write_blob(os.path.join(out_dir, sm["out"]), sm["blob"])))
        blobs.append((sm["name"], sm["out"]))

    for ft in fonts:
        entries.append(("font", ft["name"],
                        write_blob(os.path.join(out_dir, ft["out"]), ft["blob"])))
        blobs.append((ft["name"], ft["out"]))

    # After fonts, before scenes -- must stay in step with generate_header's own `assets`
    # list, which appends HUD_WINDOW at the identical point.
    hud_var_index = {hv["name"]: i for i, hv in enumerate(hud_vars)}
    hud_var_types = {hv["name"]: hv["type"] for hv in hud_vars}
    hud_windows = parse_hud_windows(
        man.get("hud_window", []), {sp["name"] for sp in sprites},
        {ns["name"] for ns in nine_slices}, set(font_names), hud_var_types)
    for hw_name, hw_blob in build_hud_windows(hud_windows, asset_index, hud_var_index,
                                              orient):
        out = f"{hw_name}.bin"
        entries.append(("hud_window", hw_name,
                        write_blob(os.path.join(out_dir, out), hw_blob)))
        blobs.append((hw_name, out))
        ordered.append(f"PNX_ASSET_HUD_WINDOW_{c_ident(hw_name)}")
        asset_index[f"PNX_ASSET_HUD_WINDOW_{c_ident(hw_name)}"] = len(ordered) - 1

    scenes = build_scenes(man, asset_index, maps, orient)
    if scenes:
        scene_out = project.get("scenes_out", "scenes.bin")
        entries.append(("scene", "scenes",
                        write_blob(os.path.join(out_dir, scene_out), scenes["blob"])))
        blobs.append(("scenes", scene_out))
        ordered.append("PNX_ASSET_SCENES_SCENES")
        asset_index["PNX_ASSET_SCENES_SCENES"] = len(ordered) - 1

    generate_header(header_path, atlases, sprites, maps, dialog, roles_by_atlas,
                    len(shared), scenes, songs, samples, fonts,
                    [out for _name, out in blobs], orient, flag_names, nine_slices,
                    hud_vars, hud_windows, compress == "huffman")
    print(f"\nheader: {header_path}")

    if fonts:
        report_font_licences(fonts)

    if package:
        sync_package_json(package, blobs)

    if preview and Image is not None:
        for a in atlases:
            p = os.path.join(out_dir, f"preview_{a['name']}.png")
            preview_atlas(a, roles_by_atlas[a["name"]], p, orient=orient)
            print(f"preview: {p}")

    report_budget(entries, budget)

    if scenes:
        # entries[] is in the same order as `ordered`, so index maps straight across.
        sizes = {i: entries[i][2] for i in range(len(entries)) if i < len(ordered)}
        pal_bytes = next((sz for kind, _n, sz in entries if kind == "palette"), 0)
        map_resident = {asset_index[f"PNX_ASSET_MAP_{c_ident(m['name'])}"]:
                        m["resident_bytes"] for m in maps}
        report_scene_budgets(scenes, sizes, pal_bytes, map_resident)

    return 0


def preview_atlas(atlas, roles, path, cols=8, orient=ORIENT_BUTTONS_RIGHT):
    """An annotated contact sheet of the tiles, drawn the way the ARTIST drew them.

    Stored tiles are rotated; the PNG they came from is not. A preview is for checking the
    carve and the autopick against the source art, so it undoes the rotation rather than
    asking someone to tilt their head -- the ids and roles it labels are the same either
    way.
    """
    tiles, T = atlas["tiles"], atlas["tile_px"]
    scale, label = 3, 10
    cell = T * scale
    rows = (len(tiles) + cols - 1) // cols
    img = Image.new("RGB", (cols * cell, rows * (cell + label)), (24, 24, 28))
    draw = ImageDraw.Draw(img)
    tag = {v: k for k, v in roles.items()}

    for idx, buf in enumerate(tiles):
        cx, cy = (idx % cols) * cell, (idx // cols) * (cell + label)
        for j in range(T):
            for i in range(T):
                ri, rj = rotate_point(i, j, T, T, orient)
                v = buf[rj * T + ri]
                if v == TRANSPARENT:
                    continue
                draw.rectangle([cx + i * scale, cy + j * scale,
                                cx + i * scale + scale - 1, cy + j * scale + scale - 1],
                               fill=(((v >> 4) & 3) * 85, ((v >> 2) & 3) * 85,
                                     (v & 3) * 85))
        note = f"{idx} {tag.get(idx, '')}".strip()
        draw.text((cx + 2, cy + cell), note,
                  fill=(120, 240, 140) if idx in tag else (200, 200, 120))
    img.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest")
    ap.add_argument("--out", help="resource output directory")
    ap.add_argument("--header", help="generated header path")
    ap.add_argument("--preview", action="store_true",
                    help="also emit annotated atlas PNGs")
    ap.add_argument("--package", nargs="?", const="package.json",
                    help="rewrite pebble.resources.media in this package.json")
    ap.add_argument("--orientation", choices=sorted(ORIENTATIONS),
                    help="override [project] orientation: where the button cluster sits "
                         "when the device is held to play")
    args = ap.parse_args()

    try:
        return build(args.manifest, args.out, args.header, args.preview, args.package,
                     args.orientation)
    except BuildError as e:
        print(f"\nasset build FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
