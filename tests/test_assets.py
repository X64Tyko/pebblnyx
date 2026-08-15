#!/usr/bin/env python3
"""Tests for the asset pipeline's validation.

The pipeline's whole value is that it *fails the build* on bad content, because content
bugs do not crash on a watch -- they present as "nothing happens" with a binary that
looks fine. That claim is worth testing directly: each case here is a manifest broken in
one specific way, and the test asserts the build rejects it for the right reason.

Every check corresponds to something that was silently wrong once, or to a limit of the
binary format that would otherwise truncate without complaint.

Run:  python3 tests/test_assets.py
"""

import contextlib
import io
import math
import os
import re
import sys
import tempfile
import tomllib
import time
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "tools"))

import pnx_assets                                          # noqa: E402
from PIL import Image                                      # noqa: E402

failures = 0
checks = 0


def make_sheet(path, tiles_across=2, tile=16):
    """A synthetic sheet with visually distinct tiles, so autopick has something to do.

    Grayscale, spaced 60 apart, which is what survives this pipeline's palette
    quantization at the default 2x2 (4 tiles) -- checked empirically, not derived: closer
    spacing (tried scaling the step down to fit more tiles in 0..255) collapses several
    tiles to the same post-quantization colour and autopick reports "too few distinct
    tiles" against a sheet that LOOKS fine. A caller wanting more than 4 roles needs
    genuinely separated colours, not more shades of grey -- see check_user_flags for one
    built that way, rather than stretching this function past what it was tuned for.
    """
    size = tiles_across * tile
    img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    px = img.load()
    for ty in range(tiles_across):
        for tx in range(tiles_across):
            # Distinct flat colours, plus one busy tile so 'accent' has a candidate.
            shade = 40 + 60 * (ty * tiles_across + tx)
            for j in range(tile):
                for i in range(tile):
                    noise = ((i + j) % 2) * 60 if (tx, ty) == (1, 1) else 0
                    v = min(255, shade + noise)
                    px[tx * tile + i, ty * tile + j] = (v, v, v, 255)
    img.save(path)


def make_colour_sheet(path, colours, tile=16):
    """A sheet of flat, VIVIDLY distinct tiles -- for a test that needs more roles than
    make_sheet's grayscale ramp survives quantization at (see its own comment)."""
    n = len(colours)
    across = int(math.ceil(math.sqrt(n)))
    size = across * tile
    img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    px = img.load()
    for idx, (r, g, b) in enumerate(colours):
        tx, ty = idx % across, idx // across
        for j in range(tile):
            for i in range(tile):
                px[tx * tile + i, ty * tile + j] = (r, g, b, 255)
    img.save(path)
    return across


BASE_MAP = """
####
#..#
#D.#
####
"""


def manifest(root, **overrides):
    """A minimal valid manifest, with pieces replaceable per test."""
    parts = {
        "atlas": '''
            [[atlas]]
            name = "tiles"
            sheet = "sheet.png"
            tile = 16
            region = [0, 0, 2, 2]
            max_tiles = 16
            out = "tiles.bin"
            autopick = ["floor", "wall", "accent"]
            [[atlas.collision]]
            tile = "wall"
            type = "solid"
        ''',
        "legend": '''
            [legend."."]
            tile = "floor"
            flags = []
            [legend."#"]
            tile = "wall"
            flags = []
            [legend."D"]
            tile = "accent"
            flags = ["warp"]
        ''',
        "maps": f'''
            [[map]]
            name = "a"
            out = "a.bin"
            start = [2, 1]
            warps = [{{ at = [1, 2], to = ["b", 1, 1] }}]
            rows = """{BASE_MAP}"""

            [[map]]
            name = "b"
            out = "b.bin"
            start = [1, 1]
            warps = []
            rows = """{BASE_MAP}"""
        ''',
    }
    parts.update(overrides)

    body = textwrap.dedent('''
        [project]
        name = "t"
        resources = "out"
        header = "out/gen.h"
    ''')
    for key in ("atlas", "legend", "maps"):
        body += textwrap.dedent(parts[key])
    # Dialog before font: `charset = "auto"` derives its glyph set from the pages, so a
    # font test that supplies dialog needs it to come first in the same manifest.
    for key in ("sprite", "dialog", "font", "scene", "tile_flags"):
        if key in parts:
            body += textwrap.dedent(parts[key])

    path = os.path.join(root, "m.toml")
    with open(path, "w") as f:
        f.write(body)
    return path


def run(root, **overrides):
    """Builds a manifest, returning None on success or the error message on failure."""
    path = manifest(root, **overrides)
    try:
        # The tool reports progress to stdout; swallow it so the test output is only
        # test results.
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(path, os.path.join(root, "out"),
                             os.path.join(root, "out", "gen.h"))
        return None
    except pnx_assets.BuildError as e:
        return str(e)


def make_recolour(src, dst, move_a_pixel=False):
    """A genuine recolour of `src`: every colour remapped, no pixel moved.

    Built by inverting each channel, which is injective on 8-bit values and stays injective
    after the 2-bit quantisation the pipeline applies -- an important property, since a remap
    that merged two colours would change the shape and be rejected for the right reason but
    make a confusing test.
    """
    img = Image.open(src).convert("RGBA")
    w, h = img.size
    sp = img.load()
    out = Image.new("RGBA", (w, h))
    op = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = sp[x, y]
            op[x, y] = (255 - r, 255 - g, 255 - b, a)
    if move_a_pixel:
        op[0, 0] = (0, 0, 0, 0)          # punch a hole: same colours, different shape
    out.save(dst)


def expect_ok(label, _extra=None, **overrides):
    global checks, failures
    checks += 1
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        if _extra: _extra(root)
        err = run(root, **overrides)
    if err is not None:
        print(f"  FAIL {label}: expected success, got {err!r}")
        failures += 1


def expect_fail(label, fragment, _extra=None, **overrides):
    """Asserts the build fails AND that the message names the actual problem.

    Checking the message matters as much as the failure: an error that says only
    "invalid manifest" leaves the author exactly as stuck as silence would.
    """
    global checks, failures
    checks += 1
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        if _extra: _extra(root)
        err = run(root, **overrides)
    if err is None:
        print(f"  FAIL {label}: expected failure, but the build SUCCEEDED")
        failures += 1
    elif fragment.lower() not in err.lower():
        print(f"  FAIL {label}: message did not mention {fragment!r}\n         got: {err}")
        failures += 1


def maps(body):
    return {"maps": body}


# ------------------------------------------------------------------- orientation
#
# M4c rotates content at BUILD time, so the engine's ordinary portrait blit draws a
# landscape screen. That makes these tests the only place the rotation is ever checked:
# there is no runtime code to catch it later, which is the point of the design and the
# reason the pipeline has to be held to it here.
#
# The claim under test is not "landscape builds". It is that the same manifest produces
# the SAME CONTENT, turned -- so every check below compares two builds of one manifest
# rather than asserting numbers someone typed in.

SPRITE_TALL = '''
    [[sprite]]
    name = "guy"
    sheet = "sheet.png"
    frames = [[0, 0, 8, 16]]
    out = "guy.bin"
'''


def build_at(root, orientation, **overrides):
    """Builds the standard manifest at one orientation into its own directory."""
    path = manifest(root, **overrides)
    out = os.path.join(root, "out_" + orientation)
    with contextlib.redirect_stdout(io.StringIO()):
        pnx_assets.build(path, out, os.path.join(out, "gen.h"), orientation=orientation)
    return out


def read_blob(out_dir, name):
    with open(os.path.join(out_dir, name), "rb") as f:
        return f.read()


def defines(out_dir):
    """The generated header as a dict, so a test can name a symbol rather than a line."""
    found = {}
    with open(os.path.join(out_dir, "gen.h")) as f:
        for line in f:
            parts = line.split()
            if len(parts) != 3 or parts[0] != "#define":
                continue
            # Hex as well as decimal: tile flag bits are emitted as 0x04 so the header
            # reads like the PNX_TILE_* constants they sit beside.
            try:
                found[parts[1]] = int(parts[2], 0)
            except ValueError:
                pass
    return found


def check(label, cond):
    global checks, failures
    checks += 1
    if not cond:
        print(f"  FAIL {label}")
        failures += 1


def check_colorkey():
    """A keyed colour becomes transparent, and index 0 is what transparent means.

    Tile art is still routinely distributed drawn on a flat background -- magenta, or
    whatever the artist used -- with no alpha channel at all. Without a key those pixels
    are opaque background: they cost palette entries, they defeat the blitter's early-out,
    and they draw a rectangle around every sprite.
    """
    with tempfile.TemporaryDirectory() as root:
        sheet = os.path.join(root, "sheet.png")
        make_sheet(sheet)

        # Paint one tile's corner magenta, the classic key colour.
        img = Image.open(sheet).convert("RGBA")
        px = img.load()
        for y in range(4):
            for x in range(4):
                px[x, y] = (255, 0, 255, 255)
        img.save(sheet)

        keyed = '''
            [[atlas]]
            name = "tiles"
            sheet = "sheet.png"
            tile = 16
            region = [0, 0, 2, 2]
            max_tiles = 16
            out = "tiles.bin"
            autopick = ["floor", "wall", "accent"]
            colorkey = [255, 0, 255]
        '''
        out = build_at(root, "portrait", atlas=keyed)

        # Atlas layout: header, then one palette slot per tile, then flags, then pixels
        # at 4bpp. The keyed corner must be index 0 -- transparent -- in tile 0.
        b = read_blob(out, "tiles.bin")
        tile_count = b[4]
        pixels = 8 + pnx_assets.pad4(bytes(tile_count)).__len__() * 2
        first = b[pixels:pixels + 8]
        check("keyed pixels are palette index 0",
              all(byte == 0 for byte in first[:2]))

    # --- and on every OTHER tile, which is where it used to stop working.
    #
    # `key` held the colour key and was then rebound to each tile's bytes inside the dedup
    # loop, so tile 0 was keyed and every tile after it was read with a bytes object as
    # its key -- matching nothing, leaving the background opaque. The case above could
    # never catch it: it keys a corner of tile 0, the one tile the bug spared.
    with tempfile.TemporaryDirectory() as root:
        sheet = os.path.join(root, "sheet.png")
        make_sheet(sheet)
        img = Image.open(sheet).convert("RGBA")
        px = img.load()
        # A block in the SECOND tile of the row, and a different one in the third, so
        # neither can dedup into the first.
        for y in range(4):
            for x in range(4):
                px[16 + x, y] = (255, 0, 255, 255)
                px[x, 16 + y] = (255, 0, 255, 255)
        img.save(sheet)

        with contextlib.redirect_stdout(io.StringIO()):
            atlas = pnx_assets.pack_atlas(
                root, {"name": "t", "sheet": "sheet.png", "tile": 16,
                       "region": [0, 0, 2, 2], "max_tiles": 16, "out": "t.bin",
                       "colorkey": [255, 0, 255]},
                pnx_assets.ORIENT_BUTTONS_RIGHT)
        keyed = [i for i, t in enumerate(atlas["tiles"])
                 if any(b == pnx_assets.TRANSPARENT for b in t)]
        check("the colour key applies to tiles after the first",
              len(keyed) >= 2 and keyed != [0])

    # And a key that is not a colour is a build failure, not a silent no-op.
    expect_fail("colorkey that is not three numbers", "colorkey must be three integers",
                atlas='''
        [[atlas]]
        name = "tiles"
        sheet = "sheet.png"
        tile = 16
        region = [0, 0, 2, 2]
        max_tiles = 16
        out = "tiles.bin"
        autopick = ["floor", "wall", "accent"]
        colorkey = "magenta"
    ''')


def check_orientation():
    """One manifest, four orientations, compared against each other."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))

        builds = {name: build_at(root, name, sprite=SPRITE_TALL)
                  for name in ("portrait", "buttons_top", "buttons_bottom", "buttons_left")}
        want = {"portrait": pnx_assets.ORIENT_BUTTONS_RIGHT,
                "buttons_top": pnx_assets.ORIENT_BUTTONS_TOP,
                "buttons_bottom": pnx_assets.ORIENT_BUTTONS_BOTTOM,
                "buttons_left": pnx_assets.ORIENT_BUTTONS_LEFT}

        # --- every blob is stamped, including the ones with no geometry. A song stamped
        #     0 in a landscape build would make "orientation-free" and "stale portrait"
        #     the same byte, and the runtime could no longer tell them apart.
        for name, out in builds.items():
            for f in sorted(os.listdir(out)):
                if not f.endswith(".bin"):
                    continue
                check(f"{f} stamped {name}", read_blob(out, f)[7] == want[name])

        flat = builds["portrait"]
        for name in ("buttons_top", "buttons_bottom", "buttons_left"):
            out, orient = builds[name], want[name]
            # buttons_left is a half-turn, not a quarter one: width and height stay put.
            # Only the two landscape orientations swap them.
            swaps = orient in pnx_assets.LANDSCAPE_ORIENTS

            # --- dimensions swap in the two landscape orientations, and hold in the one
            #     that instead turns everything upside down
            fm, rm = read_blob(flat, "a.bin"), read_blob(out, "a.bin")
            check(f"{name}: map w/h {'swap' if swaps else 'hold'}",
                  (rm[3], rm[4]) == ((fm[4], fm[3]) if swaps else (fm[3], fm[4])))

            fd, rd = defines(flat), defines(out)
            check(f"{name}: header map dims {'swap' if swaps else 'hold'}",
                  (rd["MAP_A_W"], rd["MAP_A_H"])
                  == ((fd["MAP_A_H"], fd["MAP_A_W"]) if swaps
                      else (fd["MAP_A_W"], fd["MAP_A_H"])))
            check(f"{name}: header sprite dims {'swap' if swaps else 'hold'}",
                  (rd["GUY_W"], rd["GUY_H"])
                  == ((fd["GUY_H"], fd["GUY_W"]) if swaps
                      else (fd["GUY_W"], fd["GUY_H"])))
            check(f"{name}: orientation reaches the header",
                  rd["PNX_ORIENTATION"] == orient)

            # --- the start position is rotated by the same transform as the grid it
            #     indexes. A map turned without its coordinates puts the player in a wall.
            sx, sy = pnx_assets.rotate_point(fd["MAP_A_START_X"], fd["MAP_A_START_Y"],
                                             fd["MAP_A_W"], fd["MAP_A_H"], orient)
            check(f"{name}: start rotates with the map",
                  (rd["MAP_A_START_X"], rd["MAP_A_START_Y"]) == (sx, sy))

            # --- the tile plane IS the portrait one, rotated. Read through parse_map
            #     rather than off a byte offset: cells live inside WorldTiles now, and a
            #     test that knew the layout would have to be re-derived every time the
            #     format moves -- which is exactly when this check is worth most.
            fp, rp = pnx_assets.parse_map(fm), pnx_assets.parse_map(rm)
            turned, _, _ = pnx_assets.rotate_grid(fp["cells"], fp["w"], fp["h"], orient,
                                                  stride=2)
            # Collision/warp live IN the cell word now (fold_flag_into_entry), so this one
            # check already covers what a separate "flags rotate with the plane" check
            # used to verify by hand -- there is no second structure left to compare.
            check(f"{name}: tile plane is the portrait plane rotated",
                  turned == rp["cells"])

            # --- and it costs nothing. Rotation permutes cells, so a map that changes
            #     size between orientations means a choice somewhere depends on the order
            #     cells were visited -- which is what the flag-default tally used to do.
            check(f"{name}: map costs the same bytes", len(rm) == len(fm))
            check(f"{name}: sprite costs the same bytes",
                  len(read_blob(out, "guy.bin")) == len(read_blob(flat, "guy.bin")))

    # --- a misspelling is a build failure, not a silent portrait build
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        global checks, failures
        checks += 1
        try:
            build_at(root, "landscape")
            print("  FAIL unknown orientation: expected failure, but the build SUCCEEDED")
            failures += 1
        except pnx_assets.BuildError as e:
            if "orientation" not in str(e).lower():
                print(f"  FAIL unknown orientation: message did not name it: {e}")
                failures += 1


# ------------------------------------------------------------------------- fonts

import shutil as _shutil                                   # noqa: E402
import os as _os                                           # noqa: E402

# Any real TTF will do -- these tests assert on structure, not on how a face looks. The
# licence matters only because the pipeline requires one be declared.
TEST_TTF = next(
    (p for p in ("/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
                 "/usr/share/fonts/TTF/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/gsfonts/NimbusSans-Regular.otf",
                 "/System/Library/Fonts/Helvetica.ttc")
     if _os.path.exists(p)), "")


def check_font_blob(path, depth):
    """Assert a PF blob's own header agrees with the bytes that follow it.

    This is the check the runtime performs before trusting the blitter with the data, so
    a pipeline that emits an inconsistent blob should fail here rather than on a watch.
    """
    global checks, failures
    checks += 1
    b = open(path, "rb").read()
    problems = []

    if b[:2] != b"PF":
        problems.append(f"magic {b[:2]!r}")
    if b[2] != pnx_assets.BLOB_VERSION:
        problems.append(f"version {b[2]}")
    if b[3] != depth:
        problems.append(f"depth {b[3]}, expected {depth}")

    line_height, baseline = b[4], b[5]
    if not 0 < baseline <= line_height:
        problems.append(f"baseline {baseline} against line_height {line_height}")

    count = int.from_bytes(b[8:10], "little")
    bitmap_bytes = int.from_bytes(b[10:12], "little")
    first, last, fallback = b[12], b[13], b[14]

    if fallback >= count:
        problems.append(f"fallback {fallback} of {count} glyphs")
    if first > last:
        problems.append(f"codepoint range {first}..{last}")

    index_at = 16
    map_at = index_at + count * pnx_assets.FONT_GLYPH_ENTRY
    bitmaps_at = map_at + (last - first + 1)
    if len(b) != bitmaps_at + bitmap_bytes:
        problems.append(f"length {len(b)}, header describes {bitmaps_at + bitmap_bytes}")

    # Every glyph's bitmap must lie inside the block. An offset past the end is the
    # failure that would have the blitter reading arbitrary memory as pixels.
    for i in range(count):
        e = b[index_at + i * pnx_assets.FONT_GLYPH_ENTRY:][:pnx_assets.FONT_GLYPH_ENTRY]
        off = int.from_bytes(e[:2], "little")
        w, h = e[2], e[3]
        if w == 0:
            continue
        need = off + h * ((w * depth + 7) // 8)
        if need > bitmap_bytes:
            problems.append(f"glyph {i} ({w}x{h} at {off}) needs {need} of {bitmap_bytes}")
            break

    for i, entry in enumerate(b[map_at:bitmaps_at]):
        if entry != 0xFF and entry >= count:
            problems.append(f"codepoint {first + i} maps to glyph {entry} of {count}")
            break

    if problems:
        print(f"  FAIL font blob {_os.path.basename(path)}: {'; '.join(problems)}")
        failures += 1


def _font_advance(root, dialog, font, ch):
    """Builds a manifest and reads one glyph's advance back out of the blob."""
    run(root, dialog=dialog, font=font)
    b = open(_os.path.join(root, "out", "hud.bin"), "rb").read()
    count, first, last = int.from_bytes(b[8:10], "little"), b[12], b[13]
    if not first <= ord(ch) <= last:
        return None
    index = b[16 + count * pnx_assets.FONT_GLYPH_ENTRY + (ord(ch) - first)]
    if index == 0xFF:
        return None
    return b[16 + index * pnx_assets.FONT_GLYPH_ENTRY + 4]


# ------------------------------------------------------ multi-atlas maps and WorldTiles
#
# Two features that had to land together. A map draws from several atlases by partitioning
# its 10-bit cell index into one slice per atlas, and it is stored as a grid of WorldTiles
# so a map larger than RAM is a content decision rather than an impossible one.
#
# What is worth testing here is not that a big map builds -- it is that the id partition
# and the slicing are INVERTIBLE, since both are silent when wrong: a cell that resolves to
# the wrong atlas draws the wrong picture, and a WorldTile written at the wrong offset
# draws the wrong part of the world. Neither raises anything.

TWO_ATLASES = '''
    [[atlas]]
    name = "tiles"
    sheet = "sheet.png"
    tile = 16
    region = [0, 0, 2, 2]
    max_tiles = 16
    out = "tiles.bin"
    autopick = ["floor", "wall", "accent"]
    [[atlas.collision]]
    tile = "wall"
    type = "solid"

    [[atlas]]
    name = "second"
    sheet = "sheet2.png"
    tile = 16
    region = [0, 0, 2, 2]
    max_tiles = 16
    out = "second.bin"
    autopick = ["floor", "wall", "accent"]
'''

TWO_LEGEND = '''
    [legend."."]
    tile = "floor"
    flags = []
    [legend."#"]
    tile = "wall"
    flags = []
    [legend."D"]
    tile = "accent"
    flags = ["warp"]
    [legend."s"]
    tile = "floor"
    atlas = "second"
    flags = []
'''


def second_sheet(root):
    """A second, visibly different sheet, so the two atlases cannot dedup into one."""
    make_sheet(os.path.join(root, "sheet.png"))
    img = Image.open(os.path.join(root, "sheet.png")).convert("RGBA")
    px = img.load()
    for y in range(img.size[1]):
        for x in range(img.size[0]):
            r, g, b, a = px[x, y]
            px[x, y] = (b, r, g, a)
    img.save(os.path.join(root, "sheet2.png"))


def build_maps(root, _extra=None, **overrides):
    """Builds and returns {map name: parsed blob}, so a test can assert on the content."""
    (_extra or make_sheet)(os.path.join(root, "sheet.png") if _extra is None else root)
    path = manifest(root, **overrides)
    out = os.path.join(root, "out")
    with contextlib.redirect_stdout(io.StringIO()):
        pnx_assets.build(path, out, os.path.join(out, "gen.h"))
    built = {}
    for name in os.listdir(out):
        if not name.endswith(".bin"):
            continue
        blob = read_blob(out, name)
        if blob[:2] != pnx_assets.MAGIC_MAP:
            continue
        # A map's WorldTile payloads are in bank resources beside it, `<stem>_b<N>.bin`,
        # so reassembling the cell plane means gathering them first.
        stem = name[:-4]
        banks, i = [], 0
        while os.path.exists(os.path.join(out, f"{stem}_b{i}.bin")):
            banks.append(read_blob(out, f"{stem}_b{i}.bin"))
            i += 1
        built[name] = pnx_assets.parse_map(blob, banks)
    return built


def check_worldtiles():
    # --- the id partition is arithmetic, so it is tested as arithmetic. Reaching 1024 ids
    #     through real sheets would mean five 255-tile atlases and a slow test for a bound
    #     that is one comparison.
    table, total = pnx_assets.map_tile_bases(
        "m", ["a", "b", "c"], {"a": 10, "b": 200, "c": 5}, {"a": 16, "b": 16, "c": 16})
    check("tile ids partition in declaration order",
          table == [("a", 0, 10), ("b", 10, 200), ("c", 210, 5)] and total == 215)

    try:
        pnx_assets.map_tile_bases("m", ["a", "b"], {"a": 600, "b": 600},
                                  {"a": 16, "b": 16})
        check("1024 tile ids is a build error", False)
    except pnx_assets.BuildError as e:
        check("1024 tile ids is a build error", "1200 tiles between them" in str(e))

    try:
        pnx_assets.map_tile_bases("m", ["a", "b"], {"a": 4, "b": 4}, {"a": 16, "b": 8})
        check("mixed tile sizes in one map is a build error", False)
    except pnx_assets.BuildError as e:
        check("mixed tile sizes in one map is a build error",
              "must share a tile size" in str(e))

    # --- a map that draws from two atlases resolves each legend char to the right slice
    with tempfile.TemporaryDirectory() as root:
        second_sheet(root)
        built = build_maps(root, _extra=second_sheet, atlas=TWO_ATLASES, legend=TWO_LEGEND, **maps('''
            [[map]]
            name = "a"
            out = "a.bin"
            atlases = ["tiles", "second"]
            start = [1, 1]
            rows = """
            ####
            #.s#
            ####
            """
        '''))
        m = built["a.bin"]
        check("map records both atlases", len(m["atlas_table"]) == 2)
        first_second = m["atlas_table"][1][1]

        def cell(x, y):
            i = (y * m["w"] + x) * 2
            return (m["cells"][i] | m["cells"][i + 1] << 8) & 0x03FF

        check("a cell from the first atlas keeps a low id", cell(1, 1) < first_second)
        check("a cell from the second atlas lands in its slice",
              cell(2, 1) >= first_second)
        check("both atlases are pinned by the WorldTile that uses them",
              m["masks"][0] == 0b11)

    # --- a map whose legend reaches an atlas it does not draw from, named at the cell
    expect_fail("legend names an atlas the map does not use", "which this map does not use",
                _extra=second_sheet, atlas=TWO_ATLASES, legend=TWO_LEGEND, **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        atlases = ["tiles"]
        start = [1, 1]
        rows = """
        ####
        #.s#
        ####
        """
    '''))

    # --- ...but a legend it merely shares is fine. This is the case that makes a
    #     project-wide legend usable at all: every map would otherwise have to define
    #     every character.
    expect_ok("an unused legend char may name another map's atlas",
              _extra=second_sheet, atlas=TWO_ATLASES, legend=TWO_LEGEND, **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        atlases = ["tiles"]
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    expect_fail("atlas listed twice", "listed twice",
                _extra=second_sheet, atlas=TWO_ATLASES, **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        atlases = ["tiles", "tiles"]
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    expect_fail("atlas and atlases together", "not both", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        atlas = "tiles"
        atlases = ["tiles"]
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    # --- slicing is invertible. A map three WorldTiles wide exercises the offsets, the
    #     clipped edge column and the reassembly all at once.
    with tempfile.TemporaryDirectory() as root:
        rows = "\n".join(["#" * 40] + ["#" + "." * 38 + "#"] * 18 + ["#" * 40])
        built = build_maps(root, **{"maps": f'''
            [[map]]
            name = "a"
            out = "a.bin"
            worldtile = 16
            start = [1, 1]
            rows = """
{rows}
"""
        '''})
        m = built["a.bin"]
        check("a 40x20 map slices into 3x2 WorldTiles",
              (m["cols"], m["rows"]) == (3, 2))
        check("the reassembled plane is the whole map",
              len(m["cells"]) == m["w"] * m["h"] * 2)
        check("the clipped edge column is not padded out",
              m["w"] == 40 and m["h"] == 20)
        # Corners are wall, the interior is floor: if any WorldTile landed at the wrong
        # offset the plane would disagree with the rows that produced it.
        def cell(x, y):
            i = (y * m["w"] + x) * 2
            return (m["cells"][i] | m["cells"][i + 1] << 8) & 0x03FF
        check("cells land at the coordinates they were authored at",
              cell(0, 0) == cell(39, 19) == cell(20, 0) and cell(20, 10) != cell(0, 0))

    # --- and a map bigger than the screen streams rather than sitting resident whole
    with tempfile.TemporaryDirectory() as root:
        rows = "\n".join(["#" * 200] + ["#" + "." * 198 + "#"] * 198 + ["#" * 200])
        built = build_maps(root, **{"maps": f'''
            [[map]]
            name = "a"
            out = "a.bin"
            start = [1, 1]
            rows = """
{rows}
"""
        '''})
        m = built["a.bin"]
        total = m["cols"] * m["rows"]
        check("a 200x200 map holds more WorldTiles than slots", total > m["wt_slots"])
        # The pool is exactly the window the pipeline sized it for -- asserted against
        # worldtile_window rather than a number, because the WorldTile size is now chosen
        # per map and a hardcoded 16 would only be testing today's choice.
        win = pnx_assets.worldtile_window(m["tile_px"], m["worldtile"])
        check("the resident pool is the window, margin included",
              m["wt_slots"] == win[0] * win[1])
        whole_plane = m["w"] * m["h"] * 2
        check("streaming a 200x200 map costs a pool, not the whole 80 KB plane",
              m["wt_slots"] * m["wt_slot_bytes"] < whole_plane // 8)

    # --- `resident = true` gives a slot per WorldTile: what a map cost before WorldTiles
    #     existed, kept so the two can be measured against each other rather than argued
    #     about. Checked on a map big enough for the two to differ.
    with tempfile.TemporaryDirectory() as root:
        rows = "\n".join(["#" * 120] + ["#" + "." * 118 + "#"] * 118 + ["#" * 120])
        spec = '''
            [[map]]
            name = "a"
            out = "a.bin"
            {extra}
            start = [1, 1]
            rows = """
{rows}
"""
        '''
        # `worldtile` pinned on both, so the only thing that differs is `resident`. Left to
        # itself the pipeline picks a DIFFERENT size for each -- smaller for streaming,
        # bigger for held whole -- which is correct and is checked separately below.
        pin = "worldtile = 16\n            "
        streamed = build_maps(root, **{"maps": spec.format(extra=pin, rows=rows)})["a.bin"]
        whole = build_maps(root, **{"maps": spec.format(
            extra=pin + "resident = true", rows=rows)})["a.bin"]
        total = streamed["cols"] * streamed["rows"]
        check("without `resident` a large map streams", streamed["wt_slots"] < total)
        check("`resident = true` takes a slot per WorldTile", whole["wt_slots"] == total)
        check("and the two describe the same world",
              whole["cells"] == streamed["cells"])
        check("holding it whole costs several times the pool",
              whole["wt_slots"] * whole["wt_slot_bytes"]
              > 3 * streamed["wt_slots"] * streamed["wt_slot_bytes"])

    # --- the auto size depends on WHICH MODE the map is in, and pulls opposite ways.
    #     A streaming map holds a fixed number of WorldTiles, so a bigger one means a
    #     bigger margin ring of world nobody can see; a map held whole has no ring, so
    #     bigger means fewer per-tile headers. One rule, two answers.
    stream_pick, stream_bytes, _ = pnx_assets.pick_worldtile("m", 192, 192, 16, False)
    whole_pick, whole_bytes, _ = pnx_assets.pick_worldtile("m", 192, 192, 16, True)
    check("streaming picks a smaller WorldTile than holding whole",
          stream_pick < whole_pick)
    check("the streaming choice beats the held-whole one at streaming",
          stream_bytes < pnx_assets.worldtile_resident_bytes(192, 192, 16, whole_pick))
    # And the reverse comparison does not even exist: at the streaming size this map has
    # 576 WorldTiles, more slots than the format can address, so holding it whole at that
    # size is not a more expensive option -- it is not an option.
    check("the streaming choice cannot be held whole at all",
          pnx_assets.worldtile_resident_bytes(192, 192, 16, stream_pick, True) is None)

    # Asking to hold a big world whole at a SMALL WorldTile size wants more slots than the
    # format can address, and says so. (Left to itself the auto pick avoids this by
    # choosing a bigger size -- at 32 even a 255x255 map is only 8x8 WorldTiles, so the
    # "no size works" branch in pick_worldtile is unreachable while dimensions are u8.)
    expect_fail("holding a big world whole at a small WorldTile size", "has to stream",
                **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        worldtile = 8
        resident = true
        start = [1, 1]
        rows = """
''' + "\n".join(["#" * 200] + ["#" + "." * 198 + "#"] * 198 + ["#" * 200]) + '''
"""
    '''))

    # --- worldtile size is a power of two, because the runtime shifts rather than divides
    expect_fail("worldtile must be a power of two", "power of two", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        worldtile = 12
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    # --- a scene must not also load a tileset its map streams: that is two resident copies
    expect_fail("scene reloads a tileset its map streams", "already streams", scene='''
        [scene.only]
        map = "a"
        atlases = ["tiles"]
    ''')

    # --- an atlas and a map sharing a name both become RESOURCE_ID_<NAME> in the SDK's
    #     generated header. The manifest's own handles are prefixed by kind and never
    #     collide, so this only shows up at `arm-none-eabi-gcc` -- as a redefinition
    #     warning that names neither asset and points at a generated file.
    global checks, failures
    checks += 1
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        path = manifest(root, **maps('''
            [[map]]
            name = "tiles"
            out = "map_tiles.bin"
            start = [1, 1]
            rows = """
            ####
            #..#
            ####
            """
        '''))
        with open(os.path.join(root, "package.json"), "w") as f:
            f.write('{"pebble": {"resources": {"media": []}}}')
        err = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pnx_assets.build(path, os.path.join(root, "out"),
                                 os.path.join(root, "out", "gen.h"),
                                 package=os.path.join(root, "package.json"))
        except pnx_assets.BuildError as e:
            err = str(e)
        if err is None or "RESOURCE_ID_TILES" not in err:
            print(f"  FAIL an atlas and a map sharing a name must be refused: got {err!r}")
            failures += 1


# ------------------------------------------------------- removing an atlas from the editor
#
# Until this existed the only way to undo an import was to hand-edit the TOML the editor
# exists to keep you out of. What is worth testing is not that a block disappears -- it is
# that the manifest still BUILDS afterwards, and that an atlas something still draws with
# is refused with the reason rather than deleted.

# ------------------------------------------------- raw tile indices, flips, user flags
#
# All three exist so the EDITOR can offer what the format could always carry. Roles named
# only three tiles of an atlas, the flip bits the cell format reserved were never set by
# anything, and the flag byte had six free bits nobody could name. None of that is visible
# from the runtime side, so it is tested here against the compiled bytes.

FLIP_LEGEND = '''
    [legend."."]
    tile = "floor"
    flags = []
    [legend."#"]
    tile = "wall"
    flags = []
    [legend."D"]
    tile = "accent"
    flags = ["warp"]
    [legend."r"]
    tile = 1
    flags = []
    [legend."<"]
    tile = 1
    flip = ["x"]
    flags = []
    [legend."v"]
    tile = 1
    flip = ["x", "y"]
    flags = []
'''

FLIP_ATLAS = '''
    [[atlas]]
    name = "tiles"
    sheet = "sheet.png"
    tile = 16
    region = [0, 0, 2, 2]
    max_tiles = 16
    out = "tiles.bin"
    autopick = ["floor", "wall", "accent"]
    # Explicit, because the flip bits are only honoured for flat tiles and "auto" would
    # let the sheet's redundancy decide whether this test is testing anything.
    metatiles = false
    [[atlas.collision]]
    tile = "wall"
    type = "solid"
'''

FLIP_MAP = '''
    [[map]]
    name = "a"
    out = "a.bin"
    start = [1, 1]
    warps = []
    rows = """
    #####
    #r<v#
    #...#
    #####
    """
'''


def cells(mp):
    """The cell plane as u16s, which is how every bit in it is named."""
    return [mp["cells"][i] | (mp["cells"][i + 1] << 8)
            for i in range(0, len(mp["cells"]), 2)]


def cell(mp, x, y):
    return cells(mp)[y * mp["w"] + x]


def cell_warp(mp, x, y):
    return pnx_assets.cell_warp(mp, x, y)


def check_tile_indices_and_flips():
    # --- a raw index paints the same tile a role would, and the flip bits ride along in
    #     the cell rather than costing a second copy of the art.
    with tempfile.TemporaryDirectory() as root:
        built = build_maps(root, legend=FLIP_LEGEND,
                           atlas=FLIP_ATLAS, **maps(FLIP_MAP))
        mp = built["a.bin"]
        plain, flip_x, flip_xy = cell(mp, 1, 1), cell(mp, 2, 1), cell(mp, 3, 1)

        check("a raw tile index resolves",
              plain & pnx_assets.MAP_INDEX_MASK == 1)
        check("a flipped entry names the SAME tile as the unflipped one",
              flip_x & pnx_assets.MAP_INDEX_MASK == 1
              and flip_xy & pnx_assets.MAP_INDEX_MASK == 1)
        check("an unflipped entry sets no flip bits",
              plain & (pnx_assets.MAP_FLIP_X | pnx_assets.MAP_FLIP_Y) == 0)
        check("flip = [\"x\"] sets only FLIP_X",
              flip_x & pnx_assets.MAP_FLIP_X and not flip_x & pnx_assets.MAP_FLIP_Y)
        check("flip = [\"x\", \"y\"] sets both",
              flip_xy & pnx_assets.MAP_FLIP_X and flip_xy & pnx_assets.MAP_FLIP_Y)

    # --- a quarter turn moves the cell AND turns its mirror axis with it. Getting this
    #     wrong is invisible in portrait and mirrors the wrong way in landscape, which is
    #     exactly the class of bug pre-rotation exists to make impossible.
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        path = manifest(root, legend=FLIP_LEGEND, atlas=FLIP_ATLAS, **maps(FLIP_MAP))
        out = os.path.join(root, "land")
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(path, out, os.path.join(out, "gen.h"),
                             orientation="buttons_top")
        banks = []
        i = 0
        while os.path.exists(os.path.join(out, f"a_b{i}.bin")):
            banks.append(read_blob(out, f"a_b{i}.bin"))
            i += 1
        mp = pnx_assets.parse_map(read_blob(out, "a.bin"), banks)
        turned = [c for c in cells(mp)
                  if c & pnx_assets.MAP_INDEX_MASK == 1
                  and c & (pnx_assets.MAP_FLIP_X | pnx_assets.MAP_FLIP_Y)]
        check("rotating a map turns an X mirror into a Y mirror",
              any(c & pnx_assets.MAP_FLIP_Y and not c & pnx_assets.MAP_FLIP_X
                  for c in turned))
        check("a doubly-flipped cell survives the turn doubly flipped",
              any(c & pnx_assets.MAP_FLIP_X and c & pnx_assets.MAP_FLIP_Y
                  for c in turned))

    # --- the checks that keep an index from being the loose end a role never was.
    expect_fail("a tile index past the end of the atlas", "packed",
                legend=FLIP_LEGEND.replace("tile = 1\n    flags = []",
                                           "tile = 99\n    flags = []", 1),
                atlas=FLIP_ATLAS, **maps(FLIP_MAP))
    expect_fail("flip on a metatiled atlas", "metatile",
                legend=FLIP_LEGEND, atlas=FLIP_ATLAS.replace("metatiles = false",
                                                             "metatiles = true"),
                **maps(FLIP_MAP))
    expect_fail("a nonsense flip axis", "flip must be",
                legend=FLIP_LEGEND.replace('flip = ["x"]', 'flip = ["z"]'),
                atlas=FLIP_ATLAS, **maps(FLIP_MAP))


def check_user_flags():
    """Collision as a TILE property ([[atlas.collision]]) + warp as a cell flag.

    Was check_user_flags for the old tile_flags[]-by-id custom-bit mechanism, then for a
    brief per-cell collision-mode design -- both retired (see MAP_ROTATE's comment in
    pnx_assets.py). Collision cannot vary by placement any more, so two DIFFERENT roles
    carry SCALED and COMPLEX here, not two legend characters painting the same tile:
    that would be testing exactly the thing this design makes impossible on purpose.
    """
    atlas = FLIP_ATLAS.replace(
        "region = [0, 0, 2, 2]", "region = [0, 0, 3, 3]").replace(
        'autopick = ["floor", "wall", "accent"]',
        'autopick = ["floor", "wall", "accent", "fence", "niche"]') + '''
    [[atlas.collision]]
    tile = "fence"
    type = "scaled"
    rect = [0, 8, 16, 8]
    [[atlas.collision]]
    tile = "niche"
    type = "complex"
'''
    legend = FLIP_LEGEND + '''
    [legend."~"]
    tile = "fence"
    flags = []
    extended = 7
    [legend."^"]
    tile = "niche"
    flags = ["warp"]
'''
    body = FLIP_MAP.replace("#...#", "#~^.#")

    with tempfile.TemporaryDirectory() as root:
        make_colour_sheet(os.path.join(root, "sheet.png"), [
            (220, 40, 40), (40, 220, 40), (40, 40, 220), (220, 220, 40), (220, 40, 220)])
        path = manifest(root, legend=legend, atlas=atlas, **maps(body))
        out = os.path.join(root, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(path, out, os.path.join(out, "gen.h"))

        # Collision is a TILE property now, sitting on the atlas rather than any one
        # map's cells -- checked directly against the atlas build, the same sequence
        # `build()` runs internally, rather than through a map/cell round trip that
        # would really be testing tile-id resolution a second time.
        with open(path, "rb") as f:
            man = tomllib.load(f)
        spec = man["atlas"][0]
        packed = pnx_assets.pack_atlas(root, spec)
        roles = dict(pnx_assets.autopick_tiles(packed, spec["autopick"]))
        collision = pnx_assets.parse_atlas_collision(spec, packed, roles)
        shared = []
        pnx_assets.settle_palettes([packed], [], shared)
        finished = pnx_assets.finish_atlas(packed, collision, shared)
        check("a scaled tile's mode reaches the atlas",
              finished["tile_flags"][roles["fence"]] == pnx_assets.COLLISION_SCALED)
        check("a complex tile's mode reaches the atlas",
              finished["tile_flags"][roles["niche"]] == pnx_assets.COLLISION_COMPLEX)
        check("an undeclared tile's mode defaults to NONE",
              finished["tile_flags"][roles["floor"]] == pnx_assets.COLLISION_NONE)

        # The shape data itself: not just the mode, but the rect/mask baked for it.
        check("the scaled tile's rect round-trips",
              finished["scaled_rects"][roles["fence"]] == (0, 8, 16, 8))
        check("a non-scaled tile has no rect entry",
              roles["wall"] not in finished["scaled_rects"])
        check("the complex tile is listed",
              roles["niche"] in finished["complex_tiles"])
        check("a non-complex tile is not",
              roles["wall"] not in finished["complex_tiles"])

        # Parse the baked blob back the same way the C loader will, byte for byte, rather
        # than trusting the Python-side dict alone -- that dict is a convenience the blob
        # itself does not carry, so only the bytes prove the format round-trips.
        blob = finished["blob"]
        tile_count = len(packed["tiles"])
        # finish_atlas's flat-layout body is [assign | flags | pixels | shapes] after the
        # header; tile_bytes is not stored on the dict, so derive the pixel span from T
        # and depth (colour build, 4bpp) instead of trusting a second copy of the number.
        T = packed["tile_px"]
        tile_bytes = T * T // 2  # colour build, 4bpp
        body_at = pnx_assets.HEADER_BYTES
        assign_span = (tile_count + 3) & ~3
        flags_span = (tile_count + 3) & ~3
        pixel_span = tile_count * tile_bytes
        shapes_at = body_at + assign_span + flags_span + pixel_span
        scaled_n = int.from_bytes(blob[shapes_at:shapes_at + 2], "little")
        check("one scaled entry baked into the blob", scaled_n == 1)
        srow = shapes_at + 2
        s_tile = int.from_bytes(blob[srow:srow + 2], "little")
        sx, sy, sw, sh = blob[srow + 2:srow + 6]
        check("the blob's scaled entry names the fence tile", s_tile == roles["fence"])
        check("the blob's scaled entry carries the authored rect",
              (sx, sy, sw, sh) == (0, 8, 16, 8))
        crow = srow + 6
        complex_n = int.from_bytes(blob[crow:crow + 2], "little")
        check("one complex entry baked into the blob", complex_n == 1)
        c_tile = int.from_bytes(blob[crow + 2:crow + 4], "little")
        check("the blob's complex entry names the niche tile", c_tile == roles["niche"])
        mask = blob[crow + 4:crow + 4 + (T * T + 7) // 8]
        check("the complex mask has at least one ink bit set for a non-empty tile",
              any(mask))

        # Warp is still a per-cell flag, so it is still checked through the compiled map.
        banks = []
        i = 0
        while os.path.exists(os.path.join(out, f"a_b{i}.bin")):
            banks.append(read_blob(out, f"a_b{i}.bin"))
            i += 1
        mp = pnx_assets.parse_map(read_blob(out, "a.bin"), banks)
        check("warp reaches the cell it was painted on", cell_warp(mp, 2, 2))
        check("an unflagged cell stays not warp", not cell_warp(mp, 1, 2))

        # EXTENDED: a placement-authored tag, same shape as warp but an arbitrary value
        # instead of a bit, round-tripped through the WorldTile's own sparse table
        # (slice_worldtiles) rather than the map's resident preamble.
        check("extended reaches the cell it was painted on",
              pnx_assets.cell_extended(mp, 1, 2) == 7)
        check("an untagged cell carries no extended value",
              pnx_assets.cell_extended(mp, 3, 2) is None)

    expect_fail("two [[atlas.collision]] entries for one tile", "two",
                atlas=FLIP_ATLAS + '''
                    [[atlas.collision]]
                    tile = "wall"
                    type = "scaled"
                    rect = [0, 0, 16, 16]
                ''')
    expect_fail("scaled with no rect", "rect",
                atlas=FLIP_ATLAS.replace(
                    'autopick = ["floor", "wall", "accent"]',
                    'autopick = ["floor", "wall", "accent", "fence"]') + '''
                    [[atlas.collision]]
                    tile = "fence"
                    type = "scaled"
                ''')
    expect_fail("a rect that does not fit the tile", "does not fit",
                atlas=FLIP_ATLAS.replace(
                    'autopick = ["floor", "wall", "accent"]',
                    'autopick = ["floor", "wall", "accent", "fence"]') + '''
                    [[atlas.collision]]
                    tile = "fence"
                    type = "scaled"
                    rect = [10, 10, 10, 10]
                ''')
    expect_fail("an unknown flag on a legend entry", "unknown flag",
                legend='''
                    [legend."."]
                    tile = "floor"
                    flags = ["swamp"]
                    [legend."#"]
                    tile = "wall"
                    flags = []
                    [legend."D"]
                    tile = "accent"
                    flags = ["warp"]
                ''')
    expect_fail("collision moved off the legend", "collision is a tile property",
                legend='''
                    [legend."."]
                    tile = "floor"
                    flags = ["solid"]
                ''')
    expect_fail("[tile_flags] is refused outright now", "retired",
                tile_flags="[tile_flags]\nwater = 0x04\n")
    expect_fail("extended out of range", "extended must be",
                legend='''
                    [legend."."]
                    tile = "floor"
                    extended = 256
                    flags = []
                    [legend."#"]
                    tile = "wall"
                    flags = []
                    [legend."D"]
                    tile = "accent"
                    flags = ["warp"]
                ''')


def check_atlas_offset():
    """`offset` moves the carve's pixel origin independently of `region`'s tile units --
    a sheet whose art does not start flush with (0,0) (a margin, a shared border strip
    another importer left behind) has no tile-aligned pixel to name otherwise.
    """
    with tempfile.TemporaryDirectory() as root:
        colours = [(220, 40, 40), (40, 220, 40), (40, 40, 220), (220, 220, 40)]
        clean = os.path.join(root, "clean.png")
        make_colour_sheet(clean, colours, tile=16)

        # The same grid, prefixed by a 5x3px border in a colour that never appears in the
        # grid itself -- large enough that an offset=[0,0] carve of THIS sheet would read
        # a mix of border and grid pixels per tile, not merely a shifted grid.
        ox, oy = 5, 3
        clean_img = Image.open(clean)
        w, h = clean_img.size
        bordered_img = Image.new("RGBA", (w + ox, h + oy), (10, 10, 10, 255))
        bordered_img.paste(clean_img, (ox, oy))
        bordered = os.path.join(root, "bordered.png")
        bordered_img.save(bordered)

        spec_clean = {"name": "a", "sheet": "clean.png", "tile": 16,
                      "region": [0, 0, 2, 2], "out": "a.bin"}
        spec_off = {"name": "b", "sheet": "bordered.png", "tile": 16,
                    "region": [0, 0, 2, 2], "offset": [ox, oy], "out": "b.bin"}

        clean_atlas = pnx_assets.pack_atlas(root, spec_clean)
        off_atlas = pnx_assets.pack_atlas(root, spec_off)
        check("an offset carve matches the same grid with no border",
              off_atlas["tiles"] == clean_atlas["tiles"])

        # offset=[0,0] against the SAME bordered sheet must NOT match -- proving this
        # actually exercises the offset rather than something the two sheets already
        # agreed on by construction.
        noff_atlas = pnx_assets.pack_atlas(
            root, {**spec_off, "offset": [0, 0], "name": "c"})
        check("an unset offset against a bordered sheet reads something different",
              noff_atlas["tiles"] != clean_atlas["tiles"])

        try:
            pnx_assets.pack_atlas(root, {**spec_off, "offset": [-1, 0], "name": "d"})
            check("a negative offset is refused", False)
        except pnx_assets.BuildError as e:
            check("a negative offset is refused", "negative" in str(e))

        try:
            pnx_assets.pack_atlas(root, {**spec_off, "offset": [100, 100], "name": "e"})
            check("an offset that runs the region off the sheet is refused", False)
        except pnx_assets.BuildError as e:
            check("an offset that runs the region off the sheet is refused",
                  "runs past" in str(e))

        # `origin` (sheet TILE coordinates, offset already folded in) is what the editor
        # resolves a raw carve cell back to its packed tile through -- it must name the
        # SAME cells for the offset carve as the clean one, in the same order.
        check("origin is index-aligned with tiles, offset already accounted for",
              off_atlas["origin"] == clean_atlas["origin"])


def check_complex_mask_authoring():
    """A COMPLEX tile's mask can be authored explicitly (`mask`), overriding the default
    derived from the tile's own opacity -- the format extension that makes COMPLEX
    collision actually editable rather than merely computed.
    """
    with tempfile.TemporaryDirectory() as root:
        # A fully-opaque 4x4 tile: `mask` authors an X shape into it, which the tile's
        # own opacity (all-ink) would never produce on its own -- so a mask that matches
        # the X, rather than the full square, proves the override actually took.
        make_colour_sheet(os.path.join(root, "sheet.png"), [(220, 40, 40)], tile=4)
        atlas = {"name": "a", "tiles": [bytes([0xC0] * 16)], "tile_px": 4}
        mask_text = "#..#\n.##.\n.##.\n#..#"
        entry = {"tile": 0, "type": "complex", "mask": mask_text}
        collision = pnx_assets.parse_atlas_collision({"collision": [entry]}, atlas, {})
        mode, extra = collision[0]
        check("an authored mask parses as COMPLEX", mode == pnx_assets.COLLISION_COMPLEX)

        expected = pnx_assets.pack_collision_mask(
            bytearray(0xC0 if c == "#" else 0x00 for c in mask_text.replace("\n", "")), 4)
        check("the authored mask packs to the X shape, not the tile's own full opacity",
              extra == expected)
        full_opacity = pnx_assets.pack_collision_mask(bytearray([0xC0] * 16), 4)
        check("...and that differs from what auto-derivation would have produced",
              extra != full_opacity)

        # unpack_collision_mask is the editor's own round trip (show a tile's current
        # mask as text before repainting it) -- pack then unpack must reproduce exactly
        # what was authored, or the editor would silently mutate a mask just by opening it.
        check("unpack_collision_mask round-trips an authored mask",
              pnx_assets.unpack_collision_mask(extra, 4) == mask_text.split("\n"))

        expect_fail("a mask of the wrong shape", "must be exactly",
                    atlas=FLIP_ATLAS.replace(
                        'autopick = ["floor", "wall", "accent"]',
                        'autopick = ["floor", "wall", "accent", "fence"]') + '''
                        [[atlas.collision]]
                        tile = "fence"
                        type = "complex"
                        mask = "##\\n##"
                    ''')
        # textwrap.dedent'd on its OWN, separately from FLIP_ATLAS: the two fragments sit
        # at different Python-source indentation levels, and manifest()'s later dedent
        # only strips what is common to the WHOLE combined string -- which is FLIP_ATLAS's
        # shallower 4 spaces, not this block's deeper nesting. Left undone, every mask row
        # here would carry ~20 leftover spaces of indentation and fail on shape, not on
        # the invalid character this test is actually about.
        bad_char_mask = textwrap.dedent('''
            [[atlas.collision]]
            tile = "fence"
            type = "complex"
            mask = """
            xxxxxxxxxxxxxxxx
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            ................
            """
        ''')
        expect_fail("a mask with an invalid character", "only '#'",
                    atlas=FLIP_ATLAS.replace(
                        'autopick = ["floor", "wall", "accent"]',
                        'autopick = ["floor", "wall", "accent", "fence"]') + bad_char_mask)
        expect_fail("mask on a non-complex tile", "only means something there",
                    atlas=FLIP_ATLAS.replace(
                        'autopick = ["floor", "wall", "accent"]',
                        'autopick = ["floor", "wall", "accent", "fence"]') + '''
                        [[atlas.collision]]
                        tile = "fence"
                        type = "scaled"
                        rect = [0, 0, 16, 16]
                        mask = "................"
                    ''')


def check_editor_atlas_removal():
    import shutil as sh
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "tools"))
    import pnx_editor                                       # noqa: E402

    with tempfile.TemporaryDirectory() as root:
        second_sheet(root)
        path = manifest(root, atlas=TWO_ATLASES, legend=TWO_LEGEND, **maps('''
            [[map]]
            name = "a"
            out = "a.bin"
            atlases = ["tiles", "second"]
            start = [1, 1]
            rows = """
            ####
            #.s#
            ####
            """
        '''))
        proj = pnx_editor.Project(path)

        check("an atlas a map draws with reports its user",
              any("map 'a'" in u for u in proj.atlas_users("second")))
        try:
            proj.remove_atlas("second")
            check("removing an atlas in use is refused", False)
        except ValueError as e:
            check("removing an atlas in use is refused", "still in use" in str(e))

        # Point the map at one atlas, then the other really is removable -- and what is
        # left has to build, which is the assertion that would catch a block excised at
        # the wrong line.
        sh.copy(path, path + ".bak")
        text = open(path).read().replace('atlases = ["tiles", "second"]',
                                         'atlases = ["tiles"]').replace("#.s#", "#..#")
        with open(path, "w") as f:
            f.write(text)
        proj = pnx_editor.Project(path)
        proj.remove_atlas("second")

        check("the block is gone",
              not any(a["name"] == "second" for a in proj.man.get("atlas", [])))
        # The legend entry survives, and is allowed to: nothing paints with it any more,
        # so it is a dangling reference rather than a broken build. The pipeline agrees --
        # it reports the cell, not the legend.
        check("and the legend entry pointing at it survives untouched",
              proj.man.get("legend", {}).get("s", {}).get("atlas") == "second")

        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(path, os.path.join(root, "out2"),
                             os.path.join(root, "out2", "gen.h"))
        check("the manifest still builds after a removal", True)

        try:
            proj.remove_atlas("nosuch")
            check("removing an atlas that does not exist is refused", False)
        except ValueError:
            check("removing an atlas that does not exist is refused", True)

    # The first atlas is what every map with NO `atlas` key draws with, so removing it
    # would silently re-point them at whatever is declared first instead. That rebuilds
    # clean and draws the wrong world, which is the worst shape a content bug can take --
    # and the reason this case is called out separately rather than folded into "in use".
    with tempfile.TemporaryDirectory() as root:
        second_sheet(root)
        proj = pnx_editor.Project(manifest(root, atlas=TWO_ATLASES))
        check("removing the default atlas warns about the maps that rely on it",
              any("first atlas declared" in u for u in proj.atlas_users("tiles")))
        check("a second atlas nothing uses is removable",
              proj.atlas_users("second") == [])


# ------------------------------------------------------------- editor: legend writing
#
# The editor writes the legend now, which is what makes every tile of an atlas paintable
# instead of only the three `autopick` names. The thing worth testing is not that the
# right characters land in the file -- it is that the manifest still BUILDS afterwards,
# because a legend entry that does not resolve breaks every map painting it.

# At column 0 deliberately. `manifest` runs each part through textwrap.dedent, and the
# shared fixture's rows sit at column 0 already -- so the common prefix is empty, nothing
# is dedented, and `[[map]]` ends up indented. tomllib does not mind, but save_map anchors
# on `^\[\[map\]\]`, so a test that edits a map has to supply one it can find.
EDITOR_MAP = '''
# ------------------------------------------------------------------------------ maps
#
# A section heading and a comment between the legend and the first map, because that is
# what a real manifest looks like and it is what a legend writer has to not step over.

[[map]]
name = "a"
out = "a.bin"
start = [2, 1]
warps = []
rows = """
####
#..#
####
"""
'''


def editor_project(root, **overrides):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "tools"))
    import pnx_editor                                       # noqa: E402
    return pnx_editor.Project(manifest(root, **overrides))


def builds(path, root, tag):
    """True when the manifest at `path` still compiles."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(path, os.path.join(root, tag),
                             os.path.join(root, tag, "gen.h"))
        return True
    except pnx_assets.BuildError as e:
        print(f"         build failed: {e}")
        return False


def check_editor_legend():
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, atlas=FLIP_ATLAS, **maps(EDITOR_MAP))

        # A raw index, which is the whole point: no role, no [atlas.semantic] entry, and
        # still paintable.
        proj.save_legend("q", 2, flags=["warp"])
        check("a legend entry written by index lands in the manifest",
              proj.man["legend"]["q"] == {"tile": 2, "flags": ["warp"]})

        proj.save_legend("<", 2, flip=["x"])
        check("a flipped entry records its axis",
              proj.man["legend"]["<"].get("flip") == ["x"])

        # Rewriting in place rather than appending a second block for the same character,
        # which TOML would reject outright as a duplicate key.
        proj.save_legend("q", "wall", flags=[])
        check("saving the same character twice rewrites it",
              proj.man["legend"]["q"] == {"tile": "wall", "flags": []})
        check("and leaves exactly one block for it",
              open(proj.path).read().count('[legend."q"]') == 1)

        # Rewriting must not eat the blank line separating the entry from what follows.
        # It once did, so every flag ticked closed the gap a little more.
        gap = lambda: open(proj.path).read().split('[legend."<"]')[0].endswith("\n\n")
        was = gap()
        proj.save_legend("q", "wall", flags=["warp"])
        proj.save_legend("q", "wall", flags=[])
        check("rewriting an entry keeps the blank line after it", gap() == was)

        check("the manifest still builds with the new entries",
              builds(proj.path, root, "out_legend"))

        # Placed with the other legend entries, not merely somewhere that parses. A new
        # entry once landed under the section heading and explanatory comment belonging to
        # the MAPS below it -- valid TOML that reads as though someone lost their place,
        # which in a file whose comments are half its content is a real defect.
        text = open(proj.path).read().split("\n")
        at = text.index('[legend."q"]')
        before = [l for l in text[:at] if l.strip()][-1]
        check("a new entry sits with the legend, not in the next section",
              not before.startswith("#") and not before.startswith("[["))

        # Removal is refused while a map paints it, for the same reason an atlas removal
        # is: the alternative is a manifest that only hand-editing can fix.
        proj.save_legend("z", "floor")
        proj.save_map("a", ["####", "#z.#", "####"], [2, 1], [])
        try:
            proj.remove_legend("z")
            check("removing a painted legend character is refused", False)
        except ValueError as e:
            check("removing a painted legend character is refused",
                  "still painted" in str(e))
        check("and it names the map", proj.legend_users("z") == ["a"])

        proj.save_map("a", ["####", "#..#", "####"], [2, 1], [])
        proj.remove_legend("z")
        check("once nothing paints it, it goes", "z" not in proj.man["legend"])
        check("the manifest builds after a removal",
              builds(proj.path, root, "out_removed"))

        # The checks that keep the picker from writing something the build refuses.
        for label, call in (
            ("an index past the end of the atlas",
             lambda: proj.save_legend("Q", 999)),
            ("an unknown flag", lambda: proj.save_legend("Q", 0, flags=["nope"])),
            ("an unknown atlas", lambda: proj.save_legend("Q", 0, atlas="nope")),
            ("a multi-character key", lambda: proj.save_legend("ab", 0)),
            ("a whitespace character", lambda: proj.save_legend(" ", 0)),
        ):
            try:
                call()
                check(f"the editor refuses {label}", False)
            except ValueError:
                check(f"the editor refuses {label}", True)


def _check_editor_flags_RETIRED():
    # [tile_flags] itself is retired (parse_flag_names refuses it outright -- see
    # MAP_ROTATE's comment in pnx_assets.py), so this whole test now exercises a feature
    # that cannot build. tools/pnx_editor.py's save_flag/remove_flag/flag_names methods
    # are correspondingly dead code, not yet removed -- deferred to the editor UI pass
    # for role/collision-type/rotate editing, so the removal happens alongside whatever
    # replaces this rather than leaving a UI hole in between. Kept, renamed and disabled,
    # rather than deleted outright, as the record of what that pass needs to account for.
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, atlas=FLIP_ATLAS, **maps(EDITOR_MAP))

        got = proj.save_flag("water")
        check("a new flag takes the lowest free bit", got["bit"] == 0x04)
        check("and a second takes the next", proj.save_flag("ledge")["bit"] == 0x08)
        # Adding a flag that already exists must NOT move it. It used to take "the lowest
        # free bit", which for an existing name is the next one along -- silently changing
        # what every built map and every compiled `& TILE_FLAG_WATER` already meant.
        check("re-adding an existing flag keeps its bit",
              proj.save_flag("water")["bit"] == 0x04)
        check("and does not duplicate the line",
              open(proj.path).read().count("water = ") == 1)
        check("both are readable back",
              proj.flag_names().get("water") == 0x04
              and proj.flag_names().get("ledge") == 0x08)

        proj.save_legend("~", "floor", flags=["water"])
        check("a custom flag can be set on a legend entry",
              proj.man["legend"]["~"]["flags"] == ["water"])
        check("and the manifest builds with it",
              builds(proj.path, root, "out_flags"))
        check("the flag reaches the generated header",
              defines(os.path.join(root, "out_flags")).get("TILE_FLAG_WATER") == 0x04)

        try:
            proj.remove_flag("water")
            check("removing a flag still in use is refused", False)
        except ValueError as e:
            check("removing a flag still in use is refused", "still set" in str(e))

        proj.save_legend("~", "floor", flags=[])
        proj.remove_flag("water")
        check("once nothing carries it, it goes", "water" not in proj.flag_names())

        for label, call in (
            ("redefining a built-in", lambda: proj.save_flag("solid")),
            ("a name that is not an identifier", lambda: proj.save_flag("deep water")),
            ("a bit another flag holds", lambda: proj.save_flag("mud", 0x08)),
        ):
            try:
                call()
                check(f"the editor refuses {label}", False)
            except ValueError:
                check(f"the editor refuses {label}", True)


def check_editor_roles():
    """Naming a tile writes [atlas.semantic] under the right atlas, and it compiles."""
    with tempfile.TemporaryDirectory() as root:
        second_sheet(root)
        proj = editor_project(root, atlas=TWO_ATLASES, legend=TWO_LEGEND,
                              **maps(EDITOR_MAP))

        proj.save_role("second", "door", 2)
        check("a named tile lands in the atlas's semantic table",
              proj.man["atlas"][1].get("semantic", {}).get("door") == 2)
        check("and not in the other atlas's",
              "semantic" not in proj.man["atlas"][0])

        # The subtable binds to the most recent [[atlas]] in TOML, so a table written
        # under the wrong block names a tile in the wrong tileset -- silently, because
        # both parse. This is the assertion that catches it.
        text = open(proj.path).read()
        after = text[text.index("[atlas.semantic]"):]
        before = text[:text.index("[atlas.semantic]")]
        check("the semantic table follows the atlas it belongs to",
              before.rindex('name = "second"') > before.rindex('name = "tiles"'))

        proj.save_role("second", "gate", 3)
        check("a second name joins the same table",
              proj.man["atlas"][1]["semantic"] == {"door": 2, "gate": 3})
        check("the manifest builds with named tiles",
              builds(proj.path, root, "out_roles"))
        d = defines(os.path.join(root, "out_roles"))
        check("a named tile reaches the header prefixed by its atlas",
              d.get("SECOND_TILE_DOOR") == 2 and d.get("SECOND_TILE_GATE") == 3)

        # A role only becomes paintable through a legend character, which is the same
        # path a raw index takes -- so naming and painting compose.
        proj.save_legend("g", "door", atlas="second")
        check("a legend entry can resolve through a named tile",
              builds(proj.path, root, "out_named"))

        try:
            proj.remove_role("second", "door")
            check("removing a role a legend resolves through is refused", False)
        except ValueError as e:
            check("removing a role a legend resolves through is refused",
                  "resolve through" in str(e))

        for label, call in (
            ("a role name that is not an identifier",
             lambda: proj.save_role("second", "front door", 1)),
            ("a tile index past the atlas",
             lambda: proj.save_role("second", "far", 999)),
            ("an unknown atlas", lambda: proj.save_role("nope", "x", 0)),
            ("moving an existing name to another tile",
             lambda: proj.save_role("second", "gate", 1)),
        ):
            try:
                call()
                check(f"the editor refuses {label}", False)
            except ValueError:
                check(f"the editor refuses {label}", True)

        # Pinning a name `autopick` invented is ALLOWED, and reported. The pipeline
        # applies `semantic` over `autopick` on purpose -- "a manifest can start auto and
        # be pinned down later" -- so refusing it here left every autopicked name
        # unreachable from the editor, with hand-editing the manifest as the only way out.
        info = proj.save_role("second", "wall", 2)
        check("the editor pins a name autopick already owns",
              proj.man["atlas"][1].get("semantic", {}).get("wall") == 2)
        check("and says that it was autopicked", info.get("pinned") is True)


TWO_MAPS = EDITOR_MAP + '''
[[map]]
name = "b"
out = "b.bin"
start = [2, 1]
warps = []
rows = """
####
#..#
####
"""
'''


TWO_MAPS_WARPED = '''
[[map]]
name = "a"
out = "a.bin"
start = [2, 1]
warps = [{ at = [1, 2], to = ["b", 1, 1] }]
rows = """
####
#..#
#D.#
####
"""

[[map]]
name = "b"
out = "b.bin"
start = [1, 1]
warps = []
rows = """
####
#..#
####
"""
'''


def check_map_legend():
    """A map's own [map.legend], overlaid on the project one.

    One character per cell caps a map at the printable set -- about ninety. Project-wide
    that ninety was shared by every map and every atlas in the game, which put most of a
    carved tileset permanently out of reach. Per map it is ninety EACH, and the same
    character can mean a different tile in each map, which is what these assert.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, atlas=FLIP_ATLAS, **maps(TWO_MAPS))

        proj.save_legend("%", 1, "tiles", ["warp"], [], map_name="a")
        proj.save_legend("%", 2, "tiles", [], [], map_name="b")

        by_name = {m["name"]: m for m in proj.man["map"]}
        check("a map legend entry lands in its own map",
              by_name["a"]["legend"]["%"]["tile"] == 1
              and by_name["b"]["legend"]["%"]["tile"] == 2)
        check("and not in the project legend", "%" not in proj.man.get("legend", {}))

        # The subtable has to be written AFTER `rows`: a subtable closes its parent, so an
        # entry above it would put the map's remaining keys inside [map.legend] and the
        # build would fail saying the map has no rows.
        check("the map still has its rows", "rows" in by_name["a"])

        rows_a = [r for r in by_name["a"]["rows"].strip("\n").split("\n") if r.strip()]
        rows_a[1] = "#%.#"
        proj.save_map("a", rows_a, [2, 1], [])
        check("a map paints its own character", builds(proj.path, root, "out_ml"))

        # Scoped: 'b' does not inherit 'a's characters, only the project's.
        rows_b = ["####", "#&.#", "####"]
        proj.save_map("b", rows_b, [2, 1], [])
        check("a character from another map is not in scope",
              not builds(proj.path, root, "out_ml2"))

        proj.save_map("b", ["####", "#%.#", "####"], [2, 1], [])
        check("but the map's own character of the same name is",
              builds(proj.path, root, "out_ml3"))

        # Removal is scoped too, and refuses while the map still paints it.
        try:
            proj.remove_legend("%", "a")
            check("removing a painted map character is refused", False)
        except ValueError:
            check("removing a painted map character is refused", True)

        check("legend_users reports only the owning map",
              proj.legend_users("%", "a") == ["a"])


def check_editor_scenes():
    """Scene CRUD. A scene is the framework's only load point, and it was the one part of
    the manifest the editor could not touch -- so a map could be drawn, painted and built
    and still be unreachable from the game."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        make_sheet(os.path.join(root, "hero.png"), tiles_across=1)
        proj = editor_project(root, sprite='''
            [[sprite]]
            name = "hero"
            sheet = "hero.png"
            frames = [[0, 0, 16, 16]]
            out = "hero.bin"
        ''', scene='''
            [scene.one]
            map = "a"
            # Why this scene does not load the hero, in a comment that has to survive
            # every rewrite of the table around it.
            sprites = []
        ''')

        proj.save_scene("one", "a", ["hero"], [])
        check("a scene's sprites are rewritten",
              proj.man["scene"]["one"]["sprites"] == ["hero"])
        check("and the comment inside the table survives",
              "has to survive" in open(proj.path).read())

        proj.save_scene("two", "b", [], [])
        check("a new scene is appended", "two" in proj.man["scene"])
        check("and the manifest still builds", builds(proj.path, root, "out_sc"))

        for label, call in (
            ("an unknown map", lambda: proj.save_scene("three", "nope")),
            ("an unknown sprite", lambda: proj.save_scene("three", "a", ["nope"])),
            ("an unknown font", lambda: proj.save_scene("three", "a", [], ["nope"])),
            ("a scene name that is not an identifier",
             lambda: proj.save_scene("Three Scenes", "a")),
            ("dialog with no dialog defined",
             lambda: proj.save_scene("three", "a", [], [], True)),
            ("a scene that loads nothing", lambda: proj.save_scene("three")),
            # The clash the pipeline refuses and the editor used to WRITE on every new
            # map: a scene listing an atlas its own map streams loads a second resident
            # copy, and nothing at runtime can see that.
            ("an atlas the map already streams",
             lambda: proj.save_scene("three", "a", [], [], False, ["tiles"])),
        ):
            try:
                call()
                check(f"a scene refuses {label}", False)
            except ValueError:
                check(f"a scene refuses {label}", True)

        proj.remove_scene("two")
        check("a scene is removed", "two" not in proj.man["scene"])
        try:
            proj.remove_scene("two")
            check("removing a scene that is gone is refused", False)
        except ValueError:
            check("removing a scene that is gone is refused", True)


def check_editor_new_map_scene():
    """A new map has to arrive loadable.

    add_map used to write `atlases = [...]` into the scene it generated, which the
    pipeline refuses since a map streams its own tilesets -- so every map created in the
    editor produced a manifest that would not build.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root)

        proj.add_map("fresh", 8, 6, "tiles")
        scene = proj.man["scene"]["fresh"]
        check("a new map gets a scene", scene["map"] == "fresh")
        check("and the scene does not restate the map's atlases",
              "atlases" not in scene)
        check("and the new map builds", builds(proj.path, root, "out_nm"))


def check_editor_sprites():
    """Declaring a sprite, which the Sprites tab could not do -- it painted PNGs and
    stopped, so the art existed and nothing could load it."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        # 16 wide, 48 tall: three stacked 16x16 frames.
        Image.new("RGBA", (16, 48), (30, 90, 200, 255)).save(
            os.path.join(root, "hero.png"))
        proj = editor_project(root, sprite='''
[[sprite]]
name = "npc"
sheet = "hero.png"
frames = [[0, 0, 16, 16]]
out = "npc.bin"
# Why this sprite has no anim table, in a comment that has to survive a rewrite.
''')

        proj.save_sprite("hero", "hero.png",
                         [[0, 0, 16, 16], [0, 16, 16, 16], [0, 32, 16, 16]],
                         {"stand": 0, "step_a": 1, "step_b": 2})
        got = {s["name"]: s for s in proj.sprites()}
        check("a sprite is declared", got["hero"]["frames"] == [[0, 0, 16, 16],
                                                                [0, 16, 16, 16],
                                                                [0, 32, 16, 16]])
        check("with its anim names", got["hero"]["anim"]["step_b"] == 2)
        check("and the manifest builds", builds(proj.path, root, "out_sp"))
        check("the anim names reach the header",
              "HERO_ANIM_STEP_B" in open(os.path.join(root, "out_sp", "gen.h")).read()
              or "STEP_B" in open(os.path.join(root, "out_sp", "gen.h")).read())

        # Rewriting an existing sprite keeps the comment inside its block.
        proj.save_sprite("npc", "hero.png", [[0, 0, 16, 16]], {"idle": 0})
        check("rewriting a sprite keeps its comments",
              "has to survive a rewrite" in open(proj.path).read())
        check("and can add an anim table",
              {s["name"]: s for s in proj.sprites()}["npc"]["anim"] == {"idle": 0})
        proj.save_sprite("npc", "hero.png", [[0, 0, 16, 16]], {})
        check("and can take it away again",
              {s["name"]: s for s in proj.sprites()}["npc"]["anim"] == {})
        check("and still builds", builds(proj.path, root, "out_sp2"))

        for label, call in (
            ("frames that disagree on size",
             lambda: proj.save_sprite("bad", "hero.png",
                                      [[0, 0, 16, 16], [0, 16, 8, 16]])),
            ("a frame running off the sheet",
             lambda: proj.save_sprite("bad", "hero.png", [[0, 0, 16, 99]])),
            ("an odd pixel count",
             lambda: proj.save_sprite("bad", "hero.png", [[0, 0, 3, 5]])),
            ("an anim naming a frame that does not exist",
             lambda: proj.save_sprite("bad", "hero.png", [[0, 0, 16, 16]], {"x": 4})),
            ("no frames at all", lambda: proj.save_sprite("bad", "hero.png", [])),
            ("a name that is not an identifier",
             lambda: proj.save_sprite("Bad Sprite", "hero.png", [[0, 0, 16, 16]])),
            ("an anim name that is not an identifier",
             lambda: proj.save_sprite("bad", "hero.png", [[0, 0, 16, 16]],
                                      {"step one": 0})),
        ):
            try:
                call()
                check(f"a sprite refuses {label}", False)
            except ValueError:
                check(f"a sprite refuses {label}", True)

        proj.save_scene("s1", "a", ["hero"], [])
        check("what loads a sprite is reported", proj.sprite_users("hero") == ["scene s1"])
        try:
            proj.remove_sprite("hero")
            check("removing a sprite a scene loads is refused", False)
        except ValueError:
            check("removing a sprite a scene loads is refused", True)

        proj.remove_sprite("npc")
        check("an unused sprite is removed",
              [s["name"] for s in proj.sprites()] == ["hero"])
        check("and the manifest builds", builds(proj.path, root, "out_sp3"))


def check_mapfile_format():
    """The `.pnxmap` container, on its own before anything builds with it."""
    import pnx_mapfile as mf

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "m.pnxmap")
        tiles = [{"atlas": "tiles", "index": 0, "flags": 0, "flip": ""},
                 {"atlas": "tiles", "index": "wall", "flags": 1, "flip": "x"},
                 {"atlas": "water", "index": 900, "flags": 2, "flip": "xy", "extended": 42}]
        cells = [0, 1, 2, 0, 1, 1]
        warps = [{"at": [1, 1], "to": ["cave", 3, 4], "gated": True}]
        mf.write(path, 3, 2, [2, 1], tiles, cells, warps)
        doc = mf.read(path)

        check("a map file round-trips its grid",
              (doc["w"], doc["h"], doc["start"], doc["cells"]) == (3, 2, [2, 1], cells))
        # read() adds "rotate" to every entry now (bit 2 of the same flip byte, see
        # write()'s own comment) -- none of `tiles` set it, so it should read back False.
        # It adds "extended" too, defaulting to 0 for the entries that did not set it.
        check("and its tile table",
              doc["tiles"] == [dict(t, rotate=False, extended=t.get("extended", 0))
                               for t in tiles])
        check("a nonzero extended value survives specifically",
              doc["tiles"][2]["extended"] == 42)
        check("and its warps", doc["warps"] == warps)
        # A role survives as a role. Freezing it to whatever number it held at conversion
        # is the one thing that would make migrating a manifest a silent downgrade.
        check("a role stays symbolic", doc["tiles"][1]["index"] == "wall")
        # 1024 is what the compiled cell's 10-bit index reaches; the container is wider so
        # it is never the thing that binds.
        check("an index past the old ~90 character ceiling survives",
              doc["tiles"][2]["index"] == 900)

        rows, legend = mf.to_rows(doc)
        check("a small map converts back to text", rows == [".,:", ".,,"])
        check("and the text carries a legend", len(legend) == 3)
        big = {"w": 1, "h": 1, "cells": [0],
               "tiles": [{"atlas": "a", "index": i, "flags": 0, "flip": ""}
                         for i in range(200)]}
        check("a map with more tiles than characters does not pretend to convert",
              mf.to_rows(big)[0] is None)

        for label, call in (
            ("a cell count that does not match the size",
             lambda: mf.write(path, 3, 2, [0, 0], tiles, [0])),
            ("a cell naming a tile that is not in the table",
             lambda: mf.write(path, 1, 1, [0, 0], tiles, [9])),
            ("an empty tile table", lambda: mf.write(path, 1, 1, [0, 0], [], [0])),
            ("a size the format cannot store",
             lambda: mf.write(path, 300, 1, [0, 0], tiles, [0] * 300)),
            ("a file that is not a map", lambda: mf.loads(b"XXXX" + bytes(30))),
            ("a truncated file", lambda: mf.loads(b"PNXM" + bytes(4))),
        ):
            try:
                call()
                check(f"the map format refuses {label}", False)
            except mf.MapFileError:
                check(f"the map format refuses {label}", True)


def check_source_maps():
    """A map authored in a file rather than in the manifest.

    The claim worth testing is not "it loads" -- it is that a source map goes through the
    SAME checks a text map does. A second authoring path that quietly skipped the flood
    fill or the warp checks would be worse than no second path.
    """
    import pnx_mapfile as mf

    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        os.makedirs(os.path.join(root, "maps"))

        def write_map(cells, w=6, h=5, start=(1, 1), warps=(), tiles=None):
            tiles = tiles or [
                {"atlas": "tiles", "index": "floor", "flags": 0, "flip": ""},
                # "wall" carries no flag of its own here -- SOLID is declared on the
                # atlas now (manifest()'s default [[atlas.collision]]), not in a tile
                # table's flags byte. TILE_WARP (0x01) is the only bit this byte still
                # means anything for.
                {"atlas": "tiles", "index": "wall", "flags": 0, "flip": ""},
                {"atlas": "tiles", "index": "accent", "flags": pnx_assets.TILE_WARP,
                 "flip": ""}]
            mf.write(os.path.join(root, "maps", "a.pnxmap"), w, h, start, tiles,
                     cells, warps)

        def room(w=6, h=5, fill=0):
            return [1 if (x in (0, w - 1) or y in (0, h - 1)) else fill
                    for y in range(h) for x in range(w)]

        src = '''
[[map]]
name = "a"
out = "a.bin"
source = "maps/a.pnxmap"
'''
        # manifest() always writes the same filename, so every variant is copied to a name
        # of its own. Without that the later cases build whichever manifest was written
        # last -- which is what made seven refusal tests silently pass by testing nothing.
        def variant(tag, body):
            import shutil as _sh
            dst = os.path.join(root, f"{tag}.toml")
            _sh.copy(manifest(root, maps=body), dst)
            return dst

        write_map(room())
        path = variant("src", src)
        check("a map authored in a file builds", builds(path, root, "out_src"))

        # Byte-for-byte the same as the text form. If these ever diverge, one of the two
        # authoring paths is lying about what it produces.
        text = variant("txt", '''
[[map]]
name = "a"
out = "a.bin"
start = [1, 1]
warps = []
rows = """
######
#....#
#....#
#....#
######
"""
''')
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(path, os.path.join(root, "b_src"),
                             os.path.join(root, "b_src", "gen.h"))
            pnx_assets.build(text, os.path.join(root, "b_txt"),
                             os.path.join(root, "b_txt", "gen.h"))
        check("and compiles to the same bytes as the text form",
              open(os.path.join(root, "b_src", "a.bin"), "rb").read()
              == open(os.path.join(root, "b_txt", "a.bin"), "rb").read())

        # The shared checks, reached through the file rather than through rows.
        write_map(room(), start=(0, 0))
        check("a start inside a wall is refused", not builds(path, root, "out_s1"))

        write_map(room(), start=(9, 9))
        check("a start outside the map is refused", not builds(path, root, "out_s2"))

        cells = room()
        cells[2 * 6 + 2] = 2                      # an accent tile, which carries `warp`
        write_map(cells, warps=[{"at": [2, 2], "to": ["a", 1, 1], "gated": False}])
        check("a warp on a warp-flagged tile builds", builds(path, root, "out_s3"))

        write_map(room(), warps=[{"at": [1, 1], "to": ["a", 1, 1], "gated": False}])
        check("a warp on a tile with no warp flag is refused",
              not builds(path, root, "out_s4"))

        # Sealed off: a solid ring around the warp, so the flood fill cannot reach it.
        cells = room()
        for x, y in ((3, 1), (3, 2), (3, 3), (4, 1), (4, 3), (5, 2)):
            cells[y * 6 + x] = 1
        cells[2 * 6 + 4] = 2
        write_map(cells, warps=[{"at": [4, 2], "to": ["a", 1, 1], "gated": False}])
        check("an unreachable warp is refused", not builds(path, root, "out_s5"))
        write_map(cells, warps=[{"at": [4, 2], "to": ["a", 1, 1], "gated": True}])
        check("and `gated` declares it deliberate", builds(path, root, "out_s6"))

        write_map(room(), tiles=[
            {"atlas": "tiles", "index": "floor", "flags": 0, "flip": ""},
            {"atlas": "nope", "index": 0, "flags": 1, "flip": ""}])
        check("a tile drawing from an atlas the map does not use is refused",
              not builds(path, root, "out_s7"))

        write_map(room(), tiles=[
            {"atlas": "tiles", "index": "floor", "flags": 0, "flip": ""},
            {"atlas": "tiles", "index": 9999, "flags": 1, "flip": ""}])
        check("a tile index past what the atlas packed is refused",
              not builds(path, root, "out_s8"))

        write_map(room(), tiles=[
            {"atlas": "tiles", "index": "floor", "flags": 0, "flip": ""},
            {"atlas": "tiles", "index": "nosuchrole", "flags": 1, "flip": ""}])
        check("a role the atlas does not define is refused",
              not builds(path, root, "out_s9"))

        # Both formats at once is two descriptions of one grid with nothing keeping them
        # in step, so the build refuses rather than silently picking.
        both = variant("both", '''
[[map]]
name = "a"
out = "a.bin"
source = "maps/a.pnxmap"
rows = """
###
#.#
###
"""
''')
        write_map(room())
        check("a map with both `source` and `rows` is refused",
              not builds(both, root, "out_s10"))

        neither = variant("neither", '''
[[map]]
name = "a"
out = "a.bin"
start = [1, 1]
''')
        check("a map with neither is refused", not builds(neither, root, "out_s11"))

        missing = variant("missing", '''
[[map]]
name = "a"
out = "a.bin"
source = "maps/gone.pnxmap"
''')
        check("a source file that is not there is refused",
              not builds(missing, root, "out_s12"))


def check_editor_map_migration():
    """Moving a `rows` map into its own file, through the editor."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        # A multi-line warps array, which is what the worldtiles example writes and what
        # the first version of this got wrong: it dropped the `warps = [` line and left
        # the continuation lines behind as bare TOML.
        proj = editor_project(root, maps='''
[[map]]
name = "a"
out = "a.bin"
start = [2, 1]
warps = [
  { at = [1, 2], to = ["b", 1, 1] },
]
rows = """
####
#..#
#D.#
####
"""

[[map]]
name = "b"
out = "b.bin"
start = [1, 1]
warps = []
rows = """
####
#..#
####
"""
''')
        before = {m["name"]: m for m in proj.maps()}["a"]
        info = proj.migrate_map("a")
        check("a map moves into its own file", info["source"] == "maps/a.pnxmap")
        check("and the file is on disk",
              os.path.exists(os.path.join(root, "maps", "a.pnxmap")))

        after = {m["name"]: m for m in proj.maps()}["a"]
        check("the manifest still parses", after["format"] == "source")
        check("the cells are unchanged", after["cells"] == before["cells"])
        check("the start comes with it", after["start"] == before["start"])
        check("the multi-line warps come with it",
              [w["to"][0] for w in after["warps"]] == ["b"])
        check("and it still builds", builds(proj.path, root, "out_mig"))

        try:
            proj.migrate_map("a")
            check("migrating a map that is already a file is refused", False)
        except ValueError:
            check("migrating a map that is already a file is refused", True)

        # Editing a source map writes the file and leaves the manifest alone.
        stamp = os.path.getmtime(proj.path)
        cells = list(after["cells"])
        cells[1 * after["w"] + 1] = 0
        proj.save_source_map("a", after["w"], after["h"], cells, after["tiles"],
                             after["start"], after["warps"])
        check("editing a source map writes the file",
              {m["name"]: m for m in proj.maps()}["a"]["cells"] == cells)
        check("and does not touch the manifest",
              os.path.getmtime(proj.path) == stamp)

        # New maps take the new format; the old one stays reachable on request.
        proj.add_map("fresh", 8, 6, "tiles")
        proj.add_map("oldstyle", 8, 6, "tiles", text=True)
        got = {m["name"]: m for m in proj.maps()}
        check("a new map is authored as a file", got["fresh"]["format"] == "source")
        check("and text is still available", got["oldstyle"]["format"] == "rows")
        check("both build", builds(proj.path, root, "out_mig2"))


def check_editor_sheet_frames():
    """Picking frames off a sheet, and editing one of them in place.

    The declare form could only describe a vertical stack, and the pixel canvas could only
    open whole files -- so a sheet laid out in a grid meant writing the rectangles by hand,
    and touching one pose of eight meant loading all eight.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))

        # Four 8x8 frames in a 2x2 grid, each a different flat colour, so a frame written
        # into one cell is provably not landing in another.
        cols = [(200, 40, 40), (40, 200, 40), (40, 40, 200), (200, 200, 40)]
        im = Image.new("RGBA", (16, 16), (0, 0, 0, 255))
        for i, c in enumerate(cols):
            for y in range(8):
                for x in range(8):
                    im.putpixel(((i % 2) * 8 + x, (i // 2) * 8 + y), c + (255,))
        im.save(os.path.join(root, "grid.png"))
        proj = editor_project(root)

        g = proj.sheet_frames("grid.png", 8, 8)
        check("a sheet slices into a grid of frames",
              (g["cols"], g["rows"], len(g["cells"])) == (2, 2, 4))
        check("and the cells carry their rects",
              [(c["x"], c["y"]) for c in g["cells"]]
              == [(0, 0), (8, 0), (0, 8), (8, 8)])

        # Origin and gap, for sheets with a border or gutters.
        g2 = proj.sheet_frames("grid.png", 4, 4, ox=2, oy=2, gx=2, gy=2)
        check("origin and gap move the grid",
              g2["cells"][0]["x"] == 2 and g2["cells"][1]["x"] == 8)

        before = [proj.frame_read("grid.png", c["x"], c["y"], 8, 8)["pixels"]
                  for c in g["cells"]]
        check("a single frame reads back at its own size", len(before[0]) == 64)

        # Overwrite frame 2 only.
        proj.frame_write("grid.png", 0, 8, 8, 8, [0] * 64)
        after = [proj.frame_read("grid.png", c["x"], c["y"], 8, 8)["pixels"]
                 for c in g["cells"]]
        check("writing a frame changes that frame", after[2] != before[2])
        check("and leaves every other frame alone",
              after[0] == before[0] and after[1] == before[1]
              and after[3] == before[3])
        check("and does not resize the sheet",
              Image.open(os.path.join(root, "grid.png")).size == (16, 16))

        # Picked frames go straight into a declaration, in the picked order.
        picked = [[c["x"], c["y"], c["w"], c["h"]] for c in (g["cells"][1], g["cells"][3])]
        proj.save_sprite("walk", "grid.png", picked, {"a": 0, "b": 1})
        got = {s["name"]: s for s in proj.sprites()}["walk"]
        check("frames picked off a grid become a sprite", got["frames"] == picked)
        check("and the manifest builds", builds(proj.path, root, "out_sh"))

        for label, call in (
            ("a frame outside the sheet",
             lambda: proj.frame_read("grid.png", 0, 12, 8, 8)),
            ("a write outside the sheet",
             lambda: proj.frame_write("grid.png", 12, 12, 8, 8, [0] * 64)),
            ("a pixel count that does not match the rect",
             lambda: proj.frame_write("grid.png", 0, 0, 8, 8, [0] * 10)),
            ("a sheet that is not there",
             lambda: proj.frame_write("gone.png", 0, 0, 8, 8, [0] * 64)),
        ):
            try:
                call()
                check(f"frame editing refuses {label}", False)
            except ValueError:
                check(f"frame editing refuses {label}", True)


def check_editor_dialog():
    """Text is content and lives in the manifest, so it needed an editor and had none."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, dialog='''
[dialog.greeting]
pages = ["Careful in there."]
''')

        proj.save_dialog("greeting", ["Careful in there.", "The lamps went out."])
        check("a conversation's pages are rewritten",
              len(proj.dialogs()[0]["pages"]) == 2)
        proj.save_dialog("smith", ["Need a blade?"])
        check("a new conversation is added",
              [d["name"] for d in proj.dialogs()] == ["greeting", "smith"])
        check("and the manifest builds", builds(proj.path, root, "out_dl"))

        for label, call in (
            # pack_dialog encodes ASCII with errors="replace", so anything else becomes a
            # literal '?' on the watch with nothing said.
            ("text that is not ASCII", lambda: proj.save_dialog("x", ["café"])),
            ("a page holding a newline", lambda: proj.save_dialog("x", ["a\nb"])),
            ("no pages at all", lambda: proj.save_dialog("x", [])),
            ("a name that is not an identifier",
             lambda: proj.save_dialog("Bad Name", ["a"])),
        ):
            try:
                call()
                check(f"dialog refuses {label}", False)
            except ValueError:
                check(f"dialog refuses {label}", True)

        proj.remove_dialog("smith")
        check("a conversation is removed",
              [d["name"] for d in proj.dialogs()] == ["greeting"])

        # `dialog = true` loads the whole blob, so removing an entry only strands a scene
        # when it is the last one.
        proj.save_scene("s1", "a", [], [], dialog=True)
        try:
            proj.remove_dialog("greeting")
            check("removing the last dialog a scene needs is refused", False)
        except ValueError:
            check("removing the last dialog a scene needs is refused", True)


def check_editor_map_props():
    """The M4b palette variant and the M4d streaming controls, none of which save_map
    touched -- so the streaming work M4d measured could not be tuned from the editor."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root)

        proj.set_map_props("a", worldtile=16, bank_bytes=2048, resident=True)
        got = {m["name"]: m for m in proj.maps()}["a"]
        check("worldtile is set", got["worldtile"] == 16)
        check("bank_bytes is set", got["bank_bytes"] == 2048)
        check("resident is set", got["resident"] is True)
        check("and the manifest builds", builds(proj.path, root, "out_mp"))

        # "" is how a key goes back to the pipeline's own choice, which is distinct from
        # leaving it alone -- the reason these take a string rather than None for unset.
        proj.set_map_props("a", bank_bytes="", resident=False)
        got = {m["name"]: m for m in proj.maps()}["a"]
        check("a cleared key goes back to the default", got["bank_bytes"] is None)
        check("and resident comes off", got["resident"] is False)
        check("and it still builds", builds(proj.path, root, "out_mp2"))

        for label, call in (
            ("a worldtile that is not a power of two",
             lambda: proj.set_map_props("a", worldtile=12)),
            ("a worldtile past the format's range",
             lambda: proj.set_map_props("a", worldtile=64)),
            ("more atlas slots than the map has tilesets",
             lambda: proj.set_map_props("a", atlas_slots=9)),
            ("a bank under one WorldTile's cells",
             lambda: proj.set_map_props("a", bank_bytes=100)),
            ("a palette no tileset of this map defines",
             lambda: proj.set_map_props("a", palette="nope")),
            ("an unknown map", lambda: proj.set_map_props("gone", worldtile=8)),
        ):
            try:
                call()
                check(f"map properties refuse {label}", False)
            except ValueError:
                check(f"map properties refuse {label}", True)


def check_editor_atlas_extras():
    """metatiles and variants, the two atlas keys the Import form never owned."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root)

        proj.set_atlas_extras("tiles", metatiles="true")
        check("metatiles is forced on", proj.atlas_spec("tiles")["metatiles"] is True)
        proj.set_atlas_extras("tiles", metatiles=0.25)
        check("a threshold fraction is written",
              proj.atlas_spec("tiles")["metatiles"] == 0.25)
        proj.set_atlas_extras("tiles", metatiles="auto")
        check("and it goes back to auto",
              proj.atlas_spec("tiles")["metatiles"] == "auto")
        check("the manifest builds throughout", builds(proj.path, root, "out_ae"))

        for label, call in (
            ("a threshold outside 0..1",
             lambda: proj.set_atlas_extras("tiles", metatiles=1.5)),
            ("an unknown atlas", lambda: proj.set_atlas_extras("gone", metatiles="true")),
            ("a variant file that is not there",
             lambda: proj.set_atlas_extras("tiles", variants=["nope.png"])),
        ):
            try:
                call()
                check(f"atlas extras refuse {label}", False)
            except ValueError:
                check(f"atlas extras refuse {label}", True)

        # A variant has to be a RECOLOUR of the base, which is not answerable from the
        # manifest -- so the candidate is packed to find out.
        Image.new("RGBA", (8, 8), (200, 30, 30, 255)).save(os.path.join(root, "wrong.png"))
        try:
            proj.set_atlas_extras("tiles", variants=["wrong.png"])
            check("a variant that is not the base's layout is refused", False)
        except ValueError:
            check("a variant that is not the base's layout is refused", True)


def check_editor_fonts_and_project():
    """remove_font, which add_font never had a counterpart for, and the [project] keys."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        # CI fails rather than skips when no TTF is present -- a suite that quietly
        # stops testing a feature is worse than one that fails.
        if not TEST_TTF:
            check("a TTF is available to test font removal", False)
            return
        proj = editor_project(root)
        proj.add_font({"name": "hud", "source": TEST_TTF, "size": 12, "depth": 1,
                       "threshold": 128, "charset": "ABC", "license": "OFL 1.1"})
        check("a font is declared", [f["name"] for f in proj.fonts()] == ["hud"])

        proj.save_scene("s1", "a", [], ["hud"])
        check("what loads a font is reported", proj.font_users("hud") == ["scene s1"])
        try:
            proj.remove_font("hud")
            check("removing a font a scene loads is refused", False)
        except ValueError:
            check("removing a font a scene loads is refused", True)

        proj.save_scene("s1", "a", [], [])
        proj.remove_font("hud")
        check("an unused font is removed", proj.fonts() == [])
        check("and the TTF copied into the project is left alone",
              os.path.isdir(os.path.join(root, "art", "fonts")))

        proj.set_project("budget_bytes", 300000)
        proj.set_project("name", "demo project")
        check("the budget is rewritten", proj.project["budget_bytes"] == 300000)
        check("and the name", proj.project["name"] == "demo project")
        check("and the manifest builds", builds(proj.path, root, "out_pj"))

        for label, call in (
            ("a budget past the device ceiling",
             lambda: proj.set_project("budget_bytes", 99999999)),
            ("an empty name", lambda: proj.set_project("name", "")),
            ("an absolute resources path",
             lambda: proj.set_project("resources", "/tmp/x")),
            ("a key it does not own", lambda: proj.set_project("nope", "x")),
        ):
            try:
                call()
                check(f"project settings refuse {label}", False)
            except ValueError:
                check(f"project settings refuse {label}", True)


def check_editor_map_lifecycle():
    """Renaming and deleting a map, which had no editor at all -- so iterating on a test
    layout meant hand-editing the manifest."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        # The flush-left map fixture, because that is the shape the editor writes and the
        # shape save_map's own block scan expects. `manifest()`'s default maps are indented
        # -- textwrap.dedent finds no common prefix once BASE_MAP is interpolated -- which
        # is a fixture artefact rather than anything a real manifest does.
        proj = editor_project(root, maps=TWO_MAPS_WARPED, scene='''
[scene.one]
map = "b"
''')

        # 'a' warps to 'b', and a scene loads 'b'. Both have to move with the rename or
        # the build fails a long way from the cause.
        proj.rename_map("b", "cellar")
        names = [m["name"] for m in proj.man["map"]]
        check("a map is renamed", "cellar" in names and "b" not in names)
        check("the warp aimed at it follows",
              proj.man["map"][0]["warps"][0]["to"][0] == "cellar")
        check("the scene loading it follows",
              proj.man["scene"]["one"]["map"] == "cellar")
        check("and the manifest still builds", builds(proj.path, root, "out_rn"))

        check("what points at a map is reported",
              len(proj.map_users("cellar")) == 2)
        try:
            proj.remove_map("cellar")
            check("deleting a map something points at is refused", False)
        except ValueError:
            check("deleting a map something points at is refused", True)

        proj.remove_scene("one")
        proj.save_map("a", ["####", "#..#", "####"], [1, 1], [])
        proj.remove_map("cellar")
        check("a map with nothing pointing at it is deleted",
              [m["name"] for m in proj.man["map"]] == ["a"])
        check("and the manifest still builds", builds(proj.path, root, "out_del"))

        for label, call in (
            ("renaming to a name that is taken", lambda: proj.rename_map("a", "a")),
            ("renaming a map that is gone", lambda: proj.rename_map("gone", "x")),
            ("deleting a map that is gone", lambda: proj.remove_map("gone")),
            ("a name that is not an identifier", lambda: proj.rename_map("a", "New Map")),
        ):
            try:
                call()
                check(f"the editor refuses {label}", False)
            except ValueError:
                check(f"the editor refuses {label}", True)


def check_editor_autopick():
    """The importer's one pre-build say over roles."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, atlas=FLIP_ATLAS, **maps(EDITOR_MAP))

        proj.set_autopick("tiles", ["floor", "wall", "accent", "water"])
        check("autopick is rewritten in place",
              proj.man["atlas"][0]["autopick"] == ["floor", "wall", "accent", "water"])
        check("and the manifest still builds",
              builds(proj.path, root, "out_pick"))
        check("the extra role reaches the header",
              "TILES_TILE_WATER" in defines(os.path.join(root, "out_pick")))

        try:
            proj.set_autopick("tiles", ["floor", "floor"])
            check("a repeated role is refused", False)
        except ValueError:
            check("a repeated role is refused", True)


def check_editor_map_atlases():
    """A map's tileset list round-trips, and the two spellings never both survive."""
    with tempfile.TemporaryDirectory() as root:
        second_sheet(root)
        proj = editor_project(root, atlas=TWO_ATLASES, legend=TWO_LEGEND,
                              **maps(EDITOR_MAP))

        # Scoped to the map block: a legend entry pinned to an atlas carries an
        # `atlas = "..."` line of its own, and scanning the whole file would read that as
        # the map's.
        def map_block():
            text = open(proj.path).read()
            at = text.index("[[map]]")
            return text[at:]

        proj.save_map("a", ["####", "#.s#", "####"], [1, 1], [],
                      atlases=["tiles", "second"])
        check("saving several atlases writes the list form",
              'atlases = ["tiles", "second"]' in map_block())
        check("and drops the single-atlas spelling",
              not re.search(r'^atlas\s*=', map_block(), re.M))
        check("the map reads back with both",
              proj.maps()[0]["atlases"] == ["tiles", "second"])
        check("a multi-atlas map builds", builds(proj.path, root, "out_multi"))

        proj.save_map("a", ["####", "#..#", "####"], [1, 1], [], atlases=["tiles"])
        check("going back to one atlas restores the short spelling",
              'atlas = "tiles"' in map_block() and "atlases" not in map_block())
        check("and still builds", builds(proj.path, root, "out_single"))


# ------------------------------------------------------- editor: the update check
#
# "Check for updates" froze the whole editor. The check ran inside the request handler
# holding the one lock every route takes, so a call to GitHub blocked the heartbeat, the
# map and the build button -- and because `urlopen(timeout=)` bounds the socket but NOT
# the name lookup, a machine with dead DNS froze with no upper limit. Past 25 seconds of
# blocked heartbeat the liveness watchdog decided the UI was gone and shut the editor
# down: the check could close the window it was checking from.
#
# Tested through a real socket, because the defect was in how requests are serialised and
# nothing below that layer can show it.

def check_editor_update():
    import threading
    import urllib.request
    import urllib.error
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "tools"))
    import pnx_editor                                       # noqa: E402

    updater = pnx_editor.UPDATER
    original = pnx_editor.Updater._fetch
    started = threading.Event()

    def hang(self):
        """A GitHub that never answers -- what dead DNS actually looks like."""
        started.set()
        time.sleep(30)
        return {"current": self.current, "checked": True, "available": False}

    pnx_editor.Updater._fetch = hang
    updater._cache = None
    updater._checking_since = None
    updater.CHECK_DEADLINE = 1.0

    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        path = manifest(root, atlas=FLIP_ATLAS, **maps(EDITOR_MAP))
        # Session.open() looks for assets.toml or a .pknproj; the fixture writes m.toml,
        # so the Project is attached directly. What is under test is request handling,
        # not project discovery.
        session = pnx_editor.Session()
        session.proj = pnx_editor.Project(path)

        srv = pnx_editor.EditorServer(("127.0.0.1", 0), pnx_editor.make_handler(session))
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def call(route, data=None):
            t = time.time()
            urllib.request.urlopen(f"http://127.0.0.1:{port}{route}",
                                   data=data, timeout=20).read()
            return time.time() - t

        try:
            threading.Thread(
                target=lambda: call("/api/update/check", b"{}"), daemon=True).start()
            started.wait(5)

            # The assertion the bug was: an unrelated request, while the check is out.
            check("a hung update check does not block the heartbeat",
                  call("/api/alive") < 0.5)
            check("a hung update check does not block the project",
                  call("/api/state") < 2.0)
            # A second check must not start a second call to GitHub: unbounded, that
            # leaked a thread per click against a 60-per-hour rate limit.
            check("a second check while one is running returns at once",
                  call("/api/update/check", b"{}") < 0.5)

            # And a check with nothing already in flight gives up rather than waiting
            # forever. The in-flight guard has to be cleared first -- otherwise this
            # returns down the "already running" path and would pass with no deadline
            # at all, which is exactly how it passed while the deadline was removed.
            with updater._lock:
                updater._checking_since = None
                updater._cache = None
            started.clear()
            t = time.time()
            out = updater.check(force=True)
            waited = time.time() - t
            check("the check returns within its deadline", 0.5 < waited < 3.0)
            check("and says it is still trying rather than claiming failure",
                  out.get("pending") is True and out.get("available") is False)
        finally:
            srv.shutdown()
            srv.server_close()
            pnx_editor.Updater._fetch = original
            updater._cache = None
            updater._checking_since = None
            updater.CHECK_DEADLINE = pnx_editor.Updater.CHECK_DEADLINE

    # The routes that must never take the shared lock, named rather than inferred, so
    # adding one to the handler without thinking about it shows up here.
    check("the update routes are exempt from the request lock",
          {"/api/alive", "/api/update", "/api/update/check", "/api/update/progress"}
          <= pnx_editor.LOCK_FREE_PATHS)


def check_editor_atlas_offset():
    """`offset` threads through the Import tab's own endpoints the same way it does
    through pack_atlas directly -- add/update round-trip it, and slice_grid resolves a
    packed index against the OFFSET carve, not an unshifted one.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root)

        # A second sheet: the same grid `make_sheet` already produced, prefixed by a
        # border a naive offset=[0,0] carve would read into every tile.
        clean = Image.open(os.path.join(root, "sheet.png")).convert("RGBA")
        w, h = clean.size
        ox, oy = 3, 2
        bordered = Image.new("RGBA", (w + ox, h + oy), (5, 5, 5, 255))
        bordered.paste(clean, (ox, oy))
        bordered.save(os.path.join(root, "bordered.png"))

        proj.add_atlas("b", "bordered.png", 16, [0, 0, 2, 2], 16, offset=[ox, oy])
        check("offset round-trips through atlas_spec",
              proj.atlas_spec("b")["offset"] == [ox, oy])

        s = proj.slice_grid("bordered.png", 16, [0, 0, 2, 2], offset=[ox, oy])
        uniq = [c for c in s["cells"] if c["state"] == "unique"]
        check("slice_grid resolved cells against the offset carve",
              len(uniq) > 0 and all(c["packed"] is not None for c in uniq))

        proj.update_atlas("b", "bordered.png", 16, [0, 0, 2, 2], 16, offset=[0, 0])
        check("offset clears back to [0, 0] rather than being written as a no-op line",
              proj.atlas_spec("b")["offset"] == [0, 0])
        check("the manifest still builds throughout", builds(proj.path, root, "out_off"))


def check_editor_atlas_tiles_and_collision():
    """carve_tiles (the packed-tile-aware view) and save/remove_atlas_collision -- the
    backend the Import tab's per-tile editor (role + collision) is built on.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root)

        info = proj.carve_tiles("sheet.png", 16, [0, 0, 2, 2], 16, name="tiles")
        check("carve_tiles resolves the default atlas's autopicked roles",
              {"floor", "wall", "accent"} <= {t["role"] for t in info["tiles"]})

        def by_role(res, role):
            return next(t for t in res["tiles"] if t["role"] == role)

        check("the default manifest's wall is already SOLID (from the fixture)",
              by_role(info, "wall")["collision"]["mode"] == pnx_assets.COLLISION_SOLID)
        check("floor starts with no collision entry at all",
              by_role(info, "floor")["collision"]["mode"] == pnx_assets.COLLISION_NONE)

        proj.save_atlas_collision("tiles", "floor", pnx_assets.COLLISION_SCALED,
                                  rect=[0, 8, 16, 8])
        info2 = proj.carve_tiles("sheet.png", 16, [0, 0, 2, 2], 16, name="tiles")
        floor2 = by_role(info2, "floor")
        check("a saved SCALED rect round-trips through carve_tiles",
              floor2["collision"]["mode"] == pnx_assets.COLLISION_SCALED
              and floor2["collision"]["rect"] == [0, 8, 16, 8])
        check("wall's own entry is untouched by editing floor",
              by_role(info2, "wall")["collision"]["mode"] == pnx_assets.COLLISION_SOLID)
        check("the manifest still builds after a SCALED save",
              builds(proj.path, root, "out_tiles1"))

        # Re-editing the SAME tile must REPLACE its entry, not add a second one for it --
        # parse_atlas_collision refuses two [[atlas.collision]] entries for one tile, so a
        # duplicate would fail the very next build silently until Build was pressed.
        mask_rows = ["#" * 16] * 16
        proj.save_atlas_collision("tiles", "floor", pnx_assets.COLLISION_COMPLEX,
                                  mask_rows=mask_rows)
        info3 = proj.carve_tiles("sheet.png", 16, [0, 0, 2, 2], 16, name="tiles")
        floor3 = by_role(info3, "floor")
        check("re-saving replaces the previous entry rather than duplicating it",
              floor3["collision"]["mode"] == pnx_assets.COLLISION_COMPLEX)
        check("an authored mask is flagged as authored, not auto-derived",
              floor3["collision"]["authored"] is True)
        check("the manifest still builds after replacing SCALED with COMPLEX",
              builds(proj.path, root, "out_tiles2"))

        proj.remove_atlas_collision("tiles", "floor")
        info4 = proj.carve_tiles("sheet.png", 16, [0, 0, 2, 2], 16, name="tiles")
        check("removing the entry reverts the tile to NONE",
              by_role(info4, "floor")["collision"]["mode"] == pnx_assets.COLLISION_NONE)
        check("wall is STILL untouched after floor's entry is removed",
              by_role(info4, "wall")["collision"]["mode"] == pnx_assets.COLLISION_SOLID)
        check("the manifest still builds after remove",
              builds(proj.path, root, "out_tiles3"))

        try:
            proj.remove_atlas_collision("tiles", "floor")
            check("removing a tile with no collision entry is refused", False)
        except ValueError:
            check("removing a tile with no collision entry is refused", True)

        try:
            proj.save_atlas_collision("tiles", "floor", pnx_assets.COLLISION_SCALED,
                                      rect=[0, 0, 999, 999])
            check("a rect that does not fit the tile is refused", False)
        except ValueError:
            check("a rect that does not fit the tile is refused", True)


def main():
    # The font cases assert on blob contents directly rather than only on pass/fail, so
    # they count their own checks rather than going through expect_ok.
    global checks, failures
    print("asset pipeline validation")

    expect_ok("valid manifest builds")

    # --- orientation. Runs early because everything after it assumes the portrait build
    # it has been comparing against is the one the rest of these tests describe.
    check_orientation()
    check_colorkey()

    # --- map geometry
    expect_fail("ragged rows", "ragged", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [1, 1]
        rows = """
        ####
        #..#
        #.#
        ####
        """
    '''))

    expect_fail("unknown legend char", "unknown legend char", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [1, 1]
        rows = """
        ####
        #Z.#
        ####
        """
    '''))

    expect_fail("start inside a wall", "solid", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [0, 0]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    expect_fail("start outside the map", "outside", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [99, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    # --- warps. This is the family that motivated the whole validation pass: a door
    # sealed inside a building produced a warp that could never fire, and nothing about
    # the binary looked wrong.
    expect_fail("warp sealed off from start", "unreachable", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [1, 1]
        warps = [{ at = [4, 3], to = ["b", 1, 1] }]
        rows = """
        ######
        #..###
        #..###
        ####D#
        ######
        """

        [[map]]
        name = "b"
        out = "b.bin"
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    # The escape hatch is what makes strictness correct. A room behind a button-operated door is
    # content a static flood fill cannot help calling broken, so `gated = true` states the intent
    # once, travels with the warp declaration, and is not raised again. Without it the check would
    # eventually be silenced wholesale -- which costs more than the check ever saved.
    expect_ok("gated warp is accepted without warning", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [1, 1]
        warps = [{ at = [4, 3], to = ["b", 1, 1], gated = true }]
        rows = """
        ######
        #..###
        #..###
        ####D#
        ######
        """

        [[map]]
        name = "b"
        out = "b.bin"
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    expect_fail("warp not on a warp-flagged tile", "warp' flag", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [1, 1]
        warps = [{ at = [2, 1], to = ["b", 1, 1] }]
        rows = """
        ####
        #..#
        ####
        """

        [[map]]
        name = "b"
        out = "b.bin"
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    expect_fail("warp to an unknown map", "unknown map", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [2, 1]
        warps = [{ at = [1, 2], to = ["nowhere", 1, 1] }]
        rows = """
        ####
        #..#
        #D.#
        ####
        """
    '''))

    expect_fail("warp destination inside a wall", "solid tile", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [2, 1]
        warps = [{ at = [1, 2], to = ["b", 0, 0] }]
        rows = """
        ####
        #..#
        #D.#
        ####
        """

        [[map]]
        name = "b"
        out = "b.bin"
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    expect_fail("warp destination outside the map", "outside", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [2, 1]
        warps = [{ at = [1, 2], to = ["b", 50, 50] }]
        rows = """
        ####
        #..#
        #D.#
        ####
        """

        [[map]]
        name = "b"
        out = "b.bin"
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    # A destination that is walkable but in a sealed pocket strands the player just as
    # surely as a wall does, and is far easier to author by accident.
    expect_fail("warp destination in a sealed pocket", "sealed off", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [2, 1]
        warps = [{ at = [1, 2], to = ["b", 4, 1] }]
        rows = """
        ####
        #..#
        #D.#
        ####
        """

        [[map]]
        name = "b"
        out = "b.bin"
        start = [1, 1]
        rows = """
        ######
        #..#.#
        ######
        """
    '''))

    expect_fail("duplicate map names", "duplicate", **maps('''
        [[map]]
        name = "a"
        out = "a.bin"
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """

        [[map]]
        name = "a"
        out = "a2.bin"
        start = [1, 1]
        rows = """
        ####
        #..#
        ####
        """
    '''))

    # --- legend and atlas
    # The message names the atlas, not just "its atlas": a map can draw from several now,
    # and "which atlas?" is the first thing an author asks.
    expect_fail("legend names a role its atlas lacks", "'tiles' does not define", legend='''
        [legend."."]
        tile = "nonexistent"
        flags = []
        [legend."#"]
        tile = "wall"
        flags = []
        [legend."D"]
        tile = "accent"
        flags = ["warp"]
    ''')

    expect_fail("unknown legend flag", "unknown flag", legend='''
        [legend."."]
        tile = "floor"
        flags = ["squishy"]
        [legend."#"]
        tile = "wall"
        flags = []
        [legend."D"]
        tile = "accent"
        flags = ["warp"]
    ''')

    expect_fail("atlas region past the sheet edge", "past the sheet", atlas='''
        [[atlas]]
        name = "tiles"
        sheet = "sheet.png"
        tile = 16
        region = [0, 0, 99, 99]
        max_tiles = 16
        out = "tiles.bin"
        autopick = ["floor", "wall", "accent"]
    ''')

    expect_fail("semantic index out of range", "out of range", atlas='''
        [[atlas]]
        name = "tiles"
        sheet = "sheet.png"
        tile = 16
        region = [0, 0, 2, 2]
        max_tiles = 16
        out = "tiles.bin"
        [atlas.semantic]
        floor = 0
        wall = 1
        accent = 99
    ''')

    expect_fail("missing sheet", "missing sheet", atlas='''
        [[atlas]]
        name = "tiles"
        sheet = "nope.png"
        tile = 16
        region = [0, 0, 2, 2]
        out = "tiles.bin"
        autopick = ["floor", "wall", "accent"]
    ''')

    # Explicit semantic indices must work without autopick -- that is the path a real
    # project takes once it has an artist.
    expect_ok("explicit semantic tiles, no autopick", atlas='''
        [[atlas]]
        name = "tiles"
        sheet = "sheet.png"
        tile = 16
        region = [0, 0, 2, 2]
        max_tiles = 16
        out = "tiles.bin"
        [atlas.semantic]
        floor = 0
        wall = 1
        accent = 2
    ''')

    # The per-atlas metatile threshold. A fraction is the form an artist reaches for, so a
    # typo like a percentage (12 instead of 0.12) has to be a build error naming the range
    # rather than silently meaning "never metatile".
    expect_fail("metatile threshold out of range", "out of range", atlas='''
        [[atlas]]
        name = "tiles"
        sheet = "sheet.png"
        tile = 16
        region = [0, 0, 2, 2]
        max_tiles = 16
        out = "tiles.bin"
        metatiles = 12
        autopick = ["floor", "wall", "accent"]
    ''')

    expect_ok("metatile threshold as a fraction", atlas='''
        [[atlas]]
        name = "tiles"
        sheet = "sheet.png"
        tile = 16
        region = [0, 0, 2, 2]
        max_tiles = 16
        out = "tiles.bin"
        metatiles = 0.3
        autopick = ["floor", "wall", "accent"]
    ''')

    # Palette-swapped sprite variants. The feature only holds together if a variant that is
    # not actually a recolour is refused -- otherwise one bitmap gets shared between two
    # different shapes and the second renders wrong, silently.
    import os as _os
    def _good(root):
        make_recolour(_os.path.join(root, "sheet.png"), _os.path.join(root, "var.png"))
    def _bad(root):
        make_recolour(_os.path.join(root, "sheet.png"), _os.path.join(root, "var.png"),
                      move_a_pixel=True)

    expect_ok("sprite variant, genuine recolour", _extra=_good, sprite='''
        [[sprite]]
        name = "guy"
        sheet = "sheet.png"
        frames = [[0, 0, 16, 16]]
        out = "guy.bin"
        variants = ["var.png"]
    ''')
    expect_fail("sprite variant with a moved pixel", "not a recolour",
                _extra=_bad, sprite='''
        [[sprite]]
        name = "guy"
        sheet = "sheet.png"
        frames = [[0, 0, 16, 16]]
        out = "guy.bin"
        variants = ["var.png"]
    ''')

    check_worldtiles()
    check_tile_indices_and_flips()
    check_user_flags()
    check_atlas_offset()
    check_complex_mask_authoring()

    # --- fonts
    #
    # A font is the one asset that can be *valid* and still useless: it packs cleanly,
    # ships, and is illegible on the watch. The pipeline cannot judge legibility -- that
    # is what the editor's preview is for -- so what it checks here is everything else:
    # that the licence is recorded, that the glyph set covers the text, and that the
    # numbers the blob claims match the bytes it carries.

    def _font(root):
        _shutil.copy(TEST_TTF, _os.path.join(root, "f.ttf"))

    FONT_OK = '''
        [[font]]
        name = "hud"
        source = "f.ttf"
        size = 12
        license = "SIL OFL 1.1"
        out = "hud.bin"
    '''
    DIALOG = '''
        [dialog.greet]
        pages = ["Hello there."]
    '''

    if not _os.path.exists(TEST_TTF):
        print(f"  SKIP fonts: no TTF at {TEST_TTF}")
    else:
        expect_ok("font builds", _extra=_font, dialog=DIALOG, font=FONT_OK)

        expect_fail("font without a licence", "license", _extra=_font, dialog=DIALOG,
                    font=FONT_OK.replace('license = "SIL OFL 1.1"', ""))

        expect_fail("font with a missing source", "missing source", _extra=_font,
                    dialog=DIALOG, font=FONT_OK.replace("f.ttf", "nope.ttf"))

        expect_fail("font at an absurd size", "outside 4-64", _extra=_font,
                    dialog=DIALOG, font=FONT_OK.replace("size = 12", "size = 300"))

        expect_fail("font at an invalid depth", "depth must be", _extra=_font,
                    dialog=DIALOG, font=FONT_OK.replace("size = 12", "size = 12\ndepth = 4"))

        # --- glyphs rotate with everything else, and the blob says which way the pen
        #     then walks. Without the axis the glyphs would be correct and the line
        #     would still come out as a stack of characters in one place.
        axis_of = {"portrait": pnx_assets.ADVANCE_X_POS,
                   "buttons_top": pnx_assets.ADVANCE_Y_POS,
                   "buttons_bottom": pnx_assets.ADVANCE_Y_NEG}
        flat_glyphs = None
        for name, axis in axis_of.items():
            with tempfile.TemporaryDirectory() as root:
                make_sheet(os.path.join(root, "sheet.png"))
                _font(root)
                out = build_at(root, name, dialog=DIALOG, font=FONT_OK)
                b = read_blob(out, "hud.bin")

            check(f"font advance axis is {name}'s", b[6] == axis)

            # Glyph entries: u16 offset, w, h, advance, bearing_x, bearing_y, pad.
            count = b[8] | (b[9] << 8)
            entries = [b[16 + i * pnx_assets.FONT_GLYPH_ENTRY:][:6] for i in range(count)]
            if name == "portrait":
                flat_glyphs = entries
                continue

            check(f"{name}: same glyph count", len(entries) == len(flat_glyphs))
            check(f"{name}: glyph bitmaps are on their side",
                  all((r[2], r[3]) == (f[3], f[2]) for r, f in zip(entries, flat_glyphs)))
            # Metrics are typographic -- along the baseline and up from it -- so they do
            # NOT turn with the pixels. Rotating these as well would move every glyph off
            # its own baseline.
            check(f"{name}: metrics stay typographic",
                  all(r[4:6] == f[4:6] for r, f in zip(entries, flat_glyphs)))

        expect_fail("duplicate font names", "duplicate font", _extra=_font,
                    dialog=DIALOG, font=FONT_OK + FONT_OK.replace('out = "hud.bin"',
                                                                  'out = "hud2.bin"'))

        # The message must name the offending character AND where it came from. A build
        # that says only "bad character" leaves the author grepping their dialogue.
        expect_fail("non-ASCII in dialog", "U+2019", _extra=_font, font=FONT_OK, dialog='''
            [dialog.greet]
            pages = ["It’s here."]
        ''')
        expect_fail("non-ASCII names its page", "page 1", _extra=_font, font=FONT_OK,
                    dialog='''
            [dialog.greet]
            pages = ["fine", "an em—dash"]
        ''')

        expect_fail("charset of the wrong type", "charset must be", _extra=_font,
                    dialog=DIALOG, font=FONT_OK.replace("size = 12", "size = 12\ncharset = 7"))

        expect_fail("tracking below every advance", "negative", _extra=_font,
                    dialog=DIALOG,
                    font=FONT_OK.replace("size = 12", "size = 12\ntracking = -99"))

        # --- what the blob actually contains
        checks += 1
        with tempfile.TemporaryDirectory() as root:
            make_sheet(_os.path.join(root, "sheet.png"))
            _font(root)
            err = run(root, dialog=DIALOG, font=FONT_OK + '''
                [[font]]
                name = "aa"
                source = "f.ttf"
                size = 16
                depth = 2
                license = "SIL OFL 1.1"
                out = "aa.bin"
            ''')
            if err:
                print(f"  FAIL font blob: build failed: {err}")
                failures += 1
            else:
                check_font_blob(_os.path.join(root, "out", "hud.bin"), depth=1)
                check_font_blob(_os.path.join(root, "out", "aa.bin"), depth=2)

        # `charset = "auto"` must carry exactly the characters the dialog uses, plus
        # `extra`, plus space -- no more. Shipping all 95 printable glyphs when a game
        # says twelve distinct characters is the waste this exists to avoid.
        checks += 1
        with tempfile.TemporaryDirectory() as root:
            make_sheet(_os.path.join(root, "sheet.png"))
            _font(root)
            err = run(root, dialog='''
                [dialog.greet]
                pages = ["abc"]
            ''', font=FONT_OK.replace("size = 12", 'size = 12\nextra = "XY"'))
            if err:
                print(f"  FAIL charset derivation: build failed: {err}")
                failures += 1
            else:
                blob = open(_os.path.join(root, "out", "hud.bin"), "rb").read()
                count = int.from_bytes(blob[8:10], "little")
                first, last = blob[12], blob[13]
                # ' ', 'X', 'Y', 'a', 'b', 'c'
                if (count, chr(first), chr(last)) != (6, " ", "c"):
                    print(f"  FAIL charset derivation: got {count} glyphs, "
                          f"{chr(first)!r}..{chr(last)!r}, expected 6, ' '..'c'")
                    failures += 1

        # Rasterisation must be deterministic: the same manifest twice must produce
        # byte-identical blobs, or every build churns the bundle and nobody can tell a
        # real content change from noise.
        checks += 1
        blobs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as root:
                make_sheet(_os.path.join(root, "sheet.png"))
                _font(root)
                run(root, dialog=DIALOG, font=FONT_OK)
                blobs.append(open(_os.path.join(root, "out", "hud.bin"), "rb").read())
        if blobs[0] != blobs[1]:
            print("  FAIL font rasterisation is not deterministic")
            failures += 1

        # An advance override must reach the blob, since that is how the editor's
        # per-glyph nudge persists.
        checks += 1
        with tempfile.TemporaryDirectory() as root:
            make_sheet(_os.path.join(root, "sheet.png"))
            _font(root)
            base = _font_advance(root, DIALOG, FONT_OK, "e")
            bumped = _font_advance(root, DIALOG,
                                   FONT_OK + '\n[font.advance]\ne = 21\n', "e")
            if bumped != 21 or base == 21:
                print(f"  FAIL advance override: base {base}, overridden {bumped}, "
                      f"expected 21 and not 21")
                failures += 1

    # --- the editor's inline page
    #
    # PAGE is an r-string, so a doubled backslash reaches the browser literally: '\\n'
    # becomes a backslash and an n rather than a newline, and '\\'' terminates a
    # JavaScript string early. That shipped once and took a blank tab and a console trace
    # to find, because Python is perfectly happy with it. Cheap to make impossible.
    checks += 1
    import pnx_editor                                       # noqa: E402
    page = pnx_editor.PAGE

    # Only the escapes that are UNAMBIGUOUSLY wrong here. Two others look tempting and
    # are not: a bare `\\` is legitimate in a regex character class (/[\\/]/ matches a
    # backslash), and `'\\'` is the correct way to write a one-backslash string. Both
    # appear in the tokeniser and both are right, so checking them would only teach
    # people to ignore this test.
    stray = []
    for i, line in enumerate(page.split("\n"), 1):
        for seq in (r"\\n", r"\\t", r"\\r"):
            if seq in line:
                stray.append((i, seq, line.strip()[:64]))
    if stray:
        print(f"  FAIL editor page: {len(stray)} escape(s) doubled in an r-string; "
              f"these reach the browser literally")
        for i, seq, l in stray[:3]:
            print(f"         line {i}: {seq!r} in  {l}")
        failures += 1

    # Two top-level functions with the same name: the later one silently wins, and the
    # caller of the earlier one starts doing something else entirely. That happened --
    # the code editor's `analyse()` shadowed the importer's and blanked its stats panel
    # with no error anywhere.
    checks += 1
    names = re.findall(r"^function\s+([A-Za-z_$][\w$]*)\s*\(", page, re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        print(f"  FAIL editor page: duplicate function name(s): {', '.join(dupes)}")
        failures += 1

    # Same for top-level const/let bindings, which would be a hard SyntaxError.
    checks += 1
    binds = re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", page, re.M)
    dupes = sorted({n for n in binds if binds.count(n) > 1})
    if dupes:
        print(f"  FAIL editor page: duplicate top-level binding(s): {', '.join(dupes)}")
        failures += 1

    # The page is one big inline document; a mismatched tag is invisible until something
    # silently fails to render. Comments are stripped first, because the commentary
    # explaining the markup naturally names the tags it is about.
    checks += 1
    markup = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    for tag in ("div", "section", "aside", "button", "select", "textarea", "pre",
                "header", "footer", "nav", "main", "label"):
        opens = len(re.findall(rf"<{tag}[\s>]", markup))
        closes = len(re.findall(rf"</{tag}>", markup))
        if opens != closes:
            print(f"  FAIL editor page: <{tag}> opened {opens} times, closed {closes}")
            failures += 1
            break

    check_editor_atlas_removal()
    check_editor_legend()
    check_map_legend()
    check_editor_scenes()
    check_editor_new_map_scene()
    check_editor_map_lifecycle()
    check_editor_sprites()
    check_mapfile_format()
    check_source_maps()
    check_editor_map_migration()
    check_editor_sheet_frames()
    check_editor_dialog()
    check_editor_map_props()
    check_editor_atlas_extras()
    check_editor_atlas_offset()
    check_editor_atlas_tiles_and_collision()
    check_editor_fonts_and_project()
    # check_editor_flags() -- retired, see _check_editor_flags_RETIRED's own comment
    check_editor_roles()
    check_editor_autopick()
    check_editor_map_atlases()
    check_editor_update()

    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
