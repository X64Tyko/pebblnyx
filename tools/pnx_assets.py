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

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None

# ARGB2222. 0x00 is fully transparent; opaque colours carry alpha 0b11 in the top bits.
TRANSPARENT = 0x00
OPAQUE = 0xC0

# Tile flags, mirrored in pnx_assets.h. Keep the two in step.
FLAG_SOLID = 0x01
FLAG_WARP = 0x02

FLAG_NAMES = {"solid": FLAG_SOLID, "warp": FLAG_WARP}

# Blob format versions. A mismatch between a stale .bin and a newer runtime is exactly
# the kind of failure that presents as garbage pixels rather than an error, so every
# blob is tagged and the runtime checks.
BLOB_VERSION = 5
MAGIC_ATLAS = b"PA"
MAGIC_SPRITE = b"PS"
MAGIC_MAP = b"PM"
MAGIC_DIALOG = b"PD"
MAGIC_PALETTES = b"PP"
MAGIC_SCENES = b"PC"
MAGIC_MUSIC = b"PN"
MAGIC_SAMPLE = b"PW"

HEADER_BYTES = 8

# Index 0 of every palette is transparent, following the SNES convention. Costs a slot
# (15 usable of 16) and measured at 0.1% more bytes across five real tilesets, in
# exchange for uniform transparency and a blitter that can reject a pixel before it ever
# reads the palette.
PALETTE_ENTRIES = 16
PALETTE_USABLE = PALETTE_ENTRIES - 1
TRANSPARENT_INDEX = 0

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


def blob_header(magic, a=0, b=0, c=0, d=0):
    """Common 8-byte prefix: magic[2], version, four format-specific bytes, pad.

    Padded to 8 rather than the 7 it needs, so that pixel data begins at an aligned
    offset. The blitter reads tile rows as words where it can, and a 7-byte header would
    make every one of those accesses unaligned.
    """
    return (magic + bytes([BLOB_VERSION, a & 0xFF, b & 0xFF, c & 0xFF, d & 0xFF])
            + b"\0")


# ---------------------------------------------------------------------------- atlas

def pack_atlas(root, spec):
    """Quantise a region to GColor8 and deduplicate identical tiles.

    Dedup is what makes a large sheet usable: raw, the probe's 1280x1248 source is
    1560KB, past even the 1MB device ceiling. Region selection plus dedup brings it into
    range; compression alone would not, because resources are stored already packed.
    """
    name = spec["name"]
    im = load_sheet(root, spec["sheet"])
    px = im.load()
    T = int(spec["tile"])
    rx, ry, rw, rh = spec["region"]
    max_tiles = int(spec.get("max_tiles", 255))

    sheet_w, sheet_h = im.size
    if (rx + rw) * T > sheet_w or (ry + rh) * T > sheet_h:
        raise BuildError(
            f"atlas {name!r}: region {rx},{ry} {rw}x{rh} tiles of {T}px runs past the "
            f"sheet ({sheet_w}x{sheet_h}px). Region is in TILE units, not pixels.")

    def flip_x(b):
        return b"".join(bytes(reversed(b[j * T:(j + 1) * T])) for j in range(T))

    def flip_y(b):
        return b"".join(bytes(b[(T - 1 - j) * T:(T - j) * T]) for j in range(T))

    # Mirror-aware dedup. A tile that is the horizontal, vertical or 180-degree mirror of
    # one already kept does not need its own copy -- the map entry carries two flip bits
    # and the blitter reads the source backwards. Symmetric art is common enough that this
    # is the cheapest tile saving available: two bits against 128 bytes at 4bpp 16x16.
    unique, seen, empty, mirrored = [], {}, 0, 0
    for ty in range(ry, ry + rh):
        for tx in range(rx, rx + rw):
            buf = bytearray(T * T)
            for j in range(T):
                for i in range(T):
                    buf[j * T + i] = to_gcolor8(px[tx * T + i, ty * T + j])
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
            # The true orientation is registered first so it always wins a later exact
            # match; mirrors only claim keys nothing else has taken.
            seen[key] = (idx, 0)
            for variant, bits in ((flip_x(key), 1),
                                  (flip_y(key), 2),
                                  (flip_x(flip_y(key)), 3)):
                if variant not in seen:
                    seen[variant] = (idx, bits)

    if not unique:
        raise BuildError(f"atlas {name!r}: region contains no non-empty tiles")
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

    print(f"  atlas {name}: {rw*rh} considered, {empty} empty, {len(fixed)} unique")
    if mirrored:
        saved = mirrored * (T * T // 2)
        print(f"    {mirrored} tile(s) matched a mirror of another and were dropped "
              f"({saved} bytes saved)")
    if repaired:
        print(f"    NOTE {repaired} tile(s) exceeded {PALETTE_USABLE} colours and were "
              f"reduced -- edit the art to avoid this")

    return {"name": name, "tiles": fixed, "tile_px": T, "out": spec["out"],
            # Default OFF, not "auto": the runtime cannot read the metatile layout yet,
            # and a default that emits blobs the loader rejects is worse than no
            # feature at all. Flip to "auto" when pnx_atlas_load understands it.
            "repaired": repaired, "metatiles": spec.get("metatiles", False)}


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


def finish_atlas(atlas, tile_flags, shared):
    """Palettise and pack. Called once map compilation has settled tile flags.

    `shared` is the running list of palettes from earlier atlases, so a later atlas
    reuses or extends what already exists instead of building its own.
    """
    tiles, T = atlas["tiles"], atlas["tile_px"]
    sets = [frozenset(c for c in t if c != TRANSPARENT) for t in tiles]

    before = len(shared)
    palettes, assign = merge_palettes(sets, shared)
    shared[:] = palettes

    if any(a is None for a in assign):
        raise BuildError(f"atlas {atlas['name']!r}: a tile still exceeds "
                         f"{PALETTE_USABLE} colours after reduction")
    if len(palettes) > 255:
        raise BuildError(f"atlas {atlas['name']!r}: {len(palettes)} palettes, but the "
                         f"per-tile palette index is a u8")

    flags = bytes(tile_flags.get(i, 0) for i in range(len(tiles)))

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

        body = (len(bank).to_bytes(2, "little") + b"\0\0"
                + pad4(bytes(assign)) + pad4(flags) + bytes(table) + pixels)
        atlas["blob"] = blob_header(MAGIC_ATLAS, T, len(tiles), 1) + body
        atlas["subtiles"] = len(bank)
    else:
        pixels = b"".join(pack_unit_4bpp(t, palettes[a]) for t, a in zip(tiles, assign))
        body = pad4(bytes(assign)) + pad4(flags) + pixels
        atlas["blob"] = blob_header(MAGIC_ATLAS, T, len(tiles), 0) + body
        atlas["subtiles"] = 0
    atlas["palettes"] = palettes
    atlas["assign"] = assign
    print(f"    {atlas['name']}: uses {len(set(assign))} palette(s), "
          f"{len(palettes) - before} new to the project")
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


def merge_palettes(colour_sets, existing=None):
    """Greedy merge of per-unit colour sets into palettes of PALETTE_USABLE colours.

    Merging rather than deduplicating is the whole trick. Deduplicating identical sets
    leaves 391 palettes across five real tilesets; merging any sets whose union still
    fits leaves 43 -- losslessly, because a tile is perfectly happy in a palette that
    merely contains its colours.

    `existing` lets a later atlas reuse or extend palettes an earlier one built, which is
    how sharing is discovered rather than declared.
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
            if len(p | colours) <= PALETTE_USABLE:
                palettes[pi] = p | colours
                assign[i] = pi
                break
        else:
            palettes.append(set(colours))
            assign[i] = len(palettes) - 1

    return palettes, [assign[i] for i in range(len(colour_sets))]


def palette_bytes(palette):
    """16 entries, index 0 transparent. Sets are sorted for deterministic builds; an Ordered
    palette keeps the order it was given, because its positions are already referenced by
    packed pixel data."""
    entries = [TRANSPARENT_INDEX] + (list(palette) if isinstance(palette, Ordered)
                                     else sorted(palette))
    if len(entries) > PALETTE_ENTRIES:
        raise BuildError(f"palette has {len(entries)} entries, max {PALETTE_ENTRIES}")
    return bytes(entries + [0] * (PALETTE_ENTRIES - len(entries)))


def pack_unit_4bpp(pixels, palette):
    """Two pixels per byte, high nibble first."""
    order = list(palette) if isinstance(palette, Ordered) else sorted(palette)
    lut = {c: i + 1 for i, c in enumerate(order)}
    lut[0] = TRANSPARENT_INDEX
    out = bytearray()
    for i in range(0, len(pixels), 2):
        out.append((lut[pixels[i]] << 4) | lut[pixels[i + 1]])
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


# --------------------------------------------------------------------------- sprite

def pack_sprite(root, spec):
    name = spec["name"]
    im = load_sheet(root, spec["sheet"])
    px = im.load()
    key = spec.get("colorkey")
    sheet_w, sheet_h = im.size

    frames, fw, fh = [], None, None
    for idx, rect in enumerate(spec["frames"]):
        x, y, w, h = rect
        if fw is None:
            fw, fh = w, h
        elif (w, h) != (fw, fh):
            raise BuildError(f"sprite {name!r}: frame {idx} is {w}x{h}, but frame 0 is "
                             f"{fw}x{fh} -- all frames must match")
        if x + w > sheet_w or y + h > sheet_h:
            raise BuildError(f"sprite {name!r}: frame {idx} at {x},{y} {w}x{h} runs "
                             f"past the sheet ({sheet_w}x{sheet_h})")
        buf = bytearray(w * h)
        for j in range(h):
            for i in range(w):
                buf[j * w + i] = to_gcolor8(px[x + i, y + j], key)
        frames.append(bytes(buf))

    for anim_name, frame_idx in spec.get("anim", {}).items():
        if not 0 <= frame_idx < len(frames):
            raise BuildError(f"sprite {name!r}: anim {anim_name!r} points at frame "
                             f"{frame_idx}, but there are only {len(frames)}")

    if (fw * fh) % 2:
        raise BuildError(f"sprite {name!r}: {fw}x{fh} has an odd pixel count, which "
                         f"cannot pack two-per-byte at 4bpp")

    repaired = 0
    fixed = []
    for f in frames:
        f2, merged = reduce_colours(f)
        if merged:
            repaired += 1
        fixed.append(f2)

    print(f"  sprite {name}: {len(fixed)} frames of {fw}x{fh}")
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
        for (x, y, w, h) in spec["frames"]:
            buf = bytearray(w * h)
            for j in range(h):
                for i in range(w):
                    buf[j * w + i] = to_gcolor8(vpx[x + i, y + j], key)
            vframes.append(reduce_colours(bytes(buf))[0])

        # The check that makes sharing safe: same shape, any colours.
        for idx, (base_f, var_f) in enumerate(zip(fixed, vframes)):
            if shape_signature(base_f) != shape_signature(var_f):
                raise BuildError(
                    f"sprite {name!r}: variant {vpath!r} frame {idx} is not a recolour of "
                    f"the base -- its pixel layout differs. A variant may change any colour "
                    f"but must not move, add or remove a pixel, and transparency must match. "
                    f"Drop it from `variants` and declare it as its own sprite instead.")
        variants.append({"name": vname, "path": vpath, "frames": vframes})

    return {"name": name, "w": fw, "h": fh, "frames": fixed, "variants": variants,
            "out": spec["out"], "anim": spec.get("anim", {}), "repaired": repaired}


def finish_sprite_with_variants(sprite, shared):
    """One bitmap, one palette per variant.

    Every frame packs against a single ORDERED palette rather than per-frame merged ones. It
    has to be one palette: the pixel data is shared across variants, so the index of a colour
    must mean the same thing in every frame and every recolour. And it has to be ordered,
    because two recolours sort into different colour orders and would otherwise pack to
    different indices -- which would defeat the sharing entirely.
    """
    name = sprite["name"]
    frames = sprite["frames"]
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
    pixels = b"".join(pack_unit_4bpp(f, shared[base_slot]) for f in frames)
    assign = [base_slot] * len(frames)

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

    sprite["blob"] = blob_header(MAGIC_SPRITE, sprite["w"], sprite["h"],
                                 len(frames)) + pad4(bytes(assign)) + pixels
    sprite["palettes"] = [shared[base_slot]]
    sprite["assign"] = assign
    sprite["variant_slots"] = variant_slots

    frame_bytes = sprite["w"] * sprite["h"] // 2 * len(frames)
    saved = frame_bytes * len(sprite["variants"])
    print(f"    {name}: 1 shared palette, {len(sprite['variants'])} variant(s) collapsed "
          f"({saved:,} B saved, {len(variant_slots) * PALETTE_ENTRIES} B of palettes)")
    return sprite


def finish_sprite(sprite, shared):
    frames = sprite["frames"]

    if sprite.get("variants"):
        return finish_sprite_with_variants(sprite, shared)

    sets = [frozenset(c for c in f if c != TRANSPARENT) for f in frames]

    before = len(shared)
    palettes, assign = merge_palettes(sets, shared)
    shared[:] = palettes

    if any(a is None for a in assign):
        raise BuildError(f"sprite {sprite['name']!r}: a frame still exceeds "
                         f"{PALETTE_USABLE} colours after reduction")

    pixels = b"".join(pack_unit_4bpp(f, palettes[a]) for f, a in zip(frames, assign))
    body = pad4(bytes(assign)) + pixels

    sprite["blob"] = blob_header(MAGIC_SPRITE, sprite["w"], sprite["h"],
                                 len(frames)) + body
    sprite["palettes"] = palettes
    sprite["assign"] = assign
    print(f"    {sprite['name']}: uses {len(set(assign))} palette(s), "
          f"{len(palettes) - before} new to the project")
    return sprite


# ------------------------------------------------------------------------------ map

def parse_legend(raw):
    """legend char -> (tile role name, flag byte)."""
    legend = {}
    for ch, entry in raw.items():
        if len(ch) != 1:
            raise BuildError(f"legend key {ch!r} must be exactly one character")
        flags = 0
        for f in entry.get("flags", []):
            if f not in FLAG_NAMES:
                raise BuildError(f"legend {ch!r}: unknown flag {f!r} "
                                 f"(known: {', '.join(sorted(FLAG_NAMES))})")
            flags |= FLAG_NAMES[f]
        legend[ch] = (entry["tile"], flags)
    return legend


def compile_map(spec, legend, roles, map_names):
    # `roles` is this map's atlas's role table, not a global one.
    """ASCII rows -> binary map resource.

    Format after the 8-byte header (w, h, warp_count in bytes 3..5):
        w*h tile bytes, then w*h flag bytes, then
        warp_count * (u8 tx, u8 ty, u8 dest_map, u8 dest_tx, u8 dest_ty)
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

    tiles = bytearray(w * h * 2)   # u16 per cell
    flags = bytearray(w * h)   # one byte per cell; tiles are u16, see below
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch not in legend:
                raise BuildError(f"map {name!r}: unknown legend char {ch!r} at {x},{y} "
                                 f"(known: {' '.join(sorted(legend))})")
            role, flag = legend[ch]
            if role not in roles:
                raise BuildError(
                    f"map {name!r}: legend {ch!r} names tile role {role!r}, which its "
                    f"atlas does not define. That atlas provides: "
                    f"{', '.join(sorted(roles)) or '(no roles -- give it an autopick or '
                                                   '[atlas.semantic] table)'}")
            # u16 little-endian: 10 bits of index, then PNX_MAP_FLIP_X / _Y, then four
            # reserved bits for a per-cell palette. Roles resolve to unmirrored tiles, so
            # the flip bits are zero today -- the format carries them so a tile picker can
            # place a mirrored tile without the atlas needing a second copy.
            entry = roles[role] & 0x03FF
            tiles[(y * w + x) * 2] = entry & 0xFF
            tiles[(y * w + x) * 2 + 1] = entry >> 8
            flags[y * w + x] = flag

    sx, sy = spec["start"]
    if not (0 <= sx < w and 0 <= sy < h):
        raise BuildError(f"map {name!r}: start {(sx, sy)} is outside the {w}x{h} map")
    if flags[sy * w + sx] & FLAG_SOLID:
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
        if flags[y * w + x] & FLAG_SOLID:
            continue
        reachable.add((x, y))
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]

    walkable = sum(1 for i in range(w * h) if not (flags[i] & FLAG_SOLID))
    sealed = walkable - len(reachable)

    warps = []
    for wp in spec.get("warps", []):
        tx, ty = wp["at"]
        dest_name, dtx, dty = wp["to"]
        if not (0 <= tx < w and 0 <= ty < h):
            raise BuildError(f"map {name!r}: warp at {(tx, ty)} is outside the map")
        if not flags[ty * w + tx] & FLAG_WARP:
            raise BuildError(f"map {name!r}: warp at {(tx, ty)} sits on a tile with no "
                             f"'warp' flag -- the player would walk over it and nothing "
                             f"would happen")
        if flags[ty * w + tx] & FLAG_SOLID:
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
          + (f", {sealed} sealed off" if sealed else ""))



    # The blob is built later by finish_map, once tile flag defaults are known.
    return {"name": name, "w": w, "h": h, "start": (sx, sy), "tiles": bytes(tiles),
            "out": spec["out"], "warps": warps, "reachable": reachable,
            "flags": flags}


def compute_tile_flags(maps):
    """Pick each tile's default flags: whichever value it carries most often.

    Derived rather than declared, so the manifest needs no new syntax and the legend
    stays the single place behaviour is written down. Any cell disagreeing with its
    tile's default becomes an override -- which is how one accent tile can be scenery in
    one spot and a door in another.
    """
    tally = {}
    for m in maps:
        for tile, flag in zip(m["tiles"], m["flags"]):
            tally.setdefault(tile, {}).setdefault(flag, 0)
            tally[tile][flag] += 1

    defaults = {}
    for tile, counts in tally.items():
        defaults[tile] = max(counts.items(), key=lambda kv: kv[1])[0]
    return defaults


def finish_map(m, tile_defaults, atlas_asset):
    """Build the map blob, replacing the flag plane with sparse overrides.

    `atlas_asset` is stored in the header so the runtime can find the tileset a map was
    authored against. Without it a scene with two atlases would have to guess, and
    guessing wrong draws a map in another tileset's tiles -- which looks like corrupted
    art rather than a mismatch.
    """
    w, h = m["w"], m["h"]
    overrides = bytearray()
    count = 0
    for y in range(h):
        for x in range(w):
            i = y * w + x
            tile = m["tiles"][i * 2] | (m["tiles"][i * 2 + 1] << 8)
            tile &= 0x03FF
            if m["flags"][i] != tile_defaults.get(tile, 0):
                overrides += bytes([x, y, m["flags"][i]])
                count += 1

    if count > 0xFFFF:
        raise BuildError(f"map {m['name']!r}: {count} flag overrides exceeds the u16 "
                         f"limit -- the tile flag defaults must be badly chosen")

    body = (count.to_bytes(2, "little") + b"\0\0"
            + m["tiles"] + bytes(overrides)
            + b"".join(bytes(x) for x in m["warps"]))
    m["blob"] = blob_header(MAGIC_MAP, w, h, len(m["warps"]), atlas_asset) + body

    saved = w * h - len(overrides)   # flag plane would be 1 byte/cell
    print(f"  map {m['name']}: {count} flag overrides "
          f"({saved} bytes saved over a per-cell plane)")
    return m


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
            if dest["flags"][dty * dest["w"] + dtx] & FLAG_SOLID:
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

def pack_dialog(dialogs):
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
    blob = blob_header(MAGIC_DIALOG, len(names)) + bytes(body)
    return {"names": names, "index": index, "blob": blob}


# -------------------------------------------------------------------------- samples

# One second of 16 kHz 8-bit PCM is 16,000 bytes. With ~70KB left after art, four seconds
# of recorded audio would consume the entire remaining content budget -- so samples are
# for short effects and nothing else. The cap is enforced rather than advised, because
# the failure is otherwise discovered as a bundle that will not ship.
SAMPLE_MAX_MS = 1500
SAMPLE_RATE = 16000


def pack_samples(root, specs):
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
        blob = blob_header(MAGIC_SAMPLE, 0, 0, 0, 0) + body
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


def pack_music_names(man):
    """Song names only, for the id ordering, without recompiling them."""
    return [{"name": n} for n in sorted(man.get("music", {}))]


def pack_music(specs):
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

        blob = blob_header(MAGIC_MUSIC, len(patterns), len(order), rows_per,
                           len(instruments)) + body
        print(f"  music {name}: {len(patterns)} patterns x {rows_per} rows, "
              f"{len(instruments)} instruments, {tempo}bpm, {len(blob)} bytes")
        songs.append({"name": name, "blob": blob, "out": f"music_{name}.bin"})
    return songs


# --------------------------------------------------------------------------- scenes

def build_scenes(man, asset_index, maps=()):
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

        for kind, key in (("ATLAS", "atlases"), ("SPRITE", "sprites")):
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

        # A scene whose map is drawn with a tileset the scene does not load cannot
        # possibly work. The runtime refuses it, but by then the author is holding a
        # watch showing nothing -- so it is caught here instead.
        if "map" in spec:
            for m in maps:
                if m["name"] != spec["map"]:
                    continue
                needed = f"PNX_ASSET_ATLAS_{c_ident(m['atlas'])}"
                if asset_index.get(needed) not in ids:
                    raise BuildError(
                        f"scene {name!r}: map {m['name']!r} is drawn with atlas "
                        f"{m['atlas']!r}, but the scene does not load it -- add "
                        f'atlases = ["{m["atlas"]}"]')

        index.append((len(entries), len(ids), map_id))
        entries.extend(ids)

    body = bytearray()
    for first, count, map_id in index:
        body += first.to_bytes(2, "little") + bytes([count, map_id])
    for asset_id in entries:
        body += asset_id.to_bytes(2, "little")

    print(f"  scenes: {len(names)} declared, {len(entries)} asset references")
    return {"names": names, "index": index, "entries": entries,
            "blob": blob_header(MAGIC_SCENES, len(names)) + bytes(body)}


def report_scene_budgets(scenes, sizes, palette_bytes_total):
    """Per-scene resident cost -- the number that decides the scene arena size.

    Total resource size says what ships; this says what has to be in RAM at once, which
    is the constraint that actually bites. Palettes are counted into every scene because
    they load before anything else does.
    """
    if not scenes:
        return
    print("\nscene residency (what must fit in the scene arena at once)")
    worst, worst_name = 0, ""
    for i, name in enumerate(scenes["names"]):
        first, count, _ = scenes["index"][i]
        total = palette_bytes_total + sum(sizes.get(a, 0)
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


def generate_header(path, atlases, sprites, maps, dialog, roles, palette_count=0,
                    scenes=None, songs=None, samples=None):
    L = [
        "// GENERATED by tools/pnx_assets.py -- do not edit.",
        "//",
        "// Regenerate with the manifest, never by hand. Nothing here should be typed",
        "// into game code by name: use the symbols, not the numbers.",
        "",
        "#pragma once",
        "",
        "#include <stdint.h>",
        "",
    ]

    assets = ([("PALETTES", "palettes")]
              + [("ATLAS", a["name"]) for a in atlases]
              + [("SPRITE", s["name"]) for s in sprites]
              + [("MAP", m["name"]) for m in maps]
              + ([("DIALOG", "dialog")] if dialog else [])
              + [("MUSIC", sg["name"]) for sg in (songs or [])]
              + [("SAMPLE", sm["name"]) for sm in (samples or [])]
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
    L += ["}", "#endif", ""]

    for a in atlases:
        n = c_ident(a["name"])
        L += [f"#define {n}_TILE_PX {a['tile_px']}",
              f"#define {n}_TILE_BYTES {a['tile_px'] * a['tile_px'] // 2}",
              f"#define {n}_TILE_COUNT {len(a['tiles'])}",
              f"#define {n}_PALETTE_COUNT {len(a['palettes'])}", ""]

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
        L += [f"#define {n}_W {s['w']}", f"#define {n}_H {s['h']}",
              f"#define {n}_FRAME_BYTES {s['w'] * s['h'] // 2}",
              f"#define {n}_FRAME_COUNT {len(s['frames'])}"]
        for anim_name, idx in sorted(s["anim"].items()):
            L.append(f"#define {n}_{c_ident(anim_name)} {idx}")
        L.append("")

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

    pkg.setdefault("pebble", {}).setdefault("resources", {})["media"] = media

    with open(path, "w") as f:
        json.dump(pkg, f, indent=2)
        f.write("\n")

    print(f"package.json: {len(media)} resources declared")


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

    if total > budget:
        raise BuildError(f"resources exceed the {budget} byte budget by "
                         f"{total - budget} bytes")
    return total


# ----------------------------------------------------------------------------- main

def build(manifest_path, out_dir, header_path, preview=False, package=None):
    root = os.path.dirname(os.path.abspath(manifest_path))
    with open(manifest_path, "rb") as f:
        man = tomllib.load(f)

    project = man.get("project", {})
    budget = int(project.get("budget_bytes", DEFAULT_BUDGET))

    out_dir = out_dir or os.path.join(root, project.get("resources", "resources"))
    header_path = header_path or os.path.join(root, project.get("header",
                                                                "src/c/assets_gen.h"))

    entries = []
    blobs = []

    print("building assets")
    atlases = [pack_atlas(root, a) for a in man.get("atlas", [])]

    # Tile roles are PER ATLAS. They used to be one shared dict, which forced the
    # single-tileset restriction: two atlases both defining "wall" would collide, and a
    # map had no way to say which one it meant. Explicit `semantic` still wins over
    # `autopick`, so a manifest can start auto and be pinned down later.
    roles_by_atlas = {}
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

    sprites = [pack_sprite(root, sp) for sp in man.get("sprite", [])]

    legend = parse_legend(man.get("legend", {}))
    map_specs = man.get("map", [])
    map_names = [m["name"] for m in map_specs]
    if len(set(map_names)) != len(map_names):
        raise BuildError("duplicate map names")
    if not atlases and map_specs:
        raise BuildError("maps need at least one atlas to draw with")

    default_atlas = atlases[0]["name"] if atlases else None
    maps = []
    for spec in map_specs:
        which = spec.get("atlas", default_atlas)
        if which not in roles_by_atlas:
            raise BuildError(f"map {spec['name']!r}: atlas {which!r} is not defined "
                             f"(known: {', '.join(sorted(roles_by_atlas))})")
        m = compile_map(spec, legend, roles_by_atlas[which], map_names)
        m["atlas"] = which
        maps.append(m)
    check_warp_destinations(maps)

    # Tile flags live on the tileset, so each atlas takes its defaults only from the maps
    # that actually use it -- otherwise one tileset's flags would leak into another.
    flags_by_atlas = {a["name"]: compute_tile_flags([m for m in maps
                                                     if m["atlas"] == a["name"]])
                      for a in atlases}

    dialog_specs = man.get("dialog", {})

    # Computed here rather than after packing, because a map blob now records the asset
    # id of its atlas -- which is how the runtime pairs the two without depending on the
    # order a scene happens to load them in.
    ordered = (["PNX_ASSET_PALETTES_PALETTES"]
               + [f"PNX_ASSET_ATLAS_{c_ident(a['name'])}" for a in atlases]
               + [f"PNX_ASSET_SPRITE_{c_ident(sp['name'])}" for sp in sprites]
               + [f"PNX_ASSET_MAP_{c_ident(m['name'])}" for m in maps]
               + (["PNX_ASSET_DIALOG_DIALOG"] if dialog_specs else [])
               + [f"PNX_ASSET_MUSIC_{c_ident(sg['name'])}" for sg in
                  pack_music_names(man)]
               + [f"PNX_ASSET_SAMPLE_{c_ident(n)}" for n in
                  sorted(man.get("sample", {}))])
    asset_index = {h: i for i, h in enumerate(ordered)}

    for m in maps:
        atlas_asset = asset_index[f"PNX_ASSET_ATLAS_{c_ident(m['atlas'])}"]
        finish_map(m, flags_by_atlas[m["atlas"]], atlas_asset)

    # One running palette list across every asset, so a later atlas or sprite reuses or
    # extends what earlier ones built. Sharing is discovered, never declared.
    print("palettes:")
    shared = []
    for a in atlases:
        finish_atlas(a, flags_by_atlas[a["name"]], shared)
    for sp in sprites:
        finish_sprite(sp, shared)
    palette_blob = (blob_header(MAGIC_PALETTES, len(shared))
                    + b"".join(palette_bytes(p) for p in shared))
    print(f"  {len(shared)} palettes, {len(shared) * PALETTE_ENTRIES} B shared across "
          f"every asset -- set PNX_PALETTE_SLOTS >= {len(shared)}")

    dialog = pack_dialog(dialog_specs) if dialog_specs else None
    songs = pack_music(man.get("music", {})) if man.get("music") else []
    samples = pack_samples(root, man.get("sample", {})) if man.get("sample") else []

    # Order here defines the PnxAssetId enum and the resource table, so it must match
    # generate_header's. Both walk atlases, sprites, maps, dialog in that order.
    pal_out = project.get("palettes_out", "palettes.bin")
    entries.append(("palette", "palettes",
                    write_blob(os.path.join(out_dir, pal_out), palette_blob)))
    blobs.append(("palettes", pal_out))

    for a in atlases:
        entries.append(("atlas", a["name"],
                        write_blob(os.path.join(out_dir, a["out"]), a["blob"])))
        blobs.append((a["name"], a["out"]))
    for sp in sprites:
        entries.append(("sprite", sp["name"],
                        write_blob(os.path.join(out_dir, sp["out"]), sp["blob"])))
        blobs.append((sp["name"], sp["out"]))
    for m in maps:
        entries.append(("map", m["name"],
                        write_blob(os.path.join(out_dir, m["out"]), m["blob"])))
        blobs.append((m["name"], m["out"]))
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

    scenes = build_scenes(man, asset_index, maps)
    if scenes:
        scene_out = project.get("scenes_out", "scenes.bin")
        entries.append(("scene", "scenes",
                        write_blob(os.path.join(out_dir, scene_out), scenes["blob"])))
        blobs.append(("scenes", scene_out))
        ordered.append("PNX_ASSET_SCENES_SCENES")
        asset_index["PNX_ASSET_SCENES_SCENES"] = len(ordered) - 1

    generate_header(header_path, atlases, sprites, maps, dialog, roles_by_atlas,
                    len(shared), scenes, songs, samples)
    print(f"\nheader: {header_path}")

    if package:
        sync_package_json(package, blobs)

    if preview and Image is not None:
        for a in atlases:
            p = os.path.join(out_dir, f"preview_{a['name']}.png")
            preview_atlas(a, roles_by_atlas[a["name"]], p)
            print(f"preview: {p}")

    report_budget(entries, budget)

    if scenes:
        # entries[] is in the same order as `ordered`, so index maps straight across.
        sizes = {i: entries[i][2] for i in range(len(entries)) if i < len(ordered)}
        pal_bytes = next((sz for kind, _n, sz in entries if kind == "palette"), 0)
        report_scene_budgets(scenes, sizes, pal_bytes)

    return 0


def preview_atlas(atlas, roles, path, cols=8):
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
                v = buf[j * T + i]
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
    args = ap.parse_args()

    try:
        return build(args.manifest, args.out, args.header, args.preview, args.package)
    except BuildError as e:
        print(f"\nasset build FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
