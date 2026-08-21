"""HUD variables: a named, typed runtime table (int/text) game code writes to each tick
and a HUD draw call reads back (src/pnx/gfx/pnx_hud_vars.h). Declared here, generated as
PNX_HUD_VAR_* constants by pnx_assets.py's own parse_hud_vars/generate_header.

No preview and no "users" check yet, unlike NineSliceMixin -- nothing can bind to a
hud_var until Phase 3's HUD window elements exist, so a reference check would always
report empty. Added once there is something real to check.
"""

import re

import pnx_assets as pa                                     # noqa: E402


class HudMixin:
    def hud_vars(self):
        """Every [[hud_var]], as declared -- no build required to list them."""
        return [{"name": hv.get("name"), "type": hv.get("type")}
                for hv in self.man.get("hud_var", [])]

    def _hud_var_block(self, name):
        """(lines, start, end) for one [[hud_var]] -- same walk as _nine_slice_block."""
        lines = open(self.path).read().split("\n")
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[hud_var]]":
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

    def save_hud_var(self, name, type):
        """Create or rewrite one [[hud_var]] block, validated against the real pipeline
        check (pnx_assets.parse_hud_vars), run against the full declared set with this
        edit applied -- same bargain save_nine_slice strikes with pack_nine_slice: not a
        second implementation of the name/type/duplicate rules, the actual one.
        """
        existing = [hv for hv in self.man.get("hud_var", []) if hv.get("name") != name]
        candidate = existing + [{"name": name, "type": type}]
        try:
            pa.parse_hud_vars(candidate)
        except pa.BuildError as e:
            raise ValueError(str(e))

        want = [
            ("name", f'name = "{name}"'),
            ("type", f'type = "{type}"'),
        ]

        block = self._hud_var_block(name)
        if not block:
            body = [v for _, v in want]
            lines = open(self.path).read().split("\n") + ["", "[[hud_var]]"] + body
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()
            return

        # Key at a time, same reasoning as save_nine_slice's own comment: replacing the
        # whole block would eat any comment sitting inside it.
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

    def remove_hud_var(self, name):
        """Delete a [[hud_var]]. The art/blob side has nothing to clean up -- a hud_var
        is pure declaration, no file on disk."""
        block = self._hud_var_block(name)
        if not block:
            raise ValueError(f"no hud_var named {name!r}")
        lines, start, end = block
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()
