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
    for key in ("sprite", "nine_slice", "dialog", "font", "scene", "tile_flags", "hud_var",
                "hud_window", "music"):
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


def sprite_frame(blob, frame):
    """One frame's (w, h, origin_x, origin_y, flags) read straight out of a built "PS"
    blob -- mirrors PnxSprite.frame_meta's own 8-byte record (pnx_assets.h) byte for
    byte. There is no header #define for a sprite's own dimensions any more (frames are
    not uniform size), so a test that wants a frame's built size reads the blob directly,
    the same way check_orientation already reads a map's cell plane instead of trusting a
    number someone typed in.
    """
    e = blob[pnx_assets.HEADER_BYTES + frame * 8: pnx_assets.HEADER_BYTES + frame * 8 + 8]
    return e[2], e[3], e[4], e[5], e[6]


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


HEADER_HDR_FLAGS_OFFSET = pnx_assets.HEADER_BYTES + 2  # the flags byte finish_map writes
                                                        # (v14: shared-fixed offset 2)


def check_lzss():
    """lzss_compress/lzss_decompress (M12): round-tripped directly against a range of
    inputs, then through a real map build with `compress_maps = true` -- the pipeline path
    that actually matters, compared cell-for-cell against the same content built
    uncompressed, since a compressed and an uncompressed build of the same manifest must
    parse back to the identical map.
    """
    for label, data in [
        ("empty", b""),
        ("short, below the minimum match length", b"ab"),
        ("highly repetitive", b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        ("no repetition at all", bytes(range(64))),
        ("real cell-plane-shaped data", bytes([0x07, 0x00] * 40 + [0x09, 0x00] * 12 +
                                              [0x2F, 0x20] * 3)),
    ]:
        packed = pnx_assets.lzss_compress(data)
        back = pnx_assets.lzss_decompress(packed, len(data))
        check(f"lzss round-trip: {label}", back == data)
        if data:
            check(f"lzss round-trip: {label} (compressed at least records something)",
                  len(packed) > 0)

    # --- the real pipeline path: compress_maps = true, compared against the same content
    # built uncompressed. A multi-bank map (worldtile small enough to force several banks
    # for a map with real size) exercises more than one compressed bank, not just one.
    rows = "\n".join("#" * 6 for _ in range(2)) + "\n" + "\n".join(".#..#." for _ in range(4))

    def write_manifest(root, compress):
        manifest_body = f'''
            [project]
            name = "t"
            resources = "out"
            header = "out/gen.h"
            {"compress_maps = true" if compress else ""}

            [[atlas]]
            name = "tiles"
            sheet = "sheet.png"
            tile = 16
            region = [0, 0, 2, 2]
            max_tiles = 16
            out = "tiles.bin"
            autopick = ["floor", "wall", "accent"]

            [legend."."]
            tile = "floor"
            flags = []
            [legend."#"]
            tile = "wall"
            flags = []

            [[map]]
            name = "a"
            out = "a.bin"
            start = [1, 1]
            warps = []
            worldtile = 4
            rows = """{rows}"""
        '''
        path = os.path.join(root, "m.toml")
        with open(path, "w") as f:
            f.write(textwrap.dedent(manifest_body))
        return path

    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))

        plain_path = write_manifest(root, compress=False)
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(plain_path, os.path.join(root, "plain"),
                             os.path.join(root, "plain", "gen.h"))

        compressed_path = write_manifest(root, compress=True)
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(compressed_path, os.path.join(root, "compressed"),
                             os.path.join(root, "compressed", "gen.h"))

        plain_bin = read_blob(os.path.join(root, "plain"), "a.bin")
        compressed_bin = read_blob(os.path.join(root, "compressed"), "a.bin")
        plain_bank = read_blob(os.path.join(root, "plain"), "a_b0.bin")
        compressed_bank = read_blob(os.path.join(root, "compressed"), "a_b0.bin")

        check("compress_maps: compressed bank is smaller",
              len(compressed_bank) < len(plain_bank))
        check("compress_maps: sets the compressed flag bit",
              (compressed_bin[HEADER_HDR_FLAGS_OFFSET] & 2) != 0)
        check("compress_maps: uncompressed build leaves the bit clear",
              (plain_bin[HEADER_HDR_FLAGS_OFFSET] & 2) == 0)

        plain = pnx_assets.parse_map(plain_bin, [plain_bank])
        compressed = pnx_assets.parse_map(compressed_bin, [compressed_bank])
        check("compress_maps: identical cell content to the uncompressed build",
              plain["cells"] == compressed["cells"])
        check("compress_maps: identical dimensions",
              (plain["w"], plain["h"]) == (compressed["w"], compressed["h"]))


def check_cell_dictionary():
    """build_cell_dictionary (M12): a WorldTile's cells store an index into this table
    rather than the raw entry word -- pinned directly against hand-built cell planes,
    including the idx_width boundary no real map in this repo happens to cross (never more
    than 9 distinct entries in anything measured -- see PNX_BLOB_VERSION's v13 comment).
    """
    # --- ordinary case: 4 distinct entries repeated across 6 cells, first-seen order.
    entries = [0x0007, 0x0009, 0x0007, 0x2f20, 0x0009, 0x0007]
    tiles = b"".join(e.to_bytes(2, "little") for e in entries)
    order, index_of, idx_width = pnx_assets.build_cell_dictionary(tiles)
    check("dictionary: first-seen order", order == [0x0007, 0x0009, 0x2f20])
    check("dictionary: idx_width 1 for a small table", idx_width == 1)
    check("dictionary: every entry resolves to its own index",
          [index_of[e] for e in entries] == [0, 1, 0, 2, 1, 0])

    # --- the 256/257 boundary: idx_width must switch to 2 only once it has to.
    exactly_256 = b"".join(i.to_bytes(2, "little") for i in range(256))
    _, _, w256 = pnx_assets.build_cell_dictionary(exactly_256)
    check("dictionary: idx_width stays 1 at exactly 256 entries", w256 == 1)

    exactly_257 = b"".join(i.to_bytes(2, "little") for i in range(257))
    _, _, w257 = pnx_assets.build_cell_dictionary(exactly_257)
    check("dictionary: idx_width becomes 2 at 257 entries", w257 == 2)

    # --- slice_worldtiles actually writes indices, not raw entries, at the chosen width.
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj_map = {"name": "d", "w": 3, "h": 2, "tiles": tiles,
                    "extended": bytes(6), "atlas_table": [("tiles", 0, 4)],
                    "tile_px": 16}
        cols, rows, wtiles = pnx_assets.slice_worldtiles(proj_map, 8, index_of, idx_width)
        check("slice_worldtiles: one WorldTile covers a map this small",
              (cols, rows) == (1, 1))
        cell_bytes = wtiles[0]["cells"]
        check("slice_worldtiles: cells stored at idx_width, not raw 2 bytes/cell",
              len(cell_bytes) == 6 * idx_width)
        got = [cell_bytes[i] for i in range(6)]  # idx_width == 1 here
        check("slice_worldtiles: stored indices match build_cell_dictionary's",
              got == [index_of[e] for e in entries])


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


def check_nine_slice():
    """9-slice panels: border insets round-trip through the blob, survive build-time
    rotation the same way a map's start position does, and a malformed border is a build
    error naming the actual problem rather than a wrong picture on a watch.
    """
    # --- rotate_border, pinned directly. A panel authored border 2/3/4/5 (l/t/r/b) in a
    # 10x10 panel: rotate_point's own comment derives BUTTONS_TOP as left->top->right->
    # bottom->left, so bottom becomes left, left becomes top, top becomes right, right
    # becomes bottom -- (5, 2, 3, 4). Pinned here so a future edit to rotate_point cannot
    # silently change what "a border survives a rotation" means.
    check("rotate_border buttons_top",
          pnx_assets.rotate_border(2, 3, 4, 5, 10, 10, pnx_assets.ORIENT_BUTTONS_TOP)
          == (5, 2, 3, 4))
    check("rotate_border buttons_bottom",
          pnx_assets.rotate_border(2, 3, 4, 5, 10, 10, pnx_assets.ORIENT_BUTTONS_BOTTOM)
          == (3, 4, 5, 2))
    check("rotate_border buttons_left (180)",
          pnx_assets.rotate_border(2, 3, 4, 5, 10, 10, pnx_assets.ORIENT_BUTTONS_LEFT)
          == (4, 5, 2, 3))
    check("rotate_border identity",
          pnx_assets.rotate_border(2, 3, 4, 5, 10, 10, pnx_assets.ORIENT_BUTTONS_RIGHT)
          == (2, 3, 4, 5))

    # --- pack_nine_slice/finish_nine_slice directly, called the way pack_atlas is in
    # check_colorkey's own second half: a real 6x6 panel, border 2/2/2/2 on every side.
    with tempfile.TemporaryDirectory() as root:
        sheet = os.path.join(root, "panel.png")
        make_colour_sheet(sheet, [(255, 0, 0)], tile=6)  # one flat 6x6 tile
        with contextlib.redirect_stdout(io.StringIO()):
            ns = pnx_assets.pack_nine_slice(
                root, {"name": "panel", "sheet": "panel.png",
                       "border": [2, 2, 2, 2], "out": "panel.bin"},
                pnx_assets.ORIENT_BUTTONS_RIGHT)
        check("pack_nine_slice: dimensions carried through unrotated",
              (ns["w"], ns["h"]) == (6, 6))
        check("pack_nine_slice: border carried through unrotated",
              ns["border"] == (2, 2, 2, 2))
        check("pack_nine_slice: one frame -- settle_palettes/sprite_colour_sets reuse "
              "needs exactly this shape",
              len(ns["frames"]) == 1 and len(ns["frames"][0]) == 36)

        # settle_palettes has to run before ANY frozen=True merge -- finish_sprite's own
        # docstring explains why (packing fixes indices against the settled sort order).
        shared = []
        pnx_assets.settle_palettes([], [], shared, [ns])
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.finish_nine_slice(ns, shared, pnx_assets.ORIENT_BUTTONS_RIGHT, False)
        blob = ns["blob"]
        check("blob: magic N9", blob[0:2] == b"N9")
        check("blob: header carries w/h", (blob[3], blob[4]) == (6, 6))
        check("blob: border bytes immediately follow the header",
              tuple(blob[8:12]) == (2, 2, 2, 2))
        check("blob: pixel payload is w*h/2 bytes after the border",
              len(blob) == pnx_assets.HEADER_BYTES + 4 + 6 * 6 // 2)

    # --- border validation, through the real manifest path
    expect_fail("nine_slice: border wrong length", "left, top, right, bottom",
                nine_slice='''
        [[nine_slice]]
        name = "panel"
        sheet = "sheet.png"
        border = [2, 2]
        out = "panel.bin"
    ''')
    expect_fail("nine_slice: border too big for the panel", "does not fit",
                nine_slice='''
        [[nine_slice]]
        name = "panel"
        sheet = "sheet.png"
        rect = [0, 0, 8, 8]
        border = [5, 5, 5, 5]
        out = "panel.bin"
    ''')
    expect_fail("nine_slice: negative border", "must not be negative",
                nine_slice='''
        [[nine_slice]]
        name = "panel"
        sheet = "sheet.png"
        border = [-1, 2, 2, 2]
        out = "panel.bin"
    ''')

    # --- end to end: a full manifest build emits the header constants and the asset id.
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        with contextlib.redirect_stdout(io.StringIO()):
            path = manifest(root, nine_slice='''
                [[nine_slice]]
                name = "panel"
                sheet = "sheet.png"
                rect = [0, 0, 8, 8]
                border = [2, 2, 2, 2]
                out = "panel.bin"
            ''')
            pnx_assets.build(path, os.path.join(root, "out"),
                             os.path.join(root, "out", "gen.h"))
        d = defines(os.path.join(root, "out"))
        # An asset id is an ENUM member, not a #define -- defines() only sees the latter,
        # so this one reads the header text directly rather than through that helper.
        with open(os.path.join(root, "out", "gen.h")) as f:
            header_text = f.read()
        check("header: PNX_ASSET_NINE_SLICE_PANEL exists",
              "PNX_ASSET_NINE_SLICE_PANEL," in header_text)
        check("header: PANEL_W/H", (d.get("PANEL_W"), d.get("PANEL_H")) == (8, 8))
        check("header: PANEL_BORDER_L/T/R/B",
              (d.get("PANEL_BORDER_L"), d.get("PANEL_BORDER_T"),
               d.get("PANEL_BORDER_R"), d.get("PANEL_BORDER_B")) == (2, 2, 2, 2))
        check("panel.bin was written", os.path.exists(os.path.join(root, "out", "panel.bin")))


def check_hud_vars():
    """[[hud_var]] declares a named id into the runtime table pnx_hud_vars.h reads/writes
    (speed, a timer, a dialog speaker's name) -- not a resource, so build() only has to
    validate the declarations and number them, never write a blob for this section.
    """
    expect_fail("hud_var: bad name (uppercase)", "name must be lowercase",
                hud_var='''
        [[hud_var]]
        name = "Speed"
        type = "int"
    ''')
    expect_fail("hud_var: bad name (leading digit)", "name must be lowercase",
                hud_var='''
        [[hud_var]]
        name = "1speed"
        type = "int"
    ''')
    expect_fail("hud_var: invalid type", 'type must be "int" or "text"',
                hud_var='''
        [[hud_var]]
        name = "speed"
        type = "float"
    ''')
    expect_fail("hud_var: duplicate name", "duplicate hud_var names",
                hud_var='''
        [[hud_var]]
        name = "speed"
        type = "int"
        [[hud_var]]
        name = "speed"
        type = "text"
    ''')

    # --- end to end: declarations come back as PNX_HUD_VAR_* constants, numbered by
    # sorted name (not declaration order) so reordering the manifest never renumbers a
    # variable a game already refers to.
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        with contextlib.redirect_stdout(io.StringIO()):
            path = manifest(root, hud_var='''
                [[hud_var]]
                name = "timer"
                type = "int"
                [[hud_var]]
                name = "radio_station"
                type = "text"
                [[hud_var]]
                name = "speed"
                type = "int"
            ''')
            pnx_assets.build(path, os.path.join(root, "out"),
                             os.path.join(root, "out", "gen.h"))
        d = defines(os.path.join(root, "out"))
        # Sorted: radio_station, speed, timer.
        check("header: PNX_HUD_VAR_RADIO_STATION == 0", d.get("PNX_HUD_VAR_RADIO_STATION") == 0)
        check("header: PNX_HUD_VAR_SPEED == 1", d.get("PNX_HUD_VAR_SPEED") == 1)
        check("header: PNX_HUD_VAR_TIMER == 2", d.get("PNX_HUD_VAR_TIMER") == 2)
        check("header: PNX_HUD_VAR_COUNT == 3", d.get("PNX_HUD_VAR_COUNT") == 3)

    # --- a manifest with no [[hud_var]] at all emits no constants (and, critically,
    # does not crash generate_header on an empty/absent list).
    expect_ok("hud_var: absent is fine")


# A panel element's own [[nine_slice]], reused verbatim from check_nine_slice's own
# end-to-end fixture -- proving hud_window's panel reference resolves against a REAL
# packed nine_slice is more useful than a second, disconnected implementation of the
# same box.
HUD_PANEL_NS = '''
    [[nine_slice]]
    name = "panel"
    sheet = "sheet.png"
    rect = [0, 0, 8, 8]
    border = [2, 2, 2, 2]
    out = "panel.bin"
'''
HUD_SPEED_VAR = '''
    [[hud_var]]
    name = "speed"
    type = "int"
'''


def check_hud_windows():
    """[[hud_window]] and its nested [[hud_window.element]] -- unlike hud_var, a window
    IS a resource (a real "HW" blob of placement/binding data, per
    src/pnx/gfx/pnx_hud_window.h's own top comment), so build() both validates it and
    writes it.
    """
    expect_fail("hud_window: bad name", "name must be lowercase",
                nine_slice=HUD_PANEL_NS, hud_window='''
        [[hud_window]]
        name = "Speed HUD"
        [[hud_window.element]]
        kind = "panel"
        panel = "panel"
        w = 10
        h = 10
    ''')
    expect_fail("hud_window: duplicate name", "duplicate hud_window names",
                nine_slice=HUD_PANEL_NS, hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "panel"
        panel = "panel"
        w = 10
        h = 10
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "panel"
        panel = "panel"
        w = 10
        h = 10
    ''')
    expect_fail("hud_window: no elements", "has no elements",
                hud_window='''
        [[hud_window]]
        name = "hud"
    ''')
    expect_fail("hud_window: unknown ease", "ease",
                nine_slice=HUD_PANEL_NS, hud_window='''
        [[hud_window]]
        name = "hud"
        ease = "bounce"
        [[hud_window.element]]
        kind = "panel"
        panel = "panel"
        w = 10
        h = 10
    ''')
    expect_fail("hud_window: unknown element kind", "kind",
                hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "circle"
    ''')
    expect_fail("hud_window: unknown anchor", "anchor",
                nine_slice=HUD_PANEL_NS, hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "panel"
        panel = "panel"
        anchor = "dead_center"
        w = 10
        h = 10
    ''')
    expect_fail("hud_window: panel names an unknown nine_slice", "no nine_slice named",
                hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "panel"
        panel = "nope"
        w = 10
        h = 10
    ''')
    expect_fail("hud_window: sprite names an unknown sprite", "no sprite named",
                hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "sprite"
        sprite = "nope"
    ''')
    expect_fail("hud_window: bar names an unknown hud_var", "type \"int\"",
                hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "bar"
        hud_var = "nope"
        w = 10
        h = 6
        max = 100
    ''')
    expect_fail("hud_window: bar names a text-typed hud_var", "type \"int\"",
                hud_var='''
        [[hud_var]]
        name = "label"
        type = "text"
    ''', hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "bar"
        hud_var = "label"
        w = 10
        h = 6
        max = 100
    ''')
    expect_fail("hud_window: bar missing max", "max must be a positive int",
                hud_var=HUD_SPEED_VAR, hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "bar"
        hud_var = "speed"
        w = 10
        h = 6
    ''')
    expect_fail("hud_window: text names an int-typed hud_var", "type \"text\"",
                hud_var=HUD_SPEED_VAR, hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "text"
        hud_var = "speed"
        font = "hud"
    ''')
    expect_fail("hud_window: text names an unknown font", "no font named",
                hud_var='''
        [[hud_var]]
        name = "label"
        type = "text"
    ''', hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "text"
        hud_var = "label"
        font = "nope"
    ''')
    expect_fail("hud_window: bad colour byte", "GColor8 byte",
                hud_var=HUD_SPEED_VAR, hud_window='''
        [[hud_window]]
        name = "hud"
        [[hud_window.element]]
        kind = "bar"
        hud_var = "speed"
        w = 10
        h = 6
        max = 100
        border = 999
    ''')

    # --- end to end: a panel + a bar, sorted asset ordering, header constants, a
    # written blob.
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        with contextlib.redirect_stdout(io.StringIO()):
            path = manifest(root, nine_slice=HUD_PANEL_NS, hud_var=HUD_SPEED_VAR,
                            hud_window='''
                [[hud_window]]
                name = "speed_hud"
                show_ms = 250
                hide_ms = 200
                ease = "out_cubic"
                slide = [0, 40]

                [[hud_window.element]]
                kind = "panel"
                panel = "panel"
                anchor = "bottom_left"
                offset = [4, -4]
                w = 60
                h = 20

                [[hud_window.element]]
                kind = "bar"
                hud_var = "speed"
                anchor = "top_right"
                offset = [-4, 4]
                w = 50
                h = 8
                max = 200
                border = 192
                track = 0
                fill = 255
            ''')
            pnx_assets.build(path, os.path.join(root, "out"),
                             os.path.join(root, "out", "gen.h"))
        d = defines(os.path.join(root, "out"))
        with open(os.path.join(root, "out", "gen.h")) as f:
            header_text = f.read()
        check("header: PNX_ASSET_HUD_WINDOW_SPEED_HUD exists",
              "PNX_ASSET_HUD_WINDOW_SPEED_HUD," in header_text)
        check("header: PNX_HUD_WINDOW_SPEED_HUD_ELEMENTS == 2",
              d.get("PNX_HUD_WINDOW_SPEED_HUD_ELEMENTS") == 2)
        check("speed_hud.bin was written",
              os.path.exists(os.path.join(root, "out", "speed_hud.bin")))
        with open(os.path.join(root, "out", "speed_hud.bin"), "rb") as f:
            blob = f.read()
        check("blob: magic HW", blob[0:2] == b"HW")
        check("blob: element count byte", blob[3] == 2)


def check_atlas_rotation_dedup():
    """pack_atlas drops a tile that is a 90/270-degree rotation or diagonal flip of one
    already kept, not just the 4 plain mirrors -- and does so using the EXACT bit
    semantics pnx_gfx.c's pnx_blit_4bpp applies for PNX_FLIP_ROTATE, not a naive
    transpose-then-flip.

    Getting the bit correspondence backwards would dedup a tile against an orientation
    the watch never actually draws on that MAP_ROTATE bit -- wrong art on the device, not
    a crash, so this reconstructs the engine's own pixel math (transcribed from
    pnx_gfx.c's rotate branch) rather than reusing pack_atlas's own private transpose/
    flip_x/flip_y helpers, and checks the two agree independently.
    """
    T = 16

    def engine_blit(src, bits):
        """The buffer pnx_blit_4bpp sources from for this bits value (PNX_FLIP_X=1,
        PNX_FLIP_Y=2, PNX_FLIP_ROTATE=4), transcribed index-for-index from its rotate
        branch in src/pnx/gfx/pnx_gfx.c."""
        rotate, fx, fy = bool(bits & 4), bool(bits & 1), bool(bits & 2)
        out = bytearray(T * T)
        for j in range(T):        # destination row
            for i in range(T):    # destination col
                if rotate:
                    sx_col = (T - 1 - j) if fx else j
                    sy_row = (T - 1 - i) if fy else i
                else:
                    sx_col = (T - 1 - i) if fx else i
                    sy_row = (T - 1 - j) if fy else j
                out[j * T + i] = src[sy_row * T + sx_col]
        return bytes(out)

    with tempfile.TemporaryDirectory() as root:
        # An asymmetric base tile -- no two of its 8 symmetries are pixel-identical -- so
        # a dedup that confused one orientation for another shows up as extra kept tiles,
        # not silently passes. Values are indices into a 15-entry, pairwise GColor8-distinct
        # palette (never all-zero, so no tile reads as empty).
        levels = [(r, g, b) for r in range(4) for g in range(4) for b in range(4)
                  if (r, g, b) != (0, 0, 0)][:15]
        pal = {v: tuple(L * 85 for L in levels[v - 1]) for v in range(1, 16)}
        base = bytes(((i * 7 + j * 13 + 1) % 15 + 1) for j in range(T) for i in range(T))

        variants = [base] + [engine_blit(base, bits) for bits in range(1, 8)]
        check("test fixture is fully asymmetric under all 8 symmetries",
              len(set(variants)) == 8)

        sheet = os.path.join(root, "sheet.png")
        img = Image.new("RGBA", (T * 8, T), (0, 0, 0, 255))
        px = img.load()
        for n, v in enumerate(variants):
            for j in range(T):
                for i in range(T):
                    px[n * T + i, j] = pal[v[j * T + i]] + (255,)
        img.save(sheet)

        with contextlib.redirect_stdout(io.StringIO()):
            atlas = pnx_assets.pack_atlas(
                root, {"name": "r", "sheet": "sheet.png", "tile": T,
                       "region": [0, 0, 8, 1], "max_tiles": 16, "out": "r.bin"},
                pnx_assets.ORIENT_BUTTONS_RIGHT)

        check("all 8 symmetries of one tile collapse to 1 unique tile",
              len(atlas["tiles"]) == 1)
        check("pack_atlas counted 7 mirror/rotation matches",
              atlas["mirrored"] == 7)

    # And the false-positive side: two tiles that are NOT related by any of the 8
    # symmetries must both survive. A dedup broad enough to catch real rotations but also
    # loose enough to catch unrelated art would just be a different bug wearing the fix's
    # clothes.
    with tempfile.TemporaryDirectory() as root:
        sheet = os.path.join(root, "sheet.png")
        make_sheet(sheet, tiles_across=2)   # 4 tiles, none a symmetry of another
        with contextlib.redirect_stdout(io.StringIO()):
            atlas = pnx_assets.pack_atlas(
                root, {"name": "r", "sheet": "sheet.png", "tile": 16,
                       "region": [0, 0, 2, 2], "max_tiles": 16, "out": "r.bin"},
                pnx_assets.ORIENT_BUTTONS_RIGHT)
        check("unrelated tiles are not merged by the rotation dedup",
              len(atlas["tiles"]) == 4 and atlas["mirrored"] == 0)


def check_editor_analyse_dedup():
    """The Atlas tab's live price (Project.analyse) must agree with what pack_atlas will
    actually build -- a mirror the pipeline collapses away for free must not be priced as
    a second tile just because analyse() used to run its own, cheaper, exact-match-only
    dedup that could not see mirrors or rotations at all.
    """
    with tempfile.TemporaryDirectory() as root:
        sheet = os.path.join(root, "sheet.png")
        T = 16
        img = Image.new("RGBA", (T * 2, T * 2), (0, 0, 0, 255))
        px = img.load()
        for j in range(T):
            for i in range(T):
                v = 40 + (i * 3 + j * 5) % 180
                px[i, j] = (v, v, v, 255)                     # tile (0,0)
                px[T + (T - 1 - i), j] = (v, v, v, 255)       # tile (1,0): its own mirror
        make_sheet(sheet)   # fills (0,1)/(1,1) with the default fixture's distinct tiles
        img2 = Image.open(sheet).convert("RGBA")
        img2.paste(img.crop((0, 0, T * 2, T)), (0, 0))
        img2.save(sheet)

        proj = editor_project(root)
        r = proj.analyse("sheet.png", 16, [0, 0, 2, 1], 16, None)
        check("a mirrored tile prices as the one tile pack_atlas will actually keep",
              r["unique"] == 1)


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
            #     that instead turns everything upside down. Read through parse_map, not
            #     off a raw byte offset: w/h live in the per-layer directory now (v14,
            #     M13), not the header's own bytes 3/4 (those are layer_count/
            #     primary_layer since M13, and do not rotate).
            fm, rm = read_blob(flat, "a.bin"), read_blob(out, "a.bin")
            fmp, rmp = pnx_assets.parse_map(fm), pnx_assets.parse_map(rm)
            check(f"{name}: map w/h {'swap' if swaps else 'hold'}",
                  (rmp["w"], rmp["h"]) ==
                  ((fmp["h"], fmp["w"]) if swaps else (fmp["w"], fmp["h"])))

            fd, rd = defines(flat), defines(out)
            check(f"{name}: header map dims {'swap' if swaps else 'hold'}",
                  (rd["MAP_A_W"], rd["MAP_A_H"])
                  == ((fd["MAP_A_H"], fd["MAP_A_W"]) if swaps
                      else (fd["MAP_A_W"], fd["MAP_A_H"])))
            fgw, fgh, _, _, _ = sprite_frame(read_blob(flat, "guy.bin"), 0)
            rgw, rgh, _, _, _ = sprite_frame(read_blob(out, "guy.bin"), 0)
            check(f"{name}: sprite frame dims {'swap' if swaps else 'hold'}",
                  (rgw, rgh) == ((fgh, fgw) if swaps else (fgw, fgh)))
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
        mode, kind, extra = collision[0]
        check("an authored mask parses as COMPLEX", mode == pnx_assets.COLLISION_COMPLEX)
        check("with the default kind (wall)", kind == pnx_assets.COLLISION_KIND_WALL)

        expected = pnx_assets.pack_collision_mask(
            bytearray(0xC0 if c == "#" else 0x00 for c in mask_text.replace("\n", "")), 4, 4)
        check("the authored mask packs to the X shape, not the tile's own full opacity",
              extra == expected)
        full_opacity = pnx_assets.pack_collision_mask(bytearray([0xC0] * 16), 4, 4)
        check("...and that differs from what auto-derivation would have produced",
              extra != full_opacity)

        # unpack_collision_mask is the editor's own round trip (show a tile's current
        # mask as text before repainting it) -- pack then unpack must reproduce exactly
        # what was authored, or the editor would silently mutate a mask just by opening it.
        check("unpack_collision_mask round-trips an authored mask",
              pnx_assets.unpack_collision_mask(extra, 4, 4) == mask_text.split("\n"))

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


def check_variable_frame_sprites():
    """Variable-size frames, per-frame origin, a named clip with per-frame durations, and
    per-frame collision (one auto-derived COMPLEX mask, one hand-authored SCALED rect) --
    the whole surface pack_sprite/finish_sprite gained together, built through the real
    pipeline and read back off the actual blob and generated header, not just unit-level
    pieces in isolation.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))  # 32x32, opaque throughout

        sprite_toml = '''
            [[sprite]]
            name = "hero"
            sheet = "sheet.png"
            frames = [[0, 0, 16, 16], [16, 0, 8, 16, 2, 8]]
            out = "hero.bin"

            [sprite.anim]
            stand = 0
            walk = { frames = [0, 1, 0, 1], fps = 8, loop = true, durations = [1, 2, 1, 2] }
            idle_loop = [0, 1]

            [[sprite.collision]]
            frame = 0
            type = "complex"
            kind = "hurt"

            [[sprite.collision]]
            frame = 1
            type = "scaled"
            kind = "wall"
            rect = [0, 0, 4, 8]
        '''
        err = run(root, sprite=sprite_toml)
        check("a sprite with differently sized frames builds", err is None)
        if err:
            print(f"    (build error: {err})")
            return

        out = os.path.join(root, "out")
        blob = read_blob(out, "hero.bin")
        f0 = sprite_frame(blob, 0)
        f1 = sprite_frame(blob, 1)
        check("frame 0 keeps its own 16x16", (f0[0], f0[1]) == (16, 16))
        check("frame 0's origin defaults to centred, feet at the bottom",
              (f0[2], f0[3]) == (8, 16))
        check("frame 1 keeps its own, DIFFERENT, 8x16", (f1[0], f1[1]) == (8, 16))
        check("frame 1's origin is the authored one, not the default",
              (f1[2], f1[3]) == (2, 8))

        # flags = (kind << 2) | mode -- PNX_COLLISION_KIND/MODE, pnx_assets.h.
        check("frame 0's flags pack COMPLEX + hurt",
              f0[4] == (pnx_assets.COLLISION_KIND_HURT << 2) | pnx_assets.COLLISION_COMPLEX)
        check("frame 1's flags pack SCALED + wall (the default kind)",
              f1[4] == (pnx_assets.COLLISION_KIND_WALL << 2) | pnx_assets.COLLISION_SCALED)

        rd = defines(out)
        check("a single-int anim is still a bare #define", rd["HERO_STAND"] == 0)
        check("a clip emits its frame array length as _COUNT", rd["HERO_WALK_COUNT"] == 4)
        check("...and its authored fps", rd["HERO_WALK_FPS"] == 8)
        check("...and its authored loop", rd["HERO_WALK_LOOP"] == 1)
        header = open(os.path.join(out, "gen.h")).read()
        check("...and a real FRAMES array, not just the count",
              "HERO_WALK_FRAMES[] = { 0, 1, 0, 1 }" in header)
        check("...and a real DURATIONS array when durations were authored",
              "HERO_WALK_DURATIONS[] = { 1, 2, 1, 2 }" in header)
        check("a list-form clip defaults to ANIM_DEFAULT_FPS",
              rd["HERO_IDLE_LOOP_FPS"] == pnx_assets.ANIM_DEFAULT_FPS)
        check("...and its default loop is true", rd["HERO_IDLE_LOOP_LOOP"] == 1)
        check("...and, with no authored durations, DURATIONS is the literal NULL",
              "#define HERO_IDLE_LOOP_DURATIONS NULL" in header)
        check("a single-frame pose gets no _DURATIONS symbol at all",
              "HERO_STAND_DURATIONS" not in header)

        # --- validation: each new failure mode raises through the real pipeline
        expect_fail("a clip whose fps is out of range", "fps",
                    sprite='''
                        [[sprite]]
                        name = "bad"
                        sheet = "sheet.png"
                        frames = [[0, 0, 16, 16]]
                        out = "bad.bin"
                        [sprite.anim]
                        walk = { frames = [0], fps = 0 }
                    ''')
        expect_fail("a clip's durations not matching its frame count", "durations",
                    sprite='''
                        [[sprite]]
                        name = "bad"
                        sheet = "sheet.png"
                        frames = [[0, 0, 16, 16]]
                        out = "bad.bin"
                        [sprite.anim]
                        walk = { frames = [0, 0], durations = [1] }
                    ''')
        expect_fail("a collision entry naming an out-of-range frame", "collision",
                    sprite='''
                        [[sprite]]
                        name = "bad"
                        sheet = "sheet.png"
                        frames = [[0, 0, 16, 16]]
                        out = "bad.bin"
                        [[sprite.collision]]
                        frame = 4
                        type = "solid"
                    ''')
        expect_fail("a collision kind that is not one of the four", "kind",
                    sprite='''
                        [[sprite]]
                        name = "bad"
                        sheet = "sheet.png"
                        frames = [[0, 0, 16, 16]]
                        out = "bad.bin"
                        [[sprite.collision]]
                        frame = 0
                        type = "solid"
                        kind = "nonsense"
                    ''')


def check_sprite_dedup_and_compress():
    """Two things build_sprite_frame_meta/finish_sprite gained together: identical PACKED
    frames collapsing to one shared `frame_meta` offset (always on, the same way
    pack_font already dedups glyph bitmaps), and `compress = "lzss"` LZSS-compressing the
    pixel region (project-wide, pairs with PNX_COMPRESS_MODE=PNX_COMPRESS_LZSS -- the
    compress_maps test above is the template this follows).
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))  # 32x32: a flat 16x16 quadrant at
                                                       # (0,0), a busy checkerboard one at
                                                       # (16,16) -- see make_sheet's own
                                                       # comment.

        # Frames 0 and 2 both name the SAME flat region -- identical source pixels
        # through the same palette, so their packed bytes must be byte-identical too.
        # Frame 1 names the busy region, which must NOT collapse with either.
        dedup_toml = '''
            [[sprite]]
            name = "hero"
            sheet = "sheet.png"
            frames = [[0, 0, 16, 16], [16, 16, 16, 16], [0, 0, 16, 16]]
            out = "hero.bin"
        '''
        err = run(root, sprite=dedup_toml)
        check("sprite dedup: a sprite with a repeated frame builds", err is None)
        if err is None:
            blob = read_blob(os.path.join(root, "out"), "hero.bin")

            def frame_offset(frame):
                e = blob[pnx_assets.HEADER_BYTES + frame * 8:
                        pnx_assets.HEADER_BYTES + frame * 8 + 8]
                return e[0] | (e[1] << 8)

            check("sprite dedup: frame 0 and identical frame 2 share one offset",
                  frame_offset(0) == frame_offset(2))
            check("sprite dedup: the genuinely different frame 1 does not share it",
                  frame_offset(1) != frame_offset(0))

        # --- compress_sprites: a single flat (maximally repetitive) frame, built once
        # plain and once compressed, off the same source pixels.
        def write_manifest(root, compress):
            manifest_body = f'''
                [project]
                name = "t"
                resources = "out"
                header = "out/gen.h"
                {'compress = "lzss"' if compress else 'compress = "none"'}

                [[sprite]]
                name = "flat"
                sheet = "sheet.png"
                frames = [[0, 0, 16, 16]]
                out = "flat.bin"
            '''
            path = os.path.join(root, "m.toml")
            with open(path, "w") as f:
                f.write(textwrap.dedent(manifest_body))
            return path

        plain_path = write_manifest(root, compress=False)
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(plain_path, os.path.join(root, "plain"),
                             os.path.join(root, "plain", "gen.h"))
        compressed_path = write_manifest(root, compress=True)
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(compressed_path, os.path.join(root, "compressed"),
                             os.path.join(root, "compressed", "gen.h"))

        plain_bin = read_blob(os.path.join(root, "plain"), "flat.bin")
        compressed_bin = read_blob(os.path.join(root, "compressed"), "flat.bin")

        check("compress_sprites: sets the compressed flag bit",
              (compressed_bin[4] & 1) != 0)
        check("compress_sprites: uncompressed build leaves the bit clear",
              (plain_bin[4] & 1) == 0)
        check("compress_sprites: a flat, highly repetitive frame compresses smaller",
              len(compressed_bin) < len(plain_bin))

        # frame_meta(8) + pad4(assign, 1 frame) + 2-byte COMPRESSED-length prefix, then
        # that many bytes of LZSS stream -- pnx_sprite_load's own comment documents this
        # layout, and why the prefix is the compressed count rather than the
        # uncompressed one (the uncompressed size is already derivable from frame_meta).
        meta_span = 8
        pal_span = len(pnx_assets.pad4(bytes(1)))  # one frame's `assign` byte, padded to 4
        pixel_len = 16 * 16 // 2  # one 16x16 frame at 4bpp
        prefix_at = pnx_assets.HEADER_BYTES + meta_span + pal_span
        compressed_len = compressed_bin[prefix_at] | (compressed_bin[prefix_at + 1] << 8)
        stream_at = prefix_at + 2
        check("compress_sprites: the length prefix names a stream shorter than plain pixels",
              0 < compressed_len < pixel_len)

        pixel_start = pnx_assets.HEADER_BYTES + meta_span + pal_span
        plain_pixels = plain_bin[pixel_start:pixel_start + pixel_len]
        stream = compressed_bin[stream_at:stream_at + compressed_len]
        decoded = pnx_assets.lzss_decompress(stream, pixel_len)
        check("compress_sprites: decoding the compressed stream reproduces the plain pixels",
              decoded == plain_pixels)

        # The shape tables (4 bytes of zero counts -- no collision declared) must sit
        # immediately after the stream, not after some other guessed length.
        check("compress_sprites: the blob ends exactly at the shape tables' 4 bytes",
              len(compressed_bin) == stream_at + compressed_len + 4)


def check_atlas_compress():
    """`compress = "lzss"` LZSS-compresses an atlas's tile pixel data -- project-wide,
    pairs with PNX_COMPRESS_MODE=PNX_COMPRESS_LZSS, same `compress_pixel_body` helper the
    sprite path already uses (see check_sprite_dedup_and_compress). Built through the real
    pipeline,
    plain (non-metatiled) layout: make_sheet's own flat 16x16 quadrant compresses well,
    which is what this manifest picks (region [0,0,1,1], one tile) to keep the maths
    simple to check by hand.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))  # 32x32; (0,0) quadrant is flat shade=40

        def write_manifest(root, compress):
            manifest_body = f'''
                [project]
                name = "t"
                resources = "out"
                header = "out/gen.h"
                {'compress = "lzss"' if compress else 'compress = "none"'}

                [[atlas]]
                name = "flat"
                sheet = "sheet.png"
                tile = 16
                region = [0, 0, 1, 1]
                max_tiles = 1
                out = "flat.bin"
                metatiles = false
            '''
            path = os.path.join(root, "m.toml")
            with open(path, "w") as f:
                f.write(textwrap.dedent(manifest_body))
            return path

        plain_path = write_manifest(root, compress=False)
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(plain_path, os.path.join(root, "plain"),
                             os.path.join(root, "plain", "gen.h"))
        compressed_path = write_manifest(root, compress=True)
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(compressed_path, os.path.join(root, "compressed"),
                             os.path.join(root, "compressed", "gen.h"))

        plain_bin = read_blob(os.path.join(root, "plain"), "flat.bin")
        compressed_bin = read_blob(os.path.join(root, "compressed"), "flat.bin")

        check("compress_atlases: sets the compressed flag bit (byte 6, the header's `d`)",
              (compressed_bin[6] & 1) != 0)
        check("compress_atlases: uncompressed build leaves the bit clear",
              (plain_bin[6] & 1) == 0)
        check("compress_atlases: a flat, highly repetitive tile compresses smaller",
              len(compressed_bin) < len(plain_bin))

        # header(8) + pad4(assign, 1 tile) + pad4(flags, 1 tile) + 2-byte compressed
        # length + 2-byte TRUE uncompressed pixel length, then the LZSS stream --
        # atlas_load_into's own comment documents this layout (the plain/layout-0 case:
        # no subtile_count/pad/table the metatiled one has). Atlases carry that extra
        # length field and sprites don't (compress_pixel_body's tag_len) because an
        # atlas is the one place two on-disk pixel depths of the same content coexist
        # (pack_2bit's colour vs ~bw blob) -- see compress_pixel_body's own comment.
        tables = len(pnx_assets.pad4(bytes(1))) * 2
        pixel_len = 16 * 16 // 2  # one 16x16 tile at 4bpp
        prefix_at = pnx_assets.HEADER_BYTES + tables
        compressed_len = compressed_bin[prefix_at] | (compressed_bin[prefix_at + 1] << 8)
        true_pixel_len = compressed_bin[prefix_at + 2] | (compressed_bin[prefix_at + 3] << 8)
        stream_at = prefix_at + 4
        check("compress_atlases: the length prefix names a stream shorter than plain pixels",
              0 < compressed_len < pixel_len)
        check("compress_atlases: the true-length tag matches the real uncompressed size",
              true_pixel_len == pixel_len)

        pixel_start = pnx_assets.HEADER_BYTES + tables
        plain_pixels = plain_bin[pixel_start:pixel_start + pixel_len]
        stream = compressed_bin[stream_at:stream_at + compressed_len]
        decoded = pnx_assets.lzss_decompress(stream, pixel_len)
        check("compress_atlases: decoding the compressed stream reproduces the plain pixels",
              decoded == plain_pixels)


def check_atlas_anim():
    """`[atlas.anim]` generates the same NAME_CLIP_FRAMES/_COUNT/_FPS/_LOOP/_DURATIONS
    handle shape `[sprite.anim]` already does (check_variable_frame_sprites' own
    territory), except a clip's "frames" are ATLAS TILE INDICES -- validated against the
    atlas's own POST-DEDUP tile count, the same index space [[atlas.collision]]/roles/
    autopick already use, not the raw sheet grid.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))  # region [0,0,2,2]: 4 distinct tiles

        def write_manifest(root, anim_toml):
            manifest_body = f'''
                [project]
                name = "t"
                resources = "out"
                header = "out/gen.h"

                [[atlas]]
                name = "ground"
                sheet = "sheet.png"
                tile = 16
                region = [0, 0, 2, 2]
                max_tiles = 4
                out = "ground.bin"

                {anim_toml}
            '''
            path = os.path.join(root, "m.toml")
            with open(path, "w") as f:
                f.write(textwrap.dedent(manifest_body))
            return path

        ok_path = write_manifest(root, '''
            [atlas.anim]
            water = { frames = [0, 1, 2], fps = 4, loop = true, durations = [1, 2, 1] }
            solo = 2
        ''')
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(ok_path, os.path.join(root, "ok"), os.path.join(root, "ok", "gen.h"))

        header_text = open(os.path.join(root, "ok", "gen.h")).read()
        d = defines(os.path.join(root, "ok"))

        check("atlas.anim: single-pose entry emits a plain #define",
              d.get("GROUND_SOLO") == 2)
        check("atlas.anim: clip emits its frame-index array",
              "static const uint8_t GROUND_WATER_FRAMES[] = { 0, 1, 2 };" in header_text)
        check("atlas.anim: clip emits _COUNT", d.get("GROUND_WATER_COUNT") == 3)
        check("atlas.anim: clip emits authored _FPS, not a default", d.get("GROUND_WATER_FPS") == 4)
        check("atlas.anim: clip emits _LOOP as 1/0, not true/false", d.get("GROUND_WATER_LOOP") == 1)
        check("atlas.anim: clip emits its authored _DURATIONS array",
              "static const uint8_t GROUND_WATER_DURATIONS[] = { 1, 2, 1 };" in header_text)

        # A clip with no authored durations gets the NULL sentinel, not an empty array --
        # see pnx_anim_frame's own comment on why a caller always passes this symbol
        # without needing to know which case it is.
        no_dur_path = write_manifest(root, '''
            [atlas.anim]
            plain = [0, 1]
        ''')
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(no_dur_path, os.path.join(root, "no_dur"),
                             os.path.join(root, "no_dur", "gen.h"))
        no_dur_text = open(os.path.join(root, "no_dur", "gen.h")).read()
        check("atlas.anim: an undurationed clip's _DURATIONS is the NULL sentinel",
              "#define GROUND_PLAIN_DURATIONS NULL" in no_dur_text)

        err = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pnx_assets.build(write_manifest(root, '''
                    [atlas.anim]
                    bad = { frames = [0, 99] }
                '''), os.path.join(root, "bad"), os.path.join(root, "bad", "gen.h"))
        except pnx_assets.BuildError as e:
            err = str(e)
        check("atlas.anim: a tile index past the post-dedup tile count is rejected",
              err is not None and "but there are only" in err)


def check_editor_sprite_frame_collision():
    """sprite_frames' per-frame collision info, and save/remove_sprite_collision -- the
    backend the Sprites tab's per-frame collision editor is built on. Mirrors
    check_editor_atlas_tiles_and_collision's own shape closely: same claims, sprite frame
    instead of atlas tile.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        # 16 wide, 32 tall: two stacked 16x16 frames -- frame 0 fully opaque (blue),
        # frame 1 also opaque but a different colour, so their auto-derived masks are
        # both "fully solid" and easy to reason about by hand.
        Image.new("RGBA", (16, 32), (30, 90, 200, 255)).save(
            os.path.join(root, "hero.png"))
        proj = editor_project(root, sprite='''
            [[sprite]]
            name = "hero"
            sheet = "hero.png"
            frames = [[0, 0, 16, 16], [0, 16, 16, 16]]
            out = "hero.bin"
        ''')

        info = proj.sprite_frames("hero")
        check("a fresh sprite's frames start with no collision entry at all",
              info["cells"][0]["collision"]["mode"] == pnx_assets.COLLISION_NONE
              and info["cells"][1]["collision"]["mode"] == pnx_assets.COLLISION_NONE)
        check("a fully opaque frame's auto_mask is fully ink",
              all(c == "#" * 16 for c in info["cells"][0]["collision"]["auto_mask"]))

        proj.save_sprite_collision("hero", 0, pnx_assets.COLLISION_SCALED,
                                   kind=pnx_assets.COLLISION_KIND_HURT, rect=[0, 8, 16, 8])
        info2 = proj.sprite_frames("hero")
        f0 = info2["cells"][0]["collision"]
        check("a saved SCALED rect + kind round-trips through sprite_frames",
              f0["mode"] == pnx_assets.COLLISION_SCALED and f0["rect"] == [0, 8, 16, 8]
              and f0["kind"] == pnx_assets.COLLISION_KIND_HURT)
        check("frame 1 is untouched by editing frame 0",
              info2["cells"][1]["collision"]["mode"] == pnx_assets.COLLISION_NONE)
        check("the manifest still builds after a SCALED save",
              builds(proj.path, root, "out_sfc1"))

        # Re-editing the SAME frame must REPLACE its entry, not add a second one --
        # parse_shape_collision refuses two entries for one frame, so a duplicate would
        # fail the very next build silently until Build was pressed.
        mask_rows = ["#" * 16] * 16
        proj.save_sprite_collision("hero", 0, pnx_assets.COLLISION_COMPLEX,
                                   mask_rows=mask_rows)
        info3 = proj.sprite_frames("hero")
        f0b = info3["cells"][0]["collision"]
        check("re-saving replaces the previous entry rather than duplicating it",
              f0b["mode"] == pnx_assets.COLLISION_COMPLEX)
        check("an authored mask is flagged as authored, not auto-derived",
              f0b["authored"] is True)
        check("kind reverts to the default (wall) when not re-specified",
              f0b["kind"] == pnx_assets.COLLISION_KIND_WALL)
        check("the manifest still builds after replacing SCALED with COMPLEX",
              builds(proj.path, root, "out_sfc2"))

        proj.remove_sprite_collision("hero", 0)
        info4 = proj.sprite_frames("hero")
        check("removing the entry reverts the frame to NONE",
              info4["cells"][0]["collision"]["mode"] == pnx_assets.COLLISION_NONE)
        check("the manifest still builds after remove",
              builds(proj.path, root, "out_sfc3"))

        try:
            proj.remove_sprite_collision("hero", 0)
            check("removing a frame with no collision entry is refused", False)
        except ValueError:
            check("removing a frame with no collision entry is refused", True)

        try:
            proj.save_sprite_collision("hero", 0, pnx_assets.COLLISION_SCALED,
                                       rect=[0, 0, 999, 999])
            check("a rect that does not fit the frame is refused", False)
        except ValueError:
            check("a rect that does not fit the frame is refused", True)

        try:
            proj.save_sprite_collision("hero", 99, pnx_assets.COLLISION_SOLID)
            check("collision on an out-of-range frame is refused", False)
        except ValueError:
            check("collision on an out-of-range frame is refused", True)


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

        # Frames disagreeing on size is no longer refused -- a tightly packed sheet's
        # frames are not required to share one size any more (pack_sprite's own comment).
        # A same-shaped smoke check lives in check_variable_frame_sprites() instead.
        for label, call in (
            ("an origin outside its own frame",
             lambda: proj.save_sprite("bad", "hero.png", [[0, 0, 16, 16, 99, 99]])),
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


def check_nine_slice_preview_compose():
    """_nine_slice_compose (tools/editor/project/nine_slice.py) tiles a panel the same
    way pnx_gfx_draw_nine_slice (src/pnx/gfx/pnx_nineslice.c) does at draw time -- pinned
    directly against a hand-built 6x6 panel with a distinct colour per region, the editor
    preview's own equivalent of test_gfx.c's nine-slice pixel cases.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "tools", "editor"))
    from project.nine_slice import _nine_slice_compose   # noqa: E402

    panel = Image.new("RGBA", (6, 6))
    px = panel.load()
    regions = {
        (0, 0): (255, 0, 0), (4, 0): (0, 255, 0),      # top-left, top-right corners
        (0, 4): (0, 0, 255), (4, 4): (255, 255, 0),    # bottom-left, bottom-right corners
        (2, 0): (255, 0, 255), (2, 4): (0, 255, 255),  # top, bottom edges
        (0, 2): (128, 128, 128), (4, 2): (64, 64, 64), # left, right edges
        (2, 2): (255, 128, 0),                         # centre
    }
    for (ox, oy), colour in regions.items():
        for j in range(2):
            for i in range(2):
                px[ox + i, oy + j] = colour + (255,)

    out = _nine_slice_compose(panel, 2, 2, 2, 2, 10, 8)
    op = out.load()
    check("compose: top-left corner unmoved", op[0, 0][:3] == (255, 0, 0))
    check("compose: top-right corner at the new right edge", op[9, 0][:3] == (0, 255, 0))
    check("compose: bottom-left corner at the new bottom edge", op[0, 7][:3] == (0, 0, 255))
    check("compose: bottom-right corner", op[9, 7][:3] == (255, 255, 0))
    check("compose: top edge tiles across the new width",
          op[4, 0][:3] == (255, 0, 255) and op[5, 0][:3] == (255, 0, 255))
    check("compose: left edge tiles down the new height",
          op[0, 3][:3] == (128, 128, 128))
    check("compose: centre tiles in both axes",
          op[4, 3][:3] == (255, 128, 0) and op[7, 5][:3] == (255, 128, 0))

    # Undersized box: clamps rather than reading past the source, same as
    # pnx_gfx_draw_nine_slice's own comment on the clamp.
    tiny = _nine_slice_compose(panel, 2, 2, 2, 2, 3, 6)
    check("compose: undersized box still renders the top-left corner's own colour",
          tiny.load()[0, 0][:3] == (255, 0, 0))


def check_editor_nine_slice():
    """Declaring a 9-slice panel through the editor, previewing it tiled, and the scene
    wiring that lets a game actually load one -- NineSliceMixin's own shape, mirrored
    against check_editor_sprites throughout."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        Image.new("RGBA", (8, 8), (30, 90, 200, 255)).save(
            os.path.join(root, "panel.png"))
        proj = editor_project(root, nine_slice='''
[[nine_slice]]
name = "old"
sheet = "panel.png"
border = [1, 1, 1, 1]
out = "old.bin"
# A comment that has to survive a rewrite.
''')

        proj.save_nine_slice("panel", "panel.png", [2, 2, 2, 2], [0, 0, 8, 8])
        got = {ns["name"]: ns for ns in proj.nine_slices()}
        check("a nine_slice is declared", got["panel"]["border"] == [2, 2, 2, 2])
        check("with its rect", got["panel"]["rect"] == [0, 0, 8, 8])
        check("and the manifest builds", builds(proj.path, root, "out_ns"))

        preview = proj.nine_slice_preview("panel.png", [2, 2, 2, 2], [0, 0, 8, 8],
                                          test_w=20, test_h=16)
        check("preview renders at the requested test size",
              (preview["w"], preview["h"]) == (20, 16))
        check("preview reports the source panel size",
              (preview["panel_w"], preview["panel_h"]) == (8, 8))

        # Rewriting an existing panel keeps the comment inside its block.
        proj.save_nine_slice("old", "panel.png", [3, 3, 3, 3])
        check("rewriting a nine_slice keeps its comments",
              "has to survive a rewrite" in open(proj.path).read())
        check("and updates its border",
              {ns["name"]: ns for ns in proj.nine_slices()}["old"]["border"] == [3, 3, 3, 3])

        for label, call in (
            ("a border that does not fit the panel",
             lambda: proj.save_nine_slice("bad", "panel.png", [5, 5, 5, 5], [0, 0, 8, 8])),
            ("a border of the wrong length",
             lambda: proj.save_nine_slice("bad", "panel.png", [1, 1])),
            ("a negative border",
             lambda: proj.save_nine_slice("bad", "panel.png", [-1, 1, 1, 1])),
            ("a name that is not an identifier",
             lambda: proj.save_nine_slice("Bad Panel", "panel.png", [1, 1, 1, 1])),
        ):
            try:
                call()
                check(f"a nine_slice refuses {label}", False)
            except ValueError:
                check(f"a nine_slice refuses {label}", True)

        proj.save_scene("s1", nine_slices=["panel"])
        check("what loads a nine_slice is reported",
              proj.nine_slice_users("panel") == ["scene s1"])
        try:
            proj.remove_nine_slice("panel")
            check("removing a nine_slice a scene loads is refused", False)
        except ValueError:
            check("removing a nine_slice a scene loads is refused", True)

        proj.remove_nine_slice("old")
        check("an unused nine_slice is removed",
              [ns["name"] for ns in proj.nine_slices()] == ["panel"])
        check("and the manifest still builds", builds(proj.path, root, "out_ns2"))


def check_editor_hud_vars():
    """Declaring, rewriting, and removing [[hud_var]] through the editor -- HudMixin's
    own shape, mirrored against check_editor_nine_slice minus the preview/users pieces
    that section has and this one does not yet (see HudMixin's own docstring)."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, hud_var='''
[[hud_var]]
name = "old"
type = "int"
# A comment that has to survive a rewrite.
''')

        proj.save_hud_var("speed", "int")
        proj.save_hud_var("radio_station", "text")
        got = {hv["name"]: hv for hv in proj.hud_vars()}
        check("a hud_var is declared", got["speed"]["type"] == "int")
        check("a text hud_var is declared", got["radio_station"]["type"] == "text")
        check("and the manifest builds", builds(proj.path, root, "out_hv"))

        # Rewriting an existing variable keeps the comment inside its block.
        proj.save_hud_var("old", "text")
        check("rewriting a hud_var keeps its comments",
              "has to survive a rewrite" in open(proj.path).read())
        check("and updates its type",
              {hv["name"]: hv for hv in proj.hud_vars()}["old"]["type"] == "text")

        for label, call in (
            ("a name that is not an identifier",
             lambda: proj.save_hud_var("Bad Name", "int")),
            ("an invalid type",
             lambda: proj.save_hud_var("bad", "float")),
        ):
            try:
                call()
                check(f"a hud_var refuses {label}", False)
            except ValueError:
                check(f"a hud_var refuses {label}", True)

        proj.remove_hud_var("old")
        check("an unused hud_var is removed",
              sorted(hv["name"] for hv in proj.hud_vars()) == ["radio_station", "speed"])
        check("and the manifest still builds", builds(proj.path, root, "out_hv2"))

        try:
            proj.remove_hud_var("nope")
            check("removing an unknown hud_var is refused", False)
        except ValueError:
            check("removing an unknown hud_var is refused", True)


def check_editor_hud_windows():
    """Declaring a window, adding/rewriting/removing its nested elements, previewing it,
    and removing the whole thing -- HudWindowMixin's own shape, mirrored against
    check_editor_hud_vars and check_editor_nine_slice's own [[atlas.collision]]-style
    nested editing (see HudWindowMixin's own docstring)."""
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, nine_slice=HUD_PANEL_NS, hud_var=HUD_SPEED_VAR)

        proj.save_hud_window("speed_hud", show_ms=250, hide_ms=200, ease="out_cubic",
                             slide=[0, 40])
        got = {w["name"]: w for w in proj.hud_windows()}
        check("a hud_window is declared", got["speed_hud"]["show_ms"] == 250)
        check("with its ease", got["speed_hud"]["ease"] == "out_cubic")
        check("and starts with no elements", got["speed_hud"]["elements"] == [])

        proj.save_hud_window_element("speed_hud", None, "panel", anchor="bottom_left",
                                     offset=[4, -4], panel="panel", w=60, h=20)
        proj.save_hud_window_element("speed_hud", None, "bar", anchor="top_right",
                                     offset=[-4, 4], hud_var="speed", w=50, h=8, max=200,
                                     border=192, track=0, fill=255)
        elements = {w["name"]: w for w in proj.hud_windows()}["speed_hud"]["elements"]
        check("two elements are declared", len(elements) == 2)
        check("the panel element", elements[0]["kind"] == "panel" and
              elements[0]["panel"] == "panel")
        check("the bar element", elements[1]["kind"] == "bar" and
              elements[1]["hud_var"] == "speed" and elements[1]["max"] == 200)
        check("and the manifest builds", builds(proj.path, root, "out_hw"))

        # Rewriting an existing element (by position) updates it in place, not appends.
        proj.save_hud_window_element("speed_hud", 0, "panel", anchor="bottom_left",
                                     offset=[4, -4], panel="panel", w=70, h=25)
        elements = {w["name"]: w for w in proj.hud_windows()}["speed_hud"]["elements"]
        check("rewriting an element keeps the count at two", len(elements) == 2)
        check("and updates its own fields", elements[0]["w"] == 70)

        preview = proj.hud_window_preview("speed_hud")
        check("preview reports the project's own screen size",
              (preview["w"], preview["h"]) == (proj.SCREEN_W, proj.SCREEN_H))
        check("preview renders an image", preview["img"].startswith("data:image"))

        for label, call in (
            ("a name that is not an identifier",
             lambda: proj.save_hud_window("Bad Name")),
            ("an unknown ease",
             lambda: proj.save_hud_window("hud2", ease="bounce")),
            ("a panel element naming an unknown nine_slice",
             lambda: proj.save_hud_window_element("speed_hud", None, "panel",
                                                   panel="nope", w=10, h=10)),
            ("a bar element naming an unknown hud_var",
             lambda: proj.save_hud_window_element("speed_hud", None, "bar",
                                                   hud_var="nope", w=10, h=6, max=100)),
            ("an element under an unknown window",
             lambda: proj.save_hud_window_element("nope", None, "panel", panel="panel",
                                                   w=10, h=10)),
        ):
            try:
                call()
                check(f"hud_window refuses {label}", False)
            except ValueError:
                check(f"hud_window refuses {label}", True)

        proj.remove_hud_window_element("speed_hud", 1)
        elements = {w["name"]: w for w in proj.hud_windows()}["speed_hud"]["elements"]
        check("removing an element drops it", len(elements) == 1)
        check("and the manifest still builds", builds(proj.path, root, "out_hw2"))

        proj.remove_hud_window("speed_hud")
        check("removing the window drops it entirely", proj.hud_windows() == [])
        check("and the manifest still builds with none declared",
              builds(proj.path, root, "out_hw3"))

        try:
            proj.remove_hud_window("nope")
            check("removing an unknown hud_window is refused", False)
        except ValueError:
            check("removing an unknown hud_window is refused", True)


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


def check_map_layers():
    """Multi-layer map authoring: `[[map.layer]]` sub-tables compile to the PnxMapLayer
    plumbing the runtime has carried since M13, wired through the pipeline for the first
    time (finish_map/parse_map used to hard-refuse anything but layer_count == 1). The
    claim worth testing is not "it builds" -- it is that both layers round-trip
    independently (own cells, own parallax/wrap) while start/warps/reachability stay a
    PRIMARY-layer-only concept, exactly as PnxMapLayer's own comment states, and that a
    manifest with no `[[map.layer]]` at all still builds byte-shape-identical to before.
    """
    global checks, failures

    layered_maps = f'''
        [[map]]
        name = "a"
        out = "a.bin"

        [[map.layer]]
        primary = true
        start = [2, 1]
        warps = [{{ at = [1, 2], to = ["b", 1, 1] }}]
        rows = """{BASE_MAP}"""

        [[map.layer]]
        parallax_pct_x = 128
        parallax_pct_y = 64
        wrap = true
        rows = """{BASE_MAP}"""

        [[map]]
        name = "b"
        out = "b.bin"
        start = [1, 1]
        warps = []
        rows = """{BASE_MAP}"""
    '''

    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        built = build_maps(root, maps=layered_maps)
        mp = built["a.bin"]

        checks += 1
        if mp["layer_count"] != 2 or mp["primary_layer"] != 0 or len(mp["layers"]) != 2:
            print(f"  FAIL map a: expected 2 layers, primary 0, got layer_count="
                  f"{mp['layer_count']} primary={mp['primary_layer']} "
                  f"len(layers)={len(mp['layers'])}")
            failures += 1

        # Both layers were painted from the SAME rows, so their cell planes should match
        # -- a real check that the second layer's own cells were sliced and reassembled
        # at all, not a coincidence of one layer's data leaking into the other's slot.
        checks += 1
        if mp["layers"][0]["cells"] != mp["layers"][1]["cells"]:
            print("  FAIL map a: both layers painted from the same rows should match")
            failures += 1

        checks += 1
        got = (mp["layers"][1]["parallax_pct_x"], mp["layers"][1]["parallax_pct_y"],
              mp["layers"][1]["wrap"])
        if got != (128, 64, True):
            print(f"  FAIL map a: layer 1 parallax_x/y/wrap did not round-trip, got {got}")
            failures += 1

        checks += 1
        got = (mp["layers"][0]["parallax_pct_x"], mp["layers"][0]["parallax_pct_y"],
              mp["layers"][0]["wrap"])
        if got != (255, 255, False):
            print(f"  FAIL map a: layer 0 (primary) should default to world parallax, "
                  f"no wrap, got {got}")
            failures += 1

        # Top-level mirror: everything a caller written before multi-layer maps existed
        # reads is the PRIMARY layer's own data, unchanged in shape.
        checks += 1
        if mp["cells"] != mp["layers"][0]["cells"] or mp["w"] != mp["layers"][0]["w"]:
            print("  FAIL map a: top-level w/cells should mirror the primary layer")
            failures += 1

    # No [[map.layer]] at all must still build layer_count 1 -- the whole back-compat
    # promise for every manifest that predates this.
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        built = build_maps(root)
        mp = built["a.bin"]
        checks += 1
        if mp["layer_count"] != 1 or mp["primary_layer"] != 0 or len(mp["layers"]) != 1:
            print(f"  FAIL default manifest: expected layer_count=1, got "
                  f"{mp['layer_count']}")
            failures += 1

    # Rotation touches every layer, not just the primary -- both must come out actually
    # turned, and independently (a bug that rotated only the primary, or copied one
    # layer's rotated cells onto the other, would still pass a check that only looked at
    # layer 0).
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        portrait = build_maps(root, maps=layered_maps)["a.bin"]
        out = build_at(root, "buttons_top", maps=layered_maps)
        blob = read_blob(out, "a.bin")
        banks, i = [], 0
        while os.path.exists(os.path.join(out, f"a_b{i}.bin")):
            banks.append(read_blob(out, f"a_b{i}.bin"))
            i += 1
        rotated = pnx_assets.parse_map(blob, banks)

        checks += 1
        if len(rotated["layers"]) != 2:
            print(f"  FAIL rotated map a: expected 2 layers, got {len(rotated['layers'])}")
            failures += 1

        checks += 1
        if (rotated["layers"][0]["cells"] == portrait["layers"][0]["cells"]
                or rotated["layers"][1]["cells"] == portrait["layers"][1]["cells"]):
            print("  FAIL rotated map a: cells look unrotated")
            failures += 1

        checks += 1
        if rotated["layers"][0]["cells"] != rotated["layers"][1]["cells"]:
            print("  FAIL rotated map a: both layers painted identically should still "
                  "match after rotation")
            failures += 1

    expect_fail(
        "a map declaring both [[map.layer]] and a top-level rows",
        "belong on a layer",
        maps=f'''
            [[map]]
            name = "a"
            out = "a.bin"
            rows = """{BASE_MAP}"""
            [[map.layer]]
            primary = true
            start = [2, 1]
            warps = []
            rows = """{BASE_MAP}"""
        ''')

    expect_fail(
        "a [[map.layer]] with neither rows nor source",
        "no cells",
        maps='''
            [[map]]
            name = "a"
            out = "a.bin"
            [[map.layer]]
            primary = true
            start = [2, 1]
            warps = []
        ''')


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


def check_editor_map_layers():
    """Adding, painting, and removing layers through the editor -- the UI surface for
    what check_map_layers already proved the pipeline can build. The claim worth testing
    is the round trip: add a layer, paint each format it can be authored in, remove one,
    and the manifest still builds at every step.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, maps=f'''
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
        ''')

        before = {m["name"]: m for m in proj.maps()}["a"]
        check("a fresh map has one layer", len(before["layers"]) == 1)

        proj.add_map_layer("a")
        got = {m["name"]: m for m in proj.maps()}["a"]
        check("adding a layer makes the plane explicit and adds one",
              len(got["layers"]) == 2)
        check("the primary layer kept its cells", got["layers"][got["primary"]]["cells"]
              == before["cells"])
        check("the primary layer kept start/warps",
              (got["layers"][got["primary"]]["start"],
               [w["to"][0] for w in got["layers"][got["primary"]]["warps"]])
              == (before["start"], ["b"]))
        check("the new layer is .pnxmap-backed", got["layers"][1]["format"] == "source")
        check("the new layer is blank floor",
              set(got["layers"][1]["cells"]) == {0})
        check("still builds with 2 layers", builds(proj.path, root, "out_lay1"))

        proj.add_map_layer("a")
        got = {m["name"]: m for m in proj.maps()}["a"]
        check("adding a second layer makes 3", len(got["layers"]) == 3)
        check("still builds with 3 layers", builds(proj.path, root, "out_lay2"))

        # Paint the primary (rows-backed) layer: a save through its own [[map.layer]]
        # sub-table, not the map-level rows this map no longer has.
        new_rows = ["####", "#DD#", "#..#", "####"]
        proj.save_map_layer_rows("a", got["primary"], new_rows, [1, 2], [])
        got2 = {m["name"]: m for m in proj.maps()}["a"]
        check("painting the primary rows layer changes its cells",
              got2["layers"][got2["primary"]]["cells"] != got["layers"][got["primary"]]["cells"])
        check("and start moved with it", got2["layers"][got2["primary"]]["start"] == [1, 2])

        # Paint a non-primary (.pnxmap-backed) layer directly.
        layer1 = got2["layers"][1]
        cells = list(layer1["cells"])
        cells[0] = 0
        proj.save_map_layer_source("a", 1, layer1["w"], layer1["h"], cells,
                                   layer1["tiles"])
        got3 = {m["name"]: m for m in proj.maps()}["a"]
        check("painting a non-primary source layer changes its file",
              got3["layers"][1]["cells"] == cells)
        check("still builds after painting both formats", builds(proj.path, root, "out_lay3"))

        try:
            proj.remove_map_layer("a", got3["primary"])
            check("removing the primary layer is refused", False)
        except ValueError:
            check("removing the primary layer is refused", True)

        proj.remove_map_layer("a", 2)
        got4 = {m["name"]: m for m in proj.maps()}["a"]
        check("removing a non-primary layer drops it", len(got4["layers"]) == 2)
        check("still builds after removing a layer", builds(proj.path, root, "out_lay4"))

        try:
            proj.remove_map_layer("a", 1)
            proj.remove_map_layer("a", 0)
            check("removing a map's last layer is refused", False)
        except ValueError:
            check("removing a map's last layer is refused", True)


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

def check_music_arrangement():
    """compile_arrangement / pack_music's track+clip path.

    A track IS a channel, so layering two clips on different channels should never collide --
    that's the whole point of the arrangement feature. Everything else (instrument-change
    stamping, dedup, overlap/duplicate-channel/missing-clip validation) is checked directly
    against compile_arrangement's output, which is exactly what pack_music also builds from --
    see the last case, which goes through pack_music itself to prove the two agree.
    """
    kick = {"name": "kick", "rows": ["C3", ".", "C3", "."]}
    bass = {"name": "bass", "rows": ["C2", "C2", "C2", "C2"]}
    base_spec = {
        "clip": [kick, bass],
        "track": [
            {"channel": 0, "placement": [{"clip": "kick", "start": 0}]},
            {"channel": 1, "placement": [{"instrument": 1, "start": 0},
                                        {"clip": "bass", "start": 0}]},
        ],
        "resolution": 4,
    }
    patterns, order, markers = pnx_assets.compile_arrangement(dict(base_spec))
    check("layered tracks compile to one 4-row pattern", len(patterns) == 1 and order == [0])
    rows = patterns[0]["rows"]
    check("channel 0 (kick, default instrument 0) and channel 1 (bass, instrument-changed "
          "to 1) both sound on row 0, independently",
          rows[0] == "C3:0  C2:1  .  .")
    check("kick's hold ('.') on row 1 doesn't erase bass, which keeps playing",
          rows[1] == ".  C2:1  .  .")
    check("no markers declared, none returned", markers == [])

    # --- dedup: two identical 4-row sections should compile to ONE stored pattern, reused
    #     via `order`, the same way a hand-written order list already reuses a whole pattern
    #     for a repeated section (see examples/audiotest's real one).
    repeat_spec = dict(base_spec)
    repeat_spec["track"] = [
        {"channel": 0, "placement": [{"clip": "kick", "start": 0},
                                     {"clip": "kick", "start": 4}]},
    ]
    patterns, order, _ = pnx_assets.compile_arrangement(repeat_spec)
    check("a repeated section dedupes to one unique pattern", len(patterns) == 1)
    check("but the order list still plays it twice", order == [0, 0])

    # --- markers pass through as absolute rows, and extend total_len if needed
    marker_spec = dict(base_spec)
    marker_spec["markers"] = [{"name": "drop", "at": 2}]
    _, _, markers = pnx_assets.compile_arrangement(marker_spec)
    check("a named marker's row survives compilation", markers == [2])

    # --- validation
    try:
        bad = dict(base_spec)
        bad["track"] = [{"channel": 0, "placement": [{"clip": "kick", "start": 0},
                                                      {"clip": "kick", "start": 2}]}]
        pnx_assets.compile_arrangement(bad)
        check("overlapping placements on one track is a build error", False)
    except pnx_assets.BuildError as e:
        check("overlapping placements on one track is a build error", "overlaps" in str(e))

    try:
        bad = dict(base_spec)
        bad["track"] = [{"channel": 0, "placement": []}, {"channel": 0, "placement": []}]
        pnx_assets.compile_arrangement(bad)
        check("two tracks on the same channel is a build error", False)
    except pnx_assets.BuildError as e:
        check("two tracks on the same channel is a build error",
              "more than one track" in str(e))

    try:
        bad = dict(base_spec)
        bad["track"] = [{"channel": 0, "placement": [{"clip": "nope", "start": 0}]}]
        pnx_assets.compile_arrangement(bad)
        check("a placement naming a missing clip is a build error", False)
    except pnx_assets.BuildError as e:
        check("a placement naming a missing clip is a build error", "no such clip" in str(e))

    # --- end to end through pack_music itself: an arrangement-only song (no hand-authored
    #     [[pattern]]/order at all) builds, and its header fields match what
    #     compile_arrangement alone reported.
    full_spec = dict(base_spec)
    full_spec["instrument"] = [{"wave": "square"}, {"wave": "triangle"}]
    full_spec["markers"] = [{"name": "drop", "at": 2}]
    songs = pnx_assets.pack_music({"theme": full_spec})
    check("an arrangement-only song builds through pack_music with no [[pattern]] authored",
          len(songs) == 1 and songs[0]["name"] == "theme")
    check("its named marker reaches the header list",
          songs[0]["marker_names"] == [(2, "drop")])


LEGACY_MUSIC = '''
    [music.theme]
    tempo = 100
    channels = 4
    order = [0, 1, 0]

    [[music.theme.instrument]]
    wave = "square"
    attack = 5
    decay = 50
    sustain = 180
    release = 100

    [[music.theme.instrument]]
    wave = "triangle"
    attack = 5
    decay = 50
    sustain = 180
    release = 100

    [[music.theme.pattern]]
    rows = [
      "C3:0  .     .     .    ",
      ".     .     .     .    ",
      "D3:1  .     .     .    ",
      ".     .     .     .    ",
    ]

    [[music.theme.pattern]]
    rows = [
      "E3:0  .     .     .    ",
      ".     .     .     .    ",
      ".     .     .     .    ",
      ".     .     .     .    ",
    ]
'''

# `order` deliberately AFTER the last [[pattern]] block -- which, per TOML's own
# table-scoping rules, makes it parse as a key on THAT pattern rather than on the song (see
# convert_to_arrangement's recovery for this). Not a contrived case: examples/audiotest and
# examples/overworld are both actually written this way in this repository today.
LEGACY_MUSIC_MISPLACED_ORDER = '''
    [music.theme]
    tempo = 100
    channels = 4

    [[music.theme.instrument]]
    wave = "square"
    attack = 5
    decay = 50
    sustain = 180
    release = 100

    [[music.theme.instrument]]
    wave = "triangle"
    attack = 5
    decay = 50
    sustain = 180
    release = 100

    [[music.theme.pattern]]
    rows = [
      "C3:0  .     .     .    ",
      ".     .     .     .    ",
      "D3:1  .     .     .    ",
      ".     .     .     .    ",
    ]

    [[music.theme.pattern]]
    rows = [
      "E3:0  .     .     .    ",
      ".     .     .     .    ",
      ".     .     .     .    ",
      ".     .     .     .    ",
    ]

    order = [0, 1, 0]
'''


def check_editor_music_arrangement():
    """The editor project layer's clip/track/marker CRUD (tools/editor/project/music.py), and
    the one claim that matters most for convert_to_arrangement: a legacy song's compiled
    output is BYTE-IDENTICAL before and after conversion, even though its one pattern
    (channel 0 alone) switches instrument mid-pattern -- row 0 plays instrument 0, row 2
    plays instrument 1 -- which is exactly the case that needs a clip split, not one clip.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, music=LEGACY_MUSIC)

        out1 = os.path.join(root, "out1")
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(proj.path, out1, os.path.join(out1, "gen.h"))
        before = open(os.path.join(out1, "music_theme.bin"), "rb").read()

        proj.convert_to_arrangement("theme")
        theme = proj.man["music"]["theme"]
        check("conversion drops the raw pattern table", "pattern" not in theme)
        check("conversion drops the order key", "order" not in theme)
        check("conversion adds at least one track", bool(theme.get("track")))
        # Channel 0's mid-pattern instrument change (row 0 -> inst 0, row 2 -> inst 1) has
        # to become two clips (one per run), not one -- a clip carries no instrument.
        check("the instrument change split channel 0 into more than one clip",
              len(theme.get("clip", [])) > 1)

        out2 = os.path.join(root, "out2")
        with contextlib.redirect_stdout(io.StringIO()):
            pnx_assets.build(proj.path, out2, os.path.join(out2, "gen.h"))
        after = open(os.path.join(out2, "music_theme.bin"), "rb").read()
        check("converting to an arrangement builds byte-identical output", before == after)

        try:
            proj.convert_to_arrangement("theme")
            check("converting an already-arranged song is refused", False)
        except ValueError:
            check("converting an already-arranged song is refused", True)

    # `order` placed after the last pattern (see LEGACY_MUSIC_MISPLACED_ORDER) means
    # spec.get("order") itself reads None -- not just for conversion but for the ORIGINAL
    # build too, which is a separate, pre-existing bug in this repository's manifests, not
    # something this feature introduced or can fix here. What this DOES have to do is not
    # make it worse: recover the same order the manifest visually shows rather than
    # silently falling back to "every pattern once, no repeats" like the raw build already
    # (incorrectly) does.
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, music=LEGACY_MUSIC_MISPLACED_ORDER)
        proj.convert_to_arrangement("theme")
        placements = proj.man["music"]["theme"]["track"][0]["placement"]
        starts = sorted(int(p["start"]) for p in placements if "clip" in p)
        check("a misplaced `order` key is still recovered as 3 repetitions, not 2",
              starts == [0, 2, 4, 8, 10])
        check("the manifest still builds after recovering a misplaced order",
              builds(proj.path, root, "out_misplaced"))

    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        proj = editor_project(root, music=LEGACY_MUSIC)
        proj.add_clip("theme", "lead", ["C4", ".", "-", "."])
        check("add_clip lands in the manifest",
              any(c["name"] == "lead" for c in proj.man["music"]["theme"]["clip"]))

        proj.save_track("theme", 1, [{"instrument": 0, "start": 0},
                                     {"clip": "lead", "start": 0}])
        check("save_track lands in the manifest",
              any(t["channel"] == 1 for t in proj.man["music"]["theme"]["track"]))

        proj.save_markers("theme", [{"name": "drop", "at": 4}])
        check("save_markers lands in the manifest",
              proj.man["music"]["theme"]["markers"] == [{"name": "drop", "at": 4}])

        try:
            proj.remove_clip("theme", "lead")
            check("removing a clip a track still places is refused", False)
        except ValueError as e:
            check("removing a clip a track still places is refused", "placed on" in str(e))

        try:
            proj.save_track("theme", 1, [{"clip": "lead", "start": 0},
                                         {"clip": "lead", "start": 2}])
            check("an overlapping placement on save_track is refused", False)
        except ValueError as e:
            check("an overlapping placement on save_track is refused", "overlaps" in str(e))

        try:
            proj.save_track("theme", 9, [])
            check("an out-of-range channel is refused", False)
        except ValueError:
            check("an out-of-range channel is refused", True)

        check("the manifest still builds with the new track/clip/markers",
              builds(proj.path, root, "out3"))


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
    """`offset` threads through the Atlas tab's own endpoints the same way it does
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


def check_editor_atlas_origin():
    """origin_map -- the Maps tab tile picker's "where did this tile come from" lookup.

    Dedup reorders tiles into first-seen-during-scan order, which is exactly what makes
    a packed atlas hard to read against the source art: this is the backend for showing
    a packed tile's sheet position back to the author, so a live pack_atlas call has to
    agree with the coordinates this returns, not just with the tile count.
    """
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))   # 2x2 tiles, 16px, no offset
        proj = editor_project(root)

        r = proj.origin_map("tiles")
        check("origin_map reports the sheet's real pixel size",
              r["sheet_size"] == [32, 32])
        check("origin_map reports the atlas's tile size",
              r["tile_px"] == 16)
        check("origin_map returns one origin per packed tile",
              len(r["origin"]) == 4)
        # make_sheet lays its 2x2 tiles out at (0,0) then row-major every 16px -- the same
        # order pack_atlas's carve loop scans in, so origin should match exactly.
        check("origins are the tiles' real top-left pixel positions, row-major",
              r["origin"] == [[0, 0], [16, 0], [0, 16], [16, 16]])
        check("a thumbnail comes back too", r["thumb"].startswith("data:image/"))

        try:
            proj.origin_map("nosuchatlas")
            check("an unknown atlas name is refused", False)
        except ValueError as e:
            check("an unknown atlas name is refused", "no atlas named" in str(e))


def check_editor_atlas_tiles_and_collision():
    """carve_tiles (the packed-tile-aware view) and save/remove_atlas_collision -- the
    backend the Atlas tab's per-tile editor (role + collision) is built on.
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
    check_lzss()
    check_cell_dictionary()
    check_colorkey()
    check_nine_slice()
    check_hud_vars()
    check_hud_windows()
    check_atlas_rotation_dedup()
    check_editor_analyse_dedup()

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

        # --- depth=2: baked outline + fill glyphs. outline_width/glyph_overrides only
        #     mean something once a font opts into the baked format, and a hand-painted
        #     override has to exactly match the dimensions the auto-bake produces (it
        #     replaces those levels wholesale, not a diff against them).
        DEPTH2 = FONT_OK.replace("size = 12", "size = 16\ndepth = 2\noutline_width = 2")

        expect_ok("depth=2 font with outline_width builds", _extra=_font,
                  dialog=DIALOG, font=DEPTH2)

        expect_fail("outline_width at depth=1", "depth=2", _extra=_font, dialog=DIALOG,
                    font=FONT_OK.replace("size = 12", "size = 12\noutline_width = 2"))

        expect_fail("glyph_overrides at depth=1", "depth=2", _extra=_font, dialog=DIALOG,
                    font=FONT_OK.replace(
                        'out = "hud.bin"',
                        'out = "hud.bin"\n        glyph_overrides = { H = "o.\\no." }'))

        expect_fail("outline_width below 1", "outline_width must be", _extra=_font,
                    dialog=DIALOG,
                    font=FONT_OK.replace("size = 12",
                                         "size = 12\ndepth = 2\noutline_width = 0"))

        expect_fail("glyph override wrong size", "glyph_overrides is", _extra=_font,
                    dialog=DIALOG,
                    font=DEPTH2.replace(
                        'out = "hud.bin"',
                        'out = "hud.bin"\n        glyph_overrides = { H = "..\\n.." }'))

        expect_fail("glyph override invalid character", "glyph_overrides has", _extra=_font,
                    dialog=DIALOG,
                    font=DEPTH2.replace(
                        'out = "hud.bin"',
                        'out = "hud.bin"\n        glyph_overrides = { H = "X." }'))

        # A valid override replaces the auto-bake's own levels wholesale. Rather than
        # predict the auto-bake's own dimensions for 'H' at this size/face, build once
        # without an override to read them off the blob, then paint a same-sized override
        # entirely in fill B (level 3) -- a level rasterise_glyph_styled itself never
        # produces -- so any difference in the shipped bytes can only be the override.
        checks += 1
        with tempfile.TemporaryDirectory() as root:
            make_sheet(_os.path.join(root, "sheet.png"))
            _font(root)

            def glyph_entry(blob, ch):
                count = int.from_bytes(blob[8:10], "little")
                first, last = blob[12], blob[13]
                if not first <= ord(ch) <= last:
                    return None
                index = blob[16 + count * pnx_assets.FONT_GLYPH_ENTRY + (ord(ch) - first)]
                if index == 0xFF:
                    return None
                e = blob[16 + index * pnx_assets.FONT_GLYPH_ENTRY:][:pnx_assets.FONT_GLYPH_ENTRY]
                off, w, h = int.from_bytes(e[:2], "little"), e[2], e[3]
                bitmaps_at = 16 + count * pnx_assets.FONT_GLYPH_ENTRY + (last - first + 1)
                depth = blob[3]
                row_bytes = (w * depth + 7) // 8
                return w, h, blob[bitmaps_at + off: bitmaps_at + off + row_bytes * h]

            err = run(root, dialog=DIALOG, font=DEPTH2)
            entry = None
            if err:
                print(f"  FAIL glyph override applies: baseline build failed: {err}")
                failures += 1
            else:
                blob = open(_os.path.join(root, "out", "hud.bin"), "rb").read()
                entry = glyph_entry(blob, "H")
                if entry is None:
                    print("  FAIL glyph override applies: 'H' missing from the baseline build")
                    failures += 1

            if entry is not None:
                w, h, plain_packed = entry
                override_text = "\\n".join("%" * w for _ in range(h))
                overridden = DEPTH2.replace(
                    'out = "hud.bin"',
                    f'out = "hud.bin"\n        glyph_overrides = {{ H = "{override_text}" }}')
                err2 = run(root, dialog=DIALOG, font=overridden)
                if err2:
                    print(f"  FAIL glyph override applies: build failed: {err2}")
                    failures += 1
                else:
                    blob2 = open(_os.path.join(root, "out", "hud.bin"), "rb").read()
                    entry2 = glyph_entry(blob2, "H")
                    if entry2 is None or entry2[2] == plain_packed:
                        print("  FAIL glyph override applies: bytes unchanged from the "
                              "auto-bake")
                        failures += 1

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

    # --- the editor's frontend
    #
    # Used to be one PAGE r-string in tools/pnx_editor.py (HTML, CSS and ~4500 lines of
    # JS all inline in one Python string); split into real files under
    # tools/editor/static/ -- see that package's docstring. `html` and the per-file
    # entries in `js_files` stand in for the old `page` variable, scoped to what each
    # check actually cares about.
    editor_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "tools", "editor", "static")
    with open(os.path.join(editor_dir, "index.html"), encoding="utf-8") as f:
        html = f.read()
    # In load order, as they're actually <script>-tagged: classic (non-module) scripts,
    # so every one of them shares ONE global scope with all the others -- the split is
    # files, not module boundaries. A name declared twice across this list is exactly as
    # broken as declaring it twice in one file used to be.
    js_files = ["app.js", "atlas.js", "audio-preview.js", "hud_window.js", "sprites.js"]
    js_by_file = {}
    for name in js_files:
        with open(os.path.join(editor_dir, "js", name), encoding="utf-8") as f:
            js_by_file[name] = f.read()
    js = "\n".join(js_by_file.values())

    # retired: PAGE was an r-string, so a doubled backslash ('\\n') reached the browser
    # literally instead of becoming a newline -- a bug Python's own syntax was happy
    # with, only possible because the JS lived inside a Python string literal. Now that
    # tools/editor/static/js/*.js are real .js files, there is no Python layer left to
    # do that doubling; the failure mode this caught cannot occur any more. Kept as a
    # record of what used to need checking here, per _check_editor_flags_RETIRED's
    # convention, rather than deleted outright.

    # Two top-level functions with the same name -- in one file, or (now that the script
    # is split) across two of them sharing the page's scope: the later one silently
    # wins, and the caller of the earlier one starts doing something else entirely.
    # That happened -- the code editor's `analyse()` shadowed the importer's and
    # blanked its stats panel with no error anywhere.
    checks += 1
    names = re.findall(r"^function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        print(f"  FAIL editor js: duplicate function name(s): {', '.join(dupes)}")
        failures += 1

    # Same for top-level const/let bindings, which would be a hard SyntaxError.
    checks += 1
    binds = re.findall(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", js, re.M)
    dupes = sorted({n for n in binds if binds.count(n) > 1})
    if dupes:
        print(f"  FAIL editor js: duplicate top-level binding(s): {', '.join(dupes)}")
        failures += 1

    # The shell markup is one big document; a mismatched tag is invisible until
    # something silently fails to render. Comments are stripped first, because the
    # commentary explaining the markup naturally names the tags it is about.
    checks += 1
    markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    for tag in ("div", "section", "aside", "button", "select", "textarea", "pre",
                "header", "footer", "nav", "main", "label"):
        opens = len(re.findall(rf"<{tag}[\s>]", markup))
        closes = len(re.findall(rf"</{tag}>", markup))
        if opens != closes:
            print(f"  FAIL editor html: <{tag}> opened {opens} times, closed {closes}")
            failures += 1
            break

    check_editor_atlas_removal()
    check_editor_legend()
    check_map_legend()
    check_editor_scenes()
    check_editor_new_map_scene()
    check_editor_map_lifecycle()
    check_editor_sprites()
    check_variable_frame_sprites()
    check_sprite_dedup_and_compress()
    check_atlas_compress()
    check_atlas_anim()
    check_editor_sprite_frame_collision()
    check_nine_slice_preview_compose()
    check_editor_nine_slice()
    check_editor_hud_vars()
    check_editor_hud_windows()
    check_mapfile_format()
    check_source_maps()
    check_map_layers()
    check_editor_map_migration()
    check_editor_map_layers()
    check_editor_sheet_frames()
    check_editor_dialog()
    check_editor_map_props()
    check_editor_atlas_extras()
    check_editor_atlas_offset()
    check_editor_atlas_tiles_and_collision()
    check_editor_atlas_origin()
    check_editor_fonts_and_project()
    # check_editor_flags() -- retired, see _check_editor_flags_RETIRED's own comment
    check_editor_roles()
    check_editor_autopick()
    check_editor_map_atlases()
    check_editor_update()
    check_music_arrangement()
    check_editor_music_arrangement()

    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
