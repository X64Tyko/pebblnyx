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


def parse_palettes(blob):
    h = parse_header(blob)
    n = h["a"]
    return [list(blob[HEADER + i * 16: HEADER + (i + 1) * 16]) for i in range(n)]


def parse_atlas(blob):
    h = parse_header(blob)
    px, count, layout = h["a"], h["b"], h["c"]
    tile_bytes = px * px // 2
    body = HEADER

    if layout == 0:
        pal_idx = list(blob[body: body + count])
        pixels = blob[body + pad4(count) * 2:]
        return {"tile_px": px, "count": count, "metatiled": False,
                "palette_of": pal_idx, "pixels": pixels, "tile_bytes": tile_bytes}

    subs = int.from_bytes(blob[body:body + 2], "little")
    body += 4
    pal_idx = list(blob[body: body + count])
    table_at = body + pad4(count) * 2
    defs = [[int.from_bytes(blob[table_at + (t * 4 + q) * 2:
                                 table_at + (t * 4 + q) * 2 + 2], "little")
             for q in range(4)] for t in range(count)]
    pixels = blob[table_at + count * 8:]
    return {"tile_px": px, "count": count, "metatiled": True, "subtiles": subs,
            "palette_of": pal_idx, "defs": defs, "pixels": pixels,
            "sub_bytes": tile_bytes // 4}


def parse_sprite(blob):
    h = parse_header(blob)
    w, hh, frames = h["a"], h["b"], h["c"]
    pal_idx = list(blob[HEADER: HEADER + frames])
    return {"w": w, "h": hh, "frames": frames, "palette_of": pal_idx,
            "pixels": blob[HEADER + pad4(frames):], "frame_bytes": w * hh // 2}


def parse_map(blob):
    h = parse_header(blob)
    w, hh, warps = h["a"], h["b"], h["c"]
    n_over = int.from_bytes(blob[HEADER:HEADER + 2], "little")
    at = HEADER + 4
    tiles = list(blob[at: at + w * hh])
    at += w * hh
    over = [(blob[at + i * 3], blob[at + i * 3 + 1], blob[at + i * 3 + 2])
            for i in range(n_over)]
    at += n_over * 3
    warp_list = [tuple(blob[at + i * 5: at + i * 5 + 5]) for i in range(warps)]
    return {"w": w, "h": hh, "tiles": tiles, "overrides": over, "warps": warp_list}


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
        mp = parse_map(read(os.path.join(res, spec["out"])))
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
