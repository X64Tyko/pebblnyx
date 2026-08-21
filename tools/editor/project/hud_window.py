"""HUD windows: placed elements (panels/sprites/bars/text), anchored per orientation and
bound to a [[hud_var]] (Phase 2, see project/hud.py), animated by a show/hide slide.

Mirrors AtlasMixin's [[atlas.collision]] shape throughout: [[hud_window.element]] nests
under the most recently opened [[hud_window]] the same way, so _hud_window_full_block/
_hud_window_element_entries below are the direct analogue of _atlas_full_block/
_atlas_collision_entries, adapted for elements identified by POSITION rather than by a
natural key (an element has no name of its own to key on the way a collision entry's
`tile` does).
"""

import contextlib
import io
import re

import pnx_assets as pa                                     # noqa: E402
import pnx_preview as pv                                    # noqa: E402

# 9-way anchor -> (x, y) on a W x H screen, matching pnx_hud_window.c's own
# anchor_point() exactly (that file is this module's runtime counterpart).
_HUD_ANCHOR_XY = {
    "top_left": lambda w, h: (0, 0), "top": lambda w, h: (w // 2, 0),
    "top_right": lambda w, h: (w, 0), "left": lambda w, h: (0, h // 2),
    "center": lambda w, h: (w // 2, h // 2), "right": lambda w, h: (w, h // 2),
    "bottom_left": lambda w, h: (0, h), "bottom": lambda w, h: (w // 2, h),
    "bottom_right": lambda w, h: (w, h),
}

# -1 near/top, 0 centre, 1 far/bottom -- matching pnx_hud_window.c's own anchor_side_h/
# anchor_side_v exactly (see hud_window_preview's own comment on why this has to match).
_HUD_ANCHOR_SIDE_H = {"top_right": 1, "right": 1, "bottom_right": 1,
                     "top": 0, "center": 0, "bottom": 0}
_HUD_ANCHOR_SIDE_V = {"bottom_left": 1, "bottom": 1, "bottom_right": 1,
                     "left": 0, "center": 0, "right": 0}


class HudWindowMixin:
    def hud_windows(self):
        """Every [[hud_window]] and its nested [[hud_window.element]] entries, as
        declared -- no build required to list them."""
        out = []
        for w in self.man.get("hud_window", []):
            out.append({
                "name": w.get("name"),
                "show_ms": w.get("show_ms", 200),
                "hide_ms": w.get("hide_ms", 200),
                "ease": w.get("ease", "linear"),
                "slide": list(w.get("slide", [0, 0])),
                "elements": [dict(e) for e in w.get("element", [])],
            })
        return out

    def _hud_sprite_names(self):
        return {sp.get("name") for sp in self.man.get("sprite", [])}

    def _hud_nine_slice_names(self):
        return {ns.get("name") for ns in self.man.get("nine_slice", [])}

    def _hud_font_names(self):
        return {f.get("name") for f in self.man.get("font", [])}

    def _hud_var_types(self):
        return {hv.get("name"): hv.get("type") for hv in self.man.get("hud_var", [])}

    def _hud_window_block(self, name):
        """(lines, start, end) for one [[hud_window]]'s own key span, stopping before
        any nested [[hud_window.element]] -- same walk as _nine_slice_block."""
        lines = open(self.path).read().split("\n")
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[hud_window]]":
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
            if lines[j].lstrip().startswith("["):
                end = j
                break
        return lines, start, end

    def _hud_window_full_block(self, name):
        """Like _hud_window_block, but extended past every [[hud_window.element]] that
        follows -- see this file's own top comment for why that is always correct
        (the same reasoning _atlas_full_block gives for [[atlas.collision]])."""
        block = self._hud_window_block(name)
        if not block:
            return None
        lines, start, _ = block
        end = start + 1
        while end < len(lines):
            if lines[end].lstrip().startswith("[[hud_window.element]]"):
                end += 1
                continue
            if lines[end].lstrip().startswith("["):
                break
            end += 1
        return lines, start, end

    def _hud_window_element_entries(self, name):
        """[(start, end)] for every [[hud_window.element]] belonging to `name`, in
        declaration order -- an element's POSITION is its identity here (elements carry
        no name of their own), which is what save/remove_hud_window_element's own
        `index` parameter addresses.
        """
        block = self._hud_window_full_block(name)
        if not block:
            return []
        lines, wstart, wend = block
        out = []
        i = wstart + 1
        while i < wend:
            if lines[i].strip() == "[[hud_window.element]]":
                eend = next((j for j in range(i + 1, wend)
                            if lines[j].lstrip().startswith("[")), wend)
                out.append((i, eend))
                i = eend
            else:
                i += 1
        return out

    def save_hud_window(self, name, show_ms=200, hide_ms=200, ease="linear", slide=(0, 0)):
        """Create or rewrite one [[hud_window]]'s own fields -- never touches its nested
        [[hud_window.element]] entries, matching save_atlas's own "keys only" contract
        that leaves [[atlas.collision]] alone.

        Deliberately does NOT run the real pnx_assets.parse_hud_windows here, unlike
        every other save_* in this codebase: that check requires at least one element,
        and a window is created empty here, then populated by save_hud_window_element
        afterward -- which is where the real, element-count-inclusive check runs. The
        same split save_scene's own comment describes for a structure the build-time
        check cannot usefully validate mid-edit.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")
        if ease not in pa.HUD_EASES:
            raise ValueError(f"ease must be one of {', '.join(pa.HUD_EASES)}")
        for label, ms in (("show_ms", show_ms), ("hide_ms", hide_ms)):
            if not isinstance(ms, int) or isinstance(ms, bool) or not 0 <= ms <= 0xFFFF:
                raise ValueError(f"{label} must be 0-65535")
        if not (isinstance(slide, (list, tuple)) and len(slide) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in slide)):
            raise ValueError("slide must be [dx, dy]")

        want = [
            ("name", f'name = "{name}"'),
            ("show_ms", f"show_ms = {int(show_ms)}"),
            ("hide_ms", f"hide_ms = {int(hide_ms)}"),
            ("ease", f'ease = "{ease}"'),
            ("slide", f"slide = [{int(slide[0])}, {int(slide[1])}]"),
        ]

        block = self._hud_window_block(name)
        if not block:
            body = [v for _, v in want]
            lines = open(self.path).read().split("\n") + ["", "[[hud_window]]"] + body
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()
            return

        lines, start, end = block
        for key, value in want:
            at = next((j for j in range(start + 1, end)
                       if re.match(rf"\s*{key}\s*=", lines[j])), None)
            if at is not None:
                lines[at] = value
            else:
                at = start + 1
                for j in range(start + 1, end):
                    if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                        at = j + 1
                lines[at:at] = [value]
                end += 1

        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def remove_hud_window(self, name):
        """Delete a [[hud_window]], nested [[hud_window.element]] entries included.
        Nothing outside a window's own declaration can reference it yet (no
        hud_window_users check for the same reason hud_var got none in Phase 2 -- see
        project/hud.py's own comment), so there is nothing to refuse this for.
        """
        block = self._hud_window_full_block(name)
        if not block:
            raise ValueError(f"no hud_window named {name!r}")
        lines, start, end = block
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def _hud_window_element_lines(self, kind, anchor, offset, fields):
        body = ["[[hud_window.element]]", f'kind = "{kind}"']
        if anchor != "top_left":
            body.append(f'anchor = "{anchor}"')
        if offset[0] or offset[1]:
            body.append(f"offset = [{int(offset[0])}, {int(offset[1])}]")
        if kind == "panel":
            body += [f'panel = "{fields["panel"]}"', f"w = {int(fields['w'])}",
                     f"h = {int(fields['h'])}"]
        elif kind == "sprite":
            body.append(f'sprite = "{fields["sprite"]}"')
            if fields.get("frame"):
                body.append(f"frame = {int(fields['frame'])}")
        elif kind == "bar":
            body += [f'hud_var = "{fields["hud_var"]}"', f"w = {int(fields['w'])}",
                     f"h = {int(fields['h'])}", f"max = {int(fields['max'])}"]
            for key in ("border", "track", "fill"):
                if fields.get(key) is not None:
                    body.append(f"{key} = {int(fields[key])}")
        else:  # text
            body += [f'hud_var = "{fields["hud_var"]}"', f'font = "{fields["font"]}"']
            if fields.get("colour") is not None:
                body.append(f"colour = {int(fields['colour'])}")
        return body

    def save_hud_window_element(self, window, index, kind, anchor="top_left",
                                offset=(0, 0), panel=None, sprite=None, frame=0,
                                hud_var=None, font=None, w=None, h=None, max=None,
                                border=None, track=None, fill=None, colour=None):
        """Create (index=None, or past the current count) or rewrite (index) one
        [[hud_window.element]] under `window`. Validated by running the REAL
        pnx_assets.parse_hud_windows against the window's full element list with this
        one applied -- the same bargain save_atlas_collision strikes with
        parse_atlas_collision: not a second implementation of the reference/type checks,
        the actual one.
        """
        windows = self.man.get("hud_window", [])
        wspec = next((w_ for w_ in windows if w_.get("name") == window), None)
        if not wspec:
            raise ValueError(f"no hud_window named {window!r}")

        fields = {"panel": panel, "sprite": sprite, "frame": frame, "hud_var": hud_var,
                 "font": font, "w": w, "h": h, "max": max, "border": border,
                 "track": track, "fill": fill, "colour": colour}
        elem = {"kind": kind, "anchor": anchor,
               "offset": [int(offset[0]), int(offset[1])]}
        elem.update({k: v for k, v in fields.items() if v is not None})

        existing = list(wspec.get("element", []))
        if index is None or index >= len(existing):
            new_elements, new_index = existing + [elem], len(existing)
        else:
            new_elements = list(existing)
            new_elements[index] = elem
            new_index = index

        candidate_window = dict(wspec)
        candidate_window["element"] = new_elements
        others = [w_ for w_ in windows if w_.get("name") != window]
        try:
            pa.parse_hud_windows(others + [candidate_window], self._hud_sprite_names(),
                                 self._hud_nine_slice_names(), self._hud_font_names(),
                                 self._hud_var_types())
        except pa.BuildError as e:
            raise ValueError(str(e)) from None

        lines_out = self._hud_window_element_lines(kind, anchor, offset, fields)
        lines, wstart, wend = self._hud_window_full_block(window)
        entries = self._hud_window_element_entries(window)

        if new_index < len(entries):
            estart, eend = entries[new_index]
            lines[estart:eend] = lines_out
        else:
            at = wend
            while at > wstart and lines[at - 1].strip() == "":
                at -= 1
            lines[at:at] = lines_out

        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def remove_hud_window_element(self, window, index):
        """Delete one [[hud_window.element]] by position. Leaving a window with zero
        elements is allowed here -- it is a real build() error (parse_hud_windows), the
        same looseness save_scene's own comment describes for a nested/incremental
        structure the editor lets sit mid-edit.
        """
        entries = self._hud_window_element_entries(window)
        if not entries:
            raise ValueError(f"no hud_window named {window!r}, or it has no elements")
        if not 0 <= index < len(entries):
            raise ValueError(f"element {index} out of range (window has {len(entries)})")
        lines, _wstart, _wend = self._hud_window_full_block(window)
        estart, eend = entries[index]
        gap = 1 if eend < len(lines) and lines[eend].strip() == "" else 0
        lines[estart:eend + gap] = []
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def hud_window_preview(self, name):
        """A screen-sized composite at the window's RESTING position (fully shown, no
        slide) -- the animation itself has no static preview; that is drag-and-drop-era
        scope (see the approved plan's "Future milestones"), not this pass.

        Panels reuse `_nine_slice_compose` (project/nine_slice.py) against the real
        source art, exactly as nine_slice's own preview does. Sprites crop their
        declared frame rect straight off the source sheet, the same technique
        SpritesMixin.sprite_frames already uses to preview an undeclared-build sprite.
        A bound `text` element has no live game value at author time, so it previews as
        its hud_var's own name in caps -- stated up front in the plan, not a shortcut
        taken here.
        """
        from PIL import Image, ImageDraw
        from editor.project.nine_slice import _nine_slice_compose

        wspec = next((w for w in self.man.get("hud_window", []) if w.get("name") == name),
                     None)
        if not wspec:
            raise ValueError(f"no hud_window named {name!r}")

        W, H = self.SCREEN_W, self.SCREEN_H
        canvas = Image.new("RGBA", (W, H), (32, 32, 32, 255))
        draw = ImageDraw.Draw(canvas)

        for e in wspec.get("element", []):
            kind = e.get("kind")
            anchor = e.get("anchor", "top_left")
            ax, ay = _HUD_ANCHOR_XY.get(anchor, _HUD_ANCHOR_XY["top_left"])(W, H)
            ox, oy = e.get("offset", [0, 0])
            x, y = int(ax + ox), int(ay + oy)

            # Size-aware correction, matching pnx_hud_window.c's own anchor_side_h/
            # anchor_side_v EXACTLY (that file's own comment explains why: a RIGHT/
            # BOTTOM/CENTER anchor moves the element's far edge or centre onto the
            # anchor point, not its top-left corner) -- a preview that used the naive
            # placement would show a right-anchored element running off the canvas the
            # runtime no longer does.
            ew, eh = 0, 0
            if kind == "panel":
                ew, eh = int(e.get("w", 20)), int(e.get("h", 20))
            elif kind == "bar":
                ew, eh = int(e.get("w", 20)), int(e.get("h", 8))
            elif kind == "sprite":
                sp_spec = next((sp for sp in self.man.get("sprite", [])
                               if sp.get("name") == e.get("sprite")), None)
                frs = sp_spec.get("frames", []) if sp_spec else []
                fi = int(e.get("frame", 0))
                if fi < len(frs):
                    ew, eh = int(frs[fi][2]), int(frs[fi][3])
            else:  # text -- X only, matching the runtime's own baseline-Y exception
                label = str(e.get("hud_var") or "TEXT").upper()
                ew = int(ImageDraw.Draw(Image.new("RGBA", (1, 1))).textlength(label))

            sh = _HUD_ANCHOR_SIDE_H.get(anchor, -1)
            x -= ew // 2 if sh == 0 else (ew if sh == 1 else 0)
            if kind != "text":
                sv = _HUD_ANCHOR_SIDE_V.get(anchor, -1)
                y -= eh // 2 if sv == 0 else (eh if sv == 1 else 0)

            if kind == "panel":
                spec = next((ns for ns in self.man.get("nine_slice", [])
                            if ns.get("name") == e.get("panel")), None)
                w_, h_ = ew, eh
                if spec:
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            packed = pa.pack_nine_slice(self.root, spec, self.orientation)
                        art = Image.open(self._safe(spec["sheet"])).convert("RGBA")
                        rect = spec.get("rect")
                        if rect:
                            art = art.crop((rect[0], rect[1], rect[0] + rect[2],
                                           rect[1] + rect[3]))
                        bl, bt, br, bb = packed["border"]
                        composed = _nine_slice_compose(art, bl, bt, br, bb, w_, h_)
                        canvas.paste(composed, (x, y), composed)
                        continue
                    except Exception:                        # noqa: BLE001
                        pass
                draw.rectangle([x, y, x + w_, y + h_], outline=(255, 0, 0, 255))
            elif kind == "sprite":
                spec = next((sp for sp in self.man.get("sprite", [])
                            if sp.get("name") == e.get("sprite")), None)
                frame_i = int(e.get("frame", 0))
                frames = spec.get("frames", []) if spec else []
                if spec and frame_i < len(frames):
                    try:
                        fx, fy, fw, fh = (int(v) for v in frames[frame_i][:4])
                        art = Image.open(self._safe(spec["sheet"])).convert("RGBA")
                        crop = art.crop((fx, fy, fx + fw, fy + fh))
                        canvas.paste(crop, (x, y), crop)
                    except Exception:                        # noqa: BLE001
                        pass
            elif kind == "bar":
                w_, h_ = ew, eh
                border = pv.gcolor_rgb(e.get("border", 0xC0)) or (255, 255, 255)
                track = pv.gcolor_rgb(e.get("track", 0x00)) or (0, 0, 0)
                fill = pv.gcolor_rgb(e.get("fill", 0xC0)) or (255, 255, 255)
                draw.rectangle([x, y, x + w_ - 1, y + h_ - 1], outline=border + (255,))
                draw.rectangle([x + 1, y + 1, x + w_ - 2, y + h_ - 2], fill=track + (255,))
                # Sample fill: half-full, so a preview shows the fill colour exists
                # without claiming to know a runtime value it cannot.
                half = max(0, (w_ - 2) // 2)
                if half:
                    draw.rectangle([x + 1, y + 1, x + half, y + h_ - 2], fill=fill + (255,))
            else:  # text
                label = str(e.get("hud_var") or "TEXT").upper()
                colour = pv.gcolor_rgb(e.get("colour", 0xFF)) or (255, 255, 255)
                draw.text((x, y), label, fill=colour + (255,))

        return {"w": W, "h": H, "img": pv.data_uri(canvas)}
