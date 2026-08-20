#!/usr/bin/env python3
"""The `.pnxmap` source format: a map as its own file rather than text in the manifest.

WHY THIS EXISTS
---------------
A map used to be an ASCII grid inside `assets.toml`, one character per cell, resolved
through a legend. That format is genuinely good at what it is good at -- a map is legible
at a glance, walls look like walls, and the unreachable-door bug that motivated the
pipeline's flood fill was *visible* in the text. It has two ceilings that a visual editor
runs straight into:

  * One character per cell means the printable set is the limit on how many DISTINCT
    tiles a map can place -- about ninety. The compiled cell is a u16 carrying a 10-bit
    index, so the runtime holds 1024. The authoring format was the smaller number by 11x.
  * A 255x255 map is ~65KB of text. Put two in a manifest and the manifest is map data
    with a project buried in it.

So a map may now live in its own file. `rows` is NOT deprecated and is still what the
`overworld` example uses: it stays the readable, hand-authorable, diffable form, and it is
the right choice for a small map. This is the other end of the same trade -- you give up
the readable git diff and get the tile ceiling and the file size back.

WHAT THIS IS NOT
----------------
**This is not the compiled resource.** `map_*.bin` is derived from this: rotated at build
time for a landscape orientation (M4c), sliced into WorldTiles, its flag plane computed
and de-duplicated against per-tile defaults. The same source compiles differently for
portrait and landscape, which is exactly why the source cannot be the thing that ships --
a project that threw this away could never rebuild for another orientation.

THE TILE TABLE IS THE LEGEND
----------------------------
A cell holds a u16 index into a per-map tile table, and each entry is
`(atlas, index, flip, flags)` -- which is precisely what a legend character resolves to
today. The legend did not go away; it stopped being spelled in ASCII and stopped being
shared across every map in the project.

Atlases are named through a string table rather than by position in the manifest's
`atlases` list, so reordering that list cannot silently remap every tile in a map.

An entry may name a ROLE instead of an index. That is the one property the ASCII legend
had that a table of numbers would otherwise lose: `autopick` re-derives a role from the
art on every build, so a tile referred to by role survives re-carving a sheet, while a
raw index does not. Keeping it means migrating a manifest to this format never silently
downgrades a symbolic reference into a brittle number.

FORMAT (all little-endian)
--------------------------
    magic       4   b"PNXM"
    version     1   FORMAT_VERSION
    reserved    1   0
    w           1   columns, 1..255
    h           1   rows, 1..255
    start_x     1
    start_y     1
    tile_count  2   entries in the tile table
    warp_count  2
    str_count   2
    strings         str_count * (u8 length, UTF-8 bytes)
    tiles           tile_count * (u16 atlas_str, u16 index, u16 role_str, u8 flip,
                                  u8 flags, u8 extended)
    warps           warp_count * (u8 at_x, u8 at_y, u16 dest_str, u8 to_x, u8 to_y,
                                  u8 gated)
    cells           w * h * u16, row-major, each an index into the tile table

FORMAT_VERSION 2 added `extended` to the tile table (a placement-authored u8 tag, see
MAP_EXTENDED in pnx_assets.py) -- a real byte, not a spare bit the way `rotate` was, so it
could not ride in unclaimed space the way rotate did and the version had to bump. A v1
file is refused rather than read with extended assumed 0, matching every other version
mismatch this module refuses (see `read`'s own comment).

FORMAT_VERSION 3 (M13 authoring) replaced the single `w, h` pair with a small layer
directory: `layer_count` (B) where the old `reserved` byte sat, then `layer_count *
(w, h)` right after the tile table, and `cells` becomes each layer's `w * h` u16 plane
concatenated in layer order. Layer 0 is always PRIMARY -- `start`/`warps` are its and its
alone, the same rule the runtime's own `primary_layer` field states (PnxMap's comment,
pnx_assets.h) -- so this module never needs to plumb an arbitrary primary index through
`write`/`read`, only always put the primary layer first. The tile table and warp list stay
exactly as they were: ONE shared table every layer's cells index into, matching the packed
blob's own shared cell dictionary (a map's tile id space is one shared thing, not one per
layer -- see PnxMap's comment on `cell_dict`).

Unlike the v1->v2 bump, a v2 file is not refused: `read`/`loads` upgrade one transparently
into a single-layer v3 doc in memory (see `_loads_v2`), so every `.pnxmap` already on disk
keeps opening with no manifest or file changes. The file on disk only becomes v3 once
something explicitly writes it again -- reading never rewrites it.
"""

import struct

MAGIC = b"PNXM"
FORMAT_VERSION = 3

# magic, version, layer_count, primary_layer, start_x, start_y, tile_count, warp_count,
# str_count. `primary_layer` is stored for the reader's sake but this module always WRITES
# 0 -- see the module docstring's v3 paragraph for why an arbitrary primary index is not
# worth plumbing through.
_HEADER = struct.Struct("<4sBBBBBHHH")
_LAYER_DIR = struct.Struct("<BB")  # w, h -- one per layer, layer_count of them
_TILE = struct.Struct("<HHHBBB")
NO_ROLE = 0xFFFF
_WARP = struct.Struct("<BBHBBB")

# The exact v2 layout, kept only so `_loads_v2` can decode a file nothing writes any more.
_HEADER_V2 = struct.Struct("<4sBBBBBBHHH")


class MapFileError(Exception):
    """A `.pnxmap` that cannot be read. Separate from BuildError so the editor can tell
    'this file is not a map' from 'this map does not build'."""


def write(path, w, h, start, tiles, cells, warps=()):
    """Write a single-layer map source file -- unchanged signature, still what every
    existing caller uses. Sugar for `write_layers` with one layer, which is also the only
    thing that changed: the bytes on disk are v3 now (see the module docstring), not v2,
    but a single-layer file reads back exactly the same doc shape it always has.
    """
    return write_layers(path, [{"w": w, "h": h, "cells": cells}], tiles, start, warps)


def write_layers(path, layers, tiles, start, warps=()):
    """Write a map source file, possibly more than one layer.

    `layers` is a list of {"w", "h", "cells"}, PRIMARY first (`layers[0]`) -- the only one
    `start`/`warps` apply to, matching the runtime's own primary-layer-owns-warps rule
    (PnxMap's comment, pnx_assets.h). Every layer's `cells` indexes the SAME shared `tiles`
    table: {atlas, index, flip, rotate, flags, extended} dicts exactly as `write` already
    took them. `cells` is a flat list of `w * h` indices into `tiles`, one list per layer.

    rotate rides in the same u8 as flip (bit 2, 0x04) rather than widening the row --
    old files have that bit unset, which correctly decodes as rotate=False, so that one
    was additive and did not bump FORMAT_VERSION. `extended` could not do the same trick
    -- it is a value, not a flag, so it needed a whole byte of its own and FORMAT_VERSION
    bumped to 2 for it, and again to 3 for the layer directory (see the module docstring).
    """
    if not layers:
        raise MapFileError("a map needs at least one layer")
    if len(layers) > 255:
        raise MapFileError(f"{len(layers)} layers exceeds the 255 this format stores")
    for i, layer in enumerate(layers):
        w, h = layer["w"], layer["h"]
        if not (1 <= w <= 255 and 1 <= h <= 255):
            raise MapFileError(f"layer {i}: {w}x{h} is outside the 1..255 the format "
                               f"stores per axis")
        if len(layer["cells"]) != w * h:
            raise MapFileError(f"layer {i}: expected {w * h} cells for a {w}x{h} map, "
                               f"got {len(layer['cells'])}")
    if not tiles:
        raise MapFileError("a map needs at least one tile table entry")
    for i, layer in enumerate(layers):
        for c in layer["cells"]:
            if not 0 <= c < len(tiles):
                raise MapFileError(f"layer {i}: cell index {c} is outside the "
                                   f"{len(tiles)}-entry tile table")

    # One string table for atlas names and warp destinations both: a map's destinations
    # are few and its atlases fewer, and two tables would be two things to keep in step.
    strings, index = [], {}

    def intern(s):
        if s not in index:
            index[s] = len(strings)
            strings.append(s)
        return index[s]

    tile_rows = []
    for t in tiles:
        flip = 0
        for axis in t.get("flip", "") or "":
            if axis not in "xy":
                raise MapFileError(f"flip must be 'x', 'y' or both, not {axis!r}")
            flip |= 1 if axis == "x" else 2
        if t.get("rotate", False):
            flip |= 4

        # An entry names either a raw index into the atlas or a role the atlas defines.
        # A string in `index` is the role spelling, which is what from_rows produces from
        # a legend -- so a migrated map keeps naming `wall` rather than freezing whatever
        # number `wall` happened to be on the day it was converted.
        raw = t["index"]
        if isinstance(raw, str):
            role, idx = intern(raw), 0
        else:
            role, idx = NO_ROLE, int(raw)
            if not 0 <= idx <= 0xFFFF:
                raise MapFileError(f"tile index {idx} does not fit a u16")
        ext = int(t.get("extended", 0))
        if not 0 <= ext <= 0xFF:
            raise MapFileError(f"extended value {ext} does not fit a u8")
        tile_rows.append((intern(t["atlas"]), idx, role, flip,
                          int(t.get("flags", 0)) & 0xFF, ext))

    warp_rows = []
    for wp in warps:
        ax, ay = wp["at"]
        dest, tx, ty = wp["to"]
        warp_rows.append((int(ax), int(ay), intern(dest), int(tx), int(ty),
                          1 if wp.get("gated") else 0))

    body = bytearray()
    body += _HEADER.pack(MAGIC, FORMAT_VERSION, len(layers), 0,
                         int(start[0]), int(start[1]),
                         len(tile_rows), len(warp_rows), len(strings))
    for s in strings:
        raw = s.encode("utf-8")
        if len(raw) > 255:
            raise MapFileError(f"name {s!r} is longer than the 255 bytes the table holds")
        body += bytes([len(raw)]) + raw
    for row in tile_rows:
        body += _TILE.pack(*row)
    for row in warp_rows:
        body += _WARP.pack(*row)
    for layer in layers:
        body += _LAYER_DIR.pack(layer["w"], layer["h"])
    for layer in layers:
        for c in layer["cells"]:
            body += int(c).to_bytes(2, "little")

    with open(path, "wb") as f:
        f.write(body)
    return len(body)


def read(path):
    """Read a map source file into the same shape `write` takes."""
    with open(path, "rb") as f:
        data = f.read()
    return loads(data, where=path)


def _read_strings(data, at, str_count, where):
    strings = []
    for _ in range(str_count):
        if at >= len(data):
            raise MapFileError(f"{where}: string table runs past the end of the file")
        n = data[at]
        at += 1
        strings.append(data[at:at + n].decode("utf-8"))
        at += n
    return strings, at


def _read_tiles(data, at, tile_count, strings, where):
    def name(i):
        if not 0 <= i < len(strings):
            raise MapFileError(f"{where}: string index {i} is outside the table")
        return strings[i]

    tiles = []
    for _ in range(tile_count):
        astr, idx, role, flip, flags, extended = _TILE.unpack_from(data, at)
        at += _TILE.size
        tiles.append({"atlas": name(astr),
                      "index": name(role) if role != NO_ROLE else idx,
                      "flip": ("x" if flip & 1 else "") + ("y" if flip & 2 else ""),
                      "rotate": bool(flip & 4),
                      "flags": flags,
                      "extended": extended})
    return tiles, at, name


def _read_warps(data, at, warp_count, name):
    warps = []
    for _ in range(warp_count):
        ax, ay, dstr, tx, ty, gated = _WARP.unpack_from(data, at)
        at += _WARP.size
        warps.append({"at": [ax, ay], "to": [name(dstr), tx, ty],
                      "gated": bool(gated)})
    return warps, at


def _read_cells(data, at, w, h, tiles, where, label=""):
    need = w * h * 2
    if len(data) - at < need:
        raise MapFileError(f"{where}: {label}{w}x{h} needs {need} bytes of cells, "
                           f"{len(data) - at} remain")
    cells = list(struct.unpack_from(f"<{w * h}H", data, at))
    for c in cells:
        if c >= len(tiles):
            raise MapFileError(f"{where}: {label}a cell names tile {c}, but the table "
                               f"holds {len(tiles)}")
    return cells, at + need


def _loads_v2(data, where):
    """The exact v2 layout: single `w, h` header, one cell plane. Nothing writes this any
    more (see the module docstring's v3 paragraph) but plenty of files on disk still are.
    """
    (magic, version, _res, w, h, sx, sy,
     tile_count, warp_count, str_count) = _HEADER_V2.unpack_from(data, 0)
    at = _HEADER_V2.size
    strings, at = _read_strings(data, at, str_count, where)
    tiles, at, name = _read_tiles(data, at, tile_count, strings, where)
    warps, at = _read_warps(data, at, warp_count, name)
    cells, at = _read_cells(data, at, w, h, tiles, where)
    return {"w": w, "h": h, "start": [sx, sy], "tiles": tiles, "cells": cells,
            "warps": warps, "layers": [{"w": w, "h": h, "cells": cells}]}


def _loads_v3(data, where):
    (magic, version, layer_count, primary_layer, sx, sy,
     tile_count, warp_count, str_count) = _HEADER.unpack_from(data, 0)
    if layer_count == 0:
        raise MapFileError(f"{where}: layer_count is 0 -- a map needs at least one layer")
    at = _HEADER.size
    strings, at = _read_strings(data, at, str_count, where)
    tiles, at, name = _read_tiles(data, at, tile_count, strings, where)
    warps, at = _read_warps(data, at, warp_count, name)

    dims = []
    for _ in range(layer_count):
        w, h = _LAYER_DIR.unpack_from(data, at)
        at += _LAYER_DIR.size
        dims.append((w, h))

    layers = []
    for i, (w, h) in enumerate(dims):
        cells, at = _read_cells(data, at, w, h, tiles, where, label=f"layer {i}: ")
        layers.append({"w": w, "h": h, "cells": cells})

    # `primary_layer` is read but not honoured as anything other than 0: this module
    # never writes a different value (see the module docstring), and a hand-edited file
    # naming another layer primary is out of scope rather than silently misread.
    primary = layers[primary_layer] if primary_layer < len(layers) else layers[0]
    return {"w": primary["w"], "h": primary["h"], "cells": primary["cells"],
            "start": [sx, sy], "tiles": tiles, "warps": warps, "layers": layers}


def loads(data, where="<bytes>"):
    """Read a map source file. The returned doc always carries `w`/`h`/`cells` (the
    PRIMARY layer's, i.e. `layers[0]`'s) for back-compat with every reader that predates
    multi-layer authoring, plus `layers` -- the full list -- for one that wants it.
    """
    if len(data) < 5:
        raise MapFileError(f"{where}: too short to be a map file")
    magic, version = data[0:4], data[4]
    if magic != MAGIC:
        raise MapFileError(f"{where}: not a pnxmap (magic {magic!r})")
    # Refused rather than best-effort parsed: a newer file read by an older tool would
    # produce a map that is subtly wrong, which is worse than one that does not load. v2 is
    # the one exception -- see the module docstring's v3 paragraph for why that hop alone
    # upgrades transparently instead of refusing.
    if version == 2:
        if len(data) < _HEADER_V2.size:
            raise MapFileError(f"{where}: too short to be a map file")
        return _loads_v2(data, where)
    if version != FORMAT_VERSION:
        raise MapFileError(f"{where}: format version {version}, this tool writes "
                           f"{FORMAT_VERSION} (or reads v2)")
    if len(data) < _HEADER.size:
        raise MapFileError(f"{where}: too short to be a map file")
    return _loads_v3(data, where)


def from_rows(rows, legend, flag_names, default_atlas):
    """Convert an ASCII map and its legend into the table-and-cells form.

    The migration path, and the reason `rows` does not have to be deleted to add this: a
    manifest that already builds can be moved to a file without anyone re-authoring it.

    `legend` is {char: (tile, flags_byte, atlas_or_None, flip_bits)} as parse_legend
    returns it. A role name is left as a string in the tile table's `index` slot for the
    caller to resolve -- this module does not know what an atlas packed.
    """
    grid = [r for r in rows.strip("\n").split("\n") if r.strip()]
    if not grid:
        raise MapFileError("no rows")
    h = len(grid)
    w = len(grid[0])
    for i, r in enumerate(grid):
        if len(r) != w:
            raise MapFileError(f"row {i} is {len(r)} chars, row 0 is {w}")

    tiles, seen, cells = [], {}, []
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch not in legend:
                raise MapFileError(f"unknown legend char {ch!r} at {x},{y}")
            tile, flags, atlas, flip = legend[ch]
            key = (tile, flags, atlas or default_atlas, flip)
            if key not in seen:
                seen[key] = len(tiles)
                tiles.append({"atlas": key[2], "index": tile, "flags": flags,
                              "flip": ("x" if flip & 1 else "") + ("y" if flip & 2 else ""),
                              "rotate": bool(flip & 4)})
            cells.append(seen[key])
    return {"w": w, "h": h, "tiles": tiles, "cells": cells}


def to_rows(doc, alphabet=None):
    """Render a map back to ASCII, or None when it needs more characters than exist.

    The escape hatch in the other direction: a binary map small enough to be text can be
    turned back into text, so choosing this format is not a one-way door. Returns
    (rows, legend) where legend is {char: tile table entry}.
    """
    alphabet = alphabet or (".,:;'\"!?*+-=/\\|<>()[]{}~^&%$@#0123456789"
                            "abcdefghijklmnopqrstuvwxyz"
                            "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    tiles = doc["tiles"]
    if len(tiles) > len(alphabet):
        return None, None
    chars = {i: alphabet[i] for i in range(len(tiles))}
    w, h = doc["w"], doc["h"]
    rows = ["".join(chars[doc["cells"][y * w + x]] for x in range(w)) for y in range(h)]
    return rows, {chars[i]: t for i, t in enumerate(tiles)}
