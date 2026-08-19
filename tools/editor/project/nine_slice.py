"""9-slice panels: bordered HUD art, declared once and drawn at any box size.

Mirrors SpritesMixin's shape throughout -- a nine_slice IS a sprite with one frame, no
anim, no variants, plus the four border insets a sprite has no use for (pnx_assets.py's
pack_nine_slice's own comment). `nine_slice_preview` is the one genuinely new piece: a
sprite's declaration form only ever needs to show frames as they are, but a panel's whole
point is what it looks like TILED at a box size nobody has authored art for -- so this
renders that composite live, running the same region math pnx_gfx_draw_nine_slice
(src/pnx/gfx/pnx_nineslice.c) does at draw time.
"""

import contextlib
import io
import os
import re

import pnx_assets as pa                                     # noqa: E402
import pnx_preview as pv                                    # noqa: E402


def _nine_slice_compose(panel_img, bl, bt, br, bb, dst_w, dst_h):
    """RGBA composite of `panel_img` (already cropped to the panel's own w x h) into a
    dst_w x dst_h canvas -- corners kept exact, edges/centre tiled, the last tile in each
    run truncated rather than overflowing, exactly the nine pnx_blit_4bpp_region-shaped
    calls pnx_gfx_draw_nine_slice makes, region for region, just against PIL crops instead
    of packed 4bpp bytes: this is for a browser to look at, not for the device to load.
    """
    from PIL import Image

    w, h = panel_img.size
    # Undersized box: same clamp pnx_gfx_draw_nine_slice's own comment describes.
    if bl + br > dst_w:
        bl, br = dst_w // 2, dst_w - dst_w // 2
    if bt + bb > dst_h:
        bt, bb = dst_h // 2, dst_h - dst_h // 2

    canvas = Image.new("RGBA", (max(1, dst_w), max(1, dst_h)), (0, 0, 0, 0))

    def tile(sx, sy, sw, sh, dx, dy, tw, th):
        if sw <= 0 or sh <= 0 or tw <= 0 or th <= 0:
            return
        region = panel_img.crop((sx, sy, sx + sw, sy + sh))
        oy = 0
        while oy < th:
            rh = min(sh, th - oy)
            ox = 0
            while ox < tw:
                rw = min(sw, tw - ox)
                canvas.paste(region.crop((0, 0, rw, rh)), (dx + ox, dy + oy))
                ox += rw
            oy += rh

    cw, ch = w - bl - br, h - bt - bb
    dst_cw, dst_ch = dst_w - bl - br, dst_h - bt - bb
    src_r, src_b = w - br, h - bb
    dst_r, dst_b = dst_w - br, dst_h - bb

    tile(0, 0, bl, bt, 0, 0, bl, bt)                       # top-left corner
    tile(src_r, 0, br, bt, dst_r, 0, br, bt)               # top-right corner
    tile(0, src_b, bl, bb, 0, dst_b, bl, bb)               # bottom-left corner
    tile(src_r, src_b, br, bb, dst_r, dst_b, br, bb)       # bottom-right corner
    tile(bl, 0, cw, bt, bl, 0, dst_cw, bt)                 # top edge
    tile(bl, src_b, cw, bb, bl, dst_b, dst_cw, bb)         # bottom edge
    tile(0, bt, bl, ch, 0, bt, bl, dst_ch)                 # left edge
    tile(src_r, bt, br, ch, dst_r, bt, br, dst_ch)         # right edge
    tile(bl, bt, cw, ch, bl, bt, dst_cw, dst_ch)           # centre

    return canvas


class NineSliceMixin:
    def nine_slices(self):
        """Every [[nine_slice]], with its built size where a build exists."""
        out = []
        for spec in self.man.get("nine_slice", []):
            entry = {"name": spec.get("name"), "sheet": spec.get("sheet"),
                     "rect": list(spec["rect"]) if spec.get("rect") else None,
                     "border": list(spec.get("border") or []),
                     "colorkey": list(spec["colorkey"]) if spec.get("colorkey") else None,
                     "bytes": None}
            blob = os.path.join(self.res, spec.get("out", f"{entry['name']}.bin"))
            if os.path.exists(blob):
                entry["bytes"] = os.path.getsize(blob)
            out.append(entry)
        return out

    def validate_nine_slice(self, name, sheet, border, rect=None, colorkey=None):
        """Run a candidate panel through the REAL pack_nine_slice and report what it says.

        Same bargain as validate_sprite: not a second implementation of the rect/border
        checks, the actual one -- a rect running off the sheet, a border that does not
        fit, or a negative inset all fail here rather than after the block is in the
        manifest.
        """
        spec = {"name": name or "candidate", "sheet": sheet,
                "out": f"{name or 'candidate'}.bin"}
        if rect:
            spec["rect"] = list(rect)
        if border:
            spec["border"] = list(border)
        if colorkey:
            spec["colorkey"] = list(colorkey)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                built = pa.pack_nine_slice(self.root, spec, self.orientation)
        except pa.BuildError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "w": built["w"], "h": built["h"],
                "border": list(built["border"])}

    def nine_slice_preview(self, sheet, border, rect=None, colorkey=None, test_w=48,
                           test_h=32):
        """Renders the panel TILED at an arbitrary test box size -- the one thing
        validate_nine_slice's numbers alone cannot show, and the reason this file exists
        rather than a thinner copy of SpritesMixin. Works in the AUTHOR's frame (this
        orientation's rotate_border only matters to the packed blob, not to what a panel
        looks like on the sheet it was drawn on).
        """
        from PIL import Image
        full = self._safe(sheet)
        im = Image.open(full).convert("RGBA")
        W, H = im.size
        x, y, w, h = (int(v) for v in (rect or (0, 0, W, H)))
        if x < 0 or y < 0 or x + w > W or y + h > H:
            raise ValueError(f"rect {x},{y} {w}x{h} runs past the sheet ({W}x{H})")
        if not (isinstance(border, (list, tuple)) and len(border) == 4):
            raise ValueError("border must be [left, top, right, bottom]")
        bl, bt, br, bb = (int(v) for v in border)
        if bl + br > w or bt + bb > h:
            raise ValueError(f"border {bl}/{bt}/{br}/{bb} does not fit a {w}x{h} panel")

        panel = im.crop((x, y, x + w, y + h))
        if colorkey:
            key_rgb = tuple(int(c) for c in colorkey)
            px = panel.load()
            for j in range(panel.height):
                for i in range(panel.width):
                    if px[i, j][:3] == key_rgb:
                        px[i, j] = (0, 0, 0, 0)

        test_w, test_h = max(1, int(test_w)), max(1, int(test_h))
        canvas = _nine_slice_compose(panel, bl, bt, br, bb, test_w, test_h)
        return {"w": test_w, "h": test_h, "panel_w": w, "panel_h": h,
                "img": pv.data_uri(canvas)}

    def _nine_slice_block(self, name):
        """(lines, start, end) for one [[nine_slice]] -- same walk as _sprite_block."""
        lines = open(self.path).read().split("\n")
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[nine_slice]]":
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

    def nine_slice_users(self, name):
        """The scenes that load this panel, so removing it can refuse and say which."""
        return [f"scene {s}" for s, spec in self.man.get("scene", {}).items()
                if name in spec.get("nine_slices", [])]

    def save_nine_slice(self, name, sheet, border, rect=None, colorkey=None):
        """Create or rewrite one [[nine_slice]] block, validated the way the build will."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")

        check = self.validate_nine_slice(name, sheet, border, rect, colorkey)
        if not check["ok"]:
            raise ValueError(check["error"])

        want = [
            ("name", f'name = "{name}"'),
            ("sheet", f'sheet = "{sheet}"'),
            ("rect", ("rect = [" + ", ".join(str(int(v)) for v in rect) + "]")
                     if rect else None),
            ("border", "border = [" + ", ".join(str(int(v)) for v in border) + "]"),
            ("out", f'out = "{name}.bin"'),
            ("colorkey", ("colorkey = [" + ", ".join(str(int(c)) for c in colorkey) + "]")
                         if colorkey else None),
        ]

        block = self._nine_slice_block(name)
        if not block:
            body = [v for _, v in want if v]
            lines = open(self.path).read().split("\n") + ["", "[[nine_slice]]"] + body
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()
            return

        # Key at a time, same reasoning as save_sprite's own comment: replacing the whole
        # block would eat any comment sitting inside it.
        lines, start, end = block
        for key, value in want:
            at = next((j for j in range(start + 1, end)
                       if re.match(rf"\s*{key}\s*=", lines[j])), None)
            if at is not None and value:
                lines[at] = value
            elif at is not None:
                del lines[at]
                end -= 1
            elif value:
                at = start + 1
                for j in range(start + 1, end):
                    if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                        at = j + 1
                lines[at:at] = [value]
                end += 1

        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def remove_nine_slice(self, name):
        """Delete a [[nine_slice]], once no scene loads it. The art on disk is left alone."""
        block = self._nine_slice_block(name)
        if not block:
            raise ValueError(f"no nine_slice named {name!r}")
        users = self.nine_slice_users(name)
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
