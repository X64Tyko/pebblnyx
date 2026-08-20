"""Sprite sheets and the frame-level pixel editor."""

import contextlib
import io
import json
import os
import re

import pnx_assets as pa                                     # noqa: E402
import pnx_preview as pv                                    # noqa: E402


class SpritesMixin:
    def art_files(self):
        art = os.path.join(self.root, "art")
        out = []
        for dirpath, dirnames, files in os.walk(art) if os.path.isdir(art) else []:
            dirnames[:] = [d for d in dirnames if d not in ("fonts", "__pycache__")]
            for fn in sorted(files):
                if fn.lower().endswith(".png"):
                    full = os.path.join(dirpath, fn)
                    out.append({"path": os.path.relpath(full, self.root), "name": fn,
                                "bytes": os.path.getsize(full)})
        return out

    def art_import(self, name, data, replace=False):
        """Copy a file the user picked into the project's `art/` folder.

        Until this existed the editor could only ever see art that was already inside the
        project, and it offered no way to put it there -- every sheet dropdown was filled
        by walking `art/`, so importing a sprite sheet meant finding the project folder in
        a file manager and copying the PNG in by hand. For the packaged editor that is a
        folder the user has never seen and has no reason to know the location of.

        Decoded rather than trusted. The extension says nothing about the contents, and
        the pipeline is the wrong place to discover that a .png is a renamed .webp: it
        fails there as a build error about an asset, pointing at the file rather than at
        the moment it came in. Pillow parses it here, at the only moment the user is
        holding the file and can pick a different one.
        """
        from PIL import Image

        # The BASENAME only. A browser gives a bare filename for a picked file but a full
        # path for a dropped one on some platforms, and either could carry separators; the
        # destination is chosen here, not by the client.
        base = os.path.basename((name or "").replace("\\", "/")).strip()
        stem, ext = os.path.splitext(base)
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip("._-")
        if not stem:
            raise ValueError("that file has no usable name")
        if ext.lower() != ".png":
            raise ValueError(f"art is imported as PNG; {base!r} is {ext or 'extensionless'}")

        # A sprite sheet for a 200x228 screen is kilobytes. Something megabytes wide is a
        # photo picked by mistake, and the useful moment to say so is before it is sitting
        # in the project being offered in every sheet dropdown.
        if len(data) > 16 * 1024 * 1024:
            raise ValueError(f"{base!r} is {len(data) / 1048576:.0f} MB -- that is not a "
                             f"sprite sheet for a 200x228 screen")

        try:
            im = Image.open(io.BytesIO(data))
            im.verify()                     # structure; consumes the file object
            im = Image.open(io.BytesIO(data))
            im.load()                       # and the pixels, which verify() does not read
        except Exception as e:              # noqa: BLE001
            raise ValueError(f"{base!r} is not a readable PNG: {e}") from None
        if im.format != "PNG":
            raise ValueError(f"{base!r} is a {im.format or 'unknown'} file named .png")

        rel = os.path.join("art", stem + ".png")
        full = self._safe(rel)
        if os.path.exists(full) and not replace:
            raise ValueError(f"art/{stem}.png already exists -- import it under another "
                             f"name, or confirm the replacement")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        return {"path": rel, "w": im.width, "h": im.height, "bytes": len(data)}

    def sprite_read(self, rel):
        """Load a PNG as ARGB2222 indices, so it can be edited in device colours."""
        from PIL import Image
        full = self._safe(rel)
        im = Image.open(full).convert("RGBA")
        px = im.load()
        return {"w": im.width, "h": im.height,
                "pixels": [pa.to_gcolor8(px[x, y])
                           for y in range(im.height) for x in range(im.width)]}

    def sheet_image(self, rel):
        """The WHOLE sheet, as a data-uri, untouched -- not split into per-cell
        thumbnails the way sheet_frames renders its cells.

        The sprite tab's grid picker draws its grid as LINES over this image rather than
        cropping the sheet into a grid of buttons (sheet_frames' own approach, still used
        for the picker's cell hit-testing/blank-detection), because seeing the grid
        against the intact art is what makes spacing/origin easy to eyeball -- the same
        reasoning the freeform picker draws its drag-box over this image too, for a sheet
        with no grid to overlay at all.
        """
        from PIL import Image
        im = Image.open(self._safe(rel)).convert("RGBA")
        return {"img": pv.data_uri(im), "w": im.width, "h": im.height}

    def sheet_frames(self, rel, fw, fh, ox=0, oy=0, gx=0, gy=0, colorkey=None):
        """Every frame-sized cell of a sheet, rendered, so frames can be PICKED.

        The declare form could only describe a vertical stack, which is the layout the
        engine's own examples happen to use and not the layout most sheets ship in -- a
        run of poses across a row, several rows to a character. Anything else meant
        writing `frames = [[x, y, w, h], ...]` by hand.

        Cells are read through the colour key, so a cell that is nothing but background
        reads as blank here exactly as it will when packed -- which is what makes the
        empty cells of a ragged sheet obvious before they are picked by mistake.
        """
        from PIL import Image
        im = Image.open(self._safe(rel)).convert("RGBA")
        px = im.load()
        W, H = im.size
        fw, fh = max(1, int(fw)), max(1, int(fh))
        ox, oy, gx, gy = int(ox), int(oy), int(gx), int(gy)

        cols = max(0, (W - ox + gx) // (fw + gx))
        rows = max(0, (H - oy + gy) // (fh + gy))

        # A large sheet at a small frame size is thousands of PNGs. Capped and reported
        # rather than quietly hanging the browser, the same way the atlas slicer is.
        LIMIT = 512
        capped = cols * rows > LIMIT

        cells = []
        for j in range(rows):
            for i in range(cols):
                if len(cells) >= LIMIT:
                    break
                x, y = ox + i * (fw + gx), oy + j * (fh + gy)
                if x + fw > W or y + fh > H:
                    continue
                buf = [pa.to_gcolor8(px[x + a, y + b], colorkey)
                       for b in range(fh) for a in range(fw)]
                img = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
                ip = img.load()
                for b in range(fh):
                    for a in range(fw):
                        rgb = pv.gcolor_rgb(buf[b * fw + a])
                        if rgb:
                            ip[a, b] = rgb + (255,)
                scale = max(1, min(4, 64 // max(fw, fh) or 1))
                cells.append({"i": len(cells), "x": x, "y": y, "w": fw, "h": fh,
                              "blank": not any(buf),
                              "img": pv.data_uri(img.resize((fw * scale, fh * scale),
                                                            Image.NEAREST))})
        return {"cols": cols, "rows": rows, "cells": cells, "capped": capped,
                "limit": LIMIT, "sheet": [W, H]}

    def sprite_frames(self, name):
        """Every DECLARED frame of a [[sprite]], rendered, so a sprite can be opened.

        The canvas could only open a whole PNG and guess the frame split from the file
        height against whatever height the canvas happened to be showing. For a stacked
        24x144 sheet with the canvas left at 24 that guess is six frames of 24x24, when
        the manifest sitting next to it says four of 24x36 -- and the editor never asked.

        So this asks. Frame rectangles come from the declaration rather than from a slicer,
        which also means a sprite whose frames are scattered across a sheet opens exactly
        as well as a stacked one: there is no layout to re-derive, only rects to read.

        Same cell shape as sheet_frames, so the picker markup and styling are shared.
        """
        from PIL import Image
        spec = next((sp for sp in self.man.get("sprite", [])
                     if sp.get("name") == name), None)
        if not spec:
            raise ValueError(f"no sprite named {name!r}")
        rel = spec.get("sheet")
        if not rel:
            raise ValueError(f"sprite {name!r} declares no sheet")

        key = spec.get("colorkey")
        im = Image.open(self._safe(rel)).convert("RGBA")
        px = im.load()
        W, H = im.size

        cells, bad, bufs, dims = [], [], {}, {}
        for i, f in enumerate(spec.get("frames", [])):
            # A frame entry may carry a trailing (ox, oy) origin (pack_sprite's own
            # [x,y,w,h] or [x,y,w,h,ox,oy] forms) -- only the rect matters for a preview.
            x, y, w, h = (int(v) for v in f[:4])
            # Reported rather than raised: a sprite whose sheet was re-imported smaller is
            # exactly when someone needs to open it and look, and refusing the whole list
            # because frame 5 hangs off the edge would deny them that.
            if x < 0 or y < 0 or x + w > W or y + h > H:
                bad.append(i)
                continue
            buf = [pa.to_gcolor8(px[x + a, y + b], key)
                   for b in range(h) for a in range(w)]
            bufs[i] = buf
            dims[i] = (w, h)
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ip = img.load()
            for b in range(h):
                for a in range(w):
                    rgb = pv.gcolor_rgb(buf[b * w + a])
                    if rgb:
                        ip[a, b] = rgb + (255,)
            scale = max(1, min(4, 64 // max(w, h) or 1))
            cells.append({"i": i, "x": x, "y": y, "w": w, "h": h,
                          "blank": not any(buf),
                          "img": pv.data_uri(img.resize((w * scale, h * scale),
                                                        Image.NEAREST))})

        # Collision, per frame -- parsed the same way pack_sprite itself does (frame-local,
        # PRE-rotation coordinates, matching these PRE-rotation preview thumbnails; see
        # pack_sprite's own comment on why sprite collision is authored in the author's
        # frame rather than the atlas convention's post-rotation one), so what this shows
        # is exactly what a build would validate -- not a second implementation of the check.
        def resolve_frame(entry):
            frame = entry.get("frame")
            if not isinstance(frame, int) or isinstance(frame, bool):
                raise ValueError(f"sprite {name!r}: collision entry `frame` must be an "
                                 f"integer, not {frame!r}")
            return frame
        collision = pa.parse_shape_collision(spec.get("collision", []), resolve_frame,
                                             len(spec.get("frames", [])),
                                             lambda i: dims.get(i, (0, 0)), f"sprite {name!r}")
        for c in cells:
            i = c["i"]
            w, h = dims[i]
            mode, kind, extra = collision.get(i, (pa.COLLISION_NONE, pa.COLLISION_KIND_WALL,
                                                   None))
            cinfo = {"mode": mode, "kind": kind}
            if mode == pa.COLLISION_SCALED:
                cinfo["rect"] = list(extra)
            elif mode == pa.COLLISION_COMPLEX:
                mask_bytes = extra if extra is not None else pa.pack_collision_mask(
                    bufs[i], w, h)
                cinfo["mask"] = pa.unpack_collision_mask(mask_bytes, w, h)
                cinfo["authored"] = extra is not None
            # The art's own opacity, always -- so "reset to art" has something to reset TO
            # without a second round trip, same reasoning as the atlas tile editor's own.
            cinfo["auto_mask"] = pa.unpack_collision_mask(
                pa.pack_collision_mask(bufs[i], w, h), w, h)
            c["collision"] = cinfo

        return {"name": name, "sheet": rel, "cells": cells,
                "out_of_bounds": bad, "sheet_size": [W, H]}

    def frame_read(self, rel, x, y, w, h):
        """One frame of a sheet as ARGB2222 indices, so a single pose can be edited.

        The pixel editor could only open a whole PNG, so touching one frame of an eight
        pose sheet meant loading all eight and finding the right one by eye.
        """
        from PIL import Image
        im = Image.open(self._safe(rel)).convert("RGBA")
        W, H = im.size
        x, y, w, h = int(x), int(y), int(w), int(h)
        if x < 0 or y < 0 or x + w > W or y + h > H:
            raise ValueError(f"frame {x},{y} {w}x{h} runs past the sheet ({W}x{H})")
        px = im.load()
        return {"w": w, "h": h,
                "pixels": [pa.to_gcolor8(px[x + a, y + b])
                           for b in range(h) for a in range(w)]}

    def frame_write(self, rel, x, y, w, h, pixels):
        """Composite an edited frame back into its sheet, leaving the rest untouched.

        A write rather than a replace: the other frames on the sheet are someone else's
        work, and saving one pose should not be able to lose the row it sits in.
        """
        from PIL import Image
        full = self._safe(rel)
        if not os.path.exists(full):
            raise ValueError(f"no such sheet: {rel}")
        x, y, w, h = int(x), int(y), int(w), int(h)
        if len(pixels) != w * h:
            raise ValueError(f"expected {w * h} pixels, got {len(pixels)}")

        im = Image.open(full).convert("RGBA")
        W, H = im.size
        if x < 0 or y < 0 or x + w > W or y + h > H:
            raise ValueError(f"frame {x},{y} {w}x{h} runs past the sheet ({W}x{H})")

        put = im.load()
        for i, v in enumerate(pixels):
            rgb = pv.gcolor_rgb(int(v))
            put[x + i % w, y + i // w] = (rgb + (255,)) if rgb else (0, 0, 0, 0)
        im.save(full)
        return {"ok": True, "path": os.path.relpath(full, self.root),
                "bytes": os.path.getsize(full)}

    def sprite_write(self, rel, w, h, pixels):
        """Write ARGB2222 values back out as an ordinary RGBA PNG.

        A PNG rather than a private format so the result is an ordinary art file: it goes
        through the same importer as anything drawn elsewhere, and nothing in the pipeline
        needs to know it came from here.
        """
        from PIL import Image
        if not rel.lower().endswith(".png"):
            rel += ".png"
        if len(pixels) != w * h:
            raise ValueError(f"expected {w * h} pixels, got {len(pixels)}")

        full = self._safe(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        put = im.load()
        for i, v in enumerate(pixels):
            rgb = pv.gcolor_rgb(int(v))
            if rgb:
                put[i % w, i // w] = rgb + (255,)
        im.save(full)
        return {"ok": True, "path": os.path.relpath(full, self.root),
                "bytes": os.path.getsize(full)}

    def sprites(self):
        """Every [[sprite]], with its built size where a build exists."""
        out = []
        for spec in self.man.get("sprite", []):
            frames = [list(f) for f in spec.get("frames", [])]
            entry = {"name": spec.get("name"), "sheet": spec.get("sheet"),
                     "frames": frames,
                     "anim": dict(spec.get("anim", {})),
                     "collision": list(spec.get("collision", [])),
                     "variants": list(spec.get("variants", [])),
                     "bw_variant": spec.get("bw_variant"),
                     "colorkey": list(spec["colorkey"]) if spec.get("colorkey") else None,
                     "w": frames[0][2] if frames else None,
                     "h": frames[0][3] if frames else None,
                     "bytes": None}
            blob = os.path.join(self.res, spec.get("out", f"{entry['name']}.bin"))
            if os.path.exists(blob):
                entry["bytes"] = os.path.getsize(blob)
            out.append(entry)
        return out

    def validate_sprite(self, name, sheet, frames, anim=None, variants=(),
                        colorkey=None, bw_variant=None, collision=()):
        """Run a candidate sprite through the REAL pack_sprite and report what it says.

        Same bargain as validate_atlas: not a second implementation of the frame checks,
        the actual one -- so a frame running off the sheet, an odd pixel count that cannot
        pack at 4bpp, an origin outside its own frame, an anim naming a frame that does
        not exist, or a collision entry naming a bad frame/rect/mask all fail here rather
        than after the block is in the manifest. `bw_variant` gets the same treatment:
        pack_sprite is what actually knows the variant names (derived from each path's
        basename), so this reports the real rejection rather than reimplementing the
        check.

        `sizes` replaces the single `w`/`h` this used to report: frames are not required
        to share one size any more (a tightly packed sheet's rarely do), so there is no
        longer one number to report -- a caller wanting a specific frame's size reads
        `sizes[i]`.
        """
        spec = {"name": name or "candidate", "sheet": sheet,
                "frames": [list(f) for f in frames],
                "out": f"{name or 'candidate'}.bin"}
        if anim:
            spec["anim"] = dict(anim)
        if variants:
            spec["variants"] = list(variants)
        if colorkey:
            spec["colorkey"] = list(colorkey)
        if bw_variant:
            spec["bw_variant"] = bw_variant
        if collision:
            spec["collision"] = list(collision)
        if not spec["frames"]:
            return {"ok": False, "error": "a sprite needs at least one frame"}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                built = pa.pack_sprite(self.root, spec, self.orientation)
        except pa.BuildError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "frames": len(spec["frames"]),
                "sizes": [list(d) for d in built["dims"]],
                "variants": len(spec.get("variants", [])),
                "variant_names": [v["name"] for v in built["variants"]]}

    def _sprite_block(self, name):
        """(lines, start, end) for one [[sprite]], stepping over its [sprite.anim] and
        any [[sprite.collision]] entries -- both belong to THIS sprite, not a new
        top-level section, so the end scan has to recognise the array-of-tables form
        (`[[sprite.collision]]`) as well as the plain-subtable one (`[sprite.anim]`), or
        it would treat a sprite's own collision entries as the start of something else
        and cut the block off before them.
        """
        lines = open(self.path).read().split("\n")
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[sprite]]":
                nxt = next((j for j in range(i + 1, len(lines))
                            if lines[j].lstrip().startswith("[")), len(lines))
                if any(re.match(rf'^name\s*=\s*"{re.escape(name)}"', lines[j].strip())
                       for j in range(i + 1, nxt)):
                    start = i
                    break
        if start is None:
            return None
        end = len(lines)
        for j in range(start + 1, len(lines)):
            s = lines[j].lstrip()
            if s.startswith("[") and not (s.startswith("[sprite.")
                                          or s.startswith("[[sprite.")):
                end = j
                break
        return lines, start, end

    def _sprite_collision_entries(self, name):
        """[(start, end, frame)] for every [[sprite.collision]] sub-block belonging to
        `name` -- the raw material save_sprite_collision/remove_sprite_collision search
        for a match in. Mirrors atlas.py's _atlas_collision_entries, simplified: a sprite
        frame has no role system, so `frame` is always a plain int, never a quoted role
        name.
        """
        block = self._sprite_block(name)
        if not block:
            return []
        lines, start, end = block
        out = []
        i = start + 1
        while i < end:
            if lines[i].strip() == "[[sprite.collision]]":
                estart = i
                eend = next((j for j in range(i + 1, end)
                            if lines[j].lstrip().startswith("[")), end)
                frame = None
                for j in range(estart + 1, eend):
                    m = re.match(r"\s*frame\s*=\s*(\d+)\s*$", lines[j])
                    if m:
                        frame = int(m.group(1))
                        break
                out.append((estart, eend, frame))
                i = eend
            else:
                i += 1
        return out

    def _sprite_collision_lines(self, frame, type_name, kind_name="wall", rect=None,
                                mask_rows=None):
        body = ["[[sprite.collision]]", f"frame = {int(frame)}", f'type = "{type_name}"']
        # Omitted when "wall", same as [[atlas.collision]]'s own convention -- WALL is the
        # default (COLLISION_KIND_NAMES), so an unremarkable entry stays exactly as short
        # as it always was.
        if kind_name != "wall":
            body.append(f'kind = "{kind_name}"')
        if rect is not None:
            body.append(f"rect = [{rect[0]}, {rect[1]}, {rect[2]}, {rect[3]}]")
        if mask_rows is not None:
            body.append('mask = """')
            body.extend(mask_rows)
            body.append('"""')
        return body

    def save_sprite_collision(self, name, frame, mode, kind=pa.COLLISION_KIND_WALL,
                              rect=None, mask_rows=None):
        """Create or rewrite one frame's [[sprite.collision]] entry.

        Validated by re-running pack_sprite against the sprite as declared RIGHT NOW plus
        this candidate entry, the same "prove it through the real pipeline before it is
        written" contract save_atlas_collision already keeps -- pack_sprite is what
        actually knows a frame's own (pre-rotation) w/h, so this reports the real
        rejection rather than reimplementing the bounds check.
        """
        spec = next((sp for sp in self.man.get("sprite", []) if sp.get("name") == name), None)
        if not spec:
            raise ValueError(f"no sprite named {name!r}")
        type_name = {v: k for k, v in pa.COLLISION_NAMES.items()}.get(mode)
        if type_name is None:
            raise ValueError(f"collision mode {mode!r} is not one of "
                             f"{sorted(pa.COLLISION_NAMES.values())}")
        kind_name = {v: k for k, v in pa.COLLISION_KIND_NAMES.items()}.get(kind)
        if kind_name is None:
            raise ValueError(f"collision kind {kind!r} is not one of "
                             f"{sorted(pa.COLLISION_KIND_NAMES.values())}")

        entries = [e for e in spec.get("collision", []) if e.get("frame") != frame]
        new_entry = {"frame": frame, "type": type_name}
        if kind_name != "wall":
            new_entry["kind"] = kind_name
        if rect is not None:
            new_entry["rect"] = list(rect)
        if mask_rows is not None:
            new_entry["mask"] = "\n".join(mask_rows)
        candidate = {**spec, "collision": entries + [new_entry]}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.pack_sprite(self.root, candidate, self.orientation)
        except pa.BuildError as e:
            raise ValueError(str(e)) from None

        block = self._sprite_block(name)
        if not block:
            raise ValueError(f"no sprite named {name!r}")
        lines, start, end = block
        new_lines = self._sprite_collision_lines(frame, type_name, kind_name, rect, mask_rows)

        for estart, eend, existing_frame in self._sprite_collision_entries(name):
            if existing_frame == frame:
                lines[estart:eend] = new_lines + ([""] if eend < len(lines)
                                                  and lines[eend].strip() else [])
                with open(self.path, "w") as f:
                    f.write("\n".join(lines))
                self.reload()
                return

        # No existing entry for this frame: append at the end of the sprite's full block
        # (past any collision entries already there), ahead of the blank line that
        # separates it from whatever comes next.
        at = end
        while at > start and lines[at - 1].strip() == "":
            at -= 1
        lines[at:at] = [""] + new_lines
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def remove_sprite_collision(self, name, frame):
        """Delete one frame's [[sprite.collision]] entry -- back to COLLISION_NONE."""
        if not any(sp.get("name") == name for sp in self.man.get("sprite", [])):
            raise ValueError(f"no sprite named {name!r}")
        lines, start, end = self._sprite_block(name)
        for estart, eend, existing_frame in self._sprite_collision_entries(name):
            if existing_frame == frame:
                gap = 1 if eend < len(lines) and lines[eend].strip() == "" else 0
                lines[estart:eend + gap] = []
                with open(self.path, "w") as f:
                    f.write("\n".join(lines))
                self.reload()
                return
        raise ValueError(f"frame {frame!r} of {name!r} has no [[sprite.collision]] entry")

    def sprite_users(self, name):
        """The scenes that load this sprite, so removing it can refuse and say which."""
        return [f"scene {s}" for s, spec in self.man.get("scene", {}).items()
                if name in spec.get("sprites", [])]

    @staticmethod
    def _anim_toml_line(key, value):
        """One [sprite.anim] entry, formatted by shape: an int is a pose (the `#spanim`
        field), a list or dict is a clip (the clip builder, sprites.js) -- the three forms
        pnx_assets.parse_sprite_anim itself accepts.
        """
        if isinstance(value, list):
            return f"{key} = [" + ", ".join(str(int(v)) for v in value) + "]"
        if isinstance(value, dict):
            parts = ["frames = [" + ", ".join(str(int(v)) for v in value["frames"]) + "]"]
            if value.get("fps") is not None:
                parts.append(f"fps = {int(value['fps'])}")
            if value.get("loop") is not None:
                parts.append(f"loop = {'true' if value['loop'] else 'false'}")
            if value.get("durations") is not None:
                parts.append("durations = ["
                             + ", ".join(str(int(d)) for d in value["durations"]) + "]")
            return f"{key} = {{ " + ", ".join(parts) + " }"
        return f"{key} = {int(value)}"

    def save_sprite(self, name, sheet, frames, anim=None, variants=(), colorkey=None,
                    bw_variant=None):
        """Create or rewrite one [[sprite]] block, validated the way the build will.

        `anim` is the WHOLE [sprite.anim] table, poses and clips alike: an int is a pose
        (app.js's `#spanim` field, one name per frame position), a list or table is a clip
        (pnx_assets.py's parse_sprite_anim, the sprite tab's own clip builder in
        sprites.js). Both halves round-trip through the editor now -- loaded from the
        manifest into their own UI on open, merged back into one dict here on save -- so
        this is a full replace, not a merge with what's on disk: anything this call's
        `anim` does not name is gone, which is what makes deleting a clip in the builder
        actually take effect. `[[sprite.collision]]` has no parameter here at all, so
        whatever is already there is left exactly alone (`_sprite_block` treats it as part
        of the sprite's own block, not a boundary).
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")

        anim_to_write = dict(anim or {})
        for a in anim_to_write:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", a):
                raise ValueError(f"anim name {a!r} must be lowercase letters, digits and "
                                 f"underscores -- it becomes a C identifier")

        check = self.validate_sprite(name, sheet, frames, anim_to_write, variants,
                                     colorkey, bw_variant)
        if not check["ok"]:
            raise ValueError(check["error"])

        frame_line = ("frames = ["
                      + ", ".join("[" + ", ".join(str(int(v)) for v in f) + "]"
                                  for f in frames) + "]")
        want = [
            ("name", f'name = "{name}"'),
            ("sheet", f'sheet = "{sheet}"'),
            ("frames", frame_line),
            ("out", f'out = "{name}.bin"'),
            ("colorkey", ("colorkey = [" + ", ".join(str(int(c)) for c in colorkey) + "]")
                         if colorkey else None),
            ("variants", "variants = " + json.dumps(list(variants)) if variants else None),
            # Which variant (or the base, if unset) becomes this sprite's ONE 1-bit
            # rendering -- see tools/pnx_assets.py's own comment above `bw_variant` in
            # pack_sprite for why a monochrome screen does not try to preserve colour
            # variant selection at all.
            ("bw_variant", f'bw_variant = "{bw_variant}"' if bw_variant else None),
        ]

        block = self._sprite_block(name)
        if not block:
            body = [v for _, v in want if v]
            # The subtable goes last, because it closes the block: any simple key written
            # after it would land in [sprite.anim] instead of on the sprite.
            if anim_to_write:
                body += ["", "[sprite.anim]"] + [self._anim_toml_line(k, v)
                                                 for k, v in anim_to_write.items()]
            lines = open(self.path).read().split("\n") + ["", "[[sprite]]"] + body
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()
            return

        # Key at a time, for the same reason save_scene does it: replacing the block would
        # be shorter and would eat the comments inside it. The example's npc sprite
        # explains its palette variants in two lines sitting directly above the key.
        lines, start, end = block
        anim_at = next((j for j in range(start + 1, end)
                        if lines[j].strip() == "[sprite.anim]"), None)
        # The first [[sprite.collision]] entry (if any) also bounds the "simple key"
        # region -- collision entries carry their own `type =`/`kind =` lines, which
        # would otherwise look like this SPRITE's own simple keys to the scans below.
        collision_at = next((j for j in range(start + 1, end)
                             if lines[j].strip() == "[[sprite.collision]]"), None)
        simple_end = min(x for x in (anim_at, collision_at, end) if x is not None)

        for key, value in want:
            at = next((j for j in range(start + 1, simple_end)
                       if re.match(rf"\s*{key}\s*=", lines[j])), None)
            if at is not None and value:
                lines[at] = value
            elif at is not None:
                del lines[at]
                simple_end -= 1
                end -= 1
                if anim_at is not None:
                    anim_at -= 1
            elif value:
                # One past the LAST actual `key = value` line, not "walk back over blank
                # lines from the block's end" -- the block's own end can (and here,
                # routinely does) include trailing comments that belong to the NEXT
                # section, since block detection only looks for the next `[`. A sprite's
                # own comments, sitting directly above one of ITS keys, are never touched
                # by this either way: this only chooses where a key that has never
                # appeared in the block before gets inserted, never where an existing
                # one's neighbouring comment lives.
                at = start + 1
                for j in range(start + 1, simple_end):
                    if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                        at = j + 1
                lines[at:at] = [value]
                simple_end += 1
                end += 1
                if anim_at is not None:
                    anim_at += 1

        # The anim subtable is replaced wholesale rather than key by key: an anim name is
        # a bare `name = index` (or, for a clip, `name = [...]`/`name = {...}`) pair with
        # nothing to explain, and the set of them changes as a set when frames are added
        # or removed -- `anim_to_write` is the editor's complete, current picture (poses
        # and clips both), so this is the one and only place anim gets written.
        if anim_at is not None:
            stop = next((j for j in range(anim_at + 1, end)
                         if lines[j].lstrip().startswith("[")), end)
            lines[anim_at:stop] = (["[sprite.anim]"]
                                   + [self._anim_toml_line(k, v)
                                      for k, v in anim_to_write.items()]
                                   if anim_to_write else [])
        elif anim_to_write:
            # Same reasoning as the simple-key case above -- bounded by `simple_end`, not
            # `end`, so this cannot land inside a [[sprite.collision]] entry that follows.
            at = start + 1
            for j in range(start + 1, simple_end):
                if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                    at = j + 1
            lines[at:at] = ["", "[sprite.anim]"] + [self._anim_toml_line(k, v)
                                                    for k, v in anim_to_write.items()]

        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def remove_sprite(self, name):
        """Delete a [[sprite]], once no scene loads it. The art on disk is left alone."""
        block = self._sprite_block(name)
        if not block:
            raise ValueError(f"no sprite named {name!r}")
        users = self.sprite_users(name)
        if users:
            raise ValueError(f"cannot remove {name!r} — {', '.join(users)} loads it. "
                             f"Drop it there first.")
        lines, start, end = block
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

