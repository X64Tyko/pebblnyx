"""Everything about atlases: import/carve from a sheet, roles, collision, CRUD."""

import contextlib
import io
import json
import os
import re

import pnx_assets as pa                                     # noqa: E402
import pnx_preview as pv                                    # noqa: E402


class AtlasMixin:
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
                # The editor draws tiles at whatever zoom it likes; the DEVICE draws them
                # at this size, which is what turns a screen in pixels into a frame in
                # tiles for the camera overlay.
                "tile": atlas["tile_px"],
                # A metatiled atlas cannot be drawn flipped -- pnx_tilemap_draw skips the
                # flip for composed tiles rather than mirroring the quadrant order. So the
                # picker has to hide the mirrored variants for this atlas rather than
                # offer a choice the build will refuse.
                "metatiled": bool(atlas.get("metatiled")),
                "roles": roles,
                "tiles": [pv.data_uri(self._upright(pv.tile_image(atlas, palettes, i, 2)))
                          for i in range(atlas["count"])],
            })
        return out

    def map_atlases(self, m):
        """The atlases one map draws from, in the order that fixes its tile id space.

        `atlas = "x"` and `atlases = ["x", "y"]` are the same key spelled for one or many,
        and a map naming neither draws with the first atlas declared -- the same three
        rules the pipeline's map_atlas_names applies, so the editor shows what will build.
        """
        specs = self.man.get("atlas", [])
        if "atlases" in m:
            want = m["atlases"]
            return list(want) if not isinstance(want, str) else [want]
        if "atlas" in m:
            return [m["atlas"]]
        return [specs[0]["name"]] if specs else []

    def sheets(self):
        """PNGs inside the project, so importing does not need a file dialog.

        Inside the project only. This used to walk two directories UP as well, which
        offered art that a manifest could then reference as `../../somewhere` -- a
        project that builds on this machine and nowhere else. Same reasoning as copying a
        system typeface into `art/fonts/` rather than pointing at `/usr/share/fonts`.
        Art from elsewhere gets copied in first; the editor will not quietly make a
        project that only works here.
        """
        seen, out = set(), []
        for base in [self.root]:
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

    def slice_grid(self, rel, tile, region, exclude=(), colorkey=None, offset=(0, 0),
                   max_tiles=255):
        """Every cell of a slice, rendered, so the author can pick what gets packed.

        The strip of *kept* tiles told you what the pipeline decided. It did not let you
        disagree with it -- and the decision that matters most for the budget is which
        tiles are worth keeping at all, which is a judgement about the art rather than
        arithmetic. So this returns the grid as sliced, marked up with what each cell
        would become, and the caller can drop any of it.

        Each cell also carries `packed`: the index it becomes in the ACTUAL packed atlas,
        or null for one the pipeline would never keep (empty, excluded, past `max_tiles`,
        or an exact/mirror duplicate of an earlier cell -- pack_atlas dedups mirrors too,
        which this grid's own simpler `state` classification below does not attempt to,
        so `packed` is the more honest of the two answers). Resolved through a real
        pa.pack_atlas call rather than a second dedup pass here, precisely so it cannot
        disagree with what a carve would actually produce -- this is what makes a raw
        cell click-through-able to the SAME tile index carve_tiles (and the manifest's
        own [[atlas.collision]]) uses, which is why `max_tiles` matters here at all: a
        cell past the real cap must resolve to no tile, not to an index carve_tiles never
        produced.
        """
        from PIL import Image
        path = self._safe(rel)
        im = Image.open(path).convert("RGBA")
        px = im.load()
        W, H = im.size
        rx, ry, rw, rh = region
        ox, oy = (int(v) for v in offset)
        rw = max(1, min(rw, (W - ox) // tile - rx))
        rh = max(1, min(rh, (H - oy) // tile - ry))

        # A whole large sheet is thousands of cells and as many PNGs. Capped, with the
        # cap reported, rather than quietly hanging the browser.
        LIMIT = 1024
        capped = rw * rh > LIMIT

        packed_at = {}
        try:
            spec = {"name": "_slice_grid", "sheet": rel, "tile": int(tile),
                    "region": [rx, ry, rw, rh], "max_tiles": int(max_tiles),
                    "out": "_slice_grid.bin", "exclude": list(exclude),
                    "offset": [ox, oy]}
            if colorkey:
                spec["colorkey"] = list(colorkey)
            with contextlib.redirect_stdout(io.StringIO()):
                packed = pa.pack_atlas(self.root, spec, self.orientation)
            packed_at = {sheet_xy: i for i, sheet_xy in enumerate(packed["origin"])}
        except pa.BuildError:
            pass  # an invalid carve just means no cell resolves to a packed tile yet

        excluded = {int(e) for e in exclude}
        cells, seen = [], {}
        for j in range(min(rh, LIMIT // max(1, rw) + 1)):
            for i in range(rw):
                idx = j * rw + i
                if idx >= LIMIT:
                    break
                tx, ty = rx + i, ry + j
                # Read through the key, so a cell that is nothing BUT background reads as
                # empty here exactly as it will in the pipeline -- which is the whole
                # reason to pick a key before committing to a carve.
                buf = tuple(pa.to_gcolor8(px[ox + tx * tile + a, oy + ty * tile + b], colorkey)
                            for b in range(tile) for a in range(tile))
                if not any(buf):
                    state = "empty"
                elif buf in seen:
                    state = "dup"
                else:
                    seen[buf] = idx
                    state = "unique"

                img = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
                ip = img.load()
                for b in range(tile):
                    for a in range(tile):
                        rgb = pv.gcolor_rgb(buf[b * tile + a])
                        if rgb:
                            ip[a, b] = rgb + (255,)
                cells.append({"i": idx, "x": tx, "y": ty, "state": state,
                              "excluded": idx in excluded,
                              "packed": packed_at.get((tx, ty)),
                              "img": pv.data_uri(img.resize((tile * 2, tile * 2),
                                                            Image.NEAREST))})
        return {"cols": rw, "rows": rh, "cells": cells, "capped": capped,
                "limit": LIMIT, "sheet_tiles": [W // tile, H // tile]}

    def analyse(self, rel, tile, region, max_tiles, colorkey, exclude=(),
               ink_threshold=pa.DEFAULT_INK_THRESHOLD, offset=(0, 0)):
        """Price a candidate carve before it is committed.

        This is the number that decides a project's content budget, and it is invisible
        until something is built -- so the editor computes it live. Region selection is
        where the budget is won: five complete tilesets are 111% of the appstore limit,
        while 128 tiles from each is 32%.

        `ink_threshold` drives the 1-bit preview strip: docs/PORTING.md's "the pipeline
        proposes a split by luminance [and] the editor lets you flip individual entries
        against a live 1-bit preview" -- the slider half of that sentence. Per-entry
        flipping is not built; every colour in the carve answers to the one threshold.

        `unique` is resolved through a real pa.pack_atlas call, the same way slice_grid's
        `packed` is (see that docstring), rather than a second, cheaper dedup pass here --
        pack_atlas is mirror- AND rotation-aware, and a hand-rolled exact-match-only loop
        priced a carve as bigger than the build it was pricing actually turns out to be,
        which is a quote nobody can act on.
        """
        from PIL import Image
        path = os.path.join(self.root, rel)
        im = Image.open(path).convert("RGBA")
        W, H = im.size
        rx, ry, rw, rh = region
        ox, oy = (int(v) for v in offset)

        rw = max(1, min(rw, (W - ox) // tile - rx))
        rh = max(1, min(rh, (H - oy) // tile - ry))

        spec = {"name": "_analyse", "sheet": rel, "tile": int(tile),
                "region": [rx, ry, rw, rh], "max_tiles": int(max_tiles),
                "out": "_analyse.bin", "exclude": list(exclude), "offset": [ox, oy]}
        if colorkey:
            spec["colorkey"] = list(colorkey)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                packed = pa.pack_atlas(self.root, spec, self.orientation)
            unique, empty, repaired = packed["tiles"], packed["empty"], packed["repaired"]
        except pa.BuildError:
            # An in-progress region (too small, all excluded, past the sheet) prices as
            # empty rather than failing the endpoint -- the page is still being typed
            # into, and a 500 mid-keystroke is worse than a price of zero.
            unique, empty, repaired = [], 0, 0

        sets = [frozenset(c for c in t if c) for t in unique]
        pals = pa.merge_palettes(sets)[0] if sets else []
        colours = set().union(*sets) if sets else set()

        tile_bytes = tile * tile // 2
        est = len(unique) * tile_bytes + len(pals) * pa.PALETTE_BYTES + pa.HEADER_BYTES

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

        # Same tiles, rendered as a 1-bit platform would actually draw them: ink (black)
        # where a pixel's luminance falls under the threshold, paper (white) otherwise,
        # transparent left alone. Classifying per PIXEL rather than routing through `pals`
        # is deliberate -- ink/paper is a property of the colour itself
        # (tools/pnx_assets.py's palette_ink_mask), not of which merged palette a tile
        # happened to land in, so this does not need pals to agree with what the real
        # build would assign.
        bw_strip = None
        if unique:
            cols = min(16, len(unique))
            rows = (len(unique) + cols - 1) // cols
            grid = Image.new("RGBA", (cols * tile, rows * tile), (0, 0, 0, 0))
            for i, t in enumerate(unique):
                cell = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
                cp = cell.load()
                for j in range(tile):
                    for k in range(tile):
                        c = t[j * tile + k]
                        if c == pa.TRANSPARENT:  # the GColor8 byte, not a palette index
                            continue
                        ink = pa.gcolor_luminance(c) < ink_threshold
                        cp[k, j] = (0, 0, 0, 255) if ink else (255, 255, 255, 255)
                grid.paste(cell, ((i % cols) * tile, (i // cols) * tile))
            grid = grid.resize((grid.width * 2, grid.height * 2), Image.NEAREST)
            bw_strip = pv.data_uri(grid)

        # What this carve would do to the two ceilings it spends against, priced against
        # what the project already has. "6,248 bytes" is a number; "19,239 -> 25,487 of
        # 262,144, and 6,248 less heap for whatever scene loads it" is a decision. The
        # second one is the point of importing carefully, and it was invisible until the
        # atlas had been added, built, and measured.
        current = self.estimate()
        budget = current["budget"]
        after = current["total"] + est
        app = current.get("app") or {}
        heap = app.get("heap")

        return {
            "sheet_tiles": [W // tile, H // tile],
            "considered": rw * rh, "empty": empty,
            "unique": len(unique), "capped": len(unique) >= max_tiles,
            "colours": len(colours), "palettes": len(pals), "repaired": repaired,
            "bytes": est, "pct": 100.0 * est / budget,
            "res_before": current["total"], "res_after": after, "budget": budget,
            "res_pct_after": 100.0 * after / budget if budget else 0,
            "res_over": after > budget,
            # An atlas is read whole into a pool slot, so its blob size is still its
            # resident cost -- what changed with WorldTiles is WHEN it is resident, not how
            # big it is. A map that streams its atlases pays for the slots it declared
            # rather than for all of them, and only the pipeline knows that number, so the
            # figure here is the honest upper bound: what this atlas costs while loaded.
            "heap_before": heap,
            "heap_after": (heap - est) if heap is not None else None,
            "strip": strip,
            "bw_strip": bw_strip,
            "thumb": pv.data_uri(im.resize((min(W, 480), int(H * min(W, 480) / W)),
                                           Image.NEAREST)),
        }

    @staticmethod
    def _colorkey_block(colorkey):
        """Record the key, or say nothing at all.

        No key is a legitimate answer and the common one -- art with a real alpha channel
        needs none -- so an absent key writes no line rather than `colorkey = []`, which
        would read as a setting someone chose and left empty.
        """
        if not colorkey:
            return ""
        r, g, b = (int(c) for c in colorkey)
        return (f"# Pixels of this exact colour become transparent. Sheets drawn on a "
                f"flat\n# background with no alpha channel need this; art with real "
                f"alpha does not.\ncolorkey = [{r}, {g}, {b}]\n")

    @staticmethod
    def _exclude_block(exclude):
        """Render the dropped-tile list, wrapped, with a note on what the numbers mean."""
        idx = sorted({int(e) for e in exclude})
        if not idx:
            return ""
        lines, row = [], []
        for i in idx:
            row.append(str(i))
            if len(row) == 16:
                lines.append(", ".join(row))
                row = []
        if row:
            lines.append(", ".join(row))
        body = ",\n  ".join(lines)
        return ("# Cells dropped in the editor. Indices into the region, read left to "
                "right,\n# top to bottom -- so they follow the region above and change "
                "with it.\n"
                f"exclude = [\n  {body}\n]\n")

    def validate_atlas(self, rel, tile, region, max_tiles, exclude=(), colorkey=None,
                       name=None, offset=(0, 0)):
        """Run a candidate carve through the REAL pipeline and report what it says.

        Not a re-implementation of the checks: it calls the same `pack_atlas` the build
        calls, so anything the build would reject is rejected here, in front of the person
        who can still change it. The alternative is what happened before -- the block goes
        into the manifest, Build fails, and now there is a broken atlas in the file to
        remove by hand.

        Errors block. Warnings do not: a capped carve or a repaired tile is a legitimate
        choice, and refusing it would be the editor overruling an author about their own
        art. They are said out loud instead.
        """
        spec = {"name": name or "candidate", "sheet": rel, "tile": int(tile),
                "region": list(region), "max_tiles": int(max_tiles),
                "out": f"{name or 'candidate'}.bin",
                "exclude": list(exclude), "offset": [int(v) for v in offset]}
        if colorkey:
            spec["colorkey"] = list(colorkey)

        warnings = []
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                atlas = pa.pack_atlas(self.root, spec, self.orientation)
        except pa.BuildError as e:
            return {"ok": False, "error": str(e), "warnings": []}
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "warnings": []}

        unique = len(atlas["tiles"])
        if unique >= int(max_tiles):
            warnings.append(
                f"the carve hit max_tiles ({max_tiles}) -- tiles past that were dropped, "
                f"so the atlas may be missing art. Raise max_tiles or carve less.")
        if atlas.get("repaired"):
            warnings.append(
                f"{atlas['repaired']} tile(s) had more than {pa.PALETTE_USABLE} colours "
                f"and were reduced. The art is altered; edit it to avoid that.")

        # Autopick is where a small carve fails, and it fails at BUILD time with a message
        # about roles rather than about the region -- so it is checked here too.
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.autopick_tiles(atlas, ["floor", "wall", "accent"])
        except pa.BuildError as e:
            return {"ok": False, "error": str(e), "warnings": warnings}

        est = self.estimate()
        after = est["total"] + len(atlas["blob"]) if atlas.get("blob") else est["total"]
        if after > est["budget"]:
            warnings.append(
                f"this would put resources over the {est['budget']:,} B appstore cap.")

        return {"ok": True, "error": None, "warnings": warnings,
                "unique": unique, "palettes": len(atlas.get("variants", [])) or None}

    def atlas_spec(self, name):
        """An existing atlas's settings, for loading back into the Atlas tab."""
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == name), None)
        if not spec:
            raise ValueError(f"no atlas named {name!r}")
        return {"name": name, "sheet": spec.get("sheet"), "tile": spec.get("tile", 16),
                "region": spec.get("region", [0, 0, 16, 16]),
                "offset": spec.get("offset", [0, 0]),
                "max_tiles": spec.get("max_tiles", 64),
                "colorkey": spec.get("colorkey"),
                "autopick": list(spec.get("autopick", [])),
                "semantic": {k: int(v) for k, v in spec.get("semantic", {}).items()},
                # Sent as written, so "auto" and a forced true/false stay tellable apart.
                "metatiles": spec.get("metatiles", "auto"),
                "variants": list(spec.get("variants", [])),
                "exclude": [int(e) for e in spec.get("exclude", [])
                            if not isinstance(e, (list, tuple))]}

    @staticmethod
    def _atlas_roles_live(spec, packed):
        """Role -> tile index for one CANDIDATE carve, computed the same way build() does
        it (autopick_tiles, then `semantic` overrides) -- but without a build having ever
        run, which is what lets a tile be edited by name before Build has been pressed
        once. Mirrors the exact snippet build() runs per atlas; kept in step with it by
        hand since a live preview and the real pipeline are two different call sites for
        the same decision.
        """
        r = {}
        if spec.get("autopick"):
            try:
                r.update(pa.autopick_tiles(packed, spec["autopick"]))
            except pa.BuildError:
                pass  # too few solid tiles to autopick from yet -- semantic can still work
        for role, idx in spec.get("semantic", {}).items():
            if isinstance(idx, int) and 0 <= idx < len(packed["tiles"]):
                r[role] = idx
        return r

    def carve_tiles(self, rel, tile, region, max_tiles, exclude=(), colorkey=None,
                    offset=(0, 0), name=None):
        """Every tile a candidate carve actually PACKS -- the metadata-editing
        counterpart to slice_grid's raw sheet cells, which show what goes IN. This shows
        what each tile that makes it in currently MEANS: its role, and its collision.

        Reuses the real pack_atlas / autopick_tiles / parse_atlas_collision the build
        itself calls, so a tile's role and collision here are never a second opinion --
        editing through this can never show something Build would then disagree with.

        When `name` is an atlas already in the manifest, its `autopick`/`semantic`/
        `collision` are folded in so existing role names and collision entries survive
        into the preview; a brand new carve (no such atlas yet) just shows bare tiles.
        """
        spec = {"name": name or "_carve_tiles", "sheet": rel, "tile": int(tile),
                "region": list(region), "max_tiles": int(max_tiles),
                "out": f"{name or '_carve_tiles'}.bin", "exclude": list(exclude),
                "offset": [int(v) for v in offset]}
        if colorkey:
            spec["colorkey"] = list(colorkey)

        existing = next((a for a in self.man.get("atlas", []) if a.get("name") == name),
                        None) if name else None
        if existing:
            for key in ("autopick", "semantic", "collision"):
                if key in existing:
                    spec[key] = existing[key]

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                packed = pa.pack_atlas(self.root, spec, self.orientation)
        except pa.BuildError as e:
            return {"error": str(e)}

        roles = self._atlas_roles_live(spec, packed)
        by_index = {}
        for role, idx in roles.items():
            by_index.setdefault(idx, role)

        try:
            collision = pa.parse_atlas_collision(spec, packed, roles)
        except pa.BuildError as e:
            return {"error": str(e)}

        from PIL import Image
        T = packed["tile_px"]
        tiles = []
        for i, buf in enumerate(packed["tiles"]):
            img = Image.new("RGBA", (T, T), (0, 0, 0, 0))
            ip = img.load()
            for j in range(T):
                for k in range(T):
                    rgb = pv.gcolor_rgb(buf[j * T + k])
                    if rgb:
                        ip[k, j] = rgb + (255,)

            mode, extra = collision.get(i, (pa.COLLISION_NONE, None))
            cinfo = {"mode": mode}
            if mode == pa.COLLISION_SCALED:
                cinfo["rect"] = list(extra)
            elif mode == pa.COLLISION_COMPLEX:
                mask_bytes = extra if extra is not None else pa.pack_collision_mask(buf, T)
                cinfo["mask"] = pa.unpack_collision_mask(mask_bytes, T)
                cinfo["authored"] = extra is not None
            # The art's own opacity, always -- even when an authored mask is in effect
            # above -- so "reset to art" in the editor has something to reset TO without
            # a second round trip (there is no local copy of "what the tile's own opacity
            # says" once an override replaces it in `mask`).
            cinfo["auto_mask"] = pa.unpack_collision_mask(pa.pack_collision_mask(buf, T), T)

            tiles.append({
                "index": i, "role": by_index.get(i),
                "origin": list(packed["origin"][i]),
                "img": pv.data_uri(img.resize((T * 4, T * 4), Image.NEAREST)),
                "collision": cinfo,
            })

        return {"tile_px": T, "tiles": tiles}

    def origin_map(self, name):
        """Where each of an atlas's packed tiles sits in its own source sheet, plus a
        thumbnail of the whole sheet to draw them against.

        Dedup is what makes a packed atlas hard to read: mirror- and rotation-aware
        collapsing (see pack_atlas) reorders everything into first-seen-during-scan
        order, which throws away the spatial layout that made the original art legible.
        The Maps tab's tile picker has no origin of its own to show -- it reads tiles
        from the COMPILED blob, which carries pixels and nothing about where they came
        from -- so this exists to answer "where did this tile come from" on demand,
        fetched once when the picker opens rather than folded into atlases() (which
        reloads on nearly every edit and would pay a full sheet re-read each time for
        no reason most of those reloads care about).

        A live pack_atlas call against the atlas's OWN manifest spec, the same one
        carve_tiles uses, so an origin here can never disagree with what carve_tiles or
        the build itself produced.
        """
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == name), None)
        if not spec:
            raise ValueError(f"no atlas named {name!r}")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                packed = pa.pack_atlas(self.root, spec, self.orientation)
        except pa.BuildError as e:
            return {"error": str(e)}

        from PIL import Image
        im = pa.load_sheet(self.root, spec["sheet"])
        W, H = im.size
        T = packed["tile_px"]
        ox, oy = (int(v) for v in spec.get("offset", (0, 0)))
        # Pixel top-left of each packed tile in the FULL, unscaled sheet -- the client
        # scales this itself against the thumbnail's actual rendered size, so a resize
        # here later cannot silently desync the boxes from the picture under them.
        origin_px = [[ox + tx * T, oy + ty * T] for tx, ty in packed["origin"]]

        MAXW = 480
        scale = min(1.0, MAXW / W)
        thumb = im.resize((max(1, round(W * scale)), max(1, round(H * scale))), Image.NEAREST)
        return {"thumb": pv.data_uri(thumb), "sheet_size": [W, H], "tile_px": T,
                "origin": origin_px}

    def save_role(self, atlas, role, index):
        """Name one tile of an atlas, writing [atlas.semantic] under its block.

        The subtable has to sit immediately after its own [[atlas]] block: in TOML it
        binds to the most recent array element, so the same three lines under a different
        atlas silently name a tile in the wrong tileset.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", role):
            raise ValueError("a role name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == atlas), None)
        if not spec:
            raise ValueError(f"no atlas named {atlas!r}")

        index = int(index)
        built = next((a for a in self.atlases() if a["name"] == atlas), None)
        count = built["count"] if built else spec.get("max_tiles")
        if count is not None and not 0 <= index < count:
            raise ValueError(f"atlas {atlas!r} holds {count} tiles (0-{count - 1}), "
                             f"not {index}")

        # `autopick` runs first and `semantic` overrides it -- that is the pipeline's own
        # documented behaviour ("a manifest can start auto and be pinned down later"), so
        # naming a tile the autopick already claimed is a pin, not a clash. This used to
        # be refused, which left an autopicked name unreachable from the editor entirely:
        # the only way out was hand-editing `autopick`, and the Atlas tab could not even
        # clear it. The caller is told, because the pin does move the tile every map that
        # draws through this role will use.
        pinned = role in spec.get("autopick", [])

        taken = spec.get("semantic", {}).get(role)
        if taken is not None and int(taken) != index:
            raise ValueError(f"{atlas!r} already names tile {taken} {role!r}. "
                             f"Pick another name.")

        lines, start, end = self._atlas_block(atlas)
        entry = f"{role} = {index}"

        # The block ends at the next table header, which IS `[atlas.semantic]` when one is
        # already there.
        if end < len(lines) and lines[end].strip() == "[atlas.semantic]":
            stop = next((j for j in range(end + 1, len(lines))
                         if lines[j].lstrip().startswith("[")), len(lines))
            at = next((j for j in range(end + 1, stop)
                       if re.match(rf"\s*{re.escape(role)}\s*=", lines[j])), None)
            if at is not None:
                lines[at] = entry
            else:
                while stop > end and lines[stop - 1].strip() == "":
                    stop -= 1
                lines[stop:stop] = [entry]
        else:
            while end > start and lines[end - 1].strip() == "":
                end -= 1
            lines[end:end] = ["", "# Tiles this project names. Game code refers to them as"
                                  f" {re.sub(r'[^A-Za-z0-9]', '_', atlas).upper()}_TILE_*.",
                              "[atlas.semantic]", entry, ""]

        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()
        return {"pinned": pinned}

    def remove_role(self, atlas, role):
        """Unname a tile. The tile stays; only the name goes."""
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == atlas), None)
        if not spec or role not in spec.get("semantic", {}):
            raise ValueError(f"atlas {atlas!r} does not name a tile {role!r}")
        users = [ch for ch, e in self.man.get("legend", {}).items()
                 if e.get("tile") == role and (e.get("atlas") or self._default_atlas()) == atlas]
        if users:
            raise ValueError(f"legend {', '.join(repr(c) for c in sorted(users))} "
                             f"resolve through {role!r}. Repoint them first.")
        lines, start, end = self._atlas_block(atlas)
        if not (end < len(lines) and lines[end].strip() == "[atlas.semantic]"):
            raise ValueError(f"no [atlas.semantic] table for {atlas!r}")
        stop = next((j for j in range(end + 1, len(lines))
                     if lines[j].lstrip().startswith("[")), len(lines))
        at = next((j for j in range(end + 1, stop)
                   if re.match(rf"\s*{re.escape(role)}\s*=", lines[j])), None)
        if at is None:
            raise ValueError(f"no role named {role!r}")
        del lines[at]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def _default_atlas(self):
        specs = self.man.get("atlas", [])
        return specs[0]["name"] if specs else None

    def set_autopick(self, atlas, roles):
        """Rewrite one atlas's `autopick` list.

        The importer's only pre-build say over roles: which ones to invent from the art.
        Everything else has to wait for a packed atlas, because a role is an index into
        one and dedup decides what those are.
        """
        for r in roles:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", r):
                raise ValueError(f"role {r!r} must be lowercase letters, digits and "
                                 f"underscores")
        if len(set(roles)) != len(roles):
            raise ValueError("the same role twice")
        lines, start, end = self._atlas_block(atlas)
        entry = "autopick = [" + ", ".join(f'"{r}"' for r in roles) + "]"
        at = next((j for j in range(start, end)
                   if re.match(r"\s*autopick\s*=", lines[j])), None)
        if at is not None and roles:
            lines[at] = entry
        elif at is not None:
            del lines[at]
        elif roles:
            lines[end:end] = [entry]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def _atlas_block(self, name):
        """(lines, start, end) for one [[atlas]] block, end exclusive.

        Line surgery rather than re-emitting from the parsed manifest, because re-emitting
        would discard the comments -- and in this project a manifest's comments are half
        its content.
        """
        lines = open(self.path).read().split("\n")

        # The [[atlas]] whose name matches, up to the next table header at column 0.
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[atlas]]":
                for j in range(i + 1, len(lines)):
                    if lines[j].lstrip().startswith("["):
                        break
                    m = re.match(r'\s*name\s*=\s*"([^"]+)"', lines[j])
                    if m:
                        if m.group(1) == name:
                            start = i
                        break
            if start is not None:
                break
        if start is None:
            raise ValueError(f"no [[atlas]] block named {name!r} in the manifest")

        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))
        return lines, start, end

    def _atlas_full_block(self, name):
        """Like _atlas_block, but extended past every [[atlas.collision]] sub-block that
        follows it -- which _atlas_block stops short of ON PURPOSE (update_atlas and
        set_atlas_extras only ever rewrite the keys before the first one, so simply never
        reaching that far is what leaves collision entries alone). Collision editing needs
        the opposite: the full extent, so a save can find an existing entry anywhere
        inside it, or know where the block truly ends to append a new one.

        TOML itself is why this is correct rather than a guess: `[[atlas.collision]]`
        nests under the MOST RECENTLY OPENED `[[atlas]]` table regardless of what comes
        between, so by construction it can only validly sit here, before the next
        `[[atlas]]` (or any other top-level table).
        """
        lines, start, _ = self._atlas_block(name)
        end = start + 1
        while end < len(lines):
            if lines[end].lstrip().startswith("[[atlas.collision]]"):
                end += 1
                continue
            if lines[end].lstrip().startswith("["):
                break
            end += 1
        return lines, start, end

    def _resolve_atlas_tile(self, spec, packed, roles, tile):
        """A `tile` value (role name or raw index) -> its packed index, the same
        resolution parse_atlas_collision already does -- reused via a throwaway single
        entry rather than duplicated, so there is one place that decides what a `tile`
        field means.
        """
        resolved = pa.parse_atlas_collision(
            {**spec, "collision": [{"tile": tile, "type": "solid"}]}, packed, roles)
        return next(iter(resolved))

    def _atlas_collision_entries(self, name):
        """[(start, end, tile_value)] for every [[atlas.collision]] sub-block belonging
        to `name`, `tile_value` parsed as written (int or quoted string) -- the raw
        material save_atlas_collision/remove_atlas_collision search for a match in.
        """
        lines, astart, aend = self._atlas_full_block(name)
        out = []
        i = astart + 1
        while i < aend:
            if lines[i].strip() == "[[atlas.collision]]":
                estart = i
                eend = next((j for j in range(i + 1, aend)
                            if lines[j].lstrip().startswith("[")), aend)
                tile_value = None
                for j in range(estart + 1, eend):
                    m = re.match(r'\s*tile\s*=\s*(.+?)\s*$', lines[j])
                    if m:
                        raw = m.group(1)
                        sm = re.match(r'^"(.*)"$', raw)
                        tile_value = sm.group(1) if sm else int(raw)
                        break
                out.append((estart, eend, tile_value))
                i = eend
            else:
                i += 1
        return out

    def _atlas_collision_lines(self, tile, kind, rect=None, mask_rows=None):
        body = ["[[atlas.collision]]",
               f'tile = "{tile}"' if isinstance(tile, str) else f"tile = {int(tile)}",
               f'type = "{kind}"']
        if rect is not None:
            body.append(f"rect = [{rect[0]}, {rect[1]}, {rect[2]}, {rect[3]}]")
        if mask_rows is not None:
            body.append('mask = """')
            body.extend(mask_rows)
            body.append('"""')
        return body

    def save_atlas_collision(self, atlas, tile, mode, rect=None, mask_rows=None):
        """Create or rewrite one tile's [[atlas.collision]] entry.

        `tile` is written exactly as given -- a role name or a raw index -- matching the
        one convention [[atlas.collision]] has always used (parse_atlas_collision), so a
        tile named through the picker keeps naming it symbolically and survives a recarve
        the same way a legend entry does.

        Validated by re-running parse_atlas_collision against the atlas as it is packed
        RIGHT NOW, the same "prove it through the real pipeline before it is written"
        contract validate_atlas already keeps for the carve itself.
        """
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == atlas), None)
        if not spec:
            raise ValueError(f"no atlas named {atlas!r}")
        kind = {v: k for k, v in pa.COLLISION_NAMES.items()}.get(mode)
        if kind is None:
            raise ValueError(f"collision mode {mode!r} is not one of "
                             f"{sorted(pa.COLLISION_NAMES.values())}")

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                packed = pa.pack_atlas(self.root, spec, self.orientation)
        except pa.BuildError as e:
            raise ValueError(str(e)) from None
        roles = self._atlas_roles_live(spec, packed)

        candidate = dict(spec)
        entries = [e for e in spec.get("collision", [])
                  if self._resolve_atlas_tile(spec, packed, roles, e.get("tile")) !=
                  self._resolve_atlas_tile(spec, packed, roles, tile)]
        new_entry = {"tile": tile, "type": kind}
        if rect is not None:
            new_entry["rect"] = list(rect)
        if mask_rows is not None:
            new_entry["mask"] = "\n".join(mask_rows)
        candidate["collision"] = entries + [new_entry]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.parse_atlas_collision(candidate, packed, roles)
        except pa.BuildError as e:
            raise ValueError(str(e)) from None

        target = self._resolve_atlas_tile(spec, packed, roles, tile)
        lines, astart, aend = self._atlas_full_block(atlas)
        new_lines = self._atlas_collision_lines(tile, kind, rect, mask_rows)

        for estart, eend, existing_tile in self._atlas_collision_entries(atlas):
            if self._resolve_atlas_tile(spec, packed, roles, existing_tile) == target:
                lines[estart:eend] = new_lines + ([""] if eend < len(lines)
                                                  and lines[eend].strip() else [])
                with open(self.path, "w") as f:
                    f.write("\n".join(lines))
                self.reload()
                return

        # No existing entry for this tile: append at the end of the atlas's full block
        # (past any collision entries already there), ahead of the blank line that
        # separates it from whatever comes next.
        at = aend
        while at > astart and lines[at - 1].strip() == "":
            at -= 1
        lines[at:at] = [""] + new_lines
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def remove_atlas_collision(self, atlas, tile):
        """Delete one tile's [[atlas.collision]] entry -- back to COLLISION_NONE."""
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == atlas), None)
        if not spec:
            raise ValueError(f"no atlas named {atlas!r}")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                packed = pa.pack_atlas(self.root, spec, self.orientation)
        except pa.BuildError as e:
            raise ValueError(str(e)) from None
        roles = self._atlas_roles_live(spec, packed)
        target = self._resolve_atlas_tile(spec, packed, roles, tile)

        lines, astart, aend = self._atlas_full_block(atlas)
        for estart, eend, existing_tile in self._atlas_collision_entries(atlas):
            if self._resolve_atlas_tile(spec, packed, roles, existing_tile) == target:
                gap = 1 if eend < len(lines) and lines[eend].strip() == "" else 0
                lines[estart:eend + gap] = []
                with open(self.path, "w") as f:
                    f.write("\n".join(lines))
                self.reload()
                return
        raise ValueError(f"tile {tile!r} of {atlas!r} has no [[atlas.collision]] entry")

    def atlas_users(self, name):
        """Everything that would break if this atlas went away, in words.

        Returned rather than raised, so the page can warn BEFORE the button is pressed
        instead of only explaining after it is refused.
        """
        specs = self.man.get("atlas", [])
        first = specs[0]["name"] if specs else None
        reasons = []

        for m in self.man.get("map", []):
            drawn = m.get("atlases") or ([m["atlas"]] if "atlas" in m else None)
            if drawn is None:
                # A map with no atlas key draws with the first one declared. Removing that
                # one silently re-points every such map at a different tileset, which
                # rebuilds clean and draws the wrong world.
                if name == first:
                    reasons.append(f"map {m['name']!r} has no `atlas` key, so it draws "
                                   f"with the first atlas declared -- which is this one")
            elif name in drawn:
                reasons.append(f"map {m['name']!r} draws with it")

        for sname, spec in self.man.get("scene", {}).items():
            if name in spec.get("atlases", []):
                reasons.append(f"scene {sname!r} lists it in `atlases`")

        # A legend entry naming the atlas only matters if a map actually PAINTS with that
        # character. The pipeline takes the same view -- it reports the cell, not the
        # legend -- so a character nobody used is a dangling reference, not a dependency,
        # and refusing over it would make removal impossible for no gain.
        for ch, e in self.man.get("legend", {}).items():
            if e.get("atlas") != name:
                continue
            painted = [m["name"] for m in self.man.get("map", [])
                       if ch in m.get("rows", "")]
            if painted:
                reasons.append(f"legend {ch!r} resolves against it and is painted in "
                               + ", ".join(f"map {p!r}" for p in painted))
        return reasons

    def remove_atlas(self, name):
        """Delete an [[atlas]] block, once nothing depends on it.

        The check is the point. Deleting an atlas a map draws with produces a manifest that
        fails to build -- recoverable, but only by hand-editing, which is the thing this
        exists to avoid. So the editor says what still uses it and changes nothing.
        """
        if not any(a.get("name") == name for a in self.man.get("atlas", [])):
            raise ValueError(f"no atlas named {name!r}")

        users = self.atlas_users(name)
        if users:
            raise ValueError(f"{name!r} is still in use: " + "; ".join(users)
                             + ". Point those at another atlas first.")

        lines, start, end = self._atlas_block(name)

        # Take the blank lines that separated this block from the next, so removing an
        # atlas and adding one back does not leave a widening gap. Comments ABOVE the
        # block are left alone: an editor-written atlas keeps its comments inside the
        # block, and a hand-written one may sit under a section heading that belongs to
        # the file rather than to it.
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1

        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def update_atlas(self, name, rel, tile, region, max_tiles, exclude=(),
                     colorkey=None, offset=(0, 0)):
        """Rewrite one atlas's settings in place, keeping everything else in its block.

        Only the keys the Atlas tab owns are replaced; a `metatiles` line, a
        `[atlas.semantic]` table or a paragraph explaining the carve survives untouched.
        """
        lines, start, end = self._atlas_block(name)

        want = {
            "sheet": f'sheet = "{rel}"',
            "tile": f"tile = {int(tile)}",
            "region": f"region = [{region[0]}, {region[1]}, {region[2]}, {region[3]}]",
            "max_tiles": f"max_tiles = {int(max_tiles)}",
        }
        if colorkey:
            r, g, b = (int(c) for c in colorkey)
            want["colorkey"] = f"colorkey = [{r}, {g}, {b}]"
        ox, oy = (int(v) for v in offset)
        if ox or oy:
            want["offset"] = f"offset = [{ox}, {oy}]"

        block = lines[start:end]
        seen = set()
        out = []
        skipping = False
        for line in block:
            key = (re.match(r"\s*([a-z_]+)\s*=", line) or [None, None])[1]
            # `exclude` spans several lines; drop the old one wholesale and re-emit it.
            if skipping:
                if "]" in line:
                    skipping = False
                continue
            if key == "exclude":
                skipping = "]" not in line
                continue
            if key in want:
                out.append(want[key])
                seen.add(key)
                continue
            # A key we manage that is now unset -- a cleared colour key, an offset back to
            # [0, 0] -- goes away rather than being written out as a no-op line.
            if key in ("colorkey", "offset"):
                continue
            out.append(line)

        for key, text in want.items():
            if key not in seen:
                out.append(text)
        if exclude:
            out.append(self._exclude_block(exclude).rstrip("\n"))

        lines[start:end] = out
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def set_atlas_extras(self, name, metatiles=None, variants=None):
        """The two atlas keys the Import form never owned.

        `metatiles` composes each tile from four deduplicated quadrants -- a real saving
        on a full sheet and a loss on a small carve, which is why the pipeline's default
        is "auto" and why forcing it either way has to be possible. `variants` are palette
        swaps: the same art recoloured costs a 16-byte palette instead of another copy of
        every tile, and the pipeline verifies they really are recolours.

        Validated by building the candidate, because "is this actually a recolour" is not
        a question that can be answered from the manifest.
        """
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == name), None)
        if not spec:
            raise ValueError(f"no atlas named {name!r}")

        want = []
        if metatiles is not None:
            if metatiles in ("", "auto"):
                want.append(("metatiles", 'metatiles = "auto"' if metatiles == "auto"
                             else None))
            elif metatiles in (True, False, "true", "false"):
                on = metatiles in (True, "true")
                want.append(("metatiles", f"metatiles = {'true' if on else 'false'}"))
            else:
                frac = float(metatiles)
                if not 0.0 < frac < 1.0:
                    raise ValueError("a metatiles threshold is a fraction between 0 and "
                                     "1 -- the saving quadrants must reach to be worth "
                                     "taking")
                want.append(("metatiles", f"metatiles = {frac}"))

        if variants is not None:
            for v in variants:
                if not os.path.exists(self._safe(v)):
                    raise ValueError(f"no such file: {v}")
            probe = dict(spec)
            probe["variants"] = list(variants)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    pa.pack_atlas(self.root, probe, self.orientation)
            except pa.BuildError as e:
                raise ValueError(str(e)) from None
            want.append(("variants", "variants = " + json.dumps(list(variants))
                         if variants else None))

        if not want:
            return

        lines, start, end = self._atlas_block(name)
        for key, value in want:
            at = next((j for j in range(start, end)
                       if re.match(rf"\s*{key}\s*=", lines[j])), None)
            if at is not None and value:
                lines[at] = value
            elif at is not None:
                del lines[at]
                end -= 1
            elif value:
                # One past the LAST actual key line -- see save_project's own comment for
                # why "walk back over blanks from the block's end" can land a new key
                # among comments belonging to whatever table follows this atlas.
                at = start
                for j in range(start, end):
                    if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                        at = j + 1
                lines[at:at] = [value]
                end += 1
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def add_atlas(self, name, rel, tile, region, max_tiles, exclude=(),
                  colorkey=None, offset=(0, 0)):
        """Append an [[atlas]] block. Appending keeps every existing comment intact."""
        if any(a["name"] == name for a in self.man.get("atlas", [])):
            raise ValueError(f"an atlas named {name!r} already exists")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")

        # The manifest is the build's input, so nothing that would fail the build goes
        # into it from here. Checked server-side as well as in the page: the page can be
        # bypassed, the manifest cannot be un-broken by anything but hand-editing.
        check = self.validate_atlas(rel, tile, region, max_tiles, exclude, colorkey, name,
                                    offset)
        if not check["ok"]:
            raise ValueError(check["error"])

        ox, oy = (int(v) for v in offset)
        offset_line = ""
        if ox or oy:
            offset_line = (f"# Where the tile GRID starts, in PIXELS -- separate from "
                           f"`region`'s tile units, for a sheet whose art does not begin "
                           f"flush with the sheet's own corner.\noffset = [{ox}, {oy}]\n")

        block = f'''

[[atlas]]
name = "{name}"
sheet = "{rel}"
tile = {tile}
# In TILE units, not pixels.
region = [{region[0]}, {region[1]}, {region[2]}, {region[3]}]
{offset_line}max_tiles = {max_tiles}
out = "{name}.bin"
{self._colorkey_block(colorkey)}{self._exclude_block(exclude)}
metatiles = "auto"
# Roles the legend can name. Autopicked so the atlas is usable the moment it is
# imported; replace with an explicit [atlas.semantic] table once you know which tiles
# you want, and no map or game code changes.
autopick = ["floor", "wall", "accent"]

# Collision is a property of the tile, not of where a map paints it -- see
# [[atlas.collision]] in any built example. "wall" solid by default so a freshly
# imported atlas is usable the moment it lands, same as the roles above.
[[atlas.collision]]
tile = "wall"
type = "solid"
'''
        with open(self.path, "a") as f:
            f.write(block)
        self.reload()

