#!/usr/bin/env python3
"""pebblnyx editor: a local app for building maps, managing assets and packaging.

Runs a small server on localhost and opens a browser. Nothing leaves the machine and
there is nothing to install beyond what the pipeline already needs.

The design rule throughout: **the manifest is the source of truth, and the editor is
just a hand on it.** Saving performs a surgical edit of `assets.toml` -- the block being
changed and nothing else -- so comments, ordering and hand-written sections survive.
An editor that rewrites the whole file would quietly delete the reasoning people leave
in their manifests, which is most of what makes them readable.

Maps are painted in **legend characters**, not tile indices, because that is what a map
actually stores. Painting indices would let you place a tile the legend cannot express,
and the file could then no longer round-trip.

Usage:
    tools/pnx_editor.py <manifest.toml> [--port 8765] [--no-browser]
"""

import argparse
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import tomllib
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pnx_preview as pv                                    # noqa: E402
import pnx_assets as pa                                     # noqa: E402

TOOLS = os.path.dirname(os.path.abspath(__file__))


class Project:
    """Everything the editor knows, reloaded from disk on demand."""

    def __init__(self, manifest_path):
        self.path = os.path.abspath(manifest_path)
        self.root = os.path.dirname(self.path)
        self.reload()

    def reload(self):
        with open(self.path, "rb") as f:
            self.man = tomllib.load(f)
        self.project = self.man.get("project", {})
        self.res = os.path.join(self.root, self.project.get("resources", "resources"))
        self.built = os.path.isdir(self.res) and \
            os.path.exists(os.path.join(self.res, "palettes.bin"))

    # ---------------------------------------------------------------- rendering

    def atlases(self):
        """Every atlas, with its own role table and rendered tiles.

        Roles are per atlas now, so the editor must resolve a legend character through
        the atlas the map actually uses. Reading the generated header rather than
        re-deriving keeps the editor and the build from ever disagreeing about which
        tile a role means.
        """
        if not self.built:
            return []

        palettes = pv.parse_palettes(pv.read(os.path.join(self.res, "palettes.bin")))
        header_path = os.path.join(self.root,
                                   self.project.get("header", "src/c/assets_gen.h"))
        text = open(header_path).read() if os.path.exists(header_path) else ""

        out = []
        for spec in self.man.get("atlas", []):
            name = spec["name"]
            blob = os.path.join(self.res, spec["out"])
            if not os.path.exists(blob):
                continue
            atlas = pv.parse_atlas(pv.read(blob))

            prefix = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
            roles = {m.group(1).lower(): int(m.group(2)) for m in
                     re.finditer(rf"#define {prefix}_TILE_(\w+) (\d+)", text)}
            # Drop the geometry defines the same prefix produces.
            for k in ("px", "bytes", "count"):
                roles.pop(k, None)

            out.append({
                "name": name,
                "count": atlas["count"],
                "roles": roles,
                "tiles": [pv.data_uri(pv.tile_image(atlas, palettes, i, 2))
                          for i in range(atlas["count"])],
            })
        return out

    def palette_swatches(self):
        if not self.built:
            return []
        pals = pv.parse_palettes(pv.read(os.path.join(self.res, "palettes.bin")))
        return [[("transparent" if pv.gcolor_rgb(c) is None
                  else "#%02x%02x%02x" % pv.gcolor_rgb(c)) for c in p] for p in pals]

    def maps(self):
        specs = self.man.get("atlas", [])
        default_atlas = specs[0]["name"] if specs else None
        out = []
        for m in self.man.get("map", []):
            rows = [r for r in m["rows"].strip("\n").split("\n") if r.strip()]
            out.append({"name": m["name"], "rows": rows,
                        "start": m.get("start", [1, 1]),
                        "warps": m.get("warps", []),
                        "atlas": m.get("atlas", default_atlas)})
        return out

    def state(self):
        legend = {ch: {"tile": e["tile"], "flags": e.get("flags", [])}
                  for ch, e in self.man.get("legend", {}).items()}
        return {
            "name": self.project.get("name", "project"),
            "built": self.built,
            "legend": legend,
            "atlases": self.atlases(),
            "palettes": self.palette_swatches(),
            "maps": self.maps(),
            "scenes": list(self.man.get("scene", {})),
            "budget": self.project.get("budget_bytes", 262144),
            "used": sum(os.path.getsize(os.path.join(self.res, f))
                        for f in os.listdir(self.res) if f.endswith(".bin"))
            if self.built else 0,
        }

    # ----------------------------------------------------------------- importing

    def sheets(self):
        """PNGs reachable from the project, so importing does not need a file dialog."""
        seen, out = set(), []
        roots = [self.root, os.path.dirname(self.root),
                 os.path.dirname(os.path.dirname(self.root))]
        for base in roots:
            if not os.path.isdir(base):
                continue
            for dirpath, dirnames, files in os.walk(base):
                dirnames[:] = [d for d in dirnames
                               if d not in ("build", ".git", "resources", "__pycache__")]
                if dirpath.count(os.sep) - base.count(os.sep) > 2:
                    dirnames[:] = []
                for fn in files:
                    if not fn.lower().endswith(".png"):
                        continue
                    full = os.path.realpath(os.path.join(dirpath, fn))
                    if full in seen:
                        continue
                    seen.add(full)
                    out.append({"path": os.path.relpath(full, self.root),
                                "name": fn})
            if len(out) > 60:
                break
        return sorted(out, key=lambda s: s["name"])[:60]

    def analyse(self, rel, tile, region, max_tiles, colorkey):
        """Price a candidate carve before it is committed.

        This is the number that decides a project's content budget, and it is invisible
        until something is built -- so the editor computes it live. Region selection is
        where the budget is won: five complete tilesets are 111% of the appstore limit,
        while 128 tiles from each is 32%.
        """
        from PIL import Image
        path = os.path.join(self.root, rel)
        im = Image.open(path).convert("RGBA")
        px = im.load()
        W, H = im.size
        rx, ry, rw, rh = region

        rw = max(1, min(rw, W // tile - rx))
        rh = max(1, min(rh, H // tile - ry))

        unique, seen, empty, repaired = [], set(), 0, 0
        for ty in range(ry, ry + rh):
            for tx in range(rx, rx + rw):
                buf = tuple(pa.to_gcolor8(px[tx * tile + i, ty * tile + j], colorkey)
                            for j in range(tile) for i in range(tile))
                if not any(buf):
                    empty += 1
                    continue
                if buf in seen or len(unique) >= max_tiles:
                    continue
                seen.add(buf)
                fixed, merged = pa.reduce_colours(buf)
                if merged:
                    repaired += 1
                unique.append(fixed)

        sets = [frozenset(c for c in t if c) for t in unique]
        pals = pa.merge_palettes(sets)[0] if sets else []
        colours = set().union(*sets) if sets else set()

        tile_bytes = tile * tile // 2
        est = len(unique) * tile_bytes + len(pals) * 16 + pa.HEADER_BYTES

        # Render the kept tiles so the carve is visually checkable, not just numeric.
        strip = None
        if unique:
            cols = min(16, len(unique))
            rows = (len(unique) + cols - 1) // cols
            grid = Image.new("RGBA", (cols * tile, rows * tile), (0, 0, 0, 0))
            for i, t in enumerate(unique):
                cell = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
                cp = cell.load()
                for j in range(tile):
                    for k in range(tile):
                        rgb = pv.gcolor_rgb(t[j * tile + k])
                        if rgb:
                            cp[k, j] = rgb + (255,)
                grid.paste(cell, ((i % cols) * tile, (i // cols) * tile))
            grid = grid.resize((grid.width * 2, grid.height * 2), Image.NEAREST)
            strip = pv.data_uri(grid)

        return {
            "sheet_tiles": [W // tile, H // tile],
            "considered": rw * rh, "empty": empty,
            "unique": len(unique), "capped": len(unique) >= max_tiles,
            "colours": len(colours), "palettes": len(pals), "repaired": repaired,
            "bytes": est, "pct": 100.0 * est / self.project.get("budget_bytes", 262144),
            "strip": strip,
            "thumb": pv.data_uri(im.resize((min(W, 480), int(H * min(W, 480) / W)),
                                           Image.NEAREST)),
        }

    def add_atlas(self, name, rel, tile, region, max_tiles):
        """Append an [[atlas]] block. Appending keeps every existing comment intact."""
        if any(a["name"] == name for a in self.man.get("atlas", [])):
            raise ValueError(f"an atlas named {name!r} already exists")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")

        block = f'''

[[atlas]]
name = "{name}"
sheet = "{rel}"
tile = {tile}
# In TILE units, not pixels.
region = [{region[0]}, {region[1]}, {region[2]}, {region[3]}]
max_tiles = {max_tiles}
out = "{name}.bin"
metatiles = "auto"
# Roles the legend can name. Autopicked so the atlas is usable the moment it is
# imported; replace with an explicit [atlas.semantic] table once you know which tiles
# you want, and no map or game code changes.
autopick = ["floor", "wall", "accent"]
'''
        with open(self.path, "a") as f:
            f.write(block)
        self.reload()

    def legend_chars(self):
        """Pick sensible default characters for a blank map's floor and walls.

        Derived from the legend's flags rather than assuming '.' and '#', so a project
        with its own legend gets a blank map it can actually paint on.
        """
        floor = wall = None
        for ch, e in self.man.get("legend", {}).items():
            flags = e.get("flags", [])
            if "solid" in flags and wall is None:
                wall = ch
            elif not flags and floor is None:
                floor = ch
        return floor, wall

    def add_map(self, name, w, h, atlas, with_scene=True):
        """Append a [[map]] block holding a walled, empty room, plus a scene for it.

        A map with no scene cannot be loaded, so creating one without the other would
        produce content the game has no way to reach -- the same class of dead end the
        pipeline's flood-fill check exists to catch.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")
        if any(m["name"] == name for m in self.man.get("map", [])):
            raise ValueError(f"a map named {name!r} already exists")
        if not (3 <= w <= 255 and 3 <= h <= 255):
            raise ValueError("width and height must be between 3 and 255")

        floor, wall = self.legend_chars()
        if not floor or not wall:
            raise ValueError("the legend needs a walkable tile and a solid tile")

        rows = [wall * w]
        for _ in range(h - 2):
            rows.append(wall + floor * (w - 2) + wall)
        rows.append(wall * w)

        body = "\n".join(rows)
        block = f'''

[[map]]
name = "{name}"
atlas = "{atlas}"
out = "map_{name}.bin"
start = [1, 1]
warps = []
rows = """
{body}
"""
'''
        if with_scene:
            block += f'''
[scene.{name}]
map = "{name}"
atlases = ["{atlas}"]
'''
        with open(self.path, "a") as f:
            f.write(block)
        self.reload()

    # ------------------------------------------------------------------- saving

    def save_map(self, name, rows, start, warps, atlas=None):
        """Rewrite one map's rows/start/warps in place, touching nothing else.

        Located by scanning `[[map]]` blocks for the matching name rather than by
        rewriting the parsed document, so every comment in the file survives -- which
        matters more here than elsewhere, because manifests carry the reasoning behind
        the content.
        """
        text = open(self.path).read()

        blocks = [m.start() for m in re.finditer(r"^\[\[map\]\]", text, re.M)]
        if not blocks:
            raise ValueError("no [[map]] blocks in the manifest")
        blocks.append(len(text))

        for i in range(len(blocks) - 1):
            start_i, end_i = blocks[i], blocks[i + 1]
            chunk = text[start_i:end_i]
            if not re.search(rf'^name\s*=\s*"{re.escape(name)}"', chunk, re.M):
                continue

            body = "\n".join(rows)
            chunk = re.sub(r'rows\s*=\s*""".*?"""',
                           f'rows = """\n{body}\n"""', chunk, flags=re.S)
            chunk = re.sub(r"^start\s*=\s*\[.*?\]$",
                           f"start = [{start[0]}, {start[1]}]", chunk, flags=re.M)

            if atlas:
                if re.search(r"^atlas\s*=", chunk, re.M):
                    chunk = re.sub(r'^atlas\s*=.*$', f'atlas = "{atlas}"', chunk,
                                   flags=re.M)
                else:
                    chunk = re.sub(r'(^name\s*=.*$)', rf'\1\natlas = "{atlas}"',
                                   chunk, count=1, flags=re.M)

            warp_src = ", ".join(
                '{{ at = [{}, {}], to = ["{}", {}, {}] }}'.format(
                    w["at"][0], w["at"][1], w["to"][0], w["to"][1], w["to"][2])
                for w in warps)
            if re.search(r"^warps\s*=", chunk, re.M):
                chunk = re.sub(r"^warps\s*=\s*\[.*?\]$", f"warps = [{warp_src}]",
                               chunk, flags=re.M | re.S)
            elif warp_src:
                chunk = re.sub(r"(^start\s*=.*$)", rf"\1\nwarps = [{warp_src}]",
                               chunk, count=1, flags=re.M)

            text = text[:start_i] + chunk + text[end_i:]
            with open(self.path, "w") as f:
                f.write(text)
            self.reload()
            return

        raise ValueError(f"no map named {name!r}")

    def build(self):
        """Runs the real pipeline -- the editor never writes a blob itself.

        Anything the editor produced that the pipeline would reject must fail here,
        loudly, rather than being smoothed over. The validation is the product.
        """
        pkg = os.path.join(self.root, "package.json")
        cmd = [sys.executable, os.path.join(TOOLS, "pnx_assets.py"), self.path]
        if os.path.exists(pkg):
            cmd += ["--package", pkg]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=self.root)
        self.reload()
        return {"ok": r.returncode == 0,
                "output": (r.stdout or "") + (r.stderr or "")}

# ------------------------------------------------------------------------- server

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>pebblnyx editor</title><style>
:root{--ink:#eceff4;--surface:#fff;--line:#d3dae4;--fg:#10141b;--dim:#5c6878;
  --accent:#1f6dbf;--soft:#e3edf9;--ok:#2c7a4b;--bad:#b4351c}
@media(prefers-color-scheme:dark){:root{--ink:#0d1017;--surface:#161a23;--line:#262c38;
  --fg:#dde3ec;--dim:#7b8798;--accent:#55aaff;--soft:#16283d;--ok:#5fd28d;--bad:#ff7a5c}}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;background:var(--ink);
  color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
header{display:flex;align-items:center;gap:1rem;padding:.6rem 1rem;
  border-bottom:1px solid var(--line);background:var(--surface)}
h1{margin:0;font:600 .78rem/1 ui-monospace,Menlo,monospace;letter-spacing:.14em;
  text-transform:uppercase;color:var(--dim)}
button{font:inherit;padding:.35rem .8rem;border:1px solid var(--line);border-radius:5px;
  background:var(--surface);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
select{font:inherit;padding:.3rem .5rem;background:var(--surface);color:var(--fg);
  border:1px solid var(--line);border-radius:5px}
main{flex:1;display:flex;min-height:0}
aside{width:250px;border-right:1px solid var(--line);background:var(--surface);
  overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:1.25rem}
section h2{margin:0 0 .5rem;font:600 .68rem/1 ui-monospace,Menlo,monospace;
  letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
#stage{flex:1;overflow:auto;padding:1.5rem;display:flex;align-items:flex-start;
  justify-content:center}
canvas{image-rendering:pixelated;cursor:crosshair;border:1px solid var(--line);
  border-radius:3px}
.tiles{display:flex;flex-wrap:wrap;gap:4px}
.tile{border:2px solid transparent;border-radius:4px;padding:1px;cursor:pointer;
  background:none;line-height:0}
.tile img{width:32px;height:32px;image-rendering:pixelated;display:block}
.tile.sel{border-color:var(--accent)}
.tile b{display:block;font:9px ui-monospace,monospace;color:var(--dim);text-align:center}
.pal{display:flex;gap:2px;flex-wrap:wrap;margin-bottom:6px}
.sw{width:18px;height:18px;border-radius:2px;outline:1px solid var(--line)}
.sw.clear{background:repeating-conic-gradient(from 0deg,#8886 0 25%,transparent 0 50%)
  0 0/6px 6px}
.row{display:flex;gap:.4rem;align-items:center}
.meter{height:5px;background:var(--line);border-radius:3px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--accent)}
small{color:var(--dim);font-size:.78rem}
#log{max-height:150px;overflow:auto;white-space:pre-wrap;font:11px/1.45 ui-monospace,
  Menlo,monospace;background:var(--ink);border:1px solid var(--line);border-radius:5px;
  padding:.5rem;color:var(--dim)}
#log.bad{color:var(--bad);border-color:var(--bad)}
#log.ok{color:var(--ok)}
kbd{font:11px ui-monospace,monospace;background:var(--soft);color:var(--accent);
  padding:.05em .35em;border-radius:3px}
.mini{display:flex;flex-wrap:wrap;gap:.3rem;align-items:center;margin-top:.5rem}
.mini input{font:inherit;width:4rem;padding:.25rem .4rem;background:var(--surface);
  color:var(--fg);border:1px solid var(--line);border-radius:4px}
.mini input:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.mini button{padding:.25rem .6rem}
.warp{display:flex;align-items:center;gap:.35rem;font:11px ui-monospace,Menlo,monospace;
  color:var(--dim);padding:.2rem 0;border-bottom:1px solid var(--line)}
.warp b{color:var(--fg);font-weight:600}
.warp button{padding:0 .35rem;line-height:1.3;margin-left:auto}
.imp{max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:1rem}
.fields{display:flex;flex-wrap:wrap;gap:.6rem;align-items:flex-end}
.fields label{display:flex;flex-direction:column;gap:.2rem;
  font:600 .66rem/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim)}
.fields input{font:inherit;width:5.5rem;padding:.3rem .45rem;background:var(--surface);
  color:var(--fg);border:1px solid var(--line);border-radius:5px}
.fields input:focus-visible,select:focus-visible{outline:2px solid var(--accent);
  outline-offset:1px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.stats div{background:var(--surface);padding:.6rem .75rem}
.stats b{display:block;font:600 1.15rem/1.1 ui-monospace,Menlo,monospace;
  font-variant-numeric:tabular-nums}
.stats span{font-size:.66rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--dim)}
.stats div.warn b{color:var(--bad)}
.plate{background:var(--surface);border:1px solid var(--line);border-radius:6px;
  padding:.85rem;overflow:auto}
.plate h3{margin:0 0 .5rem;font:600 .66rem/1 ui-monospace,Menlo,monospace;
  letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
.plate img{image-rendering:pixelated;display:block;max-width:100%}
</style></head><body>
<header>
  <h1>pebblnyx editor</h1>
  <button id="tabmaps" class="primary">Maps</button>
  <button id="tabimport">Import</button>
  <select id="mapsel"></select>
  <select id="atlassel" title="tileset this map is drawn with"></select>
  <span id="tool"></span>
  <div style="flex:1"></div>
  <span id="dirty"></span>
  <button id="save">Save map</button>
  <button id="build" class="primary">Build</button>
</header>
<main>
  <aside>
    <section><h2>Paint</h2><div class="tiles" id="legend"></div>
      <small id="painthint">Click to paint. <kbd>W</kbd> sets a warp, <kbd>S</kbd> the
      start.</small>
    </section>
    <section><h2>Map</h2><div id="mapinfo"><small>—</small></div>
      <div class="mini">
        <input id="nmname" placeholder="name" size="8">
        <input id="nmw" type="number" value="24" min="3" max="255" title="width">
        <input id="nmh" type="number" value="16" min="3" max="255" title="height">
        <button id="newmap">＋ Map</button>
      </div>
    </section>
    <section><h2>Transitions</h2><div id="warps"></div>
      <div class="mini">
        <span id="warpfrom">pick a tile</span>
      </div>
      <small>Press <kbd>W</kbd>, click a tile, then choose where it leads.</small>
    </section>
    <section><h2>Palettes</h2><div id="pals"></div></section>
    <section><h2>Budget</h2><div class="meter"><i id="bar"></i></div>
      <small id="budget">—</small></section>
    <section><h2>Output</h2><div id="log">Ready.</div></section>
  </aside>
  <div id="stage"><canvas id="cv"></canvas></div>
  <div id="import" style="display:none;flex:1;overflow:auto;padding:1.5rem">
    <div class="imp">
      <div class="fields">
        <label>Sheet<select id="sheet"></select></label>
        <label>Tile px<input id="tile" type="number" value="16" min="4" step="4"></label>
        <label>Region x<input id="rx" type="number" value="0" min="0"></label>
        <label>y<input id="ry" type="number" value="0" min="0"></label>
        <label>w<input id="rw" type="number" value="16" min="1"></label>
        <label>h<input id="rh" type="number" value="16" min="1"></label>
        <label>Max tiles<input id="maxt" type="number" value="64" min="1"></label>
        <label>Name<input id="aname" placeholder="cave_env"></label>
        <button id="addatlas" class="primary">Add atlas</button>
      </div>
      <div id="stats" class="stats"></div>
      <div class="plate"><h3>Sheet</h3><img id="sheetimg" alt=""></div>
      <div class="plate"><h3>Tiles kept</h3><img id="strip" alt=""></div>
    </div>
  </div>
</main>
<script>
const S={data:null,map:null,ch:null,mode:'paint',dirty:false,img:{},T:32};
const $=s=>document.querySelector(s);

async function load(){
  S.data=await (await fetch('/api/state')).json();
  $('#mapsel').innerHTML=S.data.maps.map((m,i)=>`<option value="${i}">${m.name}</option>`).join('');
  $('#atlassel').innerHTML=S.data.atlases.map(a=>`<option value="${a.name}">${a.name}</option>`).join('');
  drawPalettes(); budget();
  selectMap(0);
}

function atlas(){
  return S.data.atlases.find(a=>a.name===S.map.atlas) || S.data.atlases[0];
}

// A legend character names a tile ROLE; the role resolves through whichever atlas this
// map is drawn with. Two tilesets can both define "wall" and mean different tiles.
function resolve(ch){
  const a=atlas(), e=S.data.legend[ch];
  if(!a||!e) return null;
  const idx=a.roles[e.tile.toLowerCase()];
  if(idx===undefined||idx>=a.tiles.length) return null;
  return {uri:a.tiles[idx],index:idx,role:e.tile,flags:e.flags};
}

function drawLegend(){
  const el=$('#legend'); el.innerHTML=''; S.img={};
  const a=atlas();
  if(!a){ el.innerHTML='<small>No atlas built yet — press Build.</small>'; return }

  let usable=0, missing=[];
  for(const ch of Object.keys(S.data.legend)){
    const r=resolve(ch);
    if(!r){ missing.push(`${ch} (${S.data.legend[ch].tile})`); continue }
    usable++;
    const img=new Image(); img.src=r.uri; S.img[ch]=img;
    img.onload=draw;

    const b=document.createElement('button');
    b.className='tile'+(S.ch===ch?' sel':'');
    b.title=`${ch} → ${r.role} (tile ${r.index}) ${r.flags.join(' ')}`;
    b.innerHTML=`<img src="${r.uri}" alt="${ch}"><b>${ch}</b>`;
    b.onclick=()=>{S.ch=ch;S.mode='paint';drawLegend();tool()};
    el.appendChild(b);
  }
  if(!usable||!S.img[S.ch]) S.ch=Object.keys(S.data.legend).find(c=>resolve(c))||null;

  $('#painthint').innerHTML = missing.length
    ? `<span style="color:var(--bad)">${a.name} defines no tile for ${missing.join(', ')}
       — give it an <code>autopick</code> or <code>[atlas.semantic]</code> table.</span>`
    : 'Click to paint. <kbd>W</kbd> sets a warp, <kbd>S</kbd> the start.';
}
function drawPalettes(){
  $('#pals').innerHTML=S.data.palettes.map(p=>'<div class="pal">'+p.map(c=>
    c==='transparent'?'<div class="sw clear"></div>':
    `<div class="sw" style="background:${c}" title="${c}"></div>`).join('')+'</div>').join('');
}
function budget(){
  const pct=100*S.data.used/S.data.budget;
  $('#bar').style.width=Math.min(100,pct)+'%';
  $('#budget').textContent=`${S.data.used.toLocaleString()} B — ${pct.toFixed(1)}% of ${(S.data.budget/1024)|0}KB`;
}
function tool(){
  $('#tool').innerHTML=S.mode==='paint'?`painting <kbd>${S.ch}</kbd>`:
    S.mode==='warp'?'<kbd>click a door to add/remove a warp</kbd>':'<kbd>click to set start</kbd>';
}
function selectMap(i){
  S.map=JSON.parse(JSON.stringify(S.data.maps[i]));
  $('#atlassel').value=S.map.atlas||'';
  S.dirty=false; mark(); drawLegend(); renderWarps(); warpForm(null); info(); draw();
}
$('#atlassel').onchange=e=>{
  S.map.atlas=e.target.value;
  S.dirty=true; mark(); drawLegend(); info(); draw();
};
function mark(){$('#dirty').textContent=S.dirty?'● unsaved':''}
// A warp needs a destination map and tile, so the form appears once a source tile is
// picked rather than asking through a chain of prompts.
function warpForm(at){
  const box=$('#warpfrom');
  if(!at){ box.textContent='pick a tile'; return }
  const others=S.data.maps.map(o=>o.name);
  box.innerHTML=`from (${at[0]},${at[1]}) →
    <select id="wto">${others.map(n=>`<option>${n}</option>`).join('')}</select>
    <input id="wx" type="number" value="1" min="0" title="destination x">
    <input id="wy" type="number" value="1" min="0" title="destination y">
    <button id="wadd">Add</button>`;
  $('#wadd').onclick=()=>{
    S.map.warps.push({at,to:[$('#wto').value,+$('#wx').value,+$('#wy').value]});
    S.dirty=true; S.mode='paint'; mark(); renderWarps(); info(); tool(); draw();
    warpForm(null);
  };
}

function renderWarps(){
  const m=S.map;
  $('#warps').innerHTML = m.warps.length ? m.warps.map((w,i)=>
    `<div class="warp">(${w.at[0]},${w.at[1]}) → <b>${w.to[0]}</b> (${w.to[1]},${w.to[2]})
     <button data-i="${i}" title="remove">✕</button></div>`).join('')
    : '<small>none</small>';
  for(const b of document.querySelectorAll('#warps button'))
    b.onclick=()=>{S.map.warps.splice(+b.dataset.i,1);S.dirty=true;mark();
      renderWarps();info();draw()};
}

function info(){
  const m=S.map;
  $('#mapinfo').innerHTML=`<small>${m.rows[0].length}×${m.rows.length} · `+
    `tileset <b>${m.atlas||'—'}</b> · start (${m.start})<br>`+
    (m.warps.length?m.warps.map(w=>`warp (${w.at}) → ${w.to[0]} (${w.to[1]},${w.to[2]})`).join('<br>'):'no warps')+'</small>';
}

function draw(){
  const m=S.map,T=S.T,cv=$('#cv'),g=cv.getContext('2d');
  cv.width=m.rows[0].length*T; cv.height=m.rows.length*T;
  g.imageSmoothingEnabled=false;
  g.fillStyle='#000'; g.fillRect(0,0,cv.width,cv.height);
  for(let y=0;y<m.rows.length;y++)for(let x=0;x<m.rows[y].length;x++){
    const im=S.img[m.rows[y][x]];
    if(im&&im.complete) g.drawImage(im,x*T,y*T,T,T);
  }
  // start marker and warps, drawn over the map so placement is checkable at a glance
  g.strokeStyle='#55aaff'; g.lineWidth=2;
  g.strokeRect(m.start[0]*T+1,m.start[1]*T+1,T-2,T-2);
  g.strokeStyle='#e0913f';
  for(const w of m.warps) g.strokeRect(w.at[0]*T+1,w.at[1]*T+1,T-2,T-2);
}

$('#cv').addEventListener('mousedown',e=>paint(e,true));
$('#cv').addEventListener('mousemove',e=>{if(e.buttons)paint(e,false)});
function paint(e,click){
  const r=e.target.getBoundingClientRect();
  const x=Math.floor((e.clientX-r.left)/S.T), y=Math.floor((e.clientY-r.top)/S.T);
  const m=S.map;
  if(x<0||y<0||y>=m.rows.length||x>=m.rows[y].length) return;

  if(S.mode==='start'){ if(!click)return; m.start=[x,y]; S.mode='paint'; }
  else if(S.mode==='warp'){
    if(!click)return;
    const i=m.warps.findIndex(w=>w.at[0]===x&&w.at[1]===y);
    if(i>=0){ m.warps.splice(i,1); S.mode='paint'; warpForm(null); }
    else warpForm([x,y]);
  } else {
    const row=m.rows[y];
    if(row[x]===S.ch) return;
    m.rows[y]=row.slice(0,x)+S.ch+row.slice(x+1);
  }
  S.dirty=true; mark(); info(); tool(); draw();
}

addEventListener('keydown',e=>{
  if(e.target.tagName==='SELECT')return;
  if(e.key==='w'||e.key==='W'){S.mode='warp';tool()}
  if(e.key==='s'||e.key==='S'){S.mode='start';tool()}
  if(e.key==='Escape'){S.mode='paint';tool()}
});
$('#mapsel').onchange=e=>{
  if(S.dirty&&!confirm('Discard unsaved changes to this map?')){
    e.target.value=S.data.maps.findIndex(m=>m.name===S.map.name); return;
  }
  selectMap(+e.target.value);
};
$('#save').onclick=async()=>{
  const r=await (await fetch('/api/map',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(S.map)})).json();
  const log=$('#log');
  log.className=r.ok?'ok':'bad';
  log.textContent=r.ok?`Saved ${S.map.name} to the manifest.`:r.error;
  if(r.ok){S.dirty=false;mark()}
};
$('#newmap').onclick=async()=>{
  const name=$('#nmname').value.trim();
  if(!name){alert('Name the map first.');return}
  const r=await (await fetch('/api/newmap',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name,w:+$('#nmw').value,h:+$('#nmh').value,
      atlas:$('#atlassel').value||S.data.atlases[0].name})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  if(!r.ok){log.textContent=r.error;return}
  log.textContent=`Created map "${name}" and a scene for it. Press Build.`;
  $('#nmname').value='';
  await load();
  const i=S.data.maps.findIndex(m=>m.name===name);
  $('#mapsel').value=i; selectMap(i);
};
$('#build').onclick=async()=>{
  const log=$('#log'); log.className=''; log.textContent='Building…';
  const r=await (await fetch('/api/build',{method:'POST'})).json();
  log.className=r.ok?'ok':'bad'; log.textContent=r.output.trim()||'(no output)';
  if(r.ok){ const keep=S.map.name; await load();
    const i=S.data.maps.findIndex(m=>m.name===keep);
    $('#mapsel').value=i; selectMap(i); }
};
// ------------------------------------------------------------------ import view
let sheets=[];
function showTab(which){
  const imp=which==='import';
  $('#import').style.display=imp?'block':'none';
  $('#stage').style.display=imp?'none':'flex';
  $('#mapsel').style.display=imp?'none':'';
  $('#save').style.display=imp?'none':'';
  $('#tabmaps').className=imp?'':'primary';
  $('#tabimport').className=imp?'primary':'';
  if(imp&&!sheets.length) loadSheets();
}
$('#tabmaps').onclick=()=>showTab('maps');
$('#tabimport').onclick=()=>showTab('import');

async function loadSheets(){
  sheets=await (await fetch('/api/sheets')).json();
  $('#sheet').innerHTML=sheets.map(s=>`<option value="${s.path}">${s.name}</option>`).join('');
  analyse();
}
let pending=null;
function analyse(){
  clearTimeout(pending);
  pending=setTimeout(async()=>{
    const body={sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      max_tiles:+$('#maxt').value};
    if(!body.sheet) return;
    const r=await (await fetch('/api/analyse',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.error){$('#stats').innerHTML=`<div class="warn"><b>!</b><span>${r.error}</span></div>`;return}
    const cell=(v,l,warn)=>`<div class="${warn?'warn':''}"><b>${v}</b><span>${l}</span></div>`;
    $('#stats').innerHTML=
      cell(r.unique,'unique tiles',r.capped)+
      cell(r.colours,'colours')+
      cell(r.palettes,'palettes')+
      cell(r.bytes.toLocaleString(),'bytes')+
      cell(r.pct.toFixed(1)+'%','of budget',r.pct>25)+
      cell(r.repaired,'repaired',r.repaired>0)+
      cell(r.sheet_tiles.join('×'),'sheet tiles');
    $('#sheetimg').src=r.thumb;
    $('#strip').src=r.strip||'';
  },180);
}
for(const id of ['sheet','tile','rx','ry','rw','rh','maxt'])
  $('#'+id).addEventListener('input',analyse);

$('#addatlas').onclick=async()=>{
  const name=$('#aname').value.trim();
  if(!name){alert('Name the atlas first.');return}
  const r=await (await fetch('/api/atlas',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name,sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      max_tiles:+$('#maxt').value})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  log.textContent=r.ok?`Added [[atlas]] "${name}" to the manifest. Press Build.`:r.error;
};

load();
</script></body></html>"""


def make_handler(proj):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, json.dumps(proj.state()))
            elif self.path == "/api/sheets":
                self._send(200, json.dumps(proj.sheets()))
            else:
                self._send(404, "{}")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b"{}"
            try:
                if self.path == "/api/map":
                    m = json.loads(raw)
                    proj.save_map(m["name"], m["rows"], m["start"], m["warps"],
                                  m.get("atlas"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/analyse":
                    d = json.loads(raw)
                    key = d.get("colorkey")
                    self._send(200, json.dumps(proj.analyse(
                        d["sheet"], int(d["tile"]), d["region"],
                        int(d["max_tiles"]), tuple(key) if key else None)))
                elif self.path == "/api/atlas":
                    d = json.loads(raw)
                    proj.add_atlas(d["name"], d["sheet"], int(d["tile"]),
                                   d["region"], int(d["max_tiles"]))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/newmap":
                    d = json.loads(raw)
                    proj.add_map(d["name"], int(d["w"]), int(d["h"]), d["atlas"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/build":
                    self._send(200, json.dumps(proj.build()))
                else:
                    self._send(404, "{}")
            except Exception as e:                       # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "error": str(e),
                                            "output": str(e)}))
    return Handler


def find_manifest():
    """Locate a manifest when none was given.

    Running from an IDE's default configuration passes no arguments, and demanding one
    turns "press play" into "go and configure a run target". Searching a few obvious
    places costs nothing and makes the common case work.
    """
    here = os.getcwd()
    roots = [here, os.path.join(TOOLS, "..")]
    seen, found = set(), []

    for base in roots:
        base = os.path.abspath(base)
        for candidate in [os.path.join(base, "assets.toml")]:
            if os.path.exists(candidate) and candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
        ex = os.path.join(base, "examples")
        if os.path.isdir(ex):
            for name in sorted(os.listdir(ex)):
                candidate = os.path.join(ex, name, "assets.toml")
                if os.path.exists(candidate) and candidate not in seen:
                    seen.add(candidate)
                    found.append(candidate)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", nargs="?",
                    help="path to assets.toml; found automatically if omitted")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    manifest = args.manifest
    if not manifest:
        found = find_manifest()
        if not found:
            print("no assets.toml found -- pass one:\n"
                  "  pnx_editor.py path/to/assets.toml", file=sys.stderr)
            return 2
        manifest = found[0]
        print(f"using {os.path.relpath(manifest)}"
              + (f"  ({len(found) - 1} other project(s) found; pass a path to pick)"
                 if len(found) > 1 else ""))

    proj = Project(manifest)
    if not proj.built:
        print("note: assets are not built yet -- press Build in the editor")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), make_handler(proj)) as srv:
        url = f"http://127.0.0.1:{args.port}/"
        print(f"pebblnyx editor: {url}   (ctrl-c to stop)")
        if not args.no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
