"""Dialog: named blobs of conversation text."""

import json
import re


class DialogMixin:
    def dialogs(self):
        """Every [dialog.*] and its pages, with what the text costs."""
        out = []
        for name, spec in sorted(self.man.get("dialog", {}).items()):
            pages = list(spec.get("pages", []))
            out.append({"name": name, "pages": pages,
                        # What the blob will hold: each page NUL-terminated, plus its u16
                        # offset and the entry's own index pair.
                        "bytes": sum(len(p) + 1 for p in pages) + len(pages) * 2 + 4})
        return out

    def _dialog_block(self, name):
        """(lines, start, end) for one [dialog.x] table, or None."""
        lines = open(self.path).read().split("\n")
        want = f"[dialog.{name}]"
        for i, line in enumerate(lines):
            if line.strip() == want:
                end = next((j for j in range(i + 1, len(lines))
                            if lines[j].lstrip().startswith("[")), len(lines))
                return lines, i, end
        return None

    def dialog_users(self, name):
        """Scenes that load dialog at all.

        `dialog = true` loads the whole blob rather than one conversation, so removing an
        entry only strands a scene when it is the LAST one -- which is the case worth
        refusing, and the only one this reports.
        """
        if len(self.man.get("dialog", {})) > 1:
            return []
        return [f"scene {s}" for s, spec in self.man.get("scene", {}).items()
                if spec.get("dialog")]

    def save_dialog(self, name, pages):
        """Create or rewrite one [dialog.*] entry."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("a dialog name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        pages = [p for p in pages]
        if not pages:
            raise ValueError("a dialog entry with no pages cannot be built")
        for p in pages:
            # The packer encodes ASCII with errors="replace", so anything else becomes a
            # literal '?' on the watch with nothing said. Refused here instead, where the
            # character that will not survive is still on screen.
            try:
                p.encode("ascii")
            except UnicodeEncodeError as e:
                raise ValueError(
                    f"{p[e.start:e.end]!r} is not ASCII, and the packer would replace it "
                    f"with '?' — the glyph atlas has no room for a character no font was "
                    f"asked to rasterise") from None
            if "\n" in p:
                raise ValueError("a page is one screenful and cannot contain a newline; "
                                 "split it into two pages")

        body = [f"[dialog.{name}]", "pages = ["]
        body += [f'  {json.dumps(p)},' for p in pages]
        body += ["]"]

        block = self._dialog_block(name)
        if block:
            lines, start, end = block
            gap = 0
            while end - gap > start and lines[end - gap - 1].strip() == "":
                gap += 1
            lines[start:end] = body + [""] * gap
        else:
            lines = open(self.path).read().split("\n") + [""] + body
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def remove_dialog(self, name):
        """Delete a conversation, once removing it would not strand a scene."""
        block = self._dialog_block(name)
        if not block:
            raise ValueError(f"no dialog named {name!r}")
        users = self.dialog_users(name)
        if users:
            raise ValueError(f"cannot remove {name!r} — it is the only dialog, and "
                             f"{', '.join(users)} asks for dialog. Untick it there first.")
        lines, start, end = block
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

