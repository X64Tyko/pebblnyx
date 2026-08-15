"""Fonts, and the shared preview-canvas rendering they're previewed against."""

import contextlib
import io
import os
import re
import shutil

import pnx_assets as pa                                     # noqa: E402
import pnx_preview as pv                                    # noqa: E402


class FontsMixin:
    def font_sources(self):
        """Typefaces the editor can offer: the project's own first, then the system's.

        System fonts are listed because the alternative is a file dialog, and the barrier
        this editor exists to remove is exactly that kind of detour. Picking one COPIES it
        into the project rather than referencing it in place -- a manifest pointing at
        /usr/share/fonts builds on this machine and nowhere else.
        """
        out, seen = [], set()

        def add(path, where):
            full = os.path.realpath(path)
            if full in seen or not os.path.isfile(full):
                return
            seen.add(full)
            out.append({"path": full, "name": os.path.basename(full), "where": where,
                        "in_project": where == "project",
                        "rel": os.path.relpath(full, self.root)
                        if where == "project" else None})

        for dirpath, dirnames, files in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("build", ".git", "resources", "__pycache__")]
            for fn in files:
                if fn.lower().endswith((".ttf", ".otf")):
                    add(os.path.join(dirpath, fn), "project")

        for base in self.FONT_DIRS:
            base = os.path.expanduser(base)
            if not os.path.isdir(base):
                continue
            for dirpath, _dirnames, files in os.walk(base):
                for fn in sorted(files):
                    if fn.lower().endswith((".ttf", ".otf")):
                        add(os.path.join(dirpath, fn), "system")
                if len(out) > 400:
                    break

        out.sort(key=lambda f: (not f["in_project"], f["name"].lower()))
        return out

    def fonts(self):
        """Fonts already declared, with their built metrics where a build exists."""
        out = []
        for spec in self.man.get("font", []):
            entry = {"name": spec.get("name"), "source": spec.get("source"),
                     "size": spec.get("size", 12), "depth": spec.get("depth", 1),
                     "threshold": spec.get("threshold"),
                     "tracking": spec.get("tracking", 0),
                     "charset": spec.get("charset", "auto"),
                     "extra": spec.get("extra", ""),
                     "license": spec.get("license", ""), "bytes": None}
            blob = os.path.join(self.res, spec.get("out", f"font_{entry['name']}.bin"))
            if os.path.exists(blob):
                entry["bytes"] = os.path.getsize(blob)
            out.append(entry)
        return out

    def _rasterise(self, spec):
        """Pack a candidate font and parse it straight back.

        Deliberately round-trips through the real blob rather than rendering from the
        rasteriser's intermediate output: previewing from anything other than the shipped
        bytes is how a preview and a build come to disagree.
        """
        full = dict(spec)
        full.setdefault("name", "preview")
        # A licence is required to BUILD, not to look. Gating the preview on it would
        # mean choosing a typeface before you can see whether it is worth licensing.
        full.setdefault("license", "(unset -- required before this can be added)")

        with contextlib.redirect_stdout(io.StringIO()):
            packed = pa.pack_font(self.root, full, self.man)
        return packed, pv.parse_font(packed["blob"])

    def font_preview(self, spec):
        """Rasterise at the current settings and report what it costs and looks like."""
        packed, font = self._rasterise(spec)

        budget = self.project.get("budget_bytes", 262144)
        size = len(packed["blob"])
        blank = sum(1 for g in font["glyphs"] if not g["w"])

        return {
            "ok": True,
            "sheet": pv.data_uri(pv.font_sheet(font, scale=4)),
            "chars": "".join(packed["chars"]),
            "glyph_count": font["glyph_count"],
            "line_height": font["line_height"],
            "baseline": font["baseline"],
            "depth": font["depth"],
            "bitmap_bytes": font["bitmap_bytes"],
            "bytes": size,
            "pct": 100.0 * size / budget,
            # A glyph with no ink that is not a space means the threshold ate it, which
            # is the single most common way an imported font is quietly broken.
            "blank_glyphs": blank,
            "glyphs": [
                {"ch": packed["chars"][i], "w": g["w"], "h": g["h"],
                 "advance": g["advance"], "bx": g["bearing_x"], "by": g["bearing_y"]}
                for i, g in enumerate(font["glyphs"])
            ],
            "origin": {ch: packed["origin"].get(ch, "") for ch in packed["chars"]},
        }

    def _roles(self):
        """Role -> tile index per atlas, read from the generated header.

        Same source the map canvas uses, so a preview background is drawn with the tiles
        the build would actually place.
        """
        header_path = os.path.join(self.root,
                                   self.project.get("header", "src/c/assets_gen.h"))
        text = open(header_path).read() if os.path.exists(header_path) else ""
        out = {}
        for spec in self.man.get("atlas", []):
            prefix = re.sub(r"[^A-Za-z0-9]", "_", spec["name"]).upper()
            roles = {m.group(1).lower(): int(m.group(2)) for m in
                     re.finditer(rf"#define {prefix}_TILE_(\w+) (\d+)", text)}
            for k in ("px", "bytes", "count"):
                roles.pop(k, None)
            out[spec["name"]] = roles
        return out

    def _map_background(self, map_name, ox, oy, clear=0xC0):
        """A screen-sized crop of a real map, drawn with real tiles.

        `clear` shows through index 0, matching the device: the tilemap skips transparent
        pixels rather than writing them, so what appears behind a tile is whatever the
        frame was cleared to.
        """
        from PIL import Image

        palettes = pv.parse_palettes(pv.read(os.path.join(self.res, "palettes.bin")))
        m = next((m for m in self.maps() if m["name"] == map_name), None)
        if not m:
            return None

        # Every atlas the map draws from, since a legend character now says which one it
        # resolves against. Loading only the first would draw the other tilesets' cells as
        # holes -- which reads as missing art rather than as a preview limitation.
        loaded = {}
        for name in m["atlases"]:
            spec = next((a for a in self.man.get("atlas", []) if a["name"] == name), None)
            if not spec:
                continue
            blob = os.path.join(self.res, spec["out"])
            if os.path.exists(blob):
                loaded[name] = pv.parse_atlas(pv.read(blob))
        if not loaded:
            return None

        roles_by_atlas = self._roles()
        legend = self.man.get("legend", {})
        default_atlas = m["atlases"][0] if m["atlases"] else None
        T = next(iter(loaded.values()))["tile_px"]

        # Resolved once per character rather than once per cell, the way compile_map does.
        resolved = {}
        for ch, e in legend.items():
            which = e.get("atlas") or default_atlas
            atlas = loaded.get(which)
            if not atlas:
                continue
            idx = roles_by_atlas.get(which, {}).get(e.get("tile"))
            if idx is not None and idx < atlas["count"]:
                resolved[ch] = (atlas, idx)

        img = Image.new("RGBA", (self.SCREEN_W, self.SCREEN_H),
                        (pv.gcolor_rgb(clear) or (0, 0, 0)) + (255,))
        first_tx, first_ty = ox // T, oy // T
        for j in range(self.SCREEN_H // T + 2):
            for i in range(self.SCREEN_W // T + 2):
                tx, ty = first_tx + i, first_ty + j
                if not (0 <= ty < len(m["rows"]) and 0 <= tx < len(m["rows"][ty])):
                    continue
                hit = resolved.get(m["rows"][ty][tx])
                if not hit:
                    continue
                tile = self._upright(pv.tile_image(hit[0], palettes, hit[1]))
                # Masked, so index 0 leaves the clear colour rather than punching a hole.
                img.paste(tile, (tx * T - ox, ty * T - oy), tile)
        return img

    def font_scene(self, opts):
        """The composited preview: real background, real box, real glyphs, real size.

        The background is a choice because text fails differently depending on what is
        behind it -- a HUD sits over gameplay and has to survive whatever tile scrolls
        under it, while dialogue sits on a flat panel and only has to be comfortable.
        A preview that only ever showed one of those would pass a font that fails the
        other.
        """
        from PIL import Image

        font = None
        if opts.get("spec"):
            _packed, font = self._rasterise(opts["spec"])
        elif opts.get("font"):
            spec = next((f for f in self.man.get("font", [])
                         if f.get("name") == opts["font"]), None)
            if spec:
                blob = os.path.join(self.res,
                                    spec.get("out", f"font_{spec['name']}.bin"))
                if os.path.exists(blob):
                    font = pv.parse_font(pv.read(blob))
        if not font:
            raise ValueError("no font to preview -- pick a source, or build first")

        bg = opts.get("background", "solid")
        img = None
        if bg == "map" and self.built and opts.get("map"):
            img = self._map_background(opts["map"], int(opts.get("scroll_x", 0)),
                                       int(opts.get("scroll_y", 0)),
                                       int(opts.get("bg_colour", 0xC0)))
        if img is None:
            rgb = pv.gcolor_rgb(int(opts.get("bg_colour", 0xC0))) or (0, 0, 0)
            img = Image.new("RGBA", (self.SCREEN_W, self.SCREEN_H), rgb + (255,))

        text = opts.get("text") or ""
        pad = int(opts.get("pad", 6))
        box = opts.get("box") or {}

        if box.get("on"):
            bx, by = int(box.get("x", 8)), int(box.get("y", 140))
            bw, bh = int(box.get("w", 184)), int(box.get("h", 72))
            fill = pv.gcolor_rgb(int(box.get("colour", 0xC0))) or (0, 0, 0)
            panel = Image.new("RGBA", (bw, bh), fill + (255,))
            img.paste(panel, (bx, by))
            if box.get("border"):
                edge = pv.gcolor_rgb(int(box.get("border_colour", 0xFF))) or (255,) * 3
                px = img.load()
                for i in range(bw):
                    for yy in (by, by + bh - 1):
                        if 0 <= bx + i < img.width and 0 <= yy < img.height:
                            px[bx + i, yy] = edge + (255,)
                for j in range(bh):
                    for xx in (bx, bx + bw - 1):
                        if 0 <= xx < img.width and 0 <= by + j < img.height:
                            px[xx, by + j] = edge + (255,)
            tx, ty, tw = bx + pad, by + pad, bw - pad * 2
        else:
            tx, ty = int(opts.get("x", 4)), int(opts.get("y", 4))
            tw = self.SCREEN_W - tx * 2

        ink = pv.gcolor_rgb(int(opts.get("ink", 0xFF))) or (255, 255, 255)
        lines = pv.font_draw_wrapped(img, font, text, tx, ty + font["baseline"], tw,
                                     ink, opts.get("align", "left"))

        scale = max(1, min(4, int(opts.get("scale", 2))))
        shown = img.resize((img.width * scale, img.height * scale), Image.NEAREST) \
            if scale > 1 else img

        return {"ok": True, "image": pv.data_uri(shown),
                "scale": scale, "lines": lines,
                "line_height": font["line_height"], "baseline": font["baseline"],
                "text_height": lines * font["line_height"],
                "overflow": bool(box.get("on")) and
                (lines * font["line_height"] > int(box.get("h", 72)) - pad * 2)}

    def add_font(self, spec):
        """Append a [[font]] block, copying a system typeface in if that is what it is."""
        name = spec.get("name", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")
        if any(f.get("name") == name for f in self.man.get("font", [])):
            raise ValueError(f"a font named {name!r} already exists")
        if not spec.get("license"):
            raise ValueError("a licence is required: rasterising a typeface into the "
                             "bundle redistributes it")

        source = spec["source"]
        if os.path.isabs(source):
            dest_dir = os.path.join(self.root, "art", "fonts")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(source))
            if not os.path.exists(dest):
                shutil.copy2(source, dest)
            source = os.path.relpath(dest, self.root)

        depth = int(spec.get("depth", 1))
        lines = [
            "", "", "[[font]]",
            f'name = "{name}"',
            f'source = "{source}"',
            f"size = {int(spec.get('size', 12))}",
            f"depth = {depth}"
            + ("   # crisp" if depth == 1 else "   # antialiased"),
            f"threshold = {int(spec.get('threshold', 128 if depth == 1 else 24))}",
        ]
        if int(spec.get("tracking", 0)):
            lines.append(f"tracking = {int(spec['tracking'])}")
        lines.append(f'charset = "{spec.get("charset", "auto")}"')
        if spec.get("extra"):
            lines.append(f'extra = "{spec["extra"]}"')
        lines += [
            f'license = "{spec["license"]}"',
            f'out = "font_{name}.bin"',
            "",
        ]

        with open(self.path, "a") as f:
            f.write("\n".join(lines))
        self.reload()

    def font_users(self, name):
        """The scenes that load this face, so removing it can refuse and say which."""
        return [f"scene {s}" for s, spec in self.man.get("scene", {}).items()
                if name in spec.get("fonts", [])]

    def remove_font(self, name):
        """Delete a [[font]], once no scene loads it.

        `add_font` had no counterpart, so a face imported to try it out could only be
        taken out again by hand. The TTF it was rasterised from is left on disk: it was
        copied into the project deliberately, and deleting someone's licensed typeface
        because they dropped one derived asset would be well beyond what was asked.
        """
        lines = open(self.path).read().split("\n")
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[font]]":
                nxt = next((j for j in range(i + 1, len(lines))
                            if lines[j].lstrip().startswith("[")), len(lines))
                if any(re.match(rf'^name\s*=\s*"{re.escape(name)}"', lines[j].strip())
                       for j in range(i + 1, nxt)):
                    start = i
                    break
        if start is None:
            raise ValueError(f"no font named {name!r}")

        users = self.font_users(name)
        if users:
            raise ValueError(f"cannot remove {name!r} — {', '.join(users)} loads it. "
                             f"Drop it there first.")

        end = next((j for j in range(start + 1, len(lines))
                    if lines[j].lstrip().startswith("[")), len(lines))
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

