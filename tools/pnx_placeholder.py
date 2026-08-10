#!/usr/bin/env python3
"""Generates the example's art, so the repo carries no third-party sheets.

The framework was developed against ripped commercial tilesets, which cannot ship. Rather
than leave the example broken on clone -- a framework whose example does not run is worse
than one with no example -- this draws a small tileset and character from scratch.

Deliberately limited: a 12-colour palette, flat shapes, readable at 16px. It is also a
better demonstration of the pipeline than a real sheet, because every tile is obviously
distinct and the palette merge has something visible to do.

Usage:  tools/pnx_placeholder.py <out-dir>
"""

import os
import sys

from PIL import Image

T = 16

# A deliberately small palette, chosen so it survives quantisation to ARGB2222 without
# two entries collapsing into one -- each channel value is near a multiple of 85.
C = {
    "void":  (0, 0, 0, 0),
    "dirt":  (170, 85, 0, 255),
    "dirt2": (255, 170, 85, 255),
    "grass": (0, 170, 0, 255),
    "grass2": (85, 255, 85, 255),
    "stone": (85, 85, 85, 255),
    "stone2": (170, 170, 170, 255),
    "water": (0, 85, 170, 255),
    "water2": (85, 170, 255, 255),
    "wood":  (85, 0, 0, 255),
    "skin":  (255, 170, 170, 255),
    "cloth": (170, 0, 170, 255),
    "dark":  (0, 0, 0, 255),
}


def tile(fn):
    img = Image.new("RGBA", (T, T), C["void"])
    px = img.load()
    for y in range(T):
        for x in range(T):
            px[x, y] = fn(x, y)
    return img


def grass(x, y):
    return C["grass2"] if (x * 7 + y * 3) % 11 == 0 else C["grass"]


def dirt(x, y):
    return C["dirt2"] if (x * 5 + y * 11) % 13 < 2 else C["dirt"]


def brick(x, y):
    row = y // 4
    offset = 0 if row % 2 == 0 else 4
    if y % 4 == 0 or (x + offset) % 8 == 0:
        return C["dark"]
    return C["stone2"] if row % 2 == 0 else C["stone"]


def water(x, y):
    return C["water2"] if (y + (x // 3)) % 4 == 0 else C["water"]


def plank(x, y):
    return C["dark"] if (y % 5 == 0 or x % 7 == 0) else C["wood"]


def flowers(x, y):
    if (x % 8 == 3 or x % 8 == 4) and (y % 8 == 3 or y % 8 == 4):
        return C["cloth"]
    return grass(x, y)


def build_tileset(path):
    """Six tiles across one row, so `region = [0, 0, 6, 1]` takes the lot."""
    makers = [grass, dirt, brick, water, plank, flowers]
    sheet = Image.new("RGBA", (T * len(makers), T), C["void"])
    for i, fn in enumerate(makers):
        sheet.paste(tile(fn), (i * T, 0))
    sheet.save(path)
    return len(makers)


def hero_frame(lean):
    """A 16x24 figure. `lean` shifts the legs, which is the whole walk cycle."""
    img = Image.new("RGBA", (T, 24), C["void"])
    px = img.load()

    def box(x0, y0, x1, y1, col):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if 0 <= x < T and 0 <= y < 24:
                    px[x, y] = col

    box(5, 2, 10, 8, C["skin"])          # head
    box(5, 2, 10, 3, C["dark"])          # hair
    box(6, 5, 6, 5, C["dark"])           # eyes
    box(9, 5, 9, 5, C["dark"])
    box(4, 9, 11, 16, C["cloth"])        # body
    box(3, 10, 3, 14, C["skin"])         # arms
    box(12, 10, 12, 14, C["skin"])
    box(5 + lean, 17, 7 + lean, 23, C["wood"])        # legs
    box(8 - lean, 17, 10 - lean, 23, C["wood"])
    return img


def build_hero(path):
    frames = [hero_frame(0), hero_frame(-1), hero_frame(1)]
    sheet = Image.new("RGBA", (T, 24 * len(frames)), C["void"])
    for i, f in enumerate(frames):
        sheet.paste(f, (0, i * 24))
    sheet.save(path)
    return len(frames)


def build_npc(path):
    img = hero_frame(0)
    px = img.load()
    # Recoloured, to give the palette system two genuinely different colour sets.
    for y in range(24):
        for x in range(T):
            if px[x, y] == C["cloth"]:
                px[x, y] = C["water"]
            elif px[x, y] == C["wood"]:
                px[x, y] = C["stone"]
    img.save(path)


def build_cave(path):
    """A second tileset with a different colour set.

    Exists so the example actually exercises multiple atlases: two tilesets that both
    define floor/wall/accent and mean different tiles is the case that used to be
    impossible, and it should be visible in the example rather than only in a test.
    """
    def cave_floor(x, y):
        return C["stone2"] if (x * 3 + y * 7) % 17 < 2 else C["stone"]

    def cave_wall(x, y):
        return C["dark"] if (y % 4 == 0 or (x + y // 4 * 2) % 6 == 0) else C["wood"]

    def crystal(x, y):
        d = abs(x - 8) + abs(y - 8)
        return C["water2"] if d < 4 else (C["water"] if d < 7 else cave_floor(x, y))

    makers = [cave_floor, cave_wall, crystal, water, plank, dirt]
    sheet = Image.new("RGBA", (T * len(makers), T), C["void"])
    for i, fn in enumerate(makers):
        sheet.paste(tile(fn), (i * T, 0))
    sheet.save(path)
    return len(makers)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "art"
    os.makedirs(out, exist_ok=True)
    n = build_tileset(os.path.join(out, "tileset.png"))
    build_cave(os.path.join(out, "cave.png"))
    f = build_hero(os.path.join(out, "hero.png"))
    build_npc(os.path.join(out, "npc.png"))
    print(f"wrote {out}/tileset.png ({n} tiles), cave.png, hero.png ({f} frames), "
          f"npc.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
