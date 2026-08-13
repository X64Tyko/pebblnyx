#!/usr/bin/env python3
"""Writes assets.toml for the worldtiles example.

The field is 192x192 cells -- 73,728 bytes of cell plane, more than half `emery`'s app
RAM -- which is not something anyone types into a manifest by hand. So it is generated,
once, and the result is committed: the manifest stays the build's only input and the
editor can still open it. Re-run this to change the world; nothing reads it at build time.

    python3 examples/worldtiles/generate.py

Everything the generator decides is decided HERE rather than in the manifest, so the
constraints that make this example a streaming test -- band heights, door placement,
reachability -- are stated as code with reasons rather than as 192 lines of punctuation
nobody can check by eye.
"""

import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------- the field
#
# 192x192, in three horizontal bands of 64 rows, one atlas each.
#
# The band height is not decorative. The streamer keeps a window of 4x4 WorldTiles of 16
# cells resident -- 64 cells -- so a band of exactly 64 rows guarantees that any window
# touches at most TWO bands, and therefore at most two atlases. That is what lets the map
# declare `atlas_slots = 2` for three atlases: walking north to south loads the third and
# evicts the first, which is the behaviour this example exists to show. Make a band
# shorter and the pipeline refuses the map, naming the window that needs three.
W = H = 192
BAND = 64
BANDS = 3

# floor, wall, door -- one set per band, each resolving against that band's atlas. A door
# drawn with the meadow's tileset in the middle of the ruins would drag a third atlas into
# that window, which is the whole thing the band heights exist to prevent.
TERRAIN = [
    {"floor": ".", "wall": "#", "door": "D"},   # meadow
    {"floor": ",", "wall": "=", "door": "E"},   # ruins
    {"floor": "-", "wall": "+", "door": "F"},   # hall
]

# Where the player starts, and where each band's door sits. Doors are placed on an open
# column that the corridor below guarantees is reachable, because an unreachable door
# fails the build -- correctly, but at the end of a long generation rather than here.
START = (16, 16)
DOORS = [(96, 24), (96, 88), (96, 152)]

# A clear corridor down the middle of the map and along each band's midline, so the flood
# fill always connects and a player can actually cross the world without pathfinding
# through noise. The clutter goes everywhere else.
CORRIDOR_X = 96
CLUTTER = 0.16          # fraction of open cells that become wall
SEED = 20260812         # fixed, so the committed manifest is reproducible


def build_field():
    rng = random.Random(SEED)
    grid = [[None] * W for _ in range(H)]

    for y in range(H):
        band = min(y // BAND, BANDS - 1)
        t = TERRAIN[band]
        for x in range(W):
            edge = x == 0 or y == 0 or x == W - 1 or y == H - 1
            grid[y][x] = t["wall"] if edge else t["floor"]

    # Clutter in blocks rather than single cells: a field of isolated pixels reads as
    # noise, and blocks give the WorldTile boundaries something recognisable to cross.
    for _ in range(int(W * H * CLUTTER / 9)):
        bx, by = rng.randrange(2, W - 5), rng.randrange(2, H - 5)
        bw, bh = rng.randint(1, 3), rng.randint(1, 3)
        for y in range(by, min(by + bh, H - 1)):
            for x in range(bx, min(bx + bw, W - 1)):
                grid[y][x] = TERRAIN[min(y // BAND, BANDS - 1)]["wall"]

    def clear(x, y):
        if 0 < x < W - 1 and 0 < y < H - 1:
            grid[y][x] = TERRAIN[min(y // BAND, BANDS - 1)]["floor"]

    # The spine, and a rung across each band. Cleared AFTER the clutter so nothing can
    # seal them, which is what makes reachability a property of the generator rather than
    # something to discover from a failed build.
    for y in range(1, H - 1):
        for dx in (-1, 0, 1):
            clear(CORRIDOR_X + dx, y)
    for band in range(BANDS):
        y = band * BAND + BAND // 2
        for x in range(1, W - 1):
            for dy in (-1, 0, 1):
                clear(x, y + dy)

    for x, y in [START] + DOORS:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                clear(x + dx, y + dy)
        # Connect each door back to the spine along its own row.
        for cx in range(min(x, CORRIDOR_X), max(x, CORRIDOR_X) + 1):
            clear(cx, y)

    for i, (x, y) in enumerate(DOORS):
        grid[y][x] = TERRAIN[min(y // BAND, BANDS - 1)]["door"]

    return ["".join(row) for row in grid]


# ------------------------------------------------------------------------- the interiors
#
# Small enough to be resident whole, which is half the point of showing them beside the
# field: the same format, the same code path, and no streaming ever happens.

def room(w, h, floor, wall, door, door_at):
    rows = []
    for y in range(h):
        if y in (0, h - 1):
            rows.append(wall * w)
        else:
            rows.append(wall + floor * (w - 2) + wall)
    dx, dy = door_at
    rows[dy] = rows[dy][:dx] + door + rows[dy][dx + 1:]
    return rows


def block(rows):
    return "\n".join(rows)


MANIFEST = '''# pebblnyx -- WorldTile streaming example
#
# The overworld example shows what the framework can draw. This one shows what it can
# HOLD: a 192x192 field is 73,728 bytes of cell plane against 128 KB of app RAM on
# `emery`, so it cannot be resident and never is. It streams through sixteen WorldTiles of
# 516 bytes, and its three tilesets through two atlas slots.
#
# The field's `rows` are generated -- see generate.py, which also explains why the bands
# are exactly 64 rows tall. Everything else here is written by hand.

[project]
name = "worldtiles"
resources = "resources"
header = "src/c/assets_gen.h"
budget_bytes = 262144

# --------------------------------------------------------------------------- tilesets
#
# Three, all at 16px, because one map draws on one grid. Which one a cell uses is decided
# by its legend character, and the map's `atlases` list fixes the order of the tile id
# space they share.

[[atlas]]
name = "meadow"
sheet = "art/world.png"
tile = 16
region = [0, 0, 10, 6]
max_tiles = 48
out = "meadow.bin"
autopick = ["floor", "wall", "accent"]
metatiles = "auto"

[[atlas]]
name = "ruins"
sheet = "art/dungeon.png"
tile = 16
region = [0, 0, 10, 6]
max_tiles = 48
out = "ruins.bin"
autopick = ["floor", "wall", "accent"]
metatiles = "auto"

[[atlas]]
name = "hall"
sheet = "art/interior.png"
tile = 16
region = [0, 0, 10, 6]
max_tiles = 48
out = "hall.bin"
autopick = ["floor", "wall", "accent"]
metatiles = "auto"

# ----------------------------------------------------------------------------- legend
#
# The legend is project-wide but an atlas set is per map, so a character a map cannot draw
# is only an error if that map USES it. That is what lets one legend serve a field drawing
# from three tilesets and three interiors drawing from one each.

[legend."."]
tile = "floor"
atlas = "meadow"
flags = []

[legend."#"]
tile = "wall"
atlas = "meadow"
flags = ["solid"]

[legend."D"]
tile = "accent"
atlas = "meadow"
flags = ["warp"]

[legend.","]
tile = "floor"
atlas = "ruins"
flags = []

[legend."="]
tile = "wall"
atlas = "ruins"
flags = ["solid"]

[legend."E"]
tile = "accent"
atlas = "ruins"
flags = ["warp"]

[legend."-"]
tile = "floor"
atlas = "hall"
flags = []

[legend."+"]
tile = "wall"
atlas = "hall"
flags = ["solid"]

[legend."F"]
tile = "accent"
atlas = "hall"
flags = ["warp"]

# ------------------------------------------------------------------------------- maps

# The field. Three atlases, two slots: walking from the meadow to the hall loads the third
# tileset and evicts the first, and the HUD's `read` counter is where you watch it happen.
#
# `atlas_slots` is the interesting knob here. Remove it and the pipeline gives the map a
# slot per atlas -- 3 slots, ~18 KB, nothing ever evicted. At 2 it is ~12 KB and the
# streamer earns its keep. At 1 the build fails, naming the window that needs two.
[[map]]
name = "field"
atlases = ["meadow", "ruins", "hall"]
atlas_slots = 2
worldtile = 16
out = "map_field.bin"
start = [{start_x}, {start_y}]
warps = [
  {{ at = [{d0x}, {d0y}], to = ["hut", 9, 6] }},
  {{ at = [{d1x}, {d1y}], to = ["crypt", 11, 8] }},
  {{ at = [{d2x}, {d2y}], to = ["keep", 7, 11] }},
]
rows = """
{field}
"""

# The SAME world, held whole. `resident = true` gives the map a slot per WorldTile, which
# is what a map cost before WorldTiles existed: every cell and every tileset in RAM from
# the moment the scene loads.
#
# It is here to be measured against. Identical rows, identical tilesets, identical
# everything -- so pressing SELECT to swap between this and `field` changes exactly one
# thing, and the arena figure on the HUD is the answer. It costs a second 75 KB of
# resource to ship, which is the honest price of an A/B nobody has to take on trust.
#
# Note `atlas_slots = 3`, one per atlas, so the tilesets are held whole too. Left to
# itself the pipeline would size the pool at what the resident WINDOW needs -- two -- and
# the comparison would then be measuring a streamed cell plane against a streamed one,
# changing two things at once and answering neither.
[[map]]
name = "plain"
atlases = ["meadow", "ruins", "hall"]
atlas_slots = 3
resident = true
worldtile = 16
out = "map_plain.bin"
start = [{start_x}, {start_y}]
warps = []
rows = """
{field}
"""

# The interiors. Each fits inside one pool, so nothing streams and the code path is
# identical -- which is the claim worth demonstrating beside the field rather than
# asserting in a comment.

[[map]]
name = "hut"
atlases = ["hall"]
out = "map_hut.bin"
start = [9, 6]
warps = [{{ at = [9, 12], to = ["field", {d0x}, {d0y_below}] }}]
rows = """
{hut}
"""

[[map]]
name = "crypt"
atlases = ["ruins"]
out = "map_crypt.bin"
start = [11, 8]
warps = [{{ at = [11, 16], to = ["field", {d1x}, {d1y_below}] }}]
rows = """
{crypt}
"""

[[map]]
name = "keep"
atlases = ["hall"]
out = "map_keep.bin"
start = [7, 11]
warps = [{{ at = [7, 22], to = ["field", {d2x}, {d2y_below}] }}]
rows = """
{keep}
"""

# ---------------------------------------------------------------------------- sprites

[[sprite]]
name = "hero"
sheet = "art/hero.png"
frames = [[0, 0, 16, 24], [0, 24, 16, 24], [0, 48, 16, 24]]
out = "hero.bin"

# ------------------------------------------------------------------------------ fonts

[[font]]
name = "hud"
source = "art/fonts/LiberationSans-Regular.ttf"
size = 12
depth = 1
license = "SIL Open Font License 1.1"
# No dialog in this example, so there are no pages to derive a glyph set from. The HUD
# renders numbers and a handful of labels; naming them keeps the face at 40-odd glyphs
# instead of a full ASCII range nothing draws.
charset = "manual"
extra = "0123456789/ WTmisreadKBfpsxy.:+-fieldhutcryptkeepauto"
out = "font_hud.bin"

# ----------------------------------------------------------------------------- scenes
#
# No `atlases` key anywhere: each map owns and streams the tilesets it draws with, so a
# scene listing one would load a second resident copy. The pipeline refuses that.

[scene.field]
map = "field"
sprites = ["hero"]
fonts = ["hud"]

# The comparison scene. Same world, held whole -- see the [[map]] note above.
[scene.plain]
map = "plain"
sprites = ["hero"]
fonts = ["hud"]

[scene.hut]
map = "hut"
sprites = ["hero"]
fonts = ["hud"]

[scene.crypt]
map = "crypt"
sprites = ["hero"]
fonts = ["hud"]

[scene.keep]
map = "keep"
sprites = ["hero"]
fonts = ["hud"]
'''


def main():
    field = build_field()

    text = MANIFEST.format(
        field=block(field),
        start_x=START[0], start_y=START[1],
        d0x=DOORS[0][0], d0y=DOORS[0][1], d0y_below=DOORS[0][1] + 1,
        d1x=DOORS[1][0], d1y=DOORS[1][1], d1y_below=DOORS[1][1] + 1,
        d2x=DOORS[2][0], d2y=DOORS[2][1], d2y_below=DOORS[2][1] + 1,
        hut=block(room(20, 14, "-", "+", "F", (9, 12))),
        crypt=block(room(24, 18, ",", "=", "E", (11, 16))),
        keep=block(room(16, 24, "-", "+", "F", (7, 22))),
    )

    path = os.path.join(HERE, "assets.toml")
    with open(path, "w") as f:
        f.write(text)

    walls = sum(r.count("#") + r.count("=") + r.count("+") for r in field)
    print(f"wrote {path}")
    print(f"  field {W}x{H} = {W * H * 2:,} B of cells, {walls:,} solid, "
          f"{W * H - walls:,} open")
    print(f"  {(W + 15) // 16}x{(H + 15) // 16} WorldTiles of 16")


if __name__ == "__main__":
    main()
