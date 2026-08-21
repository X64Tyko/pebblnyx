"""Construction, reload/save, top-level state(), and project-wide display settings."""

import os
import re
import tomllib

import pnx_assets as pa                                     # noqa: E402
import pnx_project as pp                                    # noqa: E402
import pnx_preview as pv                                    # noqa: E402


class CoreMixin:
    def __init__(self, target):
        """`target` is a project FOLDER or a path to its manifest.

        Folders are the interesting case: a distributed editor is handed a directory and
        has to work out the rest. A manifest path still works, because that is what every
        existing invocation passes.
        """
        target = os.path.abspath(target)
        if os.path.isdir(target):
            self.root = target
            self.meta = pp.load(target)          # raises if it is not a project
            self.path = os.path.join(target, self.meta.get("manifest", "assets.toml"))
        else:
            self.path = target
            self.root = os.path.dirname(self.path)
            try:
                self.meta = pp.load(self.root)
            except ValueError:
                self.meta = {"name": None, "manifest": os.path.basename(self.path)}
        # ((elf, mtime), report). Reading an ELF costs two subprocesses, and the budget
        # panel repaints while a map is being painted.
        self._app_cache = None
        self.reload()

    def reload(self):
        with open(self.path, "rb") as f:
            self.man = tomllib.load(f)
        self.project = self.man.get("project", {})
        self.res = os.path.join(self.root, self.project.get("resources", "resources"))
        self.built = os.path.isdir(self.res) and \
            os.path.exists(os.path.join(self.res, "palettes.bin"))

    def _upright(self, img):
        """A tile from a built blob, turned back the way the artist drew it.

        Blobs in a landscape project are stored rotated -- that is the whole mechanism --
        but the editor is where content is AUTHORED, and an author comparing a tile grid
        against their PNG should not have to tilt their head. Everything the editor shows
        is in the author's frame; only the .bin is in the framebuffer's.
        """
        # rotate() turns anticlockwise, and `expand` keeps the pixels rather than
        # cropping them to the original box -- which for a square tile is the same thing,
        # and for anything else is the difference between a preview and a bug report.
        if self.orientation == pa.ORIENT_BUTTONS_TOP:
            return img.rotate(90, expand=True)         # baked clockwise, so undo it
        if self.orientation == pa.ORIENT_BUTTONS_BOTTOM:
            return img.rotate(-90, expand=True)
        if self.orientation == pa.ORIENT_BUTTONS_LEFT:
            return img.rotate(180, expand=True)        # a half-turn undoes itself either way
        return img

    def palette_swatches(self):
        if not self.built:
            return []
        pals = pv.parse_palettes(pv.read(os.path.join(self.res, "palettes.bin")))
        return [[("transparent" if pv.gcolor_rgb(c) is None
                  else "#%02x%02x%02x" % pv.gcolor_rgb(c)) for c in p] for p in pals]

    def state(self):
        legend = self._legend_payload(self.man.get("legend", {}))
        return {
            # Every flag name a legend entry may carry, and its bit. Built-ins first so
            # the page can show them as the fixed pair they are, then whatever
            # [tile_flags] invented.
            "flags": self.flag_names(),
            # No free bits to offer: custom [tile_flags] is retired outright until the
            # sparse per-cell EXTENDED table replaces it (see MAP_ROTATE's comment in
            # pnx_assets.py) -- there is no byte left to claim a bit of any more.
            "flag_bits_free": [],
            "name": self.project.get("name", "project"),
            "built": self.built,
            # Where this project actually lives. Worth surfacing because the editor can
            # find a manifest on its own, and "which project am I editing" then stops
            # being obvious.
            "paths": {
                "root": self.root,
                "manifest": self.path,
                "resources": self.res,
                "header": os.path.join(
                    self.root, self.project.get("header", "src/c/assets_gen.h")),
            },
            "project_file": self.meta,
            # As the manifest states them -- relative to the root. `paths` holds the
            # resolved absolute versions, which are the wrong thing to write back.
            "project_resources": self.project.get("resources", "resources"),
            "project_header": self.project.get("header", "src/c/assets_gen.h"),
            "engine": pp.framework_state(self.root, self.meta),
            "legend": legend,
            "atlases": self.atlases(),
            "palettes": self.palette_swatches(),
            "maps": self.maps(),
            "fonts": self.fonts(),
            # Keyed by name for the font preview, which picks a page to render; the list
            # form carries the per-entry cost the Dialog tab shows.
            "dialog": {k: v.get("pages", [])
                       for k, v in self.man.get("dialog", {}).items()},
            "dialogs": self.dialogs(),
            "songs": self.songs(),
            "samples": self.samples(),
            "wavs": self.wav_files(),
            "waveforms": list(pa.WAVEFORMS),
            "lfo_targets": list(pa.LFO_TARGETS),
            "filter_modes": list(pa.FILTER_MODES),
            "scenes": self.scenes(),
            "sprites": self.sprites(),
            "sprite_names": [s.get("name") for s in self.man.get("sprite", [])
                             if s.get("name")],
            "nine_slices": self.nine_slices(),
            "nine_slice_names": [ns.get("name") for ns in self.man.get("nine_slice", [])
                                 if ns.get("name")],
            "hud_vars": self.hud_vars(),
            "hud_windows": self.hud_windows(),
            # The canvas the author is working on, which is the device's display turned
            # if the project is landscape. Sent as dimensions rather than as a flag so
            # the page never has to know which way round that is.
            # From the MANIFEST, not from built blobs: `atlases` is empty until a
            # build has run, and whether an atlas exists is a question about the
            # manifest.
            "atlas_names": [a.get("name") for a in self.man.get("atlas", [])
                            if a.get("name")],
            "orientation": pa.ORIENT_NAMES[self.orientation],
            "screen": [self.SCREEN_W, self.SCREEN_H],
            "force_screen_lock": self.force_screen_lock,
            "device_address": self.device_address,
            "budget": self.project.get("budget_bytes", 262144),
            "used": sum(os.path.getsize(os.path.join(self.res, f))
                        for f in os.listdir(self.res) if f.endswith(".bin"))
            if self.built else 0,
            "app": self.app_size(),
            "save": self.save_size(),
            # The catalog behind the overhead check (below) and the emulator panel --
            # sent once here rather than hardcoded twice in the page, so a platform the
            # SDK adds or drops only has to change in one place.
            "platforms": self.PLATFORMS,
        }

    @property
    def orientation(self):
        return pa.parse_orientation(self.project.get("orientation"), "[project]")

    @property
    def landscape(self):
        # buttons_left is portrait turned upside down, not sideways -- `!= RIGHT` would
        # have swapped SCREEN_W/H for it and shown every preview at the wrong aspect.
        return self.orientation in pa.LANDSCAPE_ORIENTS

    @property
    def SCREEN_W(self):
        return self.DISPLAY_H if self.landscape else self.DISPLAY_W

    @property
    def SCREEN_H(self):
        return self.DISPLAY_W if self.landscape else self.DISPLAY_H

    def set_orientation(self, value):
        """Rewrite `orientation` inside [project], creating it if it is not there.

        Edited in place rather than appended, which every other writer here does: an
        appended key lands in whatever table happens to be last in the file, and a
        `orientation` under [[font]] is ignored silently by the pipeline and by the
        author reading their own manifest back.
        """
        if value not in pa.ORIENTATIONS:
            raise ValueError(f"unknown orientation {value!r}")

        lines = open(self.path).read().split("\n")
        start = next((i for i, l in enumerate(lines)
                      if l.strip() == "[project]"), None)
        if start is None:
            raise ValueError("this manifest has no [project] table")

        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))

        for i in range(start + 1, end):
            if re.match(r"\s*orientation\s*=", lines[i]):
                lines[i] = f'orientation = "{value}"'
                break
        else:
            lines.insert(start + 1, f'orientation = "{value}"')
            lines.insert(start + 1,
                         "# Where the button cluster sits when the watch is held to play.")

        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

