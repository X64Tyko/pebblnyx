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


def expect_ok(label, **overrides):
    global checks, failures
    checks += 1
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        err = run(root, **overrides)
    if err is not None:
        print(f"  FAIL {label}: expected success, got {err!r}")
        failures += 1


def expect_fail(label, fragment, **overrides):
    """Asserts the build fails AND that the message names the actual problem.

    Checking the message matters as much as the failure: an error that says only
    "invalid manifest" leaves the author exactly as stuck as silence would.
    """
    global checks, failures
    checks += 1
    with tempfile.TemporaryDirectory() as root:
        make_sheet(os.path.join(root, "sheet.png"))
        err = run(root, **overrides)
    if err is None:
        print(f"  FAIL {label}: expected failure, but the build SUCCEEDED")
        failures += 1
    elif fragment.lower() not in err.lower():
        print(f"  FAIL {label}: message did not mention {fragment!r}\n         got: {err}")
        failures += 1


def maps(body):
    return {"maps": body}


def main():
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

    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
