"""Maps: legend + flags, creation/lifecycle, and saving painted cells."""

import json
import os
import re

import pnx_mapfile as mf                                    # noqa: E402
import pnx_assets as pa                                     # noqa: E402


class MapsMixin:
    def flag_names(self):
        """Flag name -> bit, built-ins and the project's own together.

        Read through the pipeline's parser rather than the raw table, so a manifest the
        build would reject cannot reach the page looking valid. A broken [tile_flags]
        falls back to the built-ins: the editor still opens, which is what lets you fix
        it, and Build will say what is wrong.
        """
        try:
            return pa.parse_flag_names(self.man.get("tile_flags", {}))
        except pa.BuildError:
            # warp is the only built-in now -- collision moved off the legend entirely
            # (see [[atlas.collision]]), and [tile_flags] itself is refused outright, so
            # the only way parse_flag_names raises here is a manifest still declaring
            # one from before this change. Same fallback shape as before: the editor
            # still opens, Build says what is wrong.
            return {"warp": pa.TILE_WARP}

    @staticmethod
    def _legend_payload(raw):
        """One legend table, with every key the pipeline reads.

        `atlas` and `flip` used to be dropped, which is why the page could only ever paint
        through a map's FIRST tileset -- so this is shared between the project legend and
        each map's own rather than written twice and drifting.
        """
        return {ch: {"tile": e["tile"], "flags": e.get("flags", []),
                     "atlas": e.get("atlas"),
                     "flip": ([e["flip"]] if isinstance(e.get("flip"), str)
                              else list(e.get("flip", []))),
                     "rotate": bool(e.get("rotate", False))}
                for ch, e in raw.items()}

    def _plane_doc(self, spec, legend, default, primary):
        """One WorldTile plane -- cells + a tile table -- whatever it was authored in.

        Shared by every layer a map declares: the implicit single one when there is no
        `[[map.layer]]` at all, and each explicit sub-table when there is. `primary`
        gates whether `start`/`warps` are read -- the same primary-layer-only rule the
        pipeline enforces (finish_compile_layer's own comment, pnx_assets.py) -- so a
        background or overlay layer's doc simply has neither.

        The page used to hold a map as an array of STRINGS and index it by legend
        character, which cannot represent a `.pnxmap` at all -- a binary map has no
        characters, and above ~90 tiles there are not enough to invent. So both formats
        are normalised to the same shape here and the canvas only knows one.

        For a `rows` plane each table entry carries the character it came from, so saving
        writes back the author's own glyphs rather than reassigning them -- a map whose
        diff churned every time it was opened would not be worth keeping in text.
        """
        if "source" in spec:
            path = self._safe(spec["source"])
            doc = mf.read(path)
            for t in doc["tiles"]:
                t.setdefault("flags", 0)
            out = {"format": "source", "source": spec["source"],
                  "w": doc["w"], "h": doc["h"], "cells": doc["cells"],
                  "tiles": doc["tiles"]}
            if primary:
                out["start"] = doc["start"]
                out["warps"] = [{"at": wp["at"], "to": wp["to"], "gated": wp["gated"]}
                               for wp in doc["warps"]]
            return out

        rows = [r for r in spec.get("rows", "").strip("\n").split("\n") if r.strip()]
        plane_legend = legend
        own = spec.get("legend")
        if own:
            plane_legend = dict(legend)
            plane_legend.update(own)

        tiles, seen, cells = [], {}, []
        h = len(rows)
        w = len(rows[0]) if rows else 0
        for row in rows:
            for ch in row:
                e = plane_legend.get(ch)
                if e is None:
                    # An unknown character is carried as a placeholder rather than
                    # refused: the page has to be able to OPEN a broken map to fix it,
                    # and the build is what says no.
                    key = ("?", ch)
                    if key not in seen:
                        seen[key] = len(tiles)
                        tiles.append({"atlas": default, "index": 0, "flip": "",
                                      "rotate": False,
                                      "flags": 0, "ch": ch, "missing": True})
                    cells.append(seen[key])
                    continue
                flip = e.get("flip", [])
                flip = [flip] if isinstance(flip, str) else list(flip)
                rotate = bool(e.get("rotate", False))
                key = (ch,)
                if key not in seen:
                    seen[key] = len(tiles)
                    tiles.append({"atlas": e.get("atlas") or default,
                                  "index": e["tile"],
                                  "flip": "".join(sorted(flip)),
                                  "rotate": rotate,
                                  "flags": self._flag_byte(e.get("flags", [])),
                                  "flag_names": list(e.get("flags", [])),
                                  "ch": ch})
                cells.append(seen[key])

        out = {"format": "rows", "w": w, "h": h, "cells": cells, "tiles": tiles}
        if primary:
            out["start"] = list(spec.get("start", [1, 1]))
            out["warps"] = [dict(wp) for wp in spec.get("warps", [])]
        return out

    def map_doc(self, m):
        """A map as one or more WorldTile planes: `layers`, in the order the manifest
        declares them, and `primary` -- the index INTO that same list of whichever one
        `start`/`warps` belong to -- plus every key `map_doc` returned before multi-layer
        maps existed, mirroring `layers[primary]` at the top level, so any caller that
        only ever knew a flat map keeps working unchanged (`doc["cells"]`/`doc["w"]`/etc.
        still mean exactly what they meant).

        `layers` is deliberately kept in FILE order rather than reordered primary-first:
        `add_map_layer`/`remove_map_layer`/`save_map_layer` all address a layer by its
        index into this same list, and that index has to be stable against the manifest
        text those write, not against a view-only reordering. (The pipeline's OWN
        primary-first reordering, inside `compile_map`'s manifest driver, is a build-time
        concern of the compiled blob and is unrelated to this list's order.)

        `m["layer"]` -- `[[map.layer]]` sub-tables -- declares the planes explicitly,
        background-to-foreground in the order written, one of which may set
        `primary = true` (default: the first). No `[[map.layer]]` at all is the ORIGINAL
        shape: one implicit layer built from the map's own `rows`/`source`.
        """
        names = self.map_atlases(m)
        default = names[0] if names else None
        legend = dict(self.man.get("legend", {}))
        legend.update(m.get("legend", {}))

        layer_specs = m.get("layer")
        if layer_specs:
            primary_i = next((i for i, ls in enumerate(layer_specs)
                              if ls.get("primary")), 0)
            layers = []
            for i, ls in enumerate(layer_specs):
                d = self._plane_doc(ls, legend, default, primary=(i == primary_i))
                d["parallax_pct"] = max(0, min(255, int(ls.get("parallax_pct", 255))))
                d["wrap"] = bool(ls.get("wrap", False))
                layers.append(d)
        else:
            d = self._plane_doc(m, legend, default, primary=True)
            d["parallax_pct"] = 255
            d["wrap"] = False
            layers = [d]
            primary_i = 0

        return {**layers[primary_i], "layers": layers, "primary": primary_i}

    def _flag_byte(self, names):
        known = self.flag_names()
        out = 0
        for n in names:
            out |= known.get(n, 0)
        return out

    def maps(self):
        out = []
        for m in self.man.get("map", []):
            doc = self.map_doc(m)
            rows = [r for r in m.get("rows", "").strip("\n").split("\n") if r.strip()]
            names = self.map_atlases(m)
            out.append({"name": m["name"], "rows": rows,
                        # The normalised form the canvas actually draws: cells indexing a
                        # tile table, for a `rows` map and a `.pnxmap` alike. `layers` is
                        # every plane the map declares, PRIMARY first -- the flat
                        # format/source/w/h/cells/tiles/start/warps fields alongside it
                        # mirror layers[0], unchanged in shape from before layers existed.
                        "format": doc["format"],
                        "source": doc.get("source"),
                        "w": doc["w"], "h": doc["h"],
                        "cells": doc["cells"], "tiles": doc["tiles"],
                        "layers": doc["layers"], "primary": doc["primary"],
                        # A source map owns its own start and warps -- they are positions
                        # in a grid the file holds, so the manifest does not get a second,
                        # disagreeing copy.
                        "start": doc["start"],
                        "warps": doc["warps"],
                        # `atlas` stays for everything that only wants the first one; the
                        # list is what a map drawing from several is actually described by.
                        "atlas": names[0] if names else None,
                        "atlases": names,
                        # The M4b palette variant and the M4d streaming controls. Sent as
                        # what the manifest says rather than as the effective value, so
                        # "auto" and an explicit size stay distinguishable in the form.
                        "palette": m.get("palette"),
                        "palettes": self.map_palettes(m["name"]),
                        "worldtile": m.get("worldtile", "auto"),
                        "atlas_slots": m.get("atlas_slots"),
                        "bank_bytes": m.get("bank_bytes"),
                        "resident": bool(m.get("resident", False)),
                        # This map's own [map.legend], overlaid on the project one. Sent
                        # separately rather than pre-merged because the page has to be able
                        # to say which table an entry lives in: editing a project character
                        # changes every map, editing a map's own changes one.
                        "legend": self._legend_payload(m.get("legend", {}))})
        return out

    @staticmethod
    def _toml_key(ch):
        """One legend character as a quoted TOML key.

        The characters worth painting with are punctuation, so the two that would break
        the quoting -- a quote and a backslash -- are exactly the ones a person reaches
        for eventually.
        """
        return '"' + ch.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _map_block(self, name):
        """(lines, start, end) for one [[map]] block, end exclusive.

        `end` steps over the map's OWN subtables -- `[map.legend."x"]` binds to the most
        recent [[map]] in TOML, so a subtable is part of the block even though it opens
        with a bracket. Stopping at the first bracket instead would put anything appended
        here in front of the map's own legend, i.e. in the wrong table. `[[map.layer]]`
        sub-tables are the same story: an array-of-tables nested inside this [[map]],
        not a sibling of it, even though `[[...]]` looks like a top-level header too.
        """
        lines = open(self.path).read().split("\n")
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[map]]":
                if start is not None:
                    break
                nxt = next((j for j in range(i + 1, len(lines))
                            if lines[j].lstrip().startswith("[")), len(lines))
                if any(re.match(rf'^name\s*=\s*"{re.escape(name)}"', lines[j].strip())
                       for j in range(i + 1, nxt)):
                    start = i
        if start is None:
            return None

        end = len(lines)
        for j in range(start + 1, len(lines)):
            s = lines[j].lstrip()
            if (s.startswith("[") and not s.startswith("[map.")
                    and s.strip() != "[[map.layer]]"):
                end = j
                break
        return lines, start, end

    def _map_layer_blocks(self, name):
        """(lines, map_start, map_end, [(layer_start, layer_end), ...]) -- every
        `[[map.layer]]` sub-table inside map `name`'s own block, in FILE order (see
        map_doc's own comment for why file order, not primary-first, is what every layer
        edit addresses). Each `(layer_start, layer_end)` spans that one sub-table's own
        `[[map.layer]]` header through whatever precedes the next boundary -- another
        `[[map.layer]]`, or the end of the map's block.
        """
        found = self._map_block(name)
        if not found:
            return None
        lines, mstart, mend = found
        blocks = []
        i = mstart + 1
        while i < mend:
            if lines[i].strip() == "[[map.layer]]":
                j = i + 1
                while j < mend:
                    s = lines[j].lstrip()
                    if s.startswith("[") and not s.startswith("[map.layer."):
                        break
                    j += 1
                blocks.append((i, j))
                i = j
            else:
                i += 1
        return lines, mstart, mend, blocks

    def _legend_block(self, ch, map_name=None):
        """(lines, start, end) for one legend block, or None if it has none.

        With `map_name` this looks for `[map.legend."x"]` inside that map rather than the
        project-wide `[legend."x"]` -- the same character can legitimately be both, and
        they mean different tiles.
        """
        if map_name:
            found = self._map_block(map_name)
            if not found:
                return None
            lines, mstart, mend = found
            want = f"[map.legend.{self._toml_key(ch)}]"
            for i in range(mstart, mend):
                if lines[i].strip() == want:
                    end = next((j for j in range(i + 1, mend)
                                if lines[j].lstrip().startswith("[")), mend)
                    return lines, i, end
            return None

        lines = open(self.path).read().split("\n")
        want = f"[legend.{self._toml_key(ch)}]"
        for i, line in enumerate(lines):
            if line.strip() == want:
                end = next((j for j in range(i + 1, len(lines))
                            if lines[j].lstrip().startswith("[")), len(lines))
                return lines, i, end
        return None

    def _legend_lines(self, ch, tile, atlas=None, flags=(), flip=(), rotate=False,
                      map_name=None):
        head = "map.legend" if map_name else "legend"
        body = [f"[{head}.{self._toml_key(ch)}]"]
        # An int is a raw index into the atlas, a string is a role it defines. Both are
        # first-class; the quoting is the whole difference in the file.
        body.append(f"tile = {tile}" if isinstance(tile, int)
                    else f'tile = "{tile}"')
        if atlas:
            body.append(f'atlas = "{atlas}"')
        body.append("flags = [" + ", ".join(f'"{f}"' for f in flags) + "]")
        if flip:
            body.append("flip = [" + ", ".join(f'"{a}"' for a in flip) + "]")
        if rotate:
            body.append("rotate = true")
        return body

    def save_legend(self, ch, tile, atlas=None, flags=(), flip=(), rotate=False,
                    map_name=None):
        """Create or rewrite one legend character, validating it the way the build will.

        Checked here rather than only in the page, because a legend entry that does not
        resolve breaks every map that paints it -- and the editor is the thing that is
        supposed to make a manifest you cannot break.

        `map_name` writes the character into that map's own `[map.legend]` instead of the
        project table. One character per cell means the printable set -- about ninety --
        is the hard ceiling on how many distinct tiles can be placed; project-wide that
        ceiling was shared by the whole game, which is what put most of a carved tileset
        out of reach. Per map it is ninety each.
        """
        if map_name and not any(m.get("name") == map_name
                                for m in self.man.get("map", [])):
            raise ValueError(f"no map named {map_name!r}")
        if len(ch) != 1:
            raise ValueError("a legend character is exactly one character")
        if ch.isspace():
            raise ValueError("whitespace cannot be a legend character: the rows block "
                             "would not survive a round trip through it")

        known = self.flag_names()
        for f in flags:
            if f not in known:
                raise ValueError(f"unknown flag {f!r} (known: {', '.join(sorted(known))})")
        for axis in flip:
            if axis not in ("x", "y"):
                raise ValueError(f"flip must be \"x\" or \"y\", not {axis!r}")

        names = [a.get("name") for a in self.man.get("atlas", [])]
        if atlas and atlas not in names:
            raise ValueError(f"no atlas named {atlas!r}")
        if flip or rotate:
            which = atlas or (names[0] if names else None)
            built = next((a for a in self.atlases() if a["name"] == which), None)
            if built and built["metatiled"]:
                raise ValueError(
                    f"atlas {which!r} is metatiled, and the runtime does not flip or "
                    f"rotate a composed tile -- it would draw unmirrored and unrotated. "
                    f"Set `metatiles = false` on that atlas to paint it that way.")

        if isinstance(tile, int):
            which = atlas or (names[0] if names else None)
            built = next((a for a in self.atlases() if a["name"] == which), None)
            if built:
                count = built["count"]
            else:
                # Before the first build there is no packed atlas to count, but max_tiles
                # is a ceiling the carve cannot exceed -- so an index past it is wrong now
                # rather than wrong later, and refusing it does not depend on having built.
                spec = next((a for a in self.man.get("atlas", [])
                             if a.get("name") == which), None)
                count = spec.get("max_tiles") if spec else None
            if count is not None and not 0 <= tile < count:
                raise ValueError(f"atlas {which!r} holds {count} tiles "
                                 f"(0-{count - 1}), not {tile}")

        block = self._legend_block(ch, map_name)
        if block:
            lines, start, end = block
            # The block runs to the next table header, which means it takes the blank
            # lines separating it from that header with it. Rewriting without them would
            # close the gap a little more every time a flag was ticked, until the legend
            # was one unbroken wall of text.
            gap = 0
            while end - gap > start and lines[end - gap - 1].strip() == "":
                gap += 1
            lines[start:end] = (
                self._legend_lines(ch, tile, atlas, flags, flip, rotate, map_name) + [""] * gap)
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
        elif map_name:
            # Appended at the END of the [[map]] block, which is the only place it can go:
            # a subtable closes its parent, so anything written above `rows` would put the
            # rest of the map's own keys inside `[map.legend]` and the build would fail
            # complaining that the map has no rows.
            lines, mstart, mend = self._map_block(map_name)
            new = self._legend_lines(ch, tile, atlas, flags, flip, rotate, map_name)
            at = mend
            while at > mstart and lines[at - 1].strip() == "":
                at -= 1
            lines[at:at] = [""] + new
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
        else:
            # Appended beside the other legend entries when there are any, so the file
            # stays readable as one table rather than growing a second legend section at
            # the bottom under the maps.
            lines = open(self.path).read().split("\n")
            last = max((i for i, l in enumerate(lines)
                        if l.strip().startswith("[legend.")), default=None)
            new = self._legend_lines(ch, tile, atlas, flags, flip, rotate)
            if last is None:
                lines = lines + [""] + new
            else:
                # Straight after the last legend entry's own key/values, and no further.
                # Scanning to the next table header instead would step over the blank line
                # and the section comment that belong to whatever comes NEXT -- which put
                # new entries under the "maps" heading, where they parse correctly and
                # read as though someone had lost their place.
                end = last + 1
                while (end < len(lines) and lines[end].strip()
                       and not lines[end].lstrip().startswith("[")
                       and not lines[end].lstrip().startswith("#")):
                    end += 1
                lines[end:end] = [""] + new
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
        self.reload()

    def legend_users(self, ch, map_name=None):
        """The maps that paint this character, so removing it can refuse and say which.

        A map-scoped character is only ever painted in its own map, and a project one
        that some map has overridden is not painted by THAT map -- the override is, and it
        may well mean a different tile. So the maps carrying their own entry are skipped
        rather than counted as users of the project character.
        """
        maps = self.man.get("map", [])
        if map_name:
            return [m["name"] for m in maps
                    if m.get("name") == map_name and ch in m.get("rows", "")]
        return [m["name"] for m in maps
                if ch in m.get("rows", "") and ch not in m.get("legend", {})]

    def remove_legend(self, ch, map_name=None):
        """Delete a legend character, once no map paints it."""
        block = self._legend_block(ch, map_name)
        if not block:
            where = f"map {map_name!r}" if map_name else "the project legend"
            raise ValueError(f"no legend entry for {ch!r} in {where}")
        users = self.legend_users(ch, map_name)
        if users:
            raise ValueError(f"{ch!r} is still painted in: {', '.join(users)}. "
                             f"Paint over it first.")
        lines, start, end = block
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def save_flag(self, name, bit=None):
        """Name a bit of the tile flag byte, so a project can have its own `water`.

        The bit is written down rather than assigned by position because a flag value is
        baked into built maps AND compiled into game code: a name that silently changes
        bit would break both, and neither would say so.
        """
        if name in pa.FLAG_NAMES:
            raise ValueError(f"{name!r} is built in and cannot be redefined")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("a flag name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        current = self.flag_names()
        if bit is None:
            # A flag that already exists keeps its bit. Picking "the lowest free one"
            # would hand an existing name a DIFFERENT bit -- silently invalidating every
            # map already built with it and every `& TILE_FLAG_X` already compiled, which
            # is the one thing writing bits down instead of positions exists to prevent.
            bit = current.get(name)
        if bit is None:
            bit = next((b for b in pa.FLAG_USER_BITS if b not in current.values()), None)
            if bit is None:
                raise ValueError("all six free flag bits are taken; the flag byte is full")
        bit = int(bit)
        taken = next((n for n, b in current.items() if b == bit and n != name), None)
        if taken:
            raise ValueError(f"0x{bit:02X} is already {taken!r}")

        lines = open(self.path).read().split("\n")
        head = next((i for i, l in enumerate(lines) if l.strip() == "[tile_flags]"), None)
        entry = f"{name} = 0x{bit:02X}"
        if head is None:
            lines += ["", "# Tile flags this project invented. Each becomes a "
                          "TILE_FLAG_* define in the", "# generated header, and "
                          "pnx_map_flags gives the whole byte back to test it.",
                      "[tile_flags]", entry]
        else:
            end = next((j for j in range(head + 1, len(lines))
                        if lines[j].lstrip().startswith("[")), len(lines))
            at = next((j for j in range(head + 1, end)
                       if re.match(rf"\s*{re.escape(name)}\s*=", lines[j])), None)
            if at is not None:
                lines[at] = entry
            else:
                while end > head and lines[end - 1].strip() == "":
                    end -= 1
                lines[end:end] = [entry]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()
        return {"name": name, "bit": bit}

    def flag_users(self, name):
        """Legend characters carrying this flag -- what a removal would silently change."""
        return [ch for ch, e in self.man.get("legend", {}).items()
                if name in e.get("flags", [])]

    def remove_flag(self, name):
        if name in pa.FLAG_NAMES:
            raise ValueError(f"{name!r} is built in and cannot be removed")
        users = self.flag_users(name)
        if users:
            raise ValueError(f"{name!r} is still set on legend "
                             f"{', '.join(repr(c) for c in sorted(users))}. "
                             f"Clear it there first.")
        lines = open(self.path).read().split("\n")
        head = next((i for i, l in enumerate(lines) if l.strip() == "[tile_flags]"), None)
        if head is None:
            raise ValueError(f"no [tile_flags] table in the manifest")
        end = next((j for j in range(head + 1, len(lines))
                    if lines[j].lstrip().startswith("[")), len(lines))
        at = next((j for j in range(head + 1, end)
                   if re.match(rf"\s*{re.escape(name)}\s*=", lines[j])), None)
        if at is None:
            raise ValueError(f"no flag named {name!r}")
        del lines[at]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def legend_chars(self, atlas=None):
        """Pick sensible default characters for a blank map's floor and walls.

        Derived from the legend rather than assuming '.' and '#', so a project with its
        own legend gets a blank map it can actually paint on. "Solid" is a property of
        the TILE now ([[atlas.collision]]), not of the legend character that paints it
        -- so this looks up which tile names/indices that atlas declares solid FIRST,
        then finds a legend character whose `tile` paints one of them, rather than
        reading "solid" off the character's own flags the way it used to.
        """
        specs = self.man.get("atlas", [])
        default = specs[0]["name"] if specs else None
        target = atlas or default
        spec = next((s for s in specs if s["name"] == target), None)
        solid_tiles = {c["tile"] for c in (spec.get("collision", []) if spec else [])
                       if c.get("type") == "solid"}

        floor = wall = None
        for ch, e in self.man.get("legend", {}).items():
            # A character resolving against ANOTHER tileset cannot go in this map's blank
            # room: the build would refuse it, and the new map would open unpaintable.
            if atlas and (e.get("atlas") or default) != atlas:
                continue
            if e.get("tile") in solid_tiles and wall is None:
                wall = ch
            elif not e.get("flags") and e.get("tile") not in solid_tiles and floor is None:
                floor = ch
        return floor, wall

    def add_map(self, name, w, h, atlas, with_scene=True, text=False):
        """Create a map. New maps get their own `.pnxmap` unless `text` asks otherwise.

        The default flipped when the source format landed: a map made in the editor is
        going to be painted in the editor, where the ~90-character ceiling is the thing
        that bites first and a readable diff buys nothing. `text=True` is the escape hatch
        for a map meant to be hand-written, which is what the overworld example is.
        """
        if text:
            return self._add_text_map(name, w, h, atlas, with_scene)

        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")
        if any(m["name"] == name for m in self.man.get("map", [])):
            raise ValueError(f"a map named {name!r} already exists")
        if not (3 <= w <= 255 and 3 <= h <= 255):
            raise ValueError("width and height must be between 3 and 255")
        specs = self.man.get("atlas", [])
        atlas = atlas or (specs[0]["name"] if specs else None)
        if not atlas:
            raise ValueError("a map needs a tileset to draw with — import one first")

        # Two entries, floor and wall, named by ROLE rather than index: every atlas the
        # importer creates autopicks both, and a role survives the sheet being re-carved
        # where a number would silently start pointing at different art. Neither carries
        # a collision flag any more -- collision is a property of the TILE now
        # ([[atlas.collision]], written by add_atlas for "wall"), not of a map cell.
        tiles = [{"atlas": atlas, "index": "floor", "flip": "", "flags": 0},
                 {"atlas": atlas, "index": "wall", "flip": "", "flags": 0}]
        cells = []
        for y in range(h):
            for x in range(w):
                edge = x in (0, w - 1) or y in (0, h - 1)
                cells.append(1 if edge else 0)

        source = f"maps/{name}.pnxmap"
        path = self._safe(source)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mf.write(path, w, h, [1, 1], tiles, cells, [])

        block = f'''

[[map]]
name = "{name}"
atlas = "{atlas}"
out = "map_{name}.bin"
# Cells, start and warps live in the file. See tools/pnx_mapfile.py for why.
source = "{source}"
'''
        if with_scene:
            block += self._scene_block_for(name)
        with open(self.path, "a") as f:
            f.write(block)
        self.reload()

    def _scene_block_for(self, name):
        """A scene that loads a new map plus whatever makes it testable."""
        refs = ""
        sprites = [s["name"] for s in self.man.get("sprite", []) if s.get("name")]
        fonts = [f["name"] for f in self.man.get("font", []) if f.get("name")]
        if sprites:
            refs += f"sprites = {json.dumps(sprites)}\n"
        if fonts:
            refs += f"fonts = {json.dumps(fonts)}\n"
        return f'''
[scene.{name}]
map = "{name}"
{refs}'''

    def _add_text_map(self, name, w, h, atlas, with_scene=True):
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

        floor, wall = self.legend_chars(atlas)
        if not floor or not wall:
            raise ValueError(
                f"atlas {atlas!r} has no walkable legend character and no solid one, so "
                f"there is nothing to build a room out of. Add them from the tile picker.")

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
            # NOT `atlases`: the map declares its own tilesets, sizes its atlas pool and
            # streams them (M4d). A scene listing them too loads a second resident copy,
            # which the pipeline refuses -- so generating one made every new map unbuildable.
            block += self._scene_block_for(name)
        with open(self.path, "a") as f:
            f.write(block)
        self.reload()

    def map_palettes(self, name):
        """The palette variants a map may pick, from the atlases it draws with.

        A variant is named for its file, which is how the pipeline names it too -- so the
        list here is exactly the set `palette =` will accept.
        """
        spec = next((m for m in self.man.get("map", []) if m["name"] == name), None)
        if not spec:
            return []
        drawn = set(self.map_atlases(spec))
        out = []
        for a in self.man.get("atlas", []):
            if a.get("name") not in drawn:
                continue
            for v in a.get("variants", []):
                vname = os.path.splitext(os.path.basename(v))[0]
                if vname not in out:
                    out.append(vname)
        return out

    def set_map_props(self, name, palette=None, worldtile=None, atlas_slots=None,
                      bank_bytes=None, resident=None):
        """The map keys save_map never touched: the M4b palette variant and the M4d
        streaming controls.

        Every one of these was reachable only by hand, which meant the streaming work M4d
        measured so carefully could not be tuned from the editor that reports its cost.
        A None leaves the key alone; the string "" removes it, which is how a map goes
        back to the pipeline's own choice.
        """
        spec = next((m for m in self.man.get("map", []) if m["name"] == name), None)
        if not spec:
            raise ValueError(f"no map named {name!r}")

        want = []
        if palette is not None:
            if palette == "":
                want.append(("palette", None))
            else:
                known = self.map_palettes(name)
                if palette not in known:
                    raise ValueError(
                        f"{palette!r} is not a palette variant of this map's tilesets "
                        f"(available: {', '.join(known) or 'none'})")
                want.append(("palette", f'palette = "{palette}"'))

        if worldtile is not None:
            if worldtile in ("", "auto"):
                want.append(("worldtile", 'worldtile = "auto"' if worldtile == "auto"
                             else None))
            else:
                wt = int(worldtile)
                # Power of two, because a cell finds its WorldTile by shifting rather than
                # dividing -- which is what makes it free per drawn tile.
                if not pa.WORLDTILE_MIN <= wt <= pa.WORLDTILE_MAX or wt & (wt - 1):
                    raise ValueError(
                        f"worldtile must be a power of two between {pa.WORLDTILE_MIN} "
                        f"and {pa.WORLDTILE_MAX}")
                want.append(("worldtile", f"worldtile = {wt}"))

        if atlas_slots is not None:
            if atlas_slots == "":
                want.append(("atlas_slots", None))
            else:
                n = int(atlas_slots)
                count = len(self.map_atlases(spec))
                if not 1 <= n <= count:
                    raise ValueError(
                        f"atlas_slots must be between 1 and {count}, the number of "
                        f"tilesets this map declares -- outside that it is either a pool "
                        f"too small for one atlas or one that can never fill")
                want.append(("atlas_slots", f"atlas_slots = {n}"))

        if bank_bytes is not None:
            if bank_bytes == "":
                want.append(("bank_bytes", None))
            else:
                b = int(bank_bytes)
                if b < 512:
                    raise ValueError(
                        "bank_bytes below 512 is under one WorldTile's worth of cells, so "
                        "every bank would hold a single tile and the map would cost a "
                        "resource each")
                want.append(("bank_bytes", f"bank_bytes = {b}"))

        if resident is not None:
            want.append(("resident", "resident = true" if resident else None))

        if not want:
            return

        lines, start, end = self._map_block(name)
        # Anchored after `name`, which is always the first key: inserting further down
        # risks landing inside the `rows = """..."""` block, and anything after a
        # [map.legend] subtable would bind to the subtable instead of the map.
        anchor = next((j for j in range(start + 1, end)
                       if re.match(r"\s*name\s*=", lines[j])), start)
        for key, value in want:
            at = next((j for j in range(start + 1, end)
                       if re.match(rf"\s*{key}\s*=", lines[j])), None)
            if at is not None and value:
                lines[at] = value
            elif at is not None:
                del lines[at]
                end -= 1
                if at <= anchor:
                    anchor -= 1
            elif value:
                lines[anchor + 1:anchor + 1] = [value]
                end += 1
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def map_users(self, name):
        """What still points at this map: warps aimed at it, and scenes that load it."""
        users = []
        for m in self.man.get("map", []):
            if m["name"] == name:
                continue
            for w in m.get("warps", []):
                to = w.get("to")
                if to and to[0] == name:
                    users.append(f"map {m['name']} warps to it")
                    break
        for sname, spec in self.man.get("scene", {}).items():
            if spec.get("map") == name:
                users.append(f"scene {sname} loads it")
        return users

    def remove_map(self, name):
        """Delete a [[map]] and its own [map.legend], once nothing points at it."""
        found = self._map_block(name)
        if not found:
            raise ValueError(f"no map named {name!r}")
        users = self.map_users(name)
        if users:
            raise ValueError(f"cannot remove {name!r} — {'; '.join(users)}. "
                             f"Repoint or remove those first.")
        lines, start, end = found
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def rename_map(self, old, new):
        """Rename a map, carrying every reference with it.

        Warp destinations and scene `map =` keys are updated in the same pass rather than
        left to the author: a rename that only changes the declaration builds, and then
        fails at the first warp with an error naming a map nobody has heard of.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", new):
            raise ValueError("name must be lowercase letters, digits and underscores")
        if not self._map_block(old):
            raise ValueError(f"no map named {old!r}")
        if any(m["name"] == new for m in self.man.get("map", [])):
            raise ValueError(f"a map named {new!r} already exists")

        lines, start, end = self._map_block(old)
        for j in range(start, end):
            # Rewritten in place through a capture of the original indentation, so a
            # manifest that indents its tables keeps doing so rather than growing one
            # flush-left line in the middle of an indented block.
            m = re.match(rf'^(\s*name\s*=\s*)"{re.escape(old)}"\s*$', lines[j])
            if m:
                lines[j] = f'{m.group(1)}"{new}"'
                break

        text = "\n".join(lines)
        # Warp destinations: `to = ["old", x, y]`, wherever they appear.
        text = re.sub(rf'(to\s*=\s*\[\s*)"{re.escape(old)}"', rf'\g<1>"{new}"', text)
        # Scene map keys. Scoped to a `map =` line so a sprite or atlas that happens to
        # share the name is left alone.
        text = re.sub(rf'^(\s*map\s*=\s*)"{re.escape(old)}"', rf'\g<1>"{new}"',
                      text, flags=re.M)
        with open(self.path, "w") as f:
            f.write(text)
        self.reload()

    def save_source_map(self, name, w, h, cells, tiles, start, warps):
        """Write a `.pnxmap` map back to its own file.

        The manifest is not touched at all: a source map's grid, start and warps live in
        the file, so there is nothing here for TOML to hold and nothing that could drift
        out of step with it.
        """
        spec = next((m for m in self.man.get("map", []) if m["name"] == name), None)
        if not spec:
            raise ValueError(f"no map named {name!r}")
        if "source" not in spec:
            raise ValueError(f"map {name!r} is authored as `rows`, not a source file")

        path = self._safe(spec["source"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            mf.write(path, w, h, start, tiles, cells, warps)
        except mf.MapFileError as e:
            raise ValueError(str(e)) from None
        self.reload()

    def migrate_map(self, name, source=None):
        """Move a `rows` map into its own `.pnxmap`, leaving the manifest pointing at it.

        The one-way door this is NOT: `to_rows` in pnx_mapfile turns a small binary map
        back into text, so a project that tries this and dislikes it is not stuck. What it
        does cost is the readable git diff, which is why `rows` stays supported and why
        the overworld example is deliberately left in it.
        """
        spec = next((m for m in self.man.get("map", []) if m["name"] == name), None)
        if not spec:
            raise ValueError(f"no map named {name!r}")
        if "source" in spec:
            raise ValueError(f"map {name!r} already has a source file")

        doc = self.map_doc(spec)
        missing = [t["ch"] for t in doc["tiles"] if t.get("missing")]
        if missing:
            raise ValueError(
                f"map {name!r} paints {', '.join(repr(c) for c in sorted(set(missing)))}, "
                f"which the legend does not define — fix that before converting, or the "
                f"tiles would be frozen as index 0")

        source = source or f"maps/{name}.pnxmap"
        path = self._safe(source)
        if os.path.exists(path):
            raise ValueError(f"{source} already exists")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Roles travel as roles rather than being resolved to whatever number they hold
        # today, which is the property `autopick` gives and the one thing a table of raw
        # indices would quietly lose.
        try:
            mf.write(path, doc["w"], doc["h"], doc["start"],
                     [{k: t[k] for k in ("atlas", "index", "flip", "flags")}
                      for t in doc["tiles"]],
                     doc["cells"], doc["warps"])
        except mf.MapFileError as e:
            raise ValueError(str(e)) from None

        # The manifest keeps everything that is about the map's PLACE in the project --
        # its atlases, palette and streaming keys -- and loses only what the file now owns.
        lines, start_i, end_i = self._map_block(name)
        out = []
        # `rows` is a triple-quoted block and `warps` is routinely a multi-line array --
        # the worldtiles example writes one warp per line. Dropping only the line the key
        # sits on left the continuation lines behind as bare TOML, which parses as
        # nonsense; so each dropped key is consumed to its real end.
        depth, in_rows, drop = 0, False, False
        for line in lines[start_i:end_i]:
            if in_rows:
                if '"""' in line:
                    in_rows = False
                continue
            if depth:
                depth += line.count("[") - line.count("]")
                continue

            key = (re.match(r"\s*([a-z_]+)\s*=", line) or [None, None])[1]
            if key in ("rows", "start", "warps"):
                value = line.split("=", 1)[1]
                if key == "rows":
                    in_rows = '"""' not in value.strip()[3:]
                    out.append(f'source = "{source}"')
                else:
                    depth = value.count("[") - value.count("]")
                continue
            if line.lstrip().startswith("[map.legend."):
                break
            out.append(line)
        lines[start_i:end_i] = out
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()
        return {"source": source, "tiles": len(doc["tiles"]),
                "bytes": os.path.getsize(path)}

    @staticmethod
    def _layer_lines(primary=False, source=None, rows=None, start=None, warps=None,
                     parallax_pct=None, wrap=None):
        """The body lines of one `[[map.layer]]` sub-table."""
        body = ["[[map.layer]]"]
        if primary:
            body.append("primary = true")
        if source is not None:
            body.append(f'source = "{source}"')
        if parallax_pct is not None and parallax_pct != 255:
            body.append(f"parallax_pct = {parallax_pct}")
        if wrap:
            body.append("wrap = true")
        if start is not None:
            body.append(f"start = [{start[0]}, {start[1]}]")
        if warps is not None:
            warp_src = ", ".join(
                '{{ at = [{}, {}], to = ["{}", {}, {}] }}'.format(
                    w["at"][0], w["at"][1], w["to"][0], w["to"][1], w["to"][2])
                for w in warps)
            body.append(f"warps = [{warp_src}]")
        if rows is not None:
            clean = rows.strip("\n")
            body.append(f'rows = """\n{clean}\n"""')
        return body

    def add_map_layer(self, name):
        """Add a new, blank layer above every layer this map already has.

        The map's first layer is not a new concept -- it is whatever `rows`/`source` the
        map already had, made explicit as `primary = true` the first time a SECOND layer
        is added, in whichever format it already used (no forced conversion to `.pnxmap`
        the way `migrate_map` does -- that is a separate, deliberate choice). A map
        already declaring `[[map.layer]]` just gets one more appended.

        The new layer is always `.pnxmap`-backed regardless of the primary's own format:
        a blank layer has no existing rows to preserve, and a binary file means painting
        it never needs TOML surgery, only a rewrite of that one file.
        """
        spec = next((m for m in self.man.get("map", []) if m["name"] == name), None)
        if not spec:
            raise ValueError(f"no map named {name!r}")

        doc = self.map_doc(spec)
        primary_doc = doc["layers"][doc["primary"]]
        w, h = primary_doc["w"], primary_doc["h"]
        if not w or not h:
            raise ValueError(f"map {name!r} has no cells yet -- paint it before adding "
                             f"a layer")

        names = self.map_atlases(spec)
        atlas = names[0] if names else None
        if not atlas:
            raise ValueError(f"map {name!r} has no atlas to draw a new layer with")

        lines, mstart, mend = self._map_block(name)
        layer_specs = spec.get("layer")

        if not layer_specs:
            # Explode the map's own plane into an explicit primary [[map.layer]]: drop
            # rows/source/start/warps from the [[map]] table (consumed to their real end,
            # same as migrate_map's own key-dropping scan -- `rows` is triple-quoted and
            # `warps` is routinely multi-line, so dropping only the line the key sits on
            # would leave continuation lines behind as bare TOML), then re-add them,
            # UNCHANGED, as the new sub-table's own keys.
            body, depth, in_rows = [], 0, False
            for line in lines[mstart + 1:mend]:
                if in_rows:
                    if '"""' in line:
                        in_rows = False
                    continue
                if depth:
                    depth += line.count("[") - line.count("]")
                    continue
                key = (re.match(r"\s*([a-z_]+)\s*=", line) or [None, None])[1]
                if key in ("rows", "source", "start", "warps"):
                    value = line.split("=", 1)[1]
                    if key == "rows":
                        in_rows = '"""' not in value.strip()[3:]
                    else:
                        depth = value.count("[") - value.count("]")
                    continue
                body.append(line)

            primary_block = self._layer_lines(
                primary=True, source=spec.get("source"), rows=spec.get("rows"),
                start=spec.get("start", [1, 1]), warps=spec.get("warps", []))
            insert_at = next((i for i, l in enumerate(body)
                              if l.lstrip().startswith("[")), len(body))
            body[insert_at:insert_at] = [""] + primary_block
            lines[mstart + 1:mend] = body
            mend = mstart + 1 + len(body)
            layer_specs = [dict(spec)]  # for the filename-collision scan below only

        source = f"maps/{name}_layer{len(layer_specs) + 1}.pnxmap"
        n = len(layer_specs) + 1
        while os.path.exists(self._safe(source)):
            n += 1
            source = f"maps/{name}_layer{n}.pnxmap"
        path = self._safe(source)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tiles = [{"atlas": atlas, "index": "floor", "flip": "", "flags": 0}]
        mf.write(path, w, h, [0, 0], tiles, [0] * (w * h), [])

        new_block = self._layer_lines(primary=False, source=source)
        lines[mend:mend] = [""] + new_block
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()
        return {"source": source}

    def remove_map_layer(self, name, layer_index):
        """Remove one declared layer by its position in `map_doc`'s (file-order) `layers`
        list. Refuses to remove a map's last layer -- there is always at least one plane
        -- and refuses to remove the primary while another layer remains, since that
        would leave nothing owning start/warps; mark a different layer `primary` first.
        """
        found = self._map_layer_blocks(name)
        if not found:
            raise ValueError(f"map {name!r} has no [[map.layer]] entries to remove")
        lines, mstart, mend, blocks = found
        if not 0 <= layer_index < len(blocks):
            raise ValueError(f"map {name!r} has {len(blocks)} layers, no index "
                             f"{layer_index}")
        if len(blocks) == 1:
            raise ValueError("a map needs at least one layer -- remove the map instead")

        spec = next((m for m in self.man.get("map", []) if m["name"] == name), None)
        layer_specs = spec.get("layer", [])
        primary_i = next((i for i, ls in enumerate(layer_specs) if ls.get("primary")), 0)
        if layer_index == primary_i:
            raise ValueError("cannot remove the primary layer -- mark another layer "
                             "`primary` first")

        lstart, lend = blocks[layer_index]
        while lend < mend and lines[lend].strip() == "":
            lend += 1
        while lstart > mstart and lines[lstart - 1].strip() == "":
            lstart -= 1
        del lines[lstart:lend]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def save_map_layer_source(self, name, layer_index, w, h, cells, tiles, start=None,
                              warps=None):
        """Save one `.pnxmap`-backed layer's painted cells back to its own file.

        Layer index is into `map_doc`'s file-order `layers` list. `start`/`warps` only
        apply when this layer is the primary; passed through unchanged (defaulting to
        keeping whatever the file already has via [0, 0]/[] otherwise) when it is not.
        """
        spec = next((m for m in self.man.get("map", []) if m["name"] == name), None)
        if not spec:
            raise ValueError(f"no map named {name!r}")

        layer_specs = spec.get("layer")
        if not layer_specs:
            if layer_index != 0:
                raise ValueError(f"map {name!r} has only one layer")
            return self.save_source_map(
                name, w, h, cells, tiles,
                start if start is not None else spec.get("start", [1, 1]),
                warps if warps is not None else spec.get("warps", []))

        if not 0 <= layer_index < len(layer_specs):
            raise ValueError(f"map {name!r} has {len(layer_specs)} layers, no index "
                             f"{layer_index}")
        ls = layer_specs[layer_index]
        if "source" not in ls:
            raise ValueError(f"layer {layer_index} of map {name!r} is authored as "
                             f"`rows`, not `source`")
        primary_i = next((i for i, l2 in enumerate(layer_specs) if l2.get("primary")), 0)
        is_primary = layer_index == primary_i

        path = self._safe(ls["source"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            mf.write(path, w, h,
                    start if is_primary and start is not None else [0, 0],
                    tiles, cells,
                    warps if is_primary and warps is not None else [])
        except mf.MapFileError as e:
            raise ValueError(str(e)) from None
        self.reload()

    def save_map_layer_rows(self, name, layer_index, rows, start=None, warps=None):
        """Save one `rows`-backed layer's painted cells back to its own `[[map.layer]]`
        sub-table -- the same regex-substitution `save_map` already uses for the
        implicit single-layer case, scoped to one layer's own line range instead of the
        whole `[[map]]` block.
        """
        spec = next((m for m in self.man.get("map", []) if m["name"] == name), None)
        if not spec:
            raise ValueError(f"no map named {name!r}")

        layer_specs = spec.get("layer")
        if not layer_specs:
            if layer_index != 0:
                raise ValueError(f"map {name!r} has only one layer")
            return self.save_map(
                name, rows, start if start is not None else spec.get("start", [1, 1]),
                warps if warps is not None else spec.get("warps", []))

        if not 0 <= layer_index < len(layer_specs):
            raise ValueError(f"map {name!r} has {len(layer_specs)} layers, no index "
                             f"{layer_index}")
        if "rows" not in layer_specs[layer_index]:
            raise ValueError(f"layer {layer_index} of map {name!r} is authored as "
                             f"`source`, not `rows`")
        primary_i = next((i for i, l2 in enumerate(layer_specs) if l2.get("primary")), 0)
        is_primary = layer_index == primary_i

        lines, mstart, mend, blocks = self._map_layer_blocks(name)
        lstart, lend = blocks[layer_index]
        chunk = "\n".join(lines[lstart:lend])

        body = "\n".join(rows)
        chunk = re.sub(r'rows\s*=\s*""".*?"""', f'rows = """\n{body}\n"""', chunk,
                       flags=re.S)

        if is_primary and start is not None:
            if re.search(r"^start\s*=", chunk, re.M):
                chunk = re.sub(r"^start\s*=\s*\[.*?\]$",
                               f"start = [{start[0]}, {start[1]}]", chunk, flags=re.M)
            else:
                chunk = re.sub(r"(^\[\[map\.layer\]\]$)",
                               rf"\1\nstart = [{start[0]}, {start[1]}]", chunk,
                               count=1, flags=re.M)
        if is_primary and warps is not None:
            warp_src = ", ".join(
                '{{ at = [{}, {}], to = ["{}", {}, {}] }}'.format(
                    w["at"][0], w["at"][1], w["to"][0], w["to"][1], w["to"][2])
                for w in warps)
            if re.search(r"^warps\s*=", chunk, re.M):
                chunk = re.sub(r"^warps\s*=\s*\[.*?\]$", f"warps = [{warp_src}]", chunk,
                               flags=re.M | re.S)
            elif re.search(r"^start\s*=", chunk, re.M):
                chunk = re.sub(r"(^start\s*=.*$)", rf"\1\nwarps = [{warp_src}]", chunk,
                               count=1, flags=re.M)
            else:
                chunk = re.sub(r"(^\[\[map\.layer\]\]$)", rf"\1\nwarps = [{warp_src}]",
                               chunk, count=1, flags=re.M)

        lines[lstart:lend] = chunk.split("\n")
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def save_map(self, name, rows, start, warps, atlas=None, atlases=None):
        """Rewrite one map's rows/start/warps in place, touching nothing else.

        Located by scanning `[[map]]` blocks for the matching name rather than by
        rewriting the parsed document, so every comment in the file survives -- which
        matters more here than elsewhere, because manifests carry the reasoning behind
        the content.

        `atlas` and `atlases` are the same key spelled for one tileset or many, and the
        two are mutually exclusive in the file -- so writing either clears the other
        rather than leaving a manifest that says both.
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

            # One tileset stays spelled `atlas`, because that is what almost every map is
            # and what almost every manifest already says. The list form only appears when
            # a map actually draws from several.
            want = list(atlases) if atlases else ([atlas] if atlas else [])
            if want:
                line = (f'atlas = "{want[0]}"' if len(want) == 1
                        else "atlases = [" + ", ".join(f'"{a}"' for a in want) + "]")

                # Written over whichever spelling the file already used, so the key keeps
                # its place and any comment above it. Both spellings are then swept, which
                # is what stops a manifest that once said `atlas` and now says `atlases`
                # from quietly carrying both -- where the pipeline would take the list and
                # the leftover line would describe a map that no longer exists.
                key = re.compile(r"^(?:atlas\s*=[^\n]*|atlases\s*=\s*\[.*?\])\n",
                                 re.M | re.S)
                if key.search(chunk):
                    hits = []

                    def once(_m):
                        hits.append(1)
                        return line + "\n" if len(hits) == 1 else ""

                    chunk = key.sub(once, chunk)
                else:
                    chunk = re.sub(r"(^name\s*=[^\n]*$)",
                                   lambda m: m.group(1) + "\n" + line,
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

