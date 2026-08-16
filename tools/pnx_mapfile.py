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
"""

import struct

MAGIC = b"PNXM"
FORMAT_VERSION = 2

_HEADER = struct.Struct("<4sBBBBBBHHH")
_TILE = struct.Struct("<HHHBBB")
NO_ROLE = 0xFFFF
_WARP = struct.Struct("<BBHBBB")


class MapFileError(Exception):
    """A `.pnxmap` that cannot be read. Separate from BuildError so the editor can tell
    'this file is not a map' from 'this map does not build'."""


def write(path, w, h, start, tiles, cells, warps=()):
    """Write a map source file.

    `tiles` is a list of dicts: {atlas, index, flip, rotate, flags, extended}. `flip` is
    a string holding any of "xy"; `rotate` is a bool (transpose -- see MAP_ROTATE in
    pnx_assets.py for why one bit, not an angle); `flags` is the byte the pipeline's
    flag names resolve to; `extended` is a u8 tag (see MAP_EXTENDED in pnx_assets.py),
    0 if omitted. `cells` is a flat list of w*h indices into `tiles`. `warps` is a list
    of {at: [x, y], to: [name, x, y], gated: bool}.

    rotate rides in the same u8 as flip (bit 2, 0x04) rather than widening the row --
    old files have that bit unset, which correctly decodes as rotate=False, so that one
    was additive and did not bump FORMAT_VERSION. `extended` could not do the same trick
    -- it is a value, not a flag, so it needed a whole byte of its own and FORMAT_VERSION
    is 2 because of it.
    """
    if not (1 <= w <= 255 and 1 <= h <= 255):
        raise MapFileError(f"{w}x{h} is outside the 1..255 the format stores per axis")
    if len(cells) != w * h:
        raise MapFileError(f"expected {w * h} cells for a {w}x{h} map, got {len(cells)}")
    if not tiles:
        raise MapFileError("a map needs at least one tile table entry")
    for c in cells:
        if not 0 <= c < len(tiles):
            raise MapFileError(f"cell index {c} is outside the {len(tiles)}-entry tile "
                               f"table")

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
    body += _HEADER.pack(MAGIC, FORMAT_VERSION, 0, w, h, int(start[0]), int(start[1]),
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
    for c in cells:
        body += int(c).to_bytes(2, "little")

    with open(path, "wb") as f:
        f.write(body)
    return len(body)


def read(path):
    """Read a map source file into the same shape `write` takes."""
    with open(path, "rb") as f:
        data = f.read()
    return loads(data, where=path)


def loads(data, where="<bytes>"):
    if len(data) < _HEADER.size:
        raise MapFileError(f"{where}: too short to be a map file")
    (magic, version, _res, w, h, sx, sy,
     tile_count, warp_count, str_count) = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise MapFileError(f"{where}: not a pnxmap (magic {magic!r})")
    # Refused rather than best-effort parsed: a newer file read by an older tool would
    # produce a map that is subtly wrong, which is worse than one that does not load.
    if version != FORMAT_VERSION:
        raise MapFileError(f"{where}: format version {version}, this tool writes "
                           f"{FORMAT_VERSION}")

    at = _HEADER.size
    strings = []
    for _ in range(str_count):
        if at >= len(data):
            raise MapFileError(f"{where}: string table runs past the end of the file")
        n = data[at]
        at += 1
        strings.append(data[at:at + n].decode("utf-8"))
        at += n

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

    warps = []
    for _ in range(warp_count):
        ax, ay, dstr, tx, ty, gated = _WARP.unpack_from(data, at)
        at += _WARP.size
        warps.append({"at": [ax, ay], "to": [name(dstr), tx, ty],
                      "gated": bool(gated)})

    need = w * h * 2
    if len(data) - at < need:
        raise MapFileError(f"{where}: {w}x{h} needs {need} bytes of cells, "
                           f"{len(data) - at} remain")
    cells = list(struct.unpack_from(f"<{w * h}H", data, at))
    for c in cells:
        if c >= len(tiles):
            raise MapFileError(f"{where}: a cell names tile {c}, but the table holds "
                               f"{len(tiles)}")

    return {"w": w, "h": h, "start": [sx, sy], "tiles": tiles, "cells": cells,
            "warps": warps}


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
