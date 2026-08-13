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
import os
import re
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "tools"))

import pnx_assets                                          # noqa: E402
from PIL import Image                                      # noqa: E402

failures = 0
checks = 0


def make_sheet(path, tiles_across=2, tile=16):
    """A synthetic sheet with visually distinct tiles, so autopick has something to do."""
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
        ''',
        "legend": '''
            [legend."."]
            tile = "floor"
            flags = []
            [legend."#"]
            tile = "wall"
            flags = ["solid"]
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
    for key in ("sprite", "dialog", "font", "scene"):
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
            if len(parts) == 3 and parts[0] == "#define" and parts[2].lstrip("-").isdigit():
                found[parts[1]] = int(parts[2])
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
    """One manifest, three orientations, compared against each other."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))

        builds = {name: build_at(root, name, sprite=SPRITE_TALL)
                  for name in ("portrait", "buttons_top", "buttons_bottom")}
        want = {"portrait": pnx_assets.ORIENT_BUTTONS_RIGHT,
                "buttons_top": pnx_assets.ORIENT_BUTTONS_TOP,
                "buttons_bottom": pnx_assets.ORIENT_BUTTONS_BOTTOM}

        # --- every blob is stamped, including the ones with no geometry. A song stamped
        #     0 in a landscape build would make "orientation-free" and "stale portrait"
        #     the same byte, and the runtime could no longer tell them apart.
        for name, out in builds.items():
            for f in sorted(os.listdir(out)):
                if not f.endswith(".bin"):
                    continue
                check(f"{f} stamped {name}", read_blob(out, f)[7] == want[name])

        flat = builds["portrait"]
        for name in ("buttons_top", "buttons_bottom"):
            out, orient = builds[name], want[name]

            # --- dimensions swap, in the blob header and in the generated header
            fm, rm = read_blob(flat, "a.bin"), read_blob(out, "a.bin")
            check(f"{name}: map w/h swap", (rm[3], rm[4]) == (fm[4], fm[3]))

            fd, rd = defines(flat), defines(out)
            check(f"{name}: header map dims swap",
                  (rd["MAP_A_W"], rd["MAP_A_H"]) == (fd["MAP_A_H"], fd["MAP_A_W"]))
            check(f"{name}: header sprite dims swap",
                  (rd["GUY_W"], rd["GUY_H"]) == (fd["GUY_H"], fd["GUY_W"]))
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
            check(f"{name}: tile plane is the portrait plane rotated",
                  turned == rp["cells"])
            check(f"{name}: flag overrides rotate with the plane",
                  sorted(pnx_assets.rotate_point(x, y, fp["w"], fp["h"], orient) + (f,)
                         for x, y, f in fp["overrides"]) == sorted(rp["overrides"]))

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
    flags = ["solid"]
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
        # A 200x228 screen at 16px tiles touches at most 2 WorldTiles of 16 cells per
        # axis, plus one of margin on each side: 4x4.
        check("the resident pool covers the screen with a margin on each side",
              m["wt_slots"] == 16)
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
        streamed = build_maps(root, **{"maps": spec.format(extra="", rows=rows)})["a.bin"]
        whole = build_maps(root, **{"maps": spec.format(extra="resident = true",
                                                        rows=rows)})["a.bin"]
        total = streamed["cols"] * streamed["rows"]
        check("without `resident` a large map streams", streamed["wt_slots"] < total)
        check("`resident = true` takes a slot per WorldTile", whole["wt_slots"] == total)
        check("and the two describe the same world",
              whole["cells"] == streamed["cells"])
        check("holding it whole costs several times the pool",
              whole["wt_slots"] * whole["wt_slot_bytes"]
              > 3 * streamed["wt_slots"] * streamed["wt_slot_bytes"])

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
        flags = ["solid"]
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
        flags = ["solid"]
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

    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
