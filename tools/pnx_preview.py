#!/usr/bin/env python3
"""Visual asset report: renders what the device will actually draw.

Reads the **built blobs**, not the source art. That distinction is the whole point --
a preview generated from PNGs would show what you hoped for, while this decodes the same
bytes the watch loads, through the same palette indirection, and so cannot flatter the
result. Quantisation, palette merging, colour repair and metatile composition are all
visible because they have already happened.

This is E1: seeing the content. Editing it comes next.

Usage:
    tools/pnx_preview.py <manifest.toml> [-o report.html]
"""

import argparse
import base64
import io
import os
import sys
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image                                       # noqa: E402

HEADER = 8


def gcolor_rgb(c):
    """ARGB2222 -> RGB888. Alpha 0 is the transparent index."""
    if c == 0:
        return None
    return (((c >> 4) & 3) * 85, ((c >> 2) & 3) * 85, (c & 3) * 85)


def pad4(n):
    return (n + 3) & ~3


def read(path):
    with open(path, "rb") as f:
        return f.read()


def parse_header(blob):
    return {"magic": blob[0:2].decode("ascii", "replace"), "version": blob[2],
            "a": blob[3], "b": blob[4], "c": blob[5], "d": blob[6]}


PALETTE_ENTRIES = 16
PALETTE_BYTES = PALETTE_ENTRIES


def parse_palettes(blob):
    h = parse_header(blob)
    n = h["a"]
    return [list(blob[HEADER + i * PALETTE_BYTES: HEADER + i * PALETTE_BYTES + PALETTE_ENTRIES])
            for i in range(n)]


# ------------------------------------------------------------- compressed pixel bodies
#
# parse_atlas/parse_sprite used to assume flags == 0 (compress = "none") and slice
# "pixels" directly out of the blob -- which crashed (IndexError, reading tile N's bytes
# past the end of a much shorter compressed stream) the moment a project's default
# PNX_COMPRESS_MODE (bitplane) or an explicit "lzss"/"huffman" choice actually shipped a
# compressed pixel region, which is every project that does not override the default.
# These reconstruct the SAME flat, packed-4bpp "as if built with compress=none" byte
# string the rest of this module already expects, so tile_image/sprite_image/unpack_4bpp
# never need to know compression happened at all.


class _BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.len_bits = len(data) * 8

    def read_bit(self):
        if self.pos >= self.len_bits:
            return 0
        byte = self.data[self.pos >> 3]
        shift = 7 - (self.pos & 7)
        self.pos += 1
        return (byte >> shift) & 1

    def read_bits_msb(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v


def _bp_read_elias_gamma(r):
    n_bits = 1
    while r.read_bit():
        n_bits += 1
    mantissa = r.read_bits_msb(n_bits)
    return (1 << n_bits) + mantissa - 1


def _bits_for_k(k):
    return 1 if k <= 2 else 2 if k <= 4 else 3 if k <= 8 else 4


def decode_bitplane_unit(src, n):
    """Ports src/pnx/assets/pnx_bitplane.c's pnx_bitplane_decode bit for bit: independent
    per-plane Elias-gamma RLE, frequency-sorted local palette, raw-escape fallback.
    Returns packed 4bpp bytes (pack_unit_4bpp's own 2-pixels/byte, high-nibble-first
    layout) -- the same shape a compress="none" tile/frame's own bytes already are."""
    header = src[0]
    if header & 0x80:
        need = (n + 1) // 2
        return bytes(src[1:1 + need])

    k = (header & 0x0F) + 1
    off_bytes = (k + 1) // 2
    off_src = src[1:1 + off_bytes]
    offset_table = []
    for i in range(k):
        b = off_src[i // 2]
        offset_table.append((b >> 4) if i % 2 == 0 else (b & 0x0F))

    scratch = [0] * n
    if k == 1:
        scratch = [offset_table[0]] * n
    else:
        bits = _bits_for_k(k)
        r = _BitReader(src[1 + off_bytes:])
        for p in range(bits):
            current = r.read_bit()
            pos = 0
            while pos < n:
                run = _bp_read_elias_gamma(r)
                if current:
                    for i in range(run):
                        scratch[pos + i] |= (1 << p)
                pos += run
                current ^= 1
        scratch = [offset_table[v] for v in scratch]

    out = bytearray((n + 1) // 2)
    for i in range(0, n, 2):
        hi = scratch[i]
        lo = scratch[i + 1] if i + 1 < n else 0
        out[i // 2] = (hi << 4) | lo
    return bytes(out)


def _lzss_decompress(data, out_len):
    """Mirrors tools/pnx_assets.py's own lzss_decompress -- duplicated rather than
    imported to keep this preview module's dependency graph as small as the web payload
    already commits to (see build_web_payload.py), not because the format differs."""
    out = bytearray()
    i = 0
    while len(out) < out_len:
        flags = data[i]
        i += 1
        for bit in range(8):
            if len(out) >= out_len:
                break
            if flags & (1 << bit):
                out.append(data[i])
                i += 1
            else:
                token = (data[i] << 8) | data[i + 1]
                i += 2
                dist = (token >> 4) + 1
                length = (token & 0x0F) + 3
                start = len(out) - dist
                for k in range(length):
                    out.append(out[start + k])
    return bytes(out)


def _unit_offset_table(blob, table_off, unit_count):
    """(unit_count+1)-entry u16 LE offset table -- the shared shape
    encode_bitplane_units/encode_asset_shared_table both write (see either module's own
    comment); unit i's compressed bytes are blob[table_off+(unit_count+1)*2+table[i] :
    ...+table[i+1]]."""
    table = [int.from_bytes(blob[table_off + i * 2:table_off + i * 2 + 2], "little")
             for i in range(unit_count + 1)]
    stream_start = table_off + (unit_count + 1) * 2
    return table, stream_start


def _decode_units_flat(blob, stream_off, unit_count, unit_bytes, flags, lzss_tag_len=0):
    """Returns unit_count*unit_bytes bytes, packed 4bpp, uncompressed-layout-equivalent
    -- flags 0 already is that; 1 (LZSS) decompresses the whole region as one stream; 2
    (bitplane) walks the per-unit offset table and decodes each unit independently; 4
    (huffman) is not yet supported here (needs the project's separate huffman_table.bin,
    which callers of parse_atlas/parse_sprite do not currently have a way to hand in) --
    falls back to a solid grey placeholder rather than crashing, so a huffman-mode
    project's OTHER assets still preview correctly. `lzss_tag_len` is compress_pixel_body's
    own tag_len bytes ahead of the LZSS stream (2 for an atlas, 0 for a sprite -- see that
    function's own comment for why only atlases carry it)."""
    total = unit_count * unit_bytes
    if flags == 0:
        return bytes(blob[stream_off:stream_off + total])
    if flags & 1:
        complen = int.from_bytes(blob[stream_off:stream_off + 2], "little")
        start = stream_off + 2 + lzss_tag_len
        return _lzss_decompress(blob[start:start + complen], total)
    if flags & 2:
        table, units_start = _unit_offset_table(blob, stream_off, unit_count)
        out = bytearray()
        for i in range(unit_count):
            unit = blob[units_start + table[i]:units_start + table[i + 1]]
            out += decode_bitplane_unit(unit, unit_bytes * 2)
        return bytes(out)
    # flags & 4 (huffman): needs an external table this call has no access to.
    return bytes([0x11] * total)


def parse_atlas(blob):
    h = parse_header(blob)
    px, count, layout = h["a"], h["b"], h["c"]
    flags = h["d"]
    tile_bytes = px * px // 2
    body = HEADER

    if layout == 0:
        pal_idx = list(blob[body: body + count])
        stream_off = body + pad4(count) * 2
        pixels = _decode_units_flat(blob, stream_off, count, tile_bytes, flags, lzss_tag_len=2)
        return {"tile_px": px, "count": count, "metatiled": False,
                "palette_of": pal_idx, "pixels": pixels, "tile_bytes": tile_bytes}

    subs = int.from_bytes(blob[body:body + 2], "little")
    body += 4
    pal_idx = list(blob[body: body + count])
    table_at = body + pad4(count) * 2
    defs = [[int.from_bytes(blob[table_at + (t * 4 + q) * 2:
                                 table_at + (t * 4 + q) * 2 + 2], "little")
             for q in range(4)] for t in range(count)]
    sub_bytes = tile_bytes // 4
    stream_off = table_at + count * 8
    pixels = _decode_units_flat(blob, stream_off, subs, sub_bytes, flags, lzss_tag_len=2)
    return {"tile_px": px, "count": count, "metatiled": True, "subtiles": subs,
            "palette_of": pal_idx, "defs": defs, "pixels": pixels,
            "sub_bytes": sub_bytes}


def parse_sprite(blob):
    h = parse_header(blob)
    w, hh, frames = h["a"], h["b"], h["c"]
    pal_idx = list(blob[HEADER: HEADER + frames])
    return {"w": w, "h": hh, "frames": frames, "palette_of": pal_idx,
            "pixels": blob[HEADER + pad4(frames):], "frame_bytes": w * hh // 2}


MAP_INDEX_MASK = 0x03FF
MAP_FLIP_X = 0x0400
MAP_FLIP_Y = 0x0800


def parse_map(blob, tile_count=0):
    """Map format v2: u16 cells, plus an optional per-map palette remap table.

    Cells are u16, not u8 -- ten bits of tile index, two flip bits, four reserved for a
    per-cell palette. This reader was left on the v1 u8 layout when the format changed,
    which made every rendered map past the first row garbage. `tile_count` comes from the
    atlas and sizes the optional palette table; without it a map that carries one is
    misread, so callers that have the atlas should pass it.
    """
    h = parse_header(blob)
    w, hh, warps = h["a"], h["b"], h["c"]

    n_over = int.from_bytes(blob[HEADER:HEADER + 2], "little")
    has_palette = blob[HEADER + 2]
    at = HEADER + 4

    pal_table = list(blob[at: at + tile_count]) if has_palette else []
    at += tile_count if has_palette else 0

    cells = [int.from_bytes(blob[at + i * 2: at + i * 2 + 2], "little")
             for i in range(w * hh)]
    at += w * hh * 2

    over = [(blob[at + i * 3], blob[at + i * 3 + 1], blob[at + i * 3 + 2])
            for i in range(n_over)]
    at += n_over * 3
    warp_list = [tuple(blob[at + i * 5: at + i * 5 + 5]) for i in range(warps)]

    return {"w": w, "h": hh,
            "tiles": [c & MAP_INDEX_MASK for c in cells],
            "flips": [((c & MAP_FLIP_X) != 0, (c & MAP_FLIP_Y) != 0) for c in cells],
            "tile_palette": pal_table,
            "overrides": over, "warps": warp_list}


# ----------------------------------------------------------------------------- font

def parse_font(blob):
    """PF: metrics, a glyph index, a codepoint map and a bitmap block.

    Parsed rather than re-rasterised so the editor's preview draws the exact bytes that
    would ship. Re-rasterising for preview is how a preview and a build drift apart.
    """
    h = parse_header(blob)
    depth, line_height, baseline = h["a"], h["b"], h["c"]

    count = int.from_bytes(blob[HEADER:HEADER + 2], "little")
    bitmap_bytes = int.from_bytes(blob[HEADER + 2:HEADER + 4], "little")
    first_cp, last_cp = blob[HEADER + 4], blob[HEADER + 5]
    fallback, space_advance = blob[HEADER + 6], blob[HEADER + 7]

    at = HEADER + 8
    glyphs = []
    for i in range(count):
        e = blob[at + i * 8: at + i * 8 + 8]
        glyphs.append({
            "offset": int.from_bytes(e[:2], "little"),
            "w": e[2], "h": e[3], "advance": e[4],
            # Bearings are signed bytes; a negative x bearing is ordinary in italics
            # and in some lowercase 'j'.
            "bearing_x": e[5] - 256 if e[5] > 127 else e[5],
            "bearing_y": e[6] - 256 if e[6] > 127 else e[6],
        })
    at += count * 8

    cp_map = list(blob[at: at + (last_cp - first_cp + 1)])
    at += last_cp - first_cp + 1

    return {"depth": depth, "line_height": line_height, "baseline": baseline,
            "glyph_count": count, "bitmap_bytes": bitmap_bytes,
            "first_cp": first_cp, "last_cp": last_cp,
            "fallback": fallback, "space_advance": space_advance,
            "glyphs": glyphs, "map": cp_map, "bitmaps": blob[at: at + bitmap_bytes]}


def font_glyph_index(font, ch):
    cp = ord(ch)
    if cp < font["first_cp"] or cp > font["last_cp"]:
        return font["fallback"]
    g = font["map"][cp - font["first_cp"]]
    return font["fallback"] if g == 0xFF else g


def glyph_levels(font, index):
    """A glyph's coverage levels as rows, 0..(2**depth - 1)."""
    g = font["glyphs"][index]
    if not g["w"]:
        return []
    depth = font["depth"]
    stride = (g["w"] * depth + 7) // 8
    mask = (1 << depth) - 1
    rows = []
    for j in range(g["h"]):
        base = g["offset"] + j * stride
        row = []
        for i in range(g["w"]):
            bit = i * depth
            byte = font["bitmaps"][base + (bit >> 3)]
            row.append((byte >> (8 - depth - (bit & 7))) & mask)
        rows.append(row)
    return rows


def font_text_width(font, s):
    return sum(font["glyphs"][font_glyph_index(font, c)]["advance"]
               for c in s if c != "\n")


def font_wrap(font, s, width):
    """Break `s` into lines at `width`.

    **Mirrors next_line() in src/pnx/gfx/pnx_text.c and must stay in step with it** --
    a preview that wraps differently from the runtime is worse than no preview, because
    it looks authoritative. The same cases are asserted on both sides: test_text.c for
    the C, test_assets.py for this.
    """
    lines, i, n = [], 0, len(s)
    while i < n:
        last_space, w, j = None, 0, i
        while j < n:
            if s[j] == "\n":
                break
            adv = font["glyphs"][font_glyph_index(font, s[j])]["advance"]
            if width > 0 and w + adv > width and j != i:
                break
            if s[j] == " ":
                last_space = j
            w += adv
            j += 1

        if j < n and s[j] == "\n":
            lines.append(s[i:j])
            i = j + 1
            continue
        if j >= n:
            lines.append(s[i:j])
            break

        end = last_space if last_space is not None else j
        nxt = (last_space + 1) if last_space is not None else j
        while nxt < n and s[nxt] == " ":
            nxt += 1
        lines.append(s[i:end])
        i = nxt
    return lines


# The 2bpp blend, mirroring s_blend_13 / s_blend_23 in pnx_text.c. Expressed as the
# formula the C tables were generated from, so the preview cannot drift from a typo.
def _blend_channel(ink, dst, k):
    return (ink * k + dst * (3 - k) + 1) // 3


def font_draw(img, font, s, x, baseline_y, rgb):
    """Draws `s` onto a PIL RGBA image at a baseline, exactly as the blitter would.

    Levels below full coverage blend against what is already in the image, which is why
    the preview has to composite over the real background rather than draw text on a
    swatch and trust it.
    """
    put = img.load()
    pen = x
    ink = tuple(min(3, c // 85) for c in rgb[:3])

    for ch in s:
        if ch == "\n":
            break
        gi = font_glyph_index(font, ch)
        g = font["glyphs"][gi]
        rows = glyph_levels(font, gi)
        top = baseline_y - g["bearing_y"]

        for j, row in enumerate(rows):
            py = top + j
            if not 0 <= py < img.height:
                continue
            for i, level in enumerate(row):
                if level == 0:
                    continue
                px_x = pen + g["bearing_x"] + i
                if not 0 <= px_x < img.width:
                    continue
                if level == (1 << font["depth"]) - 1:
                    put[px_x, py] = rgb + (255,)
                    continue
                dst = put[px_x, py]
                dst_q = tuple(min(3, c // 85) for c in dst[:3])
                k = level                     # depth 2: level 1 -> k=1, level 2 -> k=2
                blended = tuple(_blend_channel(ink[c], dst_q[c], k) * 85
                                for c in range(3))
                put[px_x, py] = blended + (255,)

        pen += g["advance"]
    return pen - x


def font_draw_wrapped(img, font, s, x, baseline_y, width, rgb, align="left"):
    """Word-wrapped draw. Returns the number of lines drawn."""
    lines = font_wrap(font, s, width)
    for n, line in enumerate(lines):
        lx = x
        if align != "left":
            lw = font_text_width(font, line)
            lx += (width - lw) // 2 if align == "center" else (width - lw)
        font_draw(img, font, line, lx, baseline_y + n * font["line_height"], rgb)
    return len(lines)


def font_sheet(font, scale=3, cols=16):
    """The glyph atlas as a labelled grid -- the view that shows a broken glyph."""
    cell_w = max(8, max((g["advance"] for g in font["glyphs"]), default=8) + 2)
    cell_h = font["line_height"] + 2
    rows = (font["glyph_count"] + cols - 1) // cols

    img = Image.new("RGBA", (cols * cell_w, rows * cell_h), (0, 0, 0, 0))
    for i in range(font["glyph_count"]):
        ox, oy = (i % cols) * cell_w, (i // cols) * cell_h
        cell = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
        _draw_glyph_into(cell, font, i, 1, font["baseline"], (255, 255, 255))
        img.paste(cell, (ox, oy))

    return img.resize((img.width * scale, img.height * scale), Image.NEAREST)


def _draw_glyph_into(img, font, index, x, baseline_y, rgb):
    put = img.load()
    g = font["glyphs"][index]
    for j, row in enumerate(glyph_levels(font, index)):
        py = baseline_y - g["bearing_y"] + j
        if not 0 <= py < img.height:
            continue
        for i, level in enumerate(row):
            px_x = x + g["bearing_x"] + i
            if level and 0 <= px_x < img.width:
                scale = level / ((1 << font["depth"]) - 1)
                put[px_x, py] = tuple(int(c * scale) for c in rgb) + (255,)


# ------------------------------------------------------------------------ rendering

def unpack_4bpp(data, offset, count, palette):
    """Returns a list of RGB tuples or None, straight from the shipped bytes."""
    out = []
    for i in range(count):
        b = data[offset + (i >> 1)]
        idx = (b >> 4) if (i % 2 == 0) else (b & 0x0F)
        out.append(gcolor_rgb(palette[idx]) if idx else None)
    return out


def tile_image(atlas, palettes, index, scale=1):
    px = atlas["tile_px"]
    pal = palettes[atlas["palette_of"][index]]
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    put = img.load()

    if not atlas["metatiled"]:
        vals = unpack_4bpp(atlas["pixels"], index * atlas["tile_bytes"], px * px, pal)
        for j in range(px):
            for i in range(px):
                c = vals[j * px + i]
                if c: put[i, j] = c + (255,)
    else:
        half = px // 2
        for q, sub in enumerate(atlas["defs"][index]):
            ox, oy = (q % 2) * half, (q // 2) * half
            vals = unpack_4bpp(atlas["pixels"], sub * atlas["sub_bytes"],
                               half * half, pal)
            for j in range(half):
                for i in range(half):
                    c = vals[j * half + i]
                    if c: put[ox + i, oy + j] = c + (255,)

    if scale > 1:
        img = img.resize((px * scale, px * scale), Image.NEAREST)
    return img


def sprite_image(sp, palettes, frame, scale=1):
    pal = palettes[sp["palette_of"][frame]]
    img = Image.new("RGBA", (sp["w"], sp["h"]), (0, 0, 0, 0))
    put = img.load()
    vals = unpack_4bpp(sp["pixels"], frame * sp["frame_bytes"], sp["w"] * sp["h"], pal)
    for j in range(sp["h"]):
        for i in range(sp["w"]):
            c = vals[j * sp["w"] + i]
            if c: put[i, j] = c + (255,)
    if scale > 1:
        img = img.resize((sp["w"] * scale, sp["h"] * scale), Image.NEAREST)
    return img


def map_image(mp, atlas, palettes, scale=1):
    px = atlas["tile_px"]
    img = Image.new("RGBA", (mp["w"] * px, mp["h"] * px), (0, 0, 0, 255))
    for ty in range(mp["h"]):
        for tx in range(mp["w"]):
            img.paste(tile_image(atlas, palettes, mp["tiles"][ty * mp["w"] + tx]),
                      (tx * px, ty * px))
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    return img


def data_uri(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

# ---------------------------------------------------------------------------- page

# Design: a hardware datasheet, not a web page. The accent #55AAFF is not chosen for
# taste -- it is an actual ARGB2222 value the device can display (r=1,g=2,b=3 of 3), so
# the page is literally drawn in the palette it documents. Grounds are cool and unlit,
# like the LCD before backlight. Headings are tracked monospace in the register of a
# component datasheet; figures are tabular so columns of bytes line up.

CSS = """
:root{
  --ink:#eceff4; --surface:#ffffff; --line:#d3dae4; --fg:#10141b; --dim:#5c6878;
  --accent:#1f6dbf; --accent-soft:#e3edf9; --warn:#b4541c;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ink:#0d1017; --surface:#161a23; --line:#262c38; --fg:#dde3ec; --dim:#7b8798;
  --accent:#55aaff; --accent-soft:#16283d; --warn:#e0913f;
}}
:root[data-theme="dark"]{
  --ink:#0d1017; --surface:#161a23; --line:#262c38; --fg:#dde3ec; --dim:#7b8798;
  --accent:#55aaff; --accent-soft:#16283d; --warn:#e0913f;
}

*{box-sizing:border-box}
body{
  margin:0; padding:2.5rem 1.25rem 5rem; background:var(--ink); color:var(--fg);
  font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
}
main{max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:2.25rem}

.mono{font-family:ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Menlo,monospace}

header h1{
  font:600 1.5rem/1.2 ui-monospace,"SF Mono","Cascadia Mono",Menlo,monospace;
  margin:0 0 .4rem; letter-spacing:-.01em; text-wrap:balance;
}
header p{margin:0;color:var(--dim);max-width:62ch}

/* Summary before detail: the figures that decide whether anything is wrong. */
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-radius:6px;
  overflow:hidden}
.stat{background:var(--surface);padding:.85rem 1rem}
.stat b{display:block;font:600 1.35rem/1.1 ui-monospace,Menlo,monospace;
  font-variant-numeric:tabular-nums}
.stat span{display:block;margin-top:.2rem;font-size:.72rem;letter-spacing:.09em;
  text-transform:uppercase;color:var(--dim)}

section{display:flex;flex-direction:column;gap:.75rem}
h2{
  margin:0; font:600 .78rem/1.4 ui-monospace,Menlo,monospace;
  letter-spacing:.14em; text-transform:uppercase; color:var(--dim);
  display:flex; align-items:baseline; gap:.6rem;
}
h2::after{content:"";flex:1;height:1px;background:var(--line)}
h2 em{font-style:normal;color:var(--accent);letter-spacing:.04em}

.plate{background:var(--surface);border:1px solid var(--line);border-radius:6px;
  padding:1rem}
.grid{display:flex;flex-wrap:wrap;gap:.55rem}

.thumb{display:flex;flex-direction:column;align-items:center;gap:.25rem}
.thumb img{display:block;image-rendering:pixelated;border-radius:2px;
  outline:1px solid var(--line);
  background:repeating-conic-gradient(from 0deg,#8886 0 25%,transparent 0 50%)
    0 0/10px 10px}
.thumb span{font:10px/1 ui-monospace,Menlo,monospace;color:var(--dim);
  font-variant-numeric:tabular-nums}

.pal{display:flex;flex-wrap:wrap;gap:3px;align-items:center}
.pal + .pal{margin-top:.6rem}
.slot{font:10px/1 ui-monospace,Menlo,monospace;color:var(--dim);width:3.2rem;
  letter-spacing:.06em;text-transform:uppercase}
.sw{width:24px;height:24px;border-radius:2px;outline:1px solid var(--line)}
.sw.clear{background:repeating-conic-gradient(from 0deg,#8886 0 25%,transparent 0 50%)
  0 0/8px 8px}

.map{overflow-x:auto}
.map img{image-rendering:pixelated;display:block;border-radius:3px;
  outline:1px solid var(--line)}

table{border-collapse:collapse;width:100%;font-size:.86rem}
th,td{text-align:left;padding:.45rem .7rem;border-bottom:1px solid var(--line)}
th{font:600 .68rem/1.4 ui-monospace,Menlo,monospace;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim)}
td.n,th.n{text-align:right;font-family:ui-monospace,Menlo,monospace;
  font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
tfoot td{border-top:1px solid var(--line);border-bottom:none;font-weight:600}

.meter{height:6px;background:var(--line);border-radius:3px;overflow:hidden;
  margin-top:.9rem}
.meter i{display:block;height:100%;background:var(--accent)}

.note{margin:0;color:var(--dim);font-size:.84rem;max-width:70ch}
.plate .note{margin-top:.85rem}
code{font-family:ui-monospace,Menlo,monospace;font-size:.92em;
  background:var(--accent-soft);color:var(--accent);padding:.1em .4em;border-radius:3px}
.scroll{overflow-x:auto}
"""


def build_report(manifest_path, out_path):
    root = os.path.dirname(os.path.abspath(manifest_path))
    with open(manifest_path, "rb") as f:
        man = tomllib.load(f)

    project = man.get("project", {})
    res = os.path.join(root, project.get("resources", "resources"))
    budget = int(project.get("budget_bytes", 256 * 1024))

    palettes = parse_palettes(read(os.path.join(res, "palettes.bin")))

    sizes = {fn: os.path.getsize(os.path.join(res, fn))
             for fn in sorted(os.listdir(res)) if fn.endswith(".bin")}
    total = sum(sizes.values())

    atlases, atlas_specs = {}, man.get("atlas", [])
    for spec in atlas_specs:
        atlases[spec["name"]] = parse_atlas(read(os.path.join(res, spec["out"])))
    tile_total = sum(a["count"] for a in atlases.values())

    H = ['<main>', '<header>',
         f'<h1>{project.get("name", "pebblnyx")} &mdash; asset preview</h1>',
         '<p>Decoded from the built blobs, not the source art. Quantisation to the '
         'device\u2019s 64 colours, palette merging and metatile composition have '
         'already happened, so this is what the watch draws.</p></header>']

    # Summary first: the figures that say whether anything is wrong.
    H.append('<div class="summary">')
    for value, label in [
        (f"{total:,}", "content bytes"),
        (f"{100 * total / budget:.1f}%", f"of {budget // 1024}KB budget"),
        (f"{tile_total}", "unique tiles"),
        (f"{len(palettes)}", "palettes"),
        (f"{len(man.get('map', []))}", "maps"),
        (f"{len(man.get('scene', {}))}", "scenes"),
    ]:
        H.append(f'<div class="stat"><b>{value}</b><span>{label}</span></div>')
    H.append('</div>')

    # --- palettes
    H.append(f'<section><h2>Palettes <em>{len(palettes)} shared</em></h2>'
             f'<div class="plate">')
    for i, p in enumerate(palettes):
        H.append(f'<div class="pal"><span class="slot">slot {i}</span>')
        for j, c in enumerate(p):
            rgb = gcolor_rgb(c)
            if rgb is None:
                H.append('<div class="sw clear" title="index 0 &mdash; transparent">'
                         '</div>')
            else:
                H.append(f'<div class="sw" style="background:rgb{rgb}" '
                         f'title="index {j} &mdash; 0x{c:02X}"></div>')
        H.append('</div>')
    H.append('<p class="note">Index 0 is transparent in every palette, following the '
             'SNES convention. It costs a slot but lets the blitter reject a pixel '
             'before it reads the table.</p></div></section>')

    # --- atlases
    for spec in atlas_specs:
        a = atlases[spec["name"]]
        layout = (f'metatiled, {a["subtiles"]} quadrants'
                  if a["metatiled"] else "flat")
        H.append(f'<section><h2>Atlas &ldquo;{spec["name"]}&rdquo; '
                 f'<em>{a["count"]} tiles &middot; {a["tile_px"]}px &middot; {layout}'
                 f'</em></h2><div class="plate"><div class="grid">')
        for i in range(a["count"]):
            H.append(f'<div class="thumb">'
                     f'<img src="{data_uri(tile_image(a, palettes, i, 2))}" '
                     f'width="{a["tile_px"] * 2}" alt="tile {i}">'
                     f'<span>{i}</span></div>')
        H.append('</div></div></section>')

    # --- sprites
    for spec in man.get("sprite", []):
        sp = parse_sprite(read(os.path.join(res, spec["out"])))
        H.append(f'<section><h2>Sprite &ldquo;{spec["name"]}&rdquo; '
                 f'<em>{sp["frames"]} frames &middot; {sp["w"]}&times;{sp["h"]}</em>'
                 f'</h2><div class="plate"><div class="grid">')
        for i in range(sp["frames"]):
            H.append(f'<div class="thumb">'
                     f'<img src="{data_uri(sprite_image(sp, palettes, i, 3))}" '
                     f'width="{sp["w"] * 3}" alt="frame {i}"><span>{i}</span></div>')
        H.append('</div></div></section>')

    # --- maps, drawn with their real tiles
    first_atlas = next(iter(atlases.values()), None)
    for spec in man.get("map", []):
        mp = parse_map(read(os.path.join(res, spec["out"])),
                       first_atlas["count"] if first_atlas else 0)
        warps = ", ".join(f"({w[0]},{w[1]}) &rarr; map {w[2]} at ({w[3]},{w[4]})"
                          for w in mp["warps"]) or "none"
        H.append(f'<section><h2>Map &ldquo;{spec["name"]}&rdquo; '
                 f'<em>{mp["w"]}&times;{mp["h"]} tiles</em></h2>'
                 f'<div class="plate"><div class="map">')
        if first_atlas:
            H.append(f'<img src="{data_uri(map_image(mp, first_atlas, palettes))}" '
                     f'alt="{spec["name"]} rendered">')
        H.append(f'</div><p class="note">Warps: {warps}. '
                 f'{len(mp["overrides"])} cell(s) override their tile\u2019s flags &mdash; '
                 f'every other cell inherits from the tileset, which is what makes maps '
                 f'half the size they were.</p></div></section>')

    # --- budget
    H.append('<section><h2>Resource budget</h2><div class="plate">'
             '<div class="scroll"><table><thead><tr><th>blob</th>'
             '<th class="n">bytes</th><th class="n">share</th></tr></thead><tbody>')
    for fn, n in sorted(sizes.items(), key=lambda r: -r[1]):
        H.append(f'<tr><td><code>{fn}</code></td><td class="n">{n:,}</td>'
                 f'<td class="n">{100 * n / total:.0f}%</td></tr>')
    H.append(f'</tbody><tfoot><tr><td>total</td><td class="n">{total:,}</td>'
             f'<td class="n">{100 * total / budget:.1f}%</td></tr></tfoot></table></div>'
             f'<div class="meter"><i style="width:'
             f'{min(100, 100 * total / budget):.1f}%"></i></div>'
             f'<p class="note">The 256KB ceiling is the appstore limit; the device '
             f'itself allows 1MB. Shipping is the constraint that binds.</p>'
             f'</div></section>')

    H.append('</main>')

    with open(out_path, "w") as f:
        f.write(f'<title>{project.get("name", "pebblnyx")} asset preview</title>'
                f'<style>{CSS}</style>' + "\n".join(H))
    return out_path, total


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest")
    ap.add_argument("-o", "--out", default="preview.html")
    args = ap.parse_args()

    path, total = build_report(args.manifest, args.out)
    print(f"wrote {path} ({total:,} B of content rendered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
