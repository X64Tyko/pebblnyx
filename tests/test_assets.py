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
    for key in ("sprite", "dialog", "font"):
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


def main():
    # The font cases assert on blob contents directly rather than only on pass/fail, so
    # they count their own checks rather than going through expect_ok.
    global checks, failures
    print("asset pipeline validation")

    expect_ok("valid manifest builds")

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
    expect_fail("legend names a role its atlas lacks", "atlas does not define", legend='''
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

    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
