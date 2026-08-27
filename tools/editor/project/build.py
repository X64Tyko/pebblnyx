"""The asset pipeline build, and the size/budget estimate shown before one runs."""

import contextlib
import io
import os
import platform
import traceback

import pnx_assets as pa                                     # noqa: E402
import pnx_project as pp                                    # noqa: E402
import pnx_preview as pv                                    # noqa: E402
import size_report as sr                                    # noqa: E402

# Real struct sizes on the actual ARM target (Pebble's own 4-byte pointers, not this
# process's), measured via `nm` on a real compiled emery build rather than sizeof() here
# -- the online editor has no ARM compiler to ask (see app_size()'s own comment for why
# that method already degrades gracefully without one), so there is no way to derive
# these live. Update by hand if the structs themselves change shape.
ARENA_STRUCT_BYTES = {
    "atlas": 44,       # PnxAtlas
    "sprite": 36,      # PnxSprite
    "nine_slice": 12,  # PnxNineSlice
    "font": 24,        # PnxFont
    "map": 260,        # PnxMap
    "dialog": 16,      # PnxDialog
    "palette": 16,     # PnxPalette
}
# PnxHuffmanTable's own fixed fields (first_code/first_symbol/count[17] each, symbols
# pointer, cols, run_bits) -- the variable part (symbols[cols], 2 bytes/entry) is added
# per project from that project's own real built table, not guessed.
HUFFMAN_TABLE_FIXED_BYTES = 112


class BuildMixin:
    def _map_bytes(self, spec, roles=None, legend=None):
        """Exact blob size for a map, without building it.

        Mirrors finish_map() in the pipeline: an 8-byte header, u16 override count plus
        two bytes, an optional palette table, two bytes per cell, three per override,
        five per warp.
        """
        rows = [r for r in spec["rows"].strip("\n").split("\n") if r.strip()]
        h = len(rows)
        w = max((len(r) for r in rows), default=0)
        warps = len(spec.get("warps", []))

        pal_bytes = 0
        if spec.get("palette"):
            atlas = spec.get("atlas") or (self.man.get("atlas") or [{}])[0].get("name")
            pal_bytes = self._atlas_tile_count(atlas)

        # Flag overrides need the atlas's flag defaults, which only exist after a build.
        # Taken from the previous build's blob where there is one, because assuming zero
        # UNDER-estimates -- and underestimating is the one direction that matters here:
        # it is what lets someone sail past the cap believing they are inside it.
        overrides = self._map_overrides(spec)
        return (pa.HEADER_BYTES + 4 + pal_bytes + w * h * 2 + overrides * 3 + warps * 5,
                w, h)

    def _map_overrides(self, spec):
        """Override count from the last build, or a conservative guess if never built."""
        out = spec.get("out")
        blob = os.path.join(self.res, out) if out else None
        if blob and os.path.exists(blob):
            try:
                with open(blob, "rb") as f:
                    head = f.read(pa.HEADER_BYTES + 2)
                return int.from_bytes(head[pa.HEADER_BYTES:pa.HEADER_BYTES + 2], "little")
            except Exception:                            # noqa: BLE001
                pass
        # Never built. Rather than zero, assume the density this project's other maps
        # actually show, so a first estimate errs high instead of low.
        rows = [r for r in spec["rows"].strip("\n").split("\n") if r.strip()]
        cells = len(rows) * max((len(r) for r in rows), default=0)
        return int(cells * self._override_density())

    def _override_density(self):
        """Overrides per cell across the maps that have been built. Default 0.25."""
        seen, total_cells, total_over = False, 0, 0
        for spec in self.man.get("map", []):
            out = spec.get("out")
            blob = os.path.join(self.res, out) if out else None
            if not (blob and os.path.exists(blob)):
                continue
            try:
                with open(blob, "rb") as f:
                    head = f.read(pa.HEADER_BYTES + 2)
                w, h = head[3], head[4]
                total_over += int.from_bytes(
                    head[pa.HEADER_BYTES:pa.HEADER_BYTES + 2], "little")
                total_cells += w * h
                seen = True
            except Exception:                            # noqa: BLE001
                continue
        return (total_over / total_cells) if (seen and total_cells) else 0.25

    def _atlas_tile_count(self, name):
        if not self.built:
            return 0
        spec = next((a for a in self.man.get("atlas", []) if a["name"] == name), None)
        if not spec:
            return 0
        blob = os.path.join(self.res, spec["out"])
        if not os.path.exists(blob):
            return 0
        try:
            return pv.parse_atlas(pv.read(blob))["count"]
        except Exception:                                # noqa: BLE001
            return 0

    def _platform_resource_path(self, out, platform):
        """Which FILE `platform` actually ships for a resource declared as `out`.

        Mirrors the SDK's own `~` tag resolution for the one tag this pipeline ever
        emits: a 1-bit platform prefers `NAME~bw.EXT` over `NAME.EXT` when the tagged
        file exists (atlases and sprites only -- fonts, samples, music, palettes,
        dialog and scenes never get one, so this is a no-op for them and the base file
        is returned). Every other platform never sees the tagged file at all.
        """
        if out and platform and self.PLATFORMS.get(platform, {}).get("bw"):
            bw = pa.bw_variant_path(out)
            if os.path.exists(os.path.join(self.res, bw)):
                return bw
        return out

    def _elf_path(self, platform=None):
        """The linked ELF for `platform`, or the most recently linked one of any."""
        if platform:
            p = os.path.join(self.root, "build", platform, "pebble-app.elf")
            return p if os.path.exists(p) else None
        found = []
        for base, _dirs, files in os.walk(os.path.join(self.root, "build")):
            for f in files:
                if f.endswith(".elf"):
                    p = os.path.join(base, f)
                    found.append((os.path.getmtime(p), p))
        return max(found)[1] if found else None

    def _newest_source(self):
        """Newest mtime among the project's own C sources and its generated header.

        The app figure is the one number here that cannot update as you work: it comes
        from a linker, and the editor cannot run one. So the next best thing is knowing
        when to stop trusting it -- anything newer than the ELF means the number on screen
        describes a binary that no longer exists.

        The generated header counts. Adding a map changes no C a person wrote, but it
        changes `assets_gen.h`, and a binary linked before that does not know the map is
        there. Which means an asset build marks the app stale -- correctly, and often,
        until the editor can run a Pebble build itself.
        """
        newest = 0.0
        for base, dirs, files in os.walk(os.path.join(self.root, "src")):
            dirs[:] = [d for d in dirs if d not in ("build", "__pycache__")]
            for f in files:
                if f.endswith((".c", ".h")):
                    newest = max(newest, os.path.getmtime(os.path.join(base, f)))
        return newest

    def app_size(self, platform=None):
        """Static bytes from the last SDK build, against the uint16 ceiling.

        `platform` picks that platform's own ELF (`build/<platform>/pebble-app.elf`);
        left out, this is the most recently linked ELF of any platform, same as before
        the overhead check existed. Cached on the ELF's mtime: this shells out to `nm`
        and `readelf`, and the panel that shows it repaints on every keystroke of a map
        edit.
        """
        elf = self._elf_path(platform)
        if not elf:
            return {"known": False,
                    "why": (f"no build yet for {platform} — run a Pebble build to measure it"
                            if platform else
                            "no build yet — run a Pebble build to measure the app"),
                    "limit": sr.VIRTUAL_SIZE_LIMIT,
                    "slot": self.PLATFORMS.get(platform, {}).get("ram") if platform else None}

        stamp = os.path.getmtime(elf)
        if self._app_cache and self._app_cache[0] == (elf, stamp):
            return self._app_cache[1]

        nm, readelf = sr.find_tool("arm-none-eabi-nm"), sr.find_tool("arm-none-eabi-readelf")
        if not nm or not readelf:
            out = {"known": False,
                   "why": "the SDK's ARM tools are not installed, so the ELF cannot be read",
                   "limit": sr.VIRTUAL_SIZE_LIMIT}
            self._app_cache = ((elf, stamp), out)
            return out

        try:
            rows = sr.parse_symbols(nm, elf)
            alloc, sections = sr.allocated_size(readelf, elf)
            report = sr.build_report(rows, sr.VIRTUAL_SIZE_LIMIT, alloc, sections)
        except Exception as e:                           # noqa: BLE001
            out = {"known": False, "why": f"could not read {os.path.basename(elf)}: {e}",
                   "limit": sr.VIRTUAL_SIZE_LIMIT}
            self._app_cache = ((elf, stamp), out)
            return out

        used = report["allocated"]
        limit = report["limit"]
        # Modules, biggest first, so the panel can say WHERE the bytes went. The engine's
        # own subsystems are the interesting rows: a game usually cannot shrink `core`,
        # but it can turn a module off in pnx_config.h.
        modules = sorted(((name, m["total"]) for name, m in report["modules"].items()),
                         key=lambda kv: -kv[1])
        # `platform` is the one asked for, if any -- otherwise whichever the newest ELF
        # on disk turned out to belong to.
        platform = platform or os.path.basename(os.path.dirname(elf))
        slot = self.PLATFORMS.get(platform, {}).get("ram")
        out = {
            "known": True,
            "used": used,
            "limit": limit,
            "pct": 100.0 * used / limit if limit else 0,
            "over": used > limit,
            "warn": used > limit * 0.9,
            "mutable": report["total"]["ram"],     # data + bss
            "platform": platform,
            "slot": slot,
            # What is left for the heap, which is what an arena is sized against. Static
            # and heap come out of the same slot, so every byte of code is a byte an
            # arena cannot have.
            "heap": (slot - used) if slot else None,
            "modules": [{"name": n, "bytes": b} for n, b in modules],
            "elf": os.path.relpath(elf, self.root),
            "built": stamp,
            "stale": self._newest_source() > stamp,
        }
        self._app_cache = ((elf, stamp), out)
        return out

    def arena_estimate(self):
        """Minimum resident arena bytes this project's own content requires at startup.

        Not visible anywhere else: palettes, the huffman run-length table, and each
        scene's own atlas/sprite/nine_slice/font/map/dialog slot tables are all arena-
        allocated now (sized to the project's own real content, not a static
        PNX_SCENE_MAX_*/PNX_HUFFMAN_TABLE_MAX_COLS worst case) rather than static -- which
        is the fix, but it also means they no longer show up for free in app_size()'s own
        .bss column the way they used to. This is what makes that cost visible again,
        computed from the manifest (and the real built palettes.bin/huffman_table.bin,
        where those exist) rather than needing a device or an ARM compiler.

        A scene's slot tables are sized to whichever scene is LARGEST, not the sum of
        every scene -- pnx_arena_reset_hi frees the previous scene's slot tables before
        the next one's are allocated (pnx_scene_load's own comment), so only one scene's
        worth is ever resident at a time.

        Deliberately incomplete, and says so in "unknown": pnx_sprite_cache_init/
        pnx_tile_cache_init's own pool sizes and a map's WorldTile bank buffers are both
        sized by arguments a game passes in its OWN C code, not by anything the manifest
        states, so this cannot compute them. Listed as reasons, not silently omitted.
        """
        unknown = []

        palette_bytes = 0
        pal_path = os.path.join(self.res, "palettes.bin")
        if os.path.exists(pal_path):
            # The real, settled count (settle_palettes may share or add across every
            # atlas/sprite in ways only a real build resolves) -- not a count of manifest
            # entries, since palettes are derived, never declared.
            palette_bytes = len(pv.parse_palettes(pv.read(pal_path))) * ARENA_STRUCT_BYTES["palette"]
        else:
            unknown.append("palettes.bin not built yet")

        compress = self.project.get("compress", "bitplane")
        huffman_bytes = 0
        if compress == "huffman":
            hf_out = self.project.get("huffman_table_out", "huffman_table.bin")
            hf_path = os.path.join(self.res, hf_out)
            if os.path.exists(hf_path):
                data = pv.read(hf_path)
                # First 12 bits past the 8-byte blob header are the table's own declared
                # column count -- see pnx_huffman_table_peek_cols's own comment
                # (src/pnx/assets/pnx_huffman.h) for why this is the cheap way to size
                # the real allocation instead of assuming a worst case.
                cols = (data[8] << 4) | (data[9] >> 4)
                huffman_bytes = HUFFMAN_TABLE_FIXED_BYTES + cols * 2
            else:
                unknown.append(f"{hf_out} not built yet")

        peak_scene_bytes = 0
        peak_scene_name = None
        for name, spec in sorted(self.man.get("scene", {}).items()):
            total = (len(spec.get("atlases", [])) * ARENA_STRUCT_BYTES["atlas"]
                    + len(spec.get("sprites", [])) * ARENA_STRUCT_BYTES["sprite"]
                    + len(spec.get("nine_slices", [])) * ARENA_STRUCT_BYTES["nine_slice"]
                    + len(spec.get("fonts", [])) * ARENA_STRUCT_BYTES["font"]
                    + (ARENA_STRUCT_BYTES["map"] if "map" in spec else 0)
                    + (ARENA_STRUCT_BYTES["dialog"] if spec.get("dialog") else 0))
            if total > peak_scene_bytes:
                peak_scene_bytes, peak_scene_name = total, name

        unknown += [
            "pnx_sprite_cache_init/pnx_tile_cache_init pool sizes (set by a game's own "
            "C code, not the manifest)",
            "a map's WorldTile streaming pool (bank_bytes is per-map in the manifest, "
            "but how many banks a game keeps resident is set in its own C code)",
        ]
        return {
            "palette_bytes": palette_bytes,
            "huffman_table_bytes": huffman_bytes,
            "peak_scene_slot_bytes": peak_scene_bytes,
            "peak_scene_name": peak_scene_name,
            "total": palette_bytes + huffman_bytes + peak_scene_bytes,
            "unknown": unknown,
        }

    def save_size(self):
        """Persistent storage. Nothing to measure until M5 builds the save format.

        Present as a cell from the start, because an empty slot with a name is how a
        budget stays visible before it is spendable -- and because persist is the one
        ceiling that cannot be discovered by a build failing. It fails on a watch, on a
        write, in front of a player.
        """
        return {"known": False, "why": "save lands with M5"}

    def estimate(self, overrides=None, platform=None):
        """Current resource cost, per category, without running the pipeline.

        `overrides` lets the editor price a map it has not saved yet -- {name: rows} --
        so the number moves while a map is being painted rather than after.

        `platform`, if given, prices what THAT platform actually ships rather than the
        project's default build: `~bw`-tagged atlas and sprite blobs stand in for their
        base file on a 1-bit target (`_platform_resource_path`), and the cap becomes
        that platform's own appstore ceiling (`PLATFORMS`) rather than the project-wide
        `budget_bytes`, which predates M9 and was really always "the emery cap".
        """
        overrides = overrides or {}
        budget = (self.PLATFORMS[platform]["resources"] if platform
                  else int(self.project.get("budget_bytes", 262144)))
        entries, exact = [], True

        # Built blobs, by the manifest key that produced them.
        def blob_size(out):
            if not out:
                return None
            p = os.path.join(self.res, self._platform_resource_path(out, platform))
            return os.path.getsize(p) if os.path.exists(p) else None

        for kind, key, outfn in (
                ("atlas", "atlas", lambda s: s.get("out")),
                ("sprite", "sprite", lambda s: s.get("out")),
                ("font", "font", lambda s: s.get("out", f"font_{s.get('name')}.bin")),
        ):
            for spec in self.man.get(key, []):
                size = blob_size(outfn(spec))
                if size is None:
                    exact = False
                entries.append({"kind": kind, "name": spec.get("name"),
                                "bytes": size or 0, "known": size is not None})

        for name in ("palettes.bin", "dialog.bin", "scenes.bin"):
            size = blob_size(name)
            if size:
                entries.append({"kind": name.split(".")[0], "name": name.split(".")[0],
                                "bytes": size, "known": True})

        for sm in sorted(self.man.get("sample", {})):
            size = blob_size(f"sfx_{sm}.bin")
            entries.append({"kind": "sample", "name": sm, "bytes": size or 0,
                            "known": size is not None})
        for sg in sorted(self.man.get("music", {})):
            size = blob_size(f"music_{sg}.bin")
            entries.append({"kind": "music", "name": sg, "bytes": size or 0,
                            "known": size is not None})

        # Maps last, and always computed rather than read: they are the thing being
        # edited, so their on-disk size is the stale one.
        for spec in self.man.get("map", []):
            s = dict(spec)
            if s["name"] in overrides:
                s["rows"] = overrides[s["name"]]
            size, w, h = self._map_bytes(s)
            entries.append({"kind": "map", "name": s["name"], "bytes": size,
                            "known": True, "dims": [w, h]})

        total = sum(e["bytes"] for e in entries)
        by_kind = {}
        for e in entries:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + e["bytes"]

        return {"entries": entries, "by_kind": by_kind, "total": total,
                "budget": budget, "pct": 100.0 * total / budget if budget else 0,
                "over": total > budget, "exact": exact,
                # Warn before the cliff, not at it: at 90% one more zone is the problem.
                "warn": total > budget * 0.9,
                # The app binary rides along, because the two ceilings are spent against
                # by the same edits and reading only one of them is how a project
                # discovers the other at link time.
                "platform": platform, "app": self.app_size(platform), "save": self.save_size()}

    def build(self):
        """Runs the real pipeline -- the editor never writes a blob itself.

        Anything the editor produced that the pipeline would reject must fail here,
        loudly, rather than being smoothed over. The validation is the product.

        Called IN-PROCESS rather than shelled out. It used to run
        `sys.executable pnx_assets.py`, which is correct from a source checkout and wrong
        from a frozen binary, where sys.executable is the editor itself -- so pressing
        Build would have relaunched the editor. Importing the pipeline is also what
        EDITOR.md specifies, and it keeps a BuildError's message intact rather than
        reducing it to an exit code.
        """
        pkg = os.path.join(self.root, "package.json")
        buf = io.StringIO()
        ok = True

        # The engine is staged from the editor before every build, so a project always
        # compiles against the engine in the editor doing the compiling rather than a
        # copy it happens to be carrying.
        try:
            pp.sync_framework(self.root)
        except ValueError as e:
            buf.write(f"engine: {e}\n")

        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                pa.build(self.path, None, None,
                         package=pkg if os.path.exists(pkg) else None)
        except pa.BuildError as e:
            ok = False
            buf.write(f"\nasset build FAILED: {e}\n")
        except Exception as e:                           # noqa: BLE001
            # A crash in the pipeline is a pipeline bug, but it must not take the editor
            # down with it -- the author would lose unsaved map edits to a traceback.
            ok = False
            buf.write(f"\nasset build CRASHED: {type(e).__name__}: {e}\n")
            buf.write(traceback.format_exc())

        self.reload()
        return {"ok": ok, "output": buf.getvalue()}

