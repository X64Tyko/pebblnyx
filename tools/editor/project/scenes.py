"""Scenes: which map/sprites/fonts/dialog load together."""

import json
import re


class ScenesMixin:
    def scenes(self):
        """Every [scene.*], with what it loads and what that costs resident."""
        out = []
        for name, spec in self.man.get("scene", {}).items():
            out.append({
                "name": name,
                "map": spec.get("map"),
                "sprites": list(spec.get("sprites", [])),
                "nine_slices": list(spec.get("nine_slices", [])),
                "fonts": list(spec.get("fonts", [])),
                # Carried through rather than dropped: a scene with no map may legitimately
                # load atlases of its own (a menu drawing tiles), and silently discarding
                # the key on save would break that manifest.
                "atlases": list(spec.get("atlases", [])),
                "dialog": bool(spec.get("dialog", False)),
            })
        return sorted(out, key=lambda s: s["name"])

    def _scene_block(self, name):
        """(lines, start, end) for one [scene.x] table, or None."""
        lines = open(self.path).read().split("\n")
        want = f"[scene.{name}]"
        for i, line in enumerate(lines):
            if line.strip() == want:
                end = next((j for j in range(i + 1, len(lines))
                            if lines[j].lstrip().startswith("[")), len(lines))
                return lines, i, end
        return None

    def save_scene(self, name, map_name=None, sprites=(), fonts=(), dialog=False,
                   atlases=(), nine_slices=()):
        """Create or rewrite one [scene.*], validated the way build_scenes will validate it.

        The clash check is the one worth doing here rather than at build time: a scene
        listing an atlas its own map already streams loads a SECOND resident copy, and
        nothing at runtime can see that -- it just quietly costs twice the atlas. That is
        also the bug the editor used to write by itself on every new map.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("a scene name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")

        known_maps = [m["name"] for m in self.man.get("map", [])]
        if map_name and map_name not in known_maps:
            raise ValueError(f"no map named {map_name!r}")

        known_sprites = [s.get("name") for s in self.man.get("sprite", [])]
        for s in sprites:
            if s not in known_sprites:
                raise ValueError(f"no sprite named {s!r} "
                                 f"(known: {', '.join(known_sprites) or 'none'})")
        known_nine_slices = [ns.get("name") for ns in self.man.get("nine_slice", [])]
        for ns in nine_slices:
            if ns not in known_nine_slices:
                raise ValueError(f"no nine_slice named {ns!r} "
                                 f"(known: {', '.join(known_nine_slices) or 'none'})")
        known_fonts = [f.get("name") for f in self.man.get("font", [])]
        for f in fonts:
            if f not in known_fonts:
                raise ValueError(f"no font named {f!r} "
                                 f"(known: {', '.join(known_fonts) or 'none'})")
        known_atlases = [a.get("name") for a in self.man.get("atlas", [])]
        for a in atlases:
            if a not in known_atlases:
                raise ValueError(f"no atlas named {a!r}")

        if dialog and not self.man.get("dialog"):
            raise ValueError("this scene asks for dialog, but the manifest defines none")

        if map_name and atlases:
            spec = next(m for m in self.man.get("map", []) if m["name"] == map_name)
            streamed = set(self.map_atlases(spec))
            clash = sorted(streamed.intersection(atlases))
            if clash:
                raise ValueError(
                    f"map {map_name!r} already streams {', '.join(clash)}, so listing "
                    f"it here loads a second resident copy. Drop it.")

        if not (map_name or sprites or nine_slices or fonts or atlases or dialog):
            raise ValueError("a scene that loads nothing cannot be built")

        # Key at a time, not block at a time. Replacing the whole table would be shorter
        # and would silently eat the comments inside it -- and in this project a
        # manifest's comments are half its content: the example's cave scene explains in
        # two lines why it does NOT load the dialogue face, which is exactly the kind of
        # reasoning that never gets written down twice.
        want = [("map", f'map = "{map_name}"' if map_name else None),
                ("atlases", "atlases = " + json.dumps(list(atlases)) if atlases else None),
                ("sprites", "sprites = " + json.dumps(list(sprites)) if sprites else None),
                ("nine_slices", "nine_slices = " + json.dumps(list(nine_slices))
                                if nine_slices else None),
                ("fonts", "fonts = " + json.dumps(list(fonts)) if fonts else None),
                ("dialog", "dialog = true" if dialog else None)]

        block = self._scene_block(name)
        if not block:
            lines = open(self.path).read().split("\n")
            lines += [""] + [f"[scene.{name}]"] + [v for _, v in want if v]
        else:
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
                    # One past the LAST actual key line, not "walk back from the block's
                    # end over blanks" -- see save_project's own comment for why that can
                    # land a new key among comments belonging to whatever table follows.
                    at = start + 1
                    for j in range(start + 1, end):
                        if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                            at = j + 1
                    lines[at:at] = [value]
                    end += 1

        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def remove_scene(self, name):
        """Delete a scene. Nothing in the manifest references a scene, so this is safe --
        but game code loads them BY NAME through the generated header, so it is worth
        saying that the C will stop compiling rather than failing silently at runtime."""
        block = self._scene_block(name)
        if not block:
            raise ValueError(f"no scene named {name!r}")
        lines, start, end = block
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

