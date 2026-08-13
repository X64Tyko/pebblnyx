#!/usr/bin/env python3
"""pebblnyx editor: a local app for building maps, managing assets and packaging.

Runs a small server on localhost and opens a browser. Nothing leaves the machine and
there is nothing to install beyond what the pipeline already needs.

The design rule throughout: **the manifest is the source of truth, and the editor is
just a hand on it.** Saving performs a surgical edit of `assets.toml` -- the block being
changed and nothing else -- so comments, ordering and hand-written sections survive.
An editor that rewrites the whole file would quietly delete the reasoning people leave
in their manifests, which is most of what makes them readable.

Maps are painted in **legend characters**, not tile indices, because that is what a map
actually stores. Painting indices would let you place a tile the legend cannot express,
and the file could then no longer round-trip.

Usage:
    tools/pnx_editor.py <manifest.toml> [--port 8765] [--no-browser]
"""

import argparse
import contextlib
import errno
import http.server
import io
import json
import logging
import os
import platform
import re
import shutil
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import traceback
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pnx_preview as pv                                    # noqa: E402
import pnx_assets as pa                                     # noqa: E402
import pnx_project as pp                                    # noqa: E402
import size_report as sr                                    # noqa: E402

TOOLS = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------- toolchain
#
# Everything except producing a .pbw runs with no external dependency. The .pbw needs the
# Pebble SDK, which is ~767MB and carries its own ARM toolchain at
# <sdk>/toolchain/arm-none-eabi -- so there is exactly one thing to install, not two.
#
# **We do not ship it and we do not fetch it ourselves.** The Pebble Developer License
# grants the licence to the USER -- "limited, non-transferable, non-sublicensable" -- and
# section 5(f) prohibits distributing the SDK. So the editor drives Pebble's own
# first-party tool: it shows the terms, takes a real acceptance, and then runs
# `pebble sdk install`. The bytes go from Pebble's server to the user's disk and we never
# hold a copy, which is the same thing the user would do by hand.
#
# pebble-tool itself is MIT (github.com/coredevices/pebble-tool), so installing it on the
# user's behalf carries none of that weight.

SDK_TERMS = [
    ("Pebble Terms of Use",
     "https://developer.repebble.com/legal/terms-of-use/index.html"),
    ("Pebble Developer License",
     "https://developer.repebble.com/legal/sdk-license/index.html"),
]

# Where the acceptance is recorded. Beside the editor's own state rather than inside a
# project, because it is a property of the person, not of the game.
def _config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(base, "pebblnyx")
    os.makedirs(path, exist_ok=True)
    return path


# ------------------------------------------------------------------------ updates
#
# The editor ships as a file someone downloaded once. Without this, a fix reaches them
# only if they think to look at a releases page they may never have seen -- so in practice
# everyone runs whatever they first installed, forever.

UPDATE_REPO = "X64Tyko/pebblnyx"
UPDATE_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases"

# Which asset belongs to which machine. Matched on the platform tag the release workflow
# puts in every filename, and ordered: on Windows the installer is preferred over the
# portable zip, because someone clicking "Install" wants the wizard.
UPDATE_ASSETS = {
    ("Windows", "x86_64"): ["-windows-x86_64-setup.exe", "-windows-x86_64.zip"],
    ("Darwin", "arm64"): ["-macos-arm64.dmg"],
    ("Darwin", "x86_64"): ["-macos-x86_64.dmg"],
    ("Linux", "x86_64"): ["-linux-x86_64.tar.gz"],
}


def parse_version(text):
    """`v0.2.0-beta.3` -> ((0, 2, 0), (0, 'beta', 3)) for ordering.

    Semver's rule, and it is the one that matters here: a prerelease sorts BELOW the
    release it leads to, so 0.2.0 beats 0.2.0-beta.3 while 0.2.0-beta.3 beats 0.2.0-beta.2.
    A release with no suffix gets a leading 1 against the prerelease's 0, which is what
    makes that comparison fall out of a plain tuple compare.
    """
    text = (text or "").strip().lstrip("vV")
    base, _, pre = text.partition("-")
    nums = []
    for part in base.split("."):
        nums.append(int(part) if part.isdigit() else 0)
    while len(nums) < 3:
        nums.append(0)

    if not pre:
        return tuple(nums[:3]), (1,)

    # Numeric identifiers compare numerically, everything else as text -- so beta.10 is
    # after beta.9 rather than before it, which a plain string compare gets wrong.
    parts = [int(p) if p.isdigit() else p for p in pre.split(".")]
    return tuple(nums[:3]), tuple([0] + parts)


def newer(candidate, current):
    """True when `candidate` is a version worth offering to someone on `current`."""
    try:
        a, b = parse_version(candidate), parse_version(current)
    except Exception:                                    # noqa: BLE001
        return False
    # Mixed types inside the prerelease tuple (3 vs 'beta') raise rather than mis-order.
    try:
        return a > b
    except TypeError:
        return a[0] > b[0]


class Updater:
    """Checks for, downloads and applies a new editor build.

    Deliberately three separate steps with the user between each one. A silent background
    update is how a tool changes under someone mid-session, and this one carries the
    engine their project compiles against -- so an upgrade is a decision, not a surprise.
    """

    def __init__(self):
        self.current = pp.EDITOR_VERSION
        self._cache = None          # (checked_at, payload)
        self._lock = threading.Lock()
        self.progress = None        # (downloaded, total) while a download runs
        self.downloaded = None      # path to the verified asset, once it is here
        self.busy = False
        self.error = None

    def target(self):
        """The asset names this machine can use, most preferred first."""
        system = platform.system()
        arch = {"AMD64": "x86_64", "x86_64": "x86_64",
                "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine(),
                                                          platform.machine())
        return UPDATE_ASSETS.get((system, arch), [])

    def check(self, force=False):
        """Latest release this machine can install, and whether it beats what is running.

        Cached for an hour. The check runs on a timer at start-up and on every visit to
        Settings, and GitHub rate-limits unauthenticated callers to 60 requests an hour --
        which is a limit a user could reach by clicking around.
        """
        with self._lock:
            if self._cache and not force and time.time() - self._cache[0] < 3600:
                return self._cache[1]

        out = {"current": self.current, "checked": True, "available": False}
        try:
            req = urllib.request.Request(
                UPDATE_API + "?per_page=10",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": f"pebblnyx-editor/{self.current}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                releases = json.load(r)
        except Exception as e:                           # noqa: BLE001
            # Offline is the common case, not an error worth a dialog: the editor works
            # perfectly well without ever reaching GitHub.
            out.update({"checked": False, "why": f"could not reach GitHub: {e}"})
            with self._lock:
                self._cache = (time.time(), out)
            return out

        # Someone already running a prerelease is told about prereleases; someone on a
        # stable build is not dragged onto a beta by an update prompt.
        want_pre = len(parse_version(self.current)[1]) > 1
        wanted = self.target()

        best = None
        for rel in releases:
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not want_pre:
                continue
            tag = rel.get("tag_name", "")
            if not newer(tag, self.current):
                continue
            asset = self._pick(rel.get("assets", []), wanted)
            if not asset:
                continue                                 # nothing for this machine
            if best is None or newer(tag, best[0].get("tag_name", "")):
                best = (rel, asset)

        if best:
            rel, asset = best
            out.update({
                "available": True,
                "version": rel.get("tag_name", "").lstrip("vV"),
                "tag": rel.get("tag_name"),
                "notes": (rel.get("body") or "").strip(),
                "url": rel.get("html_url"),
                "prerelease": bool(rel.get("prerelease")),
                "asset": {"name": asset["name"], "bytes": asset.get("size", 0),
                          "url": asset["browser_download_url"]},
            })
        with self._lock:
            self._cache = (time.time(), out)
        return out

    @staticmethod
    def _pick(assets, wanted):
        for suffix in wanted:
            for a in assets:
                if a.get("name", "").endswith(suffix):
                    return a
        return None

    # ------------------------------------------------------------------ download
    #
    # On a thread, because the editor's server handles one request at a time: a 30 MB
    # download inside a handler would freeze the whole UI, including the progress polls
    # meant to show it moving.

    def start_download(self):
        if self.busy:
            return {"ok": True, "started": False, "already": True}
        self.busy = True
        self.error = None
        self.progress = (0, 0)
        threading.Thread(target=self._download_worker, daemon=True).start()
        return {"ok": True, "started": True}

    def _download_worker(self):
        try:
            r = self.download()
            if not r.get("ok"):
                self.error = r.get("error")
        finally:
            self.busy = False

    def state(self):
        got, total = self.progress or (0, 0)
        return {"busy": self.busy, "got": got, "total": total,
                "pct": (100.0 * got / total) if total else 0,
                "error": self.error,
                "ready": bool(self.downloaded and os.path.exists(self.downloaded)),
                "file": os.path.basename(self.downloaded) if self.downloaded else None}

    def download(self):
        """Fetch the asset for this machine into a temp file, and check what arrived.

        Only ever from the release the check found, on the repo named in this file --
        never a URL handed in from the page, which would turn "check for updates" into
        "download anything anyone can reach the local server with".
        """
        info = self.check()
        if not info.get("available"):
            return {"ok": False, "error": "nothing to download"}
        asset = info["asset"]
        if not asset["url"].startswith(f"https://github.com/{UPDATE_REPO}/releases/"):
            return {"ok": False, "error": f"unexpected asset host: {asset['url']}"}

        dest_dir = os.path.join(tempfile.gettempdir(), "pebblnyx-update")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, asset["name"])

        try:
            req = urllib.request.Request(
                asset["url"],
                headers={"User-Agent": f"pebblnyx-editor/{self.current}"})
            # Written beside the target and renamed at the end, so an interrupted download
            # cannot leave a truncated file that looks like a complete one.
            part = dest + ".part"
            with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or asset["bytes"] or 0)
                got = 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    self.progress = (got, total)
            if total and got != total:
                os.remove(part)
                self.progress = None
                return {"ok": False,
                        "error": f"short download: {got} of {total} bytes"}
            os.replace(part, dest)
        except Exception as e:                           # noqa: BLE001
            self.progress = None
            return {"ok": False, "error": f"download failed: {e}"}

        self.progress = None
        self.downloaded = dest
        return {"ok": True, "path": dest, "bytes": os.path.getsize(dest),
                "name": asset["name"], "version": info["version"]}

    # --------------------------------------------------------------------- apply
    #
    # Three platforms, three different meanings of "install", and only one of them can be
    # done to a running program.

    def apply(self):
        """Install what was downloaded. Returns what the user has to do next.

        Nothing here restarts the editor for the user. Losing an unsaved map to an
        automatic relaunch would be a worse bug than the one being fixed.
        """
        if not self.downloaded or not os.path.exists(self.downloaded):
            return {"ok": False, "error": "nothing downloaded yet"}

        system = platform.system()
        try:
            if system == "Windows":
                # A running .exe cannot be replaced on Windows, so the installer does it:
                # it waits for this process to exit, which it can because it is a separate
                # process. Launch it and step aside.
                os.startfile(self.downloaded)            # noqa: S606
                return {"ok": True, "action": "installer",
                        "message": "The installer is open. Close the editor and let it "
                                   "replace this version."}

            if system == "Darwin":
                # A .app cannot sensibly replace itself while running either, and a
                # scripted copy into /Applications is how an install ends up half-done.
                subprocess.run(["open", self.downloaded], check=False)
                return {"ok": True, "action": "dmg",
                        "message": "The disk image is open. Drag the app into "
                                   "Applications, replacing the old one, then reopen it."}

            return self._apply_linux()
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "error": f"could not start the install: {e}"}

    def _apply_linux(self):
        """Swap the binary in place, keeping the old one until the new one has run.

        Linux is the one platform where a running executable can be replaced: the kernel
        holds the inode this process is executing, so renaming a new file over the path
        affects only the NEXT launch. Rename rather than write-in-place for exactly that
        reason -- an open file rewritten under a running process is how you get a
        SIGBUS instead of an upgrade.
        """
        running = os.path.realpath(sys.executable if getattr(sys, "frozen", False)
                                   else sys.argv[0])
        if not getattr(sys, "frozen", False):
            return {"ok": False,
                    "error": "this is a source checkout, not a packaged build -- "
                             "`git pull` instead"}

        staging = os.path.join(os.path.dirname(self.downloaded), "unpacked")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        with tarfile.open(self.downloaded, "r:gz") as t:
            t.extractall(staging)                        # noqa: S202

        found = None
        for base, _dirs, files in os.walk(staging):
            for f in files:
                if f == os.path.basename(running) or f == "pebblnyx-editor":
                    found = os.path.join(base, f)
                    break
        if not found:
            return {"ok": False, "error": "the archive did not contain the binary"}

        try:
            backup = running + ".old"
            os.replace(running, backup)                  # keep it: this is the rollback
            shutil.copy2(found, running)
            os.chmod(running, 0o755)
        except PermissionError:
            return {"ok": False,
                    "error": f"no permission to replace {running} -- if it lives "
                             f"somewhere system-wide, move it under your home directory "
                             f"or reinstall from the .tar.gz"}

        return {"ok": True, "action": "replaced",
                "message": f"Replaced {running}. Restart the editor to run the new "
                           f"version; the old one is kept as {os.path.basename(backup)}."}


UPDATER = Updater()


class Liveness:
    """Keeps the server alive only while a UI is actually attached to it.

    The native-window build has a real signal: `webview.start()` returns when the window
    closes, and the server stops. The BROWSER build has none -- Linux ships no webview, so
    the editor opens a tab and waits on an Event that nothing ever sets. Closing the tab
    therefore did not close the editor: the process stayed, holding port 8765, and the
    next launch found something already listening and refused to start. "Closed" and
    "still running" were the same state.

    So the page says it is there, every few seconds, and stops saying it when it is gone.

    On close the page also sends an explicit goodbye, which only SHORTENS the window
    rather than exiting outright -- two tabs on one editor is a normal thing to do, and
    the second one's next heartbeat cancels the exit.
    """

    # `grace` MUST exceed the page's heartbeat interval (5s). It is the window a goodbye
    # leaves open for a surviving tab to object -- shorter than one heartbeat and closing
    # the second of two tabs would shut the editor down under the first one.
    def __init__(self, timeout=25.0, grace=8.0):
        self.timeout = timeout
        self.grace = grace
        self.last = time.time()
        self.done = threading.Event()
        self.armed = False

    def touch(self):
        self.last = time.time()

    def goodbye(self):
        self.last = min(self.last, time.time() - (self.timeout - self.grace))

    def watch(self, srv):
        """Runs on a thread; sets `done` once nothing has spoken to us in a while."""
        while not self.done.wait(2.0):
            if time.time() - self.last > self.timeout:
                print("the editor was closed -- shutting down")
                # shutdown() returns once serve_forever has stopped, so by the time the
                # main thread closes the server the listening socket is genuinely free.
                threading.Thread(target=srv.shutdown, daemon=True).start()
                self.done.set()
                return


LIVE = Liveness()


class Toolchain:
    """Finds, installs and keeps track of the Pebble SDK."""

    def __init__(self):
        self.log = []
        self.busy = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------- detection

    @staticmethod
    def pebble_path():
        return shutil.which("pebble")

    @staticmethod
    def installer():
        """How pebble-tool can be installed on this machine.

        A frozen editor has no pip of its own, so this looks for a package manager the
        user already has rather than assuming a Python environment we control.
        """
        for name, cmd in (("uv", ["uv", "tool", "install", "pebble-tool"]),
                          ("pipx", ["pipx", "install", "pebble-tool"]),
                          ("pip", [sys.executable, "-m", "pip", "install", "--user",
                                   "pebble-tool"])):
            if name == "pip":
                if getattr(sys, "frozen", False):
                    continue          # sys.executable is the editor, not an interpreter
                try:
                    subprocess.run([sys.executable, "-m", "pip", "--version"],
                                   capture_output=True, timeout=10)
                except Exception:     # noqa: BLE001
                    continue
                return name, cmd
            if shutil.which(name):
                return name, cmd
        return None, None

    def accepted(self):
        return os.path.exists(os.path.join(_config_dir(), "sdk-license-accepted"))

    def accept(self):
        """Record that the user accepted, with what and when.

        Written as a file rather than held in memory because the grant is to the person
        and persists across runs -- and because a record of what was agreed to, and when,
        is worth having if it is ever asked about.
        """
        import datetime
        with open(os.path.join(_config_dir(), "sdk-license-accepted"), "w") as f:
            f.write(f"accepted {datetime.datetime.now().isoformat(timespec='seconds')}\n")
            for title, url in SDK_TERMS:
                f.write(f"{title}: {url}\n")

    def status(self, remote=False):
        """What is installed, and whether a build can run.

        `remote` is opt-in because listing available versions hits Pebble's server, and
        doing that on every page load would be both slow and rude.
        """
        pebble = self.pebble_path()
        out = {"pebble": pebble, "installed": [], "active": None, "available": [],
               "accepted": self.accepted(), "busy": self.busy,
               "log": "".join(self.log[-400:]),
               "installer": self.installer()[0], "terms": SDK_TERMS}

        if pebble:
            try:
                r = subprocess.run([pebble, "sdk", "list"], capture_output=True,
                                   text=True, timeout=60 if remote else 15)
                section = None
                for line in (r.stdout or "").splitlines():
                    low = line.strip().lower()
                    if low.startswith("installed"):
                        section = "installed"
                    elif low.startswith("available"):
                        section = "available"
                    elif line.strip() and section:
                        v = line.strip()
                        if v.endswith("(active)"):
                            v = v.replace("(active)", "").strip()
                            out["active"] = v
                        out[section].append(v)
            except Exception as e:                       # noqa: BLE001
                out["error"] = str(e)

        out["can_build"] = bool(pebble and out["active"])
        # Newer SDKs are reported rather than installed silently: a toolchain that
        # changes under a project between builds is its own class of confusing bug.
        newer = [v for v in out["available"] if v not in out["installed"]]
        out["newer"] = newer[-1] if newer else None
        return out

    # -------------------------------------------------------------- installing

    def _run(self, cmd, label):
        self.log.append(f"\n$ {' '.join(cmd)}\n")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            for line in p.stdout:
                self.log.append(line)
                del self.log[:-400]
            return p.wait() == 0
        except FileNotFoundError:
            self.log.append(f"{label}: {cmd[0]} not found\n")
            return False
        except Exception as e:                           # noqa: BLE001
            self.log.append(f"{label} failed: {e}\n")
            return False

    def install(self, version="latest"):
        """Install pebble-tool if needed, then the SDK. Runs on a worker thread.

        Refuses without a recorded acceptance. The licence is granted to the user and
        cannot be accepted on their behalf, so this is a real gate rather than a notice --
        which is a little stricter than the official CLI, and the safer side to err on.
        """
        if not self.accepted():
            raise ValueError("the Pebble SDK licence has to be accepted first")
        with self._lock:
            if self.busy:
                raise ValueError("an install is already running")
            self.busy = True

        def work():
            try:
                if not self.pebble_path():
                    name, cmd = self.installer()
                    if not cmd:
                        self.log.append(
                            "No uv, pipx or pip found to install pebble-tool with.\n"
                            "Install one, or install pebble-tool yourself:\n"
                            "    pip install pebble-tool\n")
                        return
                    self.log.append(f"Installing pebble-tool with {name}...\n")
                    if not self._run(cmd, "pebble-tool"):
                        return

                pebble = self.pebble_path()
                if not pebble:
                    self.log.append(
                        "pebble-tool installed but `pebble` is not on PATH -- "
                        "you may need to open a new terminal or add its bin directory.\n")
                    return

                self.log.append(f"\nInstalling Pebble SDK {version} "
                                f"(~767MB, includes the ARM toolchain)...\n")
                if self._run([pebble, "sdk", "install", version], "sdk"):
                    self.log.append("\nDone. The Build button can produce a .pbw now.\n")
            finally:
                self.busy = False

        threading.Thread(target=work, daemon=True).start()


class Project:
    """Everything the editor knows, reloaded from disk on demand."""

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

    # ---------------------------------------------------------------- rendering

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
        return img

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
                "roles": roles,
                "tiles": [pv.data_uri(self._upright(pv.tile_image(atlas, palettes, i, 2)))
                          for i in range(atlas["count"])],
            })
        return out

    def palette_swatches(self):
        if not self.built:
            return []
        pals = pv.parse_palettes(pv.read(os.path.join(self.res, "palettes.bin")))
        return [[("transparent" if pv.gcolor_rgb(c) is None
                  else "#%02x%02x%02x" % pv.gcolor_rgb(c)) for c in p] for p in pals]

    def maps(self):
        specs = self.man.get("atlas", [])
        default_atlas = specs[0]["name"] if specs else None
        out = []
        for m in self.man.get("map", []):
            rows = [r for r in m["rows"].strip("\n").split("\n") if r.strip()]
            out.append({"name": m["name"], "rows": rows,
                        "start": m.get("start", [1, 1]),
                        "warps": m.get("warps", []),
                        "atlas": m.get("atlas", default_atlas)})
        return out

    def state(self):
        legend = {ch: {"tile": e["tile"], "flags": e.get("flags", [])}
                  for ch, e in self.man.get("legend", {}).items()}
        return {
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
            "engine": pp.framework_state(self.root, self.meta),
            "legend": legend,
            "atlases": self.atlases(),
            "palettes": self.palette_swatches(),
            "maps": self.maps(),
            "fonts": self.fonts(),
            "dialog": {k: v.get("pages", [])
                       for k, v in self.man.get("dialog", {}).items()},
            "scenes": list(self.man.get("scene", {})),
            # The canvas the author is working on, which is the device's display turned
            # if the project is landscape. Sent as dimensions rather than as a flag so
            # the page never has to know which way round that is.
            "orientation": pa.ORIENT_NAMES[self.orientation],
            "screen": [self.SCREEN_W, self.SCREEN_H],
            "budget": self.project.get("budget_bytes", 262144),
            "used": sum(os.path.getsize(os.path.join(self.res, f))
                        for f in os.listdir(self.res) if f.endswith(".bin"))
            if self.built else 0,
            "app": self.app_size(),
            "save": self.save_size(),
        }

    def code_symbols(self):
        """Every `pnx_*` / `PNX_*` name the engine and the generated header declare.

        This exists because of a mistake made writing the project scaffold: it called
        `pnx_platform_exit`, the real name is `pnx_platform_quit`, and nothing said so
        until a full ARM compile failed. On a watch that round trip is slow enough to
        matter, and the check that prevents it is a set membership test.
        """
        names = set()
        roots = [os.path.join(self.root, "src", "c", "pnx")]
        gen = os.path.join(self.root,
                           self.project.get("header", "src/c/assets_gen.h"))
        files = []
        for r in roots:
            if os.path.isdir(r):
                for dp, _dn, fs in os.walk(r, followlinks=True):
                    files += [os.path.join(dp, f) for f in fs if f.endswith((".h", ".c"))]
        if os.path.exists(gen):
            files.append(gen)

        ident = re.compile(r"\b((?:pnx|Pnx|PNX)[A-Za-z0-9_]*)")
        for path in files:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for m in ident.finditer(f.read()):
                        names.add(m.group(1))
            except OSError:
                continue

        # Names the generated header produces that do not carry a pnx prefix -- tile
        # roles, sprite dimensions, scene and dialog ids.
        if os.path.exists(gen):
            with open(gen, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r"^#define\s+([A-Z][A-Z0-9_]*)", f.read(), re.M):
                    names.add(m.group(1))
        return sorted(names)

    # ------------------------------------------------------------------ estimate
    #
    # The budget used to reflect the LAST BUILD, which means a map can be grown past the
    # appstore cap and only say so hours later, after the work is done. That is the worst
    # possible time to find out. So the cost is recomputed as content changes.
    #
    # Maps are computed EXACTLY -- the blob layout is arithmetic on width, height, warps
    # and flag overrides, no packing required. Everything else reuses the size of the blob
    # already on disk, because re-quantising a tileset on every keystroke would cost
    # seconds and change nothing. An asset that has never been built is the only thing
    # estimated, and it is reported as such rather than pretended to be exact.

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

    # ------------------------------------------------------------------ app size
    #
    # The OTHER budget, and the one that fails worse.
    #
    # A project spends against two unrelated ceilings. Resources -- the .bin blobs -- are
    # capped at 262,144 bytes by the appstore. The app BINARY is capped at 65,535, because
    # `virtual_size` in the Pebble app header is a uint16; exceed it and the SDK fails with
    # `struct.error: 'H' format requires 0 <= number <= 65535` and no mention of what
    # overflowed. Showing one number labelled "Budget" invited reading the comfortable one
    # as though it covered both.
    #
    # It can only be MEASURED, never predicted: it comes from the linked ELF, so it needs
    # an SDK build that the editor cannot yet run itself. Unknown is therefore a real
    # answer here, and is reported as one rather than as a zero.

    # App RAM per platform, read from the SDK's own table (see docs/ROADMAP.md, M9).
    # Everything a Pebble app owns lives in this one slot: code, rodata, statics AND the
    # heap, which is why "how much is left" is the number worth showing rather than the
    # static size alone.
    APP_RAM = {"emery": 131072, "gabbro": 131072, "flint": 65536, "basalt": 65536,
               "chalk": 65536, "diorite": 65536, "aplite": 24576}

    def _elf_path(self):
        """The most recently linked ELF under build/, whichever platform it is for."""
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

    def app_size(self):
        """Static bytes from the last SDK build, against the uint16 ceiling.

        Cached on the ELF's mtime: this shells out to `nm` and `readelf`, and the panel
        that shows it repaints on every keystroke of a map edit.
        """
        elf = self._elf_path()
        if not elf:
            return {"known": False,
                    "why": "no build yet — run a Pebble build to measure the app",
                    "limit": sr.VIRTUAL_SIZE_LIMIT}

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
        platform = os.path.basename(os.path.dirname(elf))
        slot = self.APP_RAM.get(platform)
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

    def save_size(self):
        """Persistent storage. Nothing to measure until M5 builds the save format.

        Present as a cell from the start, because an empty slot with a name is how a
        budget stays visible before it is spendable -- and because persist is the one
        ceiling that cannot be discovered by a build failing. It fails on a watch, on a
        write, in front of a player.
        """
        return {"known": False, "why": "save lands with M5"}

    def estimate(self, overrides=None):
        """Current resource cost, per category, without running the pipeline.

        `overrides` lets the editor price a map it has not saved yet -- {name: rows} --
        so the number moves while a map is being painted rather than after.
        """
        overrides = overrides or {}
        budget = int(self.project.get("budget_bytes", 262144))
        entries, exact = [], True

        # Built blobs, by the manifest key that produced them.
        def blob_size(out):
            p = os.path.join(self.res, out) if out else None
            return os.path.getsize(p) if p and os.path.exists(p) else None

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
                "app": self.app_size(), "save": self.save_size()}

    # ---------------------------------------------------------------------- code
    #
    # STUB. Enough to read and edit the project's C without leaving the editor, sized so
    # that the tab exists and has the right shape rather than pretending to be an IDE.
    # When M8's Alloy scripting lands, `.js` joins the same tree and the same read/write
    # endpoints serve it -- which is why this is a file tree over the project rather than
    # a C-specific thing.

    # The staged engine is deliberately readable: looking up what pnx_text_draw takes
    # should not mean going and finding the framework. It is NOT writable, because it is
    # overwritten from the editor's own copy before every build, so an edit here would
    # vanish at the worst possible moment.
    CODE_EXTS = (".c", ".h", ".js", ".json", ".toml")

    def _safe(self, rel):
        """Resolve a project-relative path, refusing anything outside the project.

        The editor serves on localhost, but "only local" is not the same as "only this
        project" -- a stray `../../../..` in a request should not be able to read or
        write the user's home directory.

        Checked LEXICALLY, with abspath rather than realpath. abspath collapses `..`, so
        traversal is still blocked, but it does not resolve symlinks -- and it must not,
        because this repository's own examples reach the engine through a symlinked
        `src/c/pnx`. Resolving would put those files outside the project and make the
        engine unreadable in exactly the tree where reading it is most useful.
        """
        full = os.path.abspath(os.path.join(self.root, rel))
        root = os.path.abspath(self.root)
        if full != root and not full.startswith(root + os.sep):
            raise ValueError(f"{rel!r} is outside the project")
        return full

    def code_tree(self):
        """Every source file in the project, engine last and marked read-only."""
        out = []
        for base in ("src", "."):
            start = os.path.join(self.root, base)
            if not os.path.isdir(start):
                continue
            # followlinks, because the engine reaches a project either as a staged copy
            # or -- in this repository -- as a symlink, and both should list.
            for dirpath, dirnames, files in os.walk(start, followlinks=True):
                dirnames[:] = [d for d in dirnames
                               if d not in ("build", ".git", "__pycache__", "resources",
                                            "art", "node_modules")]
                if base == ".":
                    dirnames[:] = []          # top level only, for assets.toml et al
                for fn in sorted(files):
                    if not fn.endswith(self.CODE_EXTS):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, self.root)
                    if rel in [e["path"] for e in out]:
                        continue
                    engine = rel.replace("\\", "/").startswith("src/c/pnx/")
                    out.append({
                        "path": rel, "name": fn,
                        "dir": os.path.dirname(rel) or ".",
                        "bytes": os.path.getsize(full),
                        "engine": engine,
                        "editable": not engine,
                        "generated": fn == "assets_gen.h",
                    })
        out.sort(key=lambda e: (e["engine"], e["dir"], e["name"]))
        return out

    # -------------------------------------------------------------------- linting
    #
    # A real compiler, not a heuristic. The in-page analysis catches unbalanced brackets
    # and unknown `pnx_*` names, which is genuinely useful while typing -- but it cannot
    # know that a call has the wrong argument count, and it will never say "did you mean
    # pnx_platform_quit". A host `cc -fsyntax-only` says both, in a tenth of a second,
    # and it is the same seam the framework's own host tests rely on: nothing above the
    # platform layer includes <pebble.h>, so game code compiles on a laptop.
    #
    # The alternative is finding out from a full ARM build, which needs the SDK and takes
    # long enough that people stop running it.

    LINT_TIMEOUT = 8

    @staticmethod
    def _compiler():
        for name in ("cc", "clang", "gcc"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def code_lint(self, rel):
        """Syntax-check one file against the engine, and return diagnostics."""
        try:
            path = self._safe(rel)
        except ValueError as e:
            return {"ok": False, "why": str(e)}
        if not path.endswith((".c", ".h")):
            return {"ok": False, "why": "only C sources are checked"}

        cc = self._compiler()
        if not cc:
            return {"ok": False,
                    "why": "no C compiler on PATH -- install one to get real diagnostics"}

        cmd = [cc, "-fsyntax-only", "-std=c11", "-Wall", "-Wextra",
               "-Wno-unused-parameter",
               # The host half of the platform seam. Without it the SDK headers would be
               # wanted, and they are not here.
               "-DPNX_PLATFORM_HOST",
               # The generated header only defines this when it can include the SDK's
               # resource ids, which do not exist on a host. A stub keeps the file
               # compiling so the diagnostics are about the code someone wrote.
               "-DPNX_ASSET_RESOURCE_TABLE={0}",
               "-I", os.path.join(self.root, "src", "c"),
               "-I", self.root]
        if path.endswith(".h"):
            cmd += ["-x", "c"]                   # a header is not a translation unit
        cmd.append(path)

        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.LINT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "why": f"the compiler did not finish in "
                                        f"{self.LINT_TIMEOUT}s"}
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "why": f"could not run {os.path.basename(cc)}: {e}"}

        # `file:line:col: level: message`, and only for THIS file: an error inside an
        # engine header is a real diagnostic but it is not something the author can act
        # on here, so it is reported without a line to jump to.
        out = []
        pattern = re.compile(r"^(.*?):(\d+):(?:(\d+):)?\s*(error|warning|note):\s*(.*)$")
        for line in (r.stderr or "").splitlines():
            m = pattern.match(line)
            if not m:
                continue
            where, ln, col, level, msg = m.groups()
            if level == "note":
                # Notes belong to the diagnostic above them -- "did you mean X" is the
                # useful half of an implicit-declaration error.
                if out:
                    out[-1]["note"] = msg
                continue
            try:
                here = os.path.samefile(where, path)
            except OSError:
                here = False                     # a path the compiler invented, or gone
            out.append({"line": int(ln) if here else 0,
                        "col": int(col or 0), "level": level, "msg": msg,
                        "file": os.path.relpath(where, self.root)
                        if os.path.exists(where) else where})

        return {"ok": True, "compiler": os.path.basename(cc), "diags": out,
                "clean": not out}

    def _engine_owned(self):
        try:
            return bool(pp.load(self.root).get("engine_owned"))
        except Exception:                                # noqa: BLE001
            return False

    def code_read(self, rel):
        full = self._safe(rel)
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
        engine = rel.replace("\\", "/").startswith("src/c/pnx/")
        owned = engine and self._engine_owned()

        if engine and owned:
            note = ("This project owns its engine copy — edits are kept, and it no "
                    "longer tracks the editor.")
        elif engine:
            note = ("The engine is restaged from the editor before every build, so edits "
                    "here would be overwritten. Unlock it in Settings to take ownership.")
        elif rel.endswith("assets_gen.h"):
            note = "Generated from assets.toml — edits are overwritten by the next build."
        else:
            note = ""

        return {"path": rel, "text": text, "editable": (not engine) or owned,
                "engine": engine, "note": note}

    def code_write(self, rel, text):
        if rel.replace("\\", "/").startswith("src/c/pnx/") and not self._engine_owned():
            raise ValueError("the staged engine is restaged on every build, so edits "
                             "would be lost. Take ownership of it in Settings first.")
        full = self._safe(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return {"ok": True, "bytes": len(text.encode("utf-8"))}

    # -------------------------------------------------------------------- sprites
    #
    # STUB, but a deliberately truthful one: the canvas paints in ARGB2222, the device's
    # actual 64 colours, so what is drawn is what ships. Painting in full RGB and
    # quantising on import is how you discover at build time that two colours you chose
    # to contrast collapsed into one.

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

    def sprite_read(self, rel):
        """Load a PNG as ARGB2222 indices, so it can be edited in device colours."""
        from PIL import Image
        full = self._safe(rel)
        im = Image.open(full).convert("RGBA")
        px = im.load()
        return {"w": im.width, "h": im.height,
                "pixels": [pa.to_gcolor8(px[x, y])
                           for y in range(im.height) for x in range(im.width)]}

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

    # --------------------------------------------------------------------- fonts
    #
    # A font is the one asset that can pass every check the pipeline makes and still be
    # unusable, because "legible at 12px" is not a property bytes have. That is the whole
    # reason this tab exists: the pipeline can say a font packs, only a person looking at
    # it can say it reads. So the job here is to put the real glyphs, at real size, over
    # the real background, before anything is committed to the manifest.

    FONT_DIRS = [
        # Linux
        "/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts",
        # macOS
        "/System/Library/Fonts", "/Library/Fonts", "~/Library/Fonts",
        # Windows
        "C:/Windows/Fonts",
    ]

    def font_sources(self):
        """Typefaces the editor can offer: the project's own first, then the system's.

        System fonts are listed because the alternative is a file dialog, and the barrier
        this editor exists to remove is exactly that kind of detour. Picking one COPIES it
        into the project rather than referencing it in place -- a manifest pointing at
        /usr/share/fonts builds on this machine and nowhere else.
        """
        out, seen = [], set()

        def add(path, where):
            full = os.path.realpath(path)
            if full in seen or not os.path.isfile(full):
                return
            seen.add(full)
            out.append({"path": full, "name": os.path.basename(full), "where": where,
                        "in_project": where == "project",
                        "rel": os.path.relpath(full, self.root)
                        if where == "project" else None})

        for dirpath, dirnames, files in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("build", ".git", "resources", "__pycache__")]
            for fn in files:
                if fn.lower().endswith((".ttf", ".otf")):
                    add(os.path.join(dirpath, fn), "project")

        for base in self.FONT_DIRS:
            base = os.path.expanduser(base)
            if not os.path.isdir(base):
                continue
            for dirpath, _dirnames, files in os.walk(base):
                for fn in sorted(files):
                    if fn.lower().endswith((".ttf", ".otf")):
                        add(os.path.join(dirpath, fn), "system")
                if len(out) > 400:
                    break

        out.sort(key=lambda f: (not f["in_project"], f["name"].lower()))
        return out

    def fonts(self):
        """Fonts already declared, with their built metrics where a build exists."""
        out = []
        for spec in self.man.get("font", []):
            entry = {"name": spec.get("name"), "source": spec.get("source"),
                     "size": spec.get("size", 12), "depth": spec.get("depth", 1),
                     "threshold": spec.get("threshold"),
                     "tracking": spec.get("tracking", 0),
                     "charset": spec.get("charset", "auto"),
                     "extra": spec.get("extra", ""),
                     "license": spec.get("license", ""), "bytes": None}
            blob = os.path.join(self.res, spec.get("out", f"font_{entry['name']}.bin"))
            if os.path.exists(blob):
                entry["bytes"] = os.path.getsize(blob)
            out.append(entry)
        return out

    def _rasterise(self, spec):
        """Pack a candidate font and parse it straight back.

        Deliberately round-trips through the real blob rather than rendering from the
        rasteriser's intermediate output: previewing from anything other than the shipped
        bytes is how a preview and a build come to disagree.
        """
        full = dict(spec)
        full.setdefault("name", "preview")
        # A licence is required to BUILD, not to look. Gating the preview on it would
        # mean choosing a typeface before you can see whether it is worth licensing.
        full.setdefault("license", "(unset -- required before this can be added)")

        with contextlib.redirect_stdout(io.StringIO()):
            packed = pa.pack_font(self.root, full, self.man)
        return packed, pv.parse_font(packed["blob"])

    def font_preview(self, spec):
        """Rasterise at the current settings and report what it costs and looks like."""
        packed, font = self._rasterise(spec)

        budget = self.project.get("budget_bytes", 262144)
        size = len(packed["blob"])
        blank = sum(1 for g in font["glyphs"] if not g["w"])

        return {
            "ok": True,
            "sheet": pv.data_uri(pv.font_sheet(font, scale=4)),
            "chars": "".join(packed["chars"]),
            "glyph_count": font["glyph_count"],
            "line_height": font["line_height"],
            "baseline": font["baseline"],
            "depth": font["depth"],
            "bitmap_bytes": font["bitmap_bytes"],
            "bytes": size,
            "pct": 100.0 * size / budget,
            # A glyph with no ink that is not a space means the threshold ate it, which
            # is the single most common way an imported font is quietly broken.
            "blank_glyphs": blank,
            "glyphs": [
                {"ch": packed["chars"][i], "w": g["w"], "h": g["h"],
                 "advance": g["advance"], "bx": g["bearing_x"], "by": g["bearing_y"]}
                for i, g in enumerate(font["glyphs"])
            ],
            "origin": {ch: packed["origin"].get(ch, "") for ch in packed["chars"]},
        }

    # ------------------------------------------------------- the preview canvas
    #
    # emery's display is 200x228 and never rotates. A landscape project is one whose
    # content is baked turned, so the author is looking at a 228x200 canvas -- and a
    # preview that showed them the portrait one would be previewing the wrong thing.

    DISPLAY_W, DISPLAY_H = 200, 228

    @property
    def orientation(self):
        return pa.parse_orientation(self.project.get("orientation"), "[project]")

    @property
    def landscape(self):
        return self.orientation != pa.ORIENT_BUTTONS_RIGHT

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

    def _roles(self):
        """Role -> tile index per atlas, read from the generated header.

        Same source the map canvas uses, so a preview background is drawn with the tiles
        the build would actually place.
        """
        header_path = os.path.join(self.root,
                                   self.project.get("header", "src/c/assets_gen.h"))
        text = open(header_path).read() if os.path.exists(header_path) else ""
        out = {}
        for spec in self.man.get("atlas", []):
            prefix = re.sub(r"[^A-Za-z0-9]", "_", spec["name"]).upper()
            roles = {m.group(1).lower(): int(m.group(2)) for m in
                     re.finditer(rf"#define {prefix}_TILE_(\w+) (\d+)", text)}
            for k in ("px", "bytes", "count"):
                roles.pop(k, None)
            out[spec["name"]] = roles
        return out

    def _map_background(self, map_name, ox, oy, clear=0xC0):
        """A screen-sized crop of a real map, drawn with real tiles.

        `clear` shows through index 0, matching the device: the tilemap skips transparent
        pixels rather than writing them, so what appears behind a tile is whatever the
        frame was cleared to.
        """
        from PIL import Image

        palettes = pv.parse_palettes(pv.read(os.path.join(self.res, "palettes.bin")))
        m = next((m for m in self.maps() if m["name"] == map_name), None)
        if not m:
            return None

        spec = next((a for a in self.man.get("atlas", []) if a["name"] == m["atlas"]),
                    None)
        if not spec:
            return None
        blob = os.path.join(self.res, spec["out"])
        if not os.path.exists(blob):
            return None

        atlas = pv.parse_atlas(pv.read(blob))
        roles = self._roles().get(m["atlas"], {})
        legend = self.man.get("legend", {})
        T = atlas["tile_px"]

        img = Image.new("RGBA", (self.SCREEN_W, self.SCREEN_H),
                        (pv.gcolor_rgb(clear) or (0, 0, 0)) + (255,))
        first_tx, first_ty = ox // T, oy // T
        for j in range(self.SCREEN_H // T + 2):
            for i in range(self.SCREEN_W // T + 2):
                tx, ty = first_tx + i, first_ty + j
                if not (0 <= ty < len(m["rows"]) and 0 <= tx < len(m["rows"][ty])):
                    continue
                ch = m["rows"][ty][tx]
                role = legend.get(ch, {}).get("tile")
                idx = roles.get(role)
                if idx is None or idx >= atlas["count"]:
                    continue
                tile = self._upright(pv.tile_image(atlas, palettes, idx))
                # Masked, so index 0 leaves the clear colour rather than punching a hole.
                img.paste(tile, (tx * T - ox, ty * T - oy), tile)
        return img

    def font_scene(self, opts):
        """The composited preview: real background, real box, real glyphs, real size.

        The background is a choice because text fails differently depending on what is
        behind it -- a HUD sits over gameplay and has to survive whatever tile scrolls
        under it, while dialogue sits on a flat panel and only has to be comfortable.
        A preview that only ever showed one of those would pass a font that fails the
        other.
        """
        from PIL import Image

        font = None
        if opts.get("spec"):
            _packed, font = self._rasterise(opts["spec"])
        elif opts.get("font"):
            spec = next((f for f in self.man.get("font", [])
                         if f.get("name") == opts["font"]), None)
            if spec:
                blob = os.path.join(self.res,
                                    spec.get("out", f"font_{spec['name']}.bin"))
                if os.path.exists(blob):
                    font = pv.parse_font(pv.read(blob))
        if not font:
            raise ValueError("no font to preview -- pick a source, or build first")

        bg = opts.get("background", "solid")
        img = None
        if bg == "map" and self.built and opts.get("map"):
            img = self._map_background(opts["map"], int(opts.get("scroll_x", 0)),
                                       int(opts.get("scroll_y", 0)),
                                       int(opts.get("bg_colour", 0xC0)))
        if img is None:
            rgb = pv.gcolor_rgb(int(opts.get("bg_colour", 0xC0))) or (0, 0, 0)
            img = Image.new("RGBA", (self.SCREEN_W, self.SCREEN_H), rgb + (255,))

        text = opts.get("text") or ""
        pad = int(opts.get("pad", 6))
        box = opts.get("box") or {}

        if box.get("on"):
            bx, by = int(box.get("x", 8)), int(box.get("y", 140))
            bw, bh = int(box.get("w", 184)), int(box.get("h", 72))
            fill = pv.gcolor_rgb(int(box.get("colour", 0xC0))) or (0, 0, 0)
            panel = Image.new("RGBA", (bw, bh), fill + (255,))
            img.paste(panel, (bx, by))
            if box.get("border"):
                edge = pv.gcolor_rgb(int(box.get("border_colour", 0xFF))) or (255,) * 3
                px = img.load()
                for i in range(bw):
                    for yy in (by, by + bh - 1):
                        if 0 <= bx + i < img.width and 0 <= yy < img.height:
                            px[bx + i, yy] = edge + (255,)
                for j in range(bh):
                    for xx in (bx, bx + bw - 1):
                        if 0 <= xx < img.width and 0 <= by + j < img.height:
                            px[xx, by + j] = edge + (255,)
            tx, ty, tw = bx + pad, by + pad, bw - pad * 2
        else:
            tx, ty = int(opts.get("x", 4)), int(opts.get("y", 4))
            tw = self.SCREEN_W - tx * 2

        ink = pv.gcolor_rgb(int(opts.get("ink", 0xFF))) or (255, 255, 255)
        lines = pv.font_draw_wrapped(img, font, text, tx, ty + font["baseline"], tw,
                                     ink, opts.get("align", "left"))

        scale = max(1, min(4, int(opts.get("scale", 2))))
        shown = img.resize((img.width * scale, img.height * scale), Image.NEAREST) \
            if scale > 1 else img

        return {"ok": True, "image": pv.data_uri(shown),
                "scale": scale, "lines": lines,
                "line_height": font["line_height"], "baseline": font["baseline"],
                "text_height": lines * font["line_height"],
                "overflow": bool(box.get("on")) and
                (lines * font["line_height"] > int(box.get("h", 72)) - pad * 2)}

    def add_font(self, spec):
        """Append a [[font]] block, copying a system typeface in if that is what it is."""
        name = spec.get("name", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")
        if any(f.get("name") == name for f in self.man.get("font", [])):
            raise ValueError(f"a font named {name!r} already exists")
        if not spec.get("license"):
            raise ValueError("a licence is required: rasterising a typeface into the "
                             "bundle redistributes it")

        source = spec["source"]
        if os.path.isabs(source):
            dest_dir = os.path.join(self.root, "art", "fonts")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(source))
            if not os.path.exists(dest):
                shutil.copy2(source, dest)
            source = os.path.relpath(dest, self.root)

        depth = int(spec.get("depth", 1))
        lines = [
            "", "", "[[font]]",
            f'name = "{name}"',
            f'source = "{source}"',
            f"size = {int(spec.get('size', 12))}",
            f"depth = {depth}"
            + ("   # crisp" if depth == 1 else "   # antialiased"),
            f"threshold = {int(spec.get('threshold', 128 if depth == 1 else 24))}",
        ]
        if int(spec.get("tracking", 0)):
            lines.append(f"tracking = {int(spec['tracking'])}")
        lines.append(f'charset = "{spec.get("charset", "auto")}"')
        if spec.get("extra"):
            lines.append(f'extra = "{spec["extra"]}"')
        lines += [
            f'license = "{spec["license"]}"',
            f'out = "font_{name}.bin"',
            "",
        ]

        with open(self.path, "a") as f:
            f.write("\n".join(lines))
        self.reload()

    # ----------------------------------------------------------------- importing

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

    def slice_grid(self, rel, tile, region, exclude=(), colorkey=None):
        """Every cell of a slice, rendered, so the author can pick what gets packed.

        The strip of *kept* tiles told you what the pipeline decided. It did not let you
        disagree with it -- and the decision that matters most for the budget is which
        tiles are worth keeping at all, which is a judgement about the art rather than
        arithmetic. So this returns the grid as sliced, marked up with what each cell
        would become, and the caller can drop any of it.
        """
        from PIL import Image
        path = self._safe(rel)
        im = Image.open(path).convert("RGBA")
        px = im.load()
        W, H = im.size
        rx, ry, rw, rh = region
        rw = max(1, min(rw, W // tile - rx))
        rh = max(1, min(rh, H // tile - ry))

        # A whole large sheet is thousands of cells and as many PNGs. Capped, with the
        # cap reported, rather than quietly hanging the browser.
        LIMIT = 1024
        capped = rw * rh > LIMIT

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
                buf = tuple(pa.to_gcolor8(px[tx * tile + a, ty * tile + b], colorkey)
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
                              "img": pv.data_uri(img.resize((tile * 2, tile * 2),
                                                            Image.NEAREST))})
        return {"cols": rw, "rows": rh, "cells": cells, "capped": capped,
                "limit": LIMIT, "sheet_tiles": [W // tile, H // tile]}

    def analyse(self, rel, tile, region, max_tiles, colorkey, exclude=()):
        """Price a candidate carve before it is committed.

        This is the number that decides a project's content budget, and it is invisible
        until something is built -- so the editor computes it live. Region selection is
        where the budget is won: five complete tilesets are 111% of the appstore limit,
        while 128 tiles from each is 32%.
        """
        from PIL import Image
        path = os.path.join(self.root, rel)
        im = Image.open(path).convert("RGBA")
        px = im.load()
        W, H = im.size
        rx, ry, rw, rh = region

        rw = max(1, min(rw, W // tile - rx))
        rh = max(1, min(rh, H // tile - ry))

        excluded = {int(e) for e in exclude}
        unique, seen, empty, repaired = [], set(), 0, 0
        for ty in range(ry, ry + rh):
            for tx in range(rx, rx + rw):
                # Same order and the same exclusion rule pack_atlas uses, so the price
                # quoted here is the price the build charges.
                if ((ty - ry) * rw + (tx - rx)) in excluded:
                    continue
                buf = tuple(pa.to_gcolor8(px[tx * tile + i, ty * tile + j], colorkey)
                            for j in range(tile) for i in range(tile))
                if not any(buf):
                    empty += 1
                    continue
                if buf in seen or len(unique) >= max_tiles:
                    continue
                seen.add(buf)
                fixed, merged = pa.reduce_colours(buf)
                if merged:
                    repaired += 1
                unique.append(fixed)

        sets = [frozenset(c for c in t if c) for t in unique]
        pals = pa.merge_palettes(sets)[0] if sets else []
        colours = set().union(*sets) if sets else set()

        tile_bytes = tile * tile // 2
        est = len(unique) * tile_bytes + len(pals) * 16 + pa.HEADER_BYTES

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
            # An atlas is read whole into the scene arena and stays there for the
            # scene's life, so its blob size is also its resident cost.
            "heap_before": heap,
            "heap_after": (heap - est) if heap is not None else None,
            "strip": strip,
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

    def add_atlas(self, name, rel, tile, region, max_tiles, exclude=(),
                  colorkey=None):
        """Append an [[atlas]] block. Appending keeps every existing comment intact."""
        if any(a["name"] == name for a in self.man.get("atlas", [])):
            raise ValueError(f"an atlas named {name!r} already exists")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")

        block = f'''

[[atlas]]
name = "{name}"
sheet = "{rel}"
tile = {tile}
# In TILE units, not pixels.
region = [{region[0]}, {region[1]}, {region[2]}, {region[3]}]
max_tiles = {max_tiles}
out = "{name}.bin"
{self._colorkey_block(colorkey)}{self._exclude_block(exclude)}
metatiles = "auto"
# Roles the legend can name. Autopicked so the atlas is usable the moment it is
# imported; replace with an explicit [atlas.semantic] table once you know which tiles
# you want, and no map or game code changes.
autopick = ["floor", "wall", "accent"]
'''
        with open(self.path, "a") as f:
            f.write(block)
        self.reload()

    def legend_chars(self):
        """Pick sensible default characters for a blank map's floor and walls.

        Derived from the legend's flags rather than assuming '.' and '#', so a project
        with its own legend gets a blank map it can actually paint on.
        """
        floor = wall = None
        for ch, e in self.man.get("legend", {}).items():
            flags = e.get("flags", [])
            if "solid" in flags and wall is None:
                wall = ch
            elif not flags and floor is None:
                floor = ch
        return floor, wall

    def add_map(self, name, w, h, atlas, with_scene=True):
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

        floor, wall = self.legend_chars()
        if not floor or not wall:
            raise ValueError("the legend needs a walkable tile and a solid tile")

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
            block += f'''
[scene.{name}]
map = "{name}"
atlases = ["{atlas}"]
'''
        with open(self.path, "a") as f:
            f.write(block)
        self.reload()

    # ------------------------------------------------------------------- saving

    def save_map(self, name, rows, start, warps, atlas=None):
        """Rewrite one map's rows/start/warps in place, touching nothing else.

        Located by scanning `[[map]]` blocks for the matching name rather than by
        rewriting the parsed document, so every comment in the file survives -- which
        matters more here than elsewhere, because manifests carry the reasoning behind
        the content.
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

            if atlas:
                if re.search(r"^atlas\s*=", chunk, re.M):
                    chunk = re.sub(r'^atlas\s*=.*$', f'atlas = "{atlas}"', chunk,
                                   flags=re.M)
                else:
                    chunk = re.sub(r'(^name\s*=.*$)', rf'\1\natlas = "{atlas}"',
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

# ------------------------------------------------------------------------- server

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>pebblnyx editor</title><style>
:root{--ink:#eceff4;--surface:#fff;--line:#d3dae4;--fg:#10141b;--dim:#5c6878;
  --accent:#1f6dbf;--soft:#e3edf9;--ok:#2c7a4b;--bad:#b4351c}
@media(prefers-color-scheme:dark){:root{--ink:#0d1017;--surface:#161a23;--line:#262c38;
  --fg:#dde3ec;--dim:#7b8798;--accent:#55aaff;--soft:#16283d;--ok:#5fd28d;--bad:#ff7a5c}}
*{box-sizing:border-box}
/* Shell: rail | work, with the work column stacking toolbar / content / output /
   status. Grid rather than nested flex so the output panel can be collapsed by changing
   one row rather than by chasing flex-basis through three ancestors. */
body{margin:0;height:100vh;display:grid;grid-template-columns:auto 1fr;overflow:hidden;
  background:var(--ink);
  color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
/* Rows: toolbar, budget strip, the work itself, output, status. The 1fr has to name the
   work row explicitly -- adding a child without updating this handed the spare height to
   whatever landed in the 1fr slot. */
/* Column flex, NOT a grid with a positional row template.
   The template named the flexible row by position, so it depended on how many children
   happened to exist -- and one of them (the update banner) is display:none most of the
   time, which removes it as a grid item and shifts every row after it. That handed the
   1fr to the output panel and squeezed the editor into a hundred pixels. Flex asks the
   child which one grows, so hiding a sibling cannot move it. */
#work{display:flex;flex-direction:column;min-width:0;min-height:0}
#work>header,#updbanner,#budgetbar,#outpanel,#statusbar{flex:0 0 auto}
#work>main{flex:1 1 auto}
/* The update banner. Accent-coloured rather than warning-coloured: a new version is good
   news, not a problem, and colouring it like a failure trains people to dismiss it. */
#updbanner{display:flex;align-items:center;gap:.6rem;padding:.35rem .9rem;
  background:color-mix(in srgb,var(--accent) 18%,var(--bg));
  border-bottom:1px solid var(--line);font-size:.82rem}
#updbanner button{padding:.15rem .55rem;font-size:.78rem}
#updbannerhide{background:none;border:none;color:var(--dim);cursor:pointer}
#updbody{max-height:16rem;overflow:auto;white-space:pre-wrap;font-size:.78rem;
  background:var(--ink);padding:.6rem .8rem;border-radius:4px;margin:.7rem 0 0}

#rail{display:flex;flex-direction:column;gap:2px;width:62px;padding:.4rem .3rem;
  background:var(--surface);border-right:1px solid var(--line)}
.act{display:flex;flex-direction:column;align-items:center;gap:.15rem;padding:.5rem .2rem;
  border:0;border-radius:6px;background:none;color:var(--dim);cursor:pointer;
  border-left:2px solid transparent}
.act i{font-style:normal;font-size:1.05rem;line-height:1}
.act em{font-style:normal;font-size:.58rem;letter-spacing:.04em}
.act:hover{background:var(--soft);color:var(--fg)}
/* The active activity is marked by a bar as well as a colour, so it survives being
   looked at by someone who cannot separate the two. */
.act.on{color:var(--accent);background:var(--soft);border-left-color:var(--accent)}
.act:disabled{opacity:.35;cursor:not-allowed;background:none}

header{display:flex;align-items:center;gap:.7rem;padding:.45rem .8rem;
  border-bottom:1px solid var(--line);background:var(--surface);min-height:2.4rem}
header b{font:600 .72rem/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim)}
#ctxbar{display:flex;align-items:center;gap:.5rem}

#outpanel{display:grid;grid-template-rows:auto 1fr;border-top:1px solid var(--line);
  background:var(--surface);max-height:34vh}
#outpanel.hidden{grid-template-rows:auto 0}
#outpanel.hidden #log{display:none}
.outbar{display:flex;align-items:center;gap:.6rem;padding:.3rem .8rem}
.outbar b{font:600 .62rem/1 ui-monospace,Menlo,monospace;letter-spacing:.12em;
  text-transform:uppercase;color:var(--dim)}
.outbar span{font-size:.7rem;color:var(--dim)}
.outbar button{padding:.15rem .5rem;font-size:.72rem}

#statusbar{display:flex;align-items:center;gap:1.1rem;padding:0 .8rem;height:24px;
  background:var(--accent);color:#fff;font:11px ui-monospace,Menlo,monospace}
#statusbar span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button{font:inherit;padding:.35rem .8rem;border:1px solid var(--line);border-radius:5px;
  background:var(--surface);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
select{font:inherit;padding:.3rem .5rem;background:var(--surface);color:var(--fg);
  border:1px solid var(--line);border-radius:5px}
main{display:flex;min-height:0;min-width:0;overflow:hidden}
aside{width:270px;flex:none;border-right:1px solid var(--line);background:var(--surface);
  overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:1.25rem}
section h2{margin:0 0 .5rem;font:600 .68rem/1 ui-monospace,Menlo,monospace;
  letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
#stage{flex:1;overflow:auto;padding:1.5rem;display:flex;align-items:flex-start;
  justify-content:center}
canvas{image-rendering:pixelated;cursor:crosshair;border:1px solid var(--line);
  border-radius:3px}
.tiles{display:flex;flex-wrap:wrap;gap:4px}
.tile{border:2px solid transparent;border-radius:4px;padding:1px;cursor:pointer;
  background:none;line-height:0}
.tile img{width:32px;height:32px;image-rendering:pixelated;display:block}
.tile.sel{border-color:var(--accent)}
.tile b{display:block;font:9px ui-monospace,monospace;color:var(--dim);text-align:center}
.pal{display:flex;gap:2px;flex-wrap:wrap;margin-bottom:6px}
.sw{width:18px;height:18px;border-radius:2px;outline:1px solid var(--line)}
.sw.clear{background:repeating-conic-gradient(from 0deg,#8886 0 25%,transparent 0 50%)
  0 0/6px 6px}
.row{display:flex;gap:.4rem;align-items:center}
.meter{height:5px;background:var(--line);border-radius:3px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--accent)}
.meter.unknown i{background:repeating-linear-gradient(90deg,var(--line) 0 6px,
  transparent 6px 12px);width:100%}

/* The global budget strip. Four equal cells so no ceiling looks more important than
   another, and a full-width band so it is impossible to work without it in view. */
#budgetbar{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
  background:var(--line);border-bottom:1px solid var(--line);flex:0 0 auto}
.bcell{background:var(--bg);padding:.45rem .9rem .5rem;display:flex;
  flex-direction:column;gap:.25rem;min-width:0}
.bhead{display:flex;justify-content:space-between;align-items:baseline;gap:.5rem}
.bhead span{font-size:.72rem;letter-spacing:.04em;text-transform:uppercase;
  color:var(--dim)}
.bhead b{font-size:.86rem;font-variant-numeric:tabular-nums}
.bcell small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.72rem}
/* Over budget is not a shade of the same colour: the cell changes ground, because this
   has to be noticeable from across a room and while looking at something else. */
.bcell.over{background:color-mix(in srgb,var(--bad) 22%,var(--bg))}
.bcell.over .bhead b{color:var(--bad);font-weight:700}
.bcell.warnb .bhead b{color:#d08b2c}
.bcell.stale .bhead b{opacity:.55}
small{color:var(--dim);font-size:.78rem}
#log{margin:0;overflow:auto;white-space:pre-wrap;font:11px/1.5 ui-monospace,
  Menlo,monospace;background:var(--ink);padding:.5rem .8rem;color:var(--dim)}
#log.bad{color:var(--bad)}
#log.ok{color:var(--ok)}
kbd{font:11px ui-monospace,monospace;background:var(--soft);color:var(--accent);
  padding:.05em .35em;border-radius:3px}
.mini{display:flex;flex-wrap:wrap;gap:.3rem;align-items:center;margin-top:.5rem}
.mini input{font:inherit;width:4rem;padding:.25rem .4rem;background:var(--surface);
  color:var(--fg);border:1px solid var(--line);border-radius:4px}
.mini input:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.mini button{padding:.25rem .6rem}
.warp{display:flex;align-items:center;gap:.35rem;font:11px ui-monospace,Menlo,monospace;
  color:var(--dim);padding:.2rem 0;border-bottom:1px solid var(--line)}
.warp b{color:var(--fg);font-weight:600}
.warp button{padding:0 .35rem;line-height:1.3;margin-left:auto}
.imp{max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:1rem}
.fields{display:flex;flex-wrap:wrap;gap:.6rem;align-items:flex-end}
.fields label{display:flex;flex-direction:column;gap:.2rem;
  font:600 .66rem/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim)}
.fields input{font:inherit;width:5.5rem;padding:.3rem .45rem;background:var(--surface);
  color:var(--fg);border:1px solid var(--line);border-radius:5px}
.fields input:focus-visible,select:focus-visible{outline:2px solid var(--accent);
  outline-offset:1px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.stats div{background:var(--surface);padding:.6rem .75rem}
.stats b{display:block;font:600 1.15rem/1.1 ui-monospace,Menlo,monospace;
  font-variant-numeric:tabular-nums}
.stats span{font-size:.66rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--dim)}
.stats div.warn b{color:var(--bad)}
.plate{background:var(--surface);border:1px solid var(--line);border-radius:6px;
  padding:.85rem;overflow:auto}
/* Fonts tab. Controls left, device canvas right and sticky, so the preview stays in
   view while a slider is dragged -- the one interaction this tab exists for. */
.fontgrid{display:grid;grid-template-columns:minmax(260px,320px) 1fr;gap:1.25rem;
  align-items:start}
.fontctl section{margin-bottom:1rem}
.fontview{display:grid;gap:1rem}
.fields.col{display:grid;grid-template-columns:1fr;gap:.5rem}
.fields.col label{display:grid;grid-template-columns:5.5rem 1fr;align-items:center;
  gap:.5rem;font-size:.78rem;color:var(--dim)}
.fields.col label.check{grid-template-columns:auto 1fr;justify-content:start}
.fields.col input,.fields.col select,.fields.col textarea{width:100%}
.fields.col input[type=range]{padding:0}
.fields.col input[type=checkbox]{width:auto}
.pair{display:flex;gap:.3rem}
.pair input{width:100%}
/* A colour chip beside the select rather than a coloured <option>: Chrome paints the
   selected option's background into the closed control, which renders white ink on a
   white swatch as an empty box. The chip shows the colour, the select stays legible. */
.swrow{display:flex;align-items:center;gap:.35rem;min-width:0}
.swrow i{width:1rem;height:1rem;flex:none;border:1px solid var(--line);border-radius:3px;
  background:#000}
.swrow select{flex:1;min-width:0}
select.sw{font:11px ui-monospace,monospace}
#ftext,#fdlg{width:100%;font:inherit;padding:.4rem .5rem;background:var(--surface);
  color:var(--fg);border:1px solid var(--line);border-radius:5px;resize:vertical}
#fdlg{margin-bottom:.4rem}
.plate.wide img{display:block;max-width:100%;image-rendering:pixelated}
/* Checkerboard behind the device canvas, so a preview whose background is genuinely
   transparent is distinguishable from one that is white. */
#fscenewrap{display:inline-block;padding:6px;border-radius:4px;
  background:repeating-conic-gradient(var(--soft) 0% 25%,transparent 0% 50%) 50%/12px 12px}
#fscene{display:block;image-rendering:pixelated}
#fsheet{background:#0d1017;padding:6px;border-radius:4px}
#fchars{display:block;margin-top:.4rem;word-break:break-all;
  font:11px ui-monospace,monospace}
#fdeclared{display:grid;gap:.35rem;font:11px ui-monospace,Menlo,monospace}
#fdeclared div{display:flex;justify-content:space-between;gap:.5rem;
  padding:.3rem .45rem;background:var(--soft);border-radius:4px}
/* The slice grid. Cells carry their own state colour so what a tile will BECOME is
   visible without reading a legend: kept, a duplicate that costs nothing, empty, or
   dropped by hand. */
/* The crop box. Positioned in PERCENTAGES of the sheet image, so it stays correct
   whatever size the thumbnail is rendered at -- the region is in tile units and the
   sheet's size in tiles is known, which makes the arithmetic scale-free. */
#sheetwrap{position:relative;display:inline-block;line-height:0}
#sheetwrap img{display:block;image-rendering:pixelated;max-width:100%}
#cropbox{position:absolute;border:2px solid var(--accent);
  box-shadow:0 0 0 9999px rgba(0,0,0,.55);pointer-events:none;display:none}
.zoom{display:flex;align-items:center;gap:.4rem;font-size:.78rem;color:var(--dim)}
.zoom input{width:7rem}
/* `.fields label` stacks its children in a column and outspecifies a bare `.keypick`,
   so this row of controls needs the same specificity to stay a row. */
.fields label.keypick{flex-direction:row;align-items:center;gap:.4rem}
/* Checkerboarded when there is no key, because "no colour" and "black" must not look the
   same -- black is a perfectly ordinary key to choose. */
#keyswatch{width:1.1rem;height:1.1rem;border-radius:3px;border:1px solid var(--line);
  display:inline-block}
#keyswatch.none{background:repeating-conic-gradient(var(--line) 0 25%,transparent 0 50%)
  50%/8px 8px}
#keyval{font:11px ui-monospace,Menlo,monospace;color:var(--dim);min-width:6.5rem}
#sheetwrap.picking{cursor:crosshair}
#sheetwrap.picking img{outline:2px dashed var(--accent);outline-offset:2px}
#slice{display:grid;gap:2px;overflow:auto;max-height:60vh}
#slice button{padding:0;border:2px solid transparent;border-radius:3px;background:none;
  line-height:0;cursor:pointer;position:relative}
#slice img{display:block;image-rendering:pixelated;width:100%;height:auto}
#slice .unique{border-color:var(--ok)}
#slice .dup{border-color:var(--line);opacity:.75}
#slice .empty{border-color:transparent;opacity:.25}
#slice .off{border-color:var(--bad);opacity:.3;filter:grayscale(1)}
#slice button:hover{border-color:var(--accent)}
#slnote{font-size:.72rem;color:var(--dim)}

/* Sprites. The palette is the device's 64 colours laid out as an 8x8 block, so picking
   one is a glance rather than a scroll through hex codes. */
.pal{display:grid;grid-template-columns:repeat(8,1fr);gap:2px}
.pal i{display:block;aspect-ratio:1;border-radius:2px;cursor:pointer;
  border:2px solid transparent}
.pal i.on{border-color:var(--fg)}
.pal i.tr{background-image:repeating-conic-gradient(#888 0% 25%,#ccc 0% 50%);
  background-size:8px 8px}
#pxwrap,#px1wrap{display:inline-block;padding:6px;border-radius:4px;
  background:repeating-conic-gradient(var(--soft) 0% 25%,transparent 0% 50%) 50%/12px 12px}
#pxcv,#pxcv1{display:block;image-rendering:pixelated;cursor:crosshair}
#pxcv1{cursor:default}

/* Code. A full-height two-pane layout rather than a card, because an editor that only
   fills part of the window is an editor nobody writes in. */
.codegrid{display:grid;grid-template-columns:minmax(200px,270px) 1fr;height:100%}
.codetree{border-right:1px solid var(--line);overflow:auto;padding:.6rem;
  background:var(--surface)}
#codelist{display:grid;gap:1px}
#codelist button{text-align:left;font:11px ui-monospace,Menlo,monospace;
  padding:.28rem .45rem;width:100%;border-color:transparent;background:none}
#codelist button:hover{background:var(--soft)}
#codelist button.on{background:var(--soft);color:var(--accent)}
#codelist button.ro{color:var(--dim)}
.cdir{font:600 11px ui-monospace,Menlo,monospace;color:var(--fg);cursor:pointer;
  padding:.18rem .4rem;user-select:none;white-space:nowrap}
.cdir:hover{background:var(--soft)}
.cdir.ro{color:var(--dim);font-weight:500}
.cdir small{opacity:.6;font-weight:400}
#codelist .grp{font:600 .62rem/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim);padding:.6rem .45rem .25rem}
.codemain{display:flex;flex-direction:column;min-width:0}
.codebar{display:flex;align-items:center;gap:.6rem;padding:.5rem .8rem;
  border-bottom:1px solid var(--line);background:var(--surface)}
.codebar b{font:12px ui-monospace,Menlo,monospace}
.codebar span{font-size:.72rem;color:var(--dim)}
.codeedit{flex:1;min-height:0;position:relative}
/* The overlay only lines up if BOTH layers share every metric that affects wrapping and
   glyph position: font, size, line-height, padding, tab-size, white-space. Any drift
   here shows up as highlighting sliding away from the text further down the file. */
#codescroll{position:absolute;inset:0;overflow:auto}
#codehl,#codetext{margin:0;border:0;padding:.8rem 1rem;tab-size:2;
  font:12px/1.55 ui-monospace,Menlo,Consolas,monospace;
  white-space:pre;word-wrap:normal;overflow-wrap:normal}
#codehl{position:absolute;inset:0;pointer-events:none;color:var(--fg);
  background:var(--ink);min-height:100%;min-width:100%}
#codehl code{font:inherit}
#codetext{position:relative;display:block;width:100%;min-height:100%;outline:0;
  resize:none;background:transparent;color:transparent;caret-color:var(--fg);
  overflow:hidden}
#codetext::selection{background:rgba(85,170,255,.32);color:transparent}
#codetext[readonly]{caret-color:transparent}
#codehl[data-ro="1"]{color:var(--dim)}
.tk-c{color:#6b7a8d;font-style:italic}   /* comment */
.tk-s{color:#7fc98a}                     /* string / char */
.tk-p{color:#c084d8}                     /* preprocessor */
.tk-k{color:#55aaff}                     /* keyword */
.tk-t{color:#5ecfd0}                     /* type */
.tk-n{color:#d8a657}                     /* number */
.tk-x{color:#e0af68}                     /* a pnx / generated symbol */
.tk-bad{color:var(--bad);text-decoration:underline wavy var(--bad)}
@media(prefers-color-scheme:light){
  .tk-c{color:#7a8899} .tk-s{color:#2f7d43} .tk-p{color:#8b3fa8}
  .tk-k{color:#1f6dbf} .tk-t{color:#0f7b7d} .tk-n{color:#a35d00} .tk-x{color:#8a5a00}
}
#codediag{max-height:26%;overflow:auto;border-top:1px solid var(--line);
  background:var(--surface);font:11px/1.5 ui-monospace,Menlo,monospace}
#codediag:empty{display:none}
#codediag div{display:flex;gap:.6rem;padding:.28rem .8rem;cursor:pointer}
#codediag div:hover{background:var(--soft)}
#codediag b{color:var(--bad);font-weight:600}
#codediag b.cc{color:var(--bad)}
#codediag b.warn{color:#d08b2c}
#codediag b.edit{color:var(--dim)}
#codediag .note span{color:var(--dim)}
#codediag i{color:var(--dim);font-style:normal;min-width:3.5rem}

/* Toolchain tab. Prose gets a readable measure rather than the full window width -- it
   is the one place in this editor a person has to actually read something. */
.sdkwrap{display:grid;gap:1rem;max-width:52rem}
.prose{margin:0 0 .7rem;font-size:.85rem;line-height:1.55;color:var(--dim);max-width:60ch}
.prose b{color:var(--fg)}
.terms{display:grid;gap:.3rem;margin:.5rem 0 .8rem}
.terms a{display:block;padding:.45rem .6rem;background:var(--soft);border-radius:5px;
  color:var(--accent);text-decoration:none;font:12px ui-monospace,Menlo,monospace}
.terms a:hover{text-decoration:underline}
label.accept{display:flex;align-items:center;gap:.5rem;font-size:.85rem;
  margin-bottom:.7rem;cursor:pointer}
label.accept input{width:auto}
.row{display:flex;gap:.5rem;flex-wrap:wrap}
button:disabled{opacity:.45;cursor:not-allowed}
#sdklog{margin:0;max-height:340px;overflow:auto;white-space:pre-wrap;
  font:11px/1.5 ui-monospace,Menlo,monospace;color:var(--dim)}
#sdkstatus,#projinfo{display:grid;gap:.35rem;font:12px ui-monospace,Menlo,monospace}
#sdkstatus .k,#projinfo .k{color:var(--dim);display:inline-block;min-width:9rem}
#sdkstatus .yes{color:var(--ok)} #sdkstatus .no{color:var(--bad)}
#projinfo .p{word-break:break-all}
#recent,#pickerlist{display:grid;gap:.25rem;margin-top:.35rem}
#pickerlist{max-height:260px;overflow:auto}
#recent button,#pickerlist button{text-align:left;font:11px ui-monospace,Menlo,monospace;
  padding:.35rem .5rem;width:100%}
#pickerlist .isproj{color:var(--ok)}
#pickernote{display:block;margin-top:.5rem}
.plate h3{margin:0 0 .5rem;font:600 .66rem/1 ui-monospace,Menlo,monospace;
  letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
.plate img{image-rendering:pixelated;display:block;max-width:100%}
</style></head><body>
<!-- Activity rail, editor area, status bar: the shape VS Code and Rider settled on, and
     for the reason they settled on it. Six top tabs was already crowded and every new
     capability added a seventh; a rail scales down the side and leaves the whole width
     to the thing being worked on. -->
<div id="rail">
  <button id="tabmaps" class="act on" data-t="maps"><i>▦</i><em>Maps</em></button>
  <button id="tabpixel" class="act" data-t="pixel"><i>✎</i><em>Sprites</em></button>
  <button id="tabfonts" class="act" data-t="fonts"><i>A</i><em>Fonts</em></button>
  <button id="tabimport" class="act" data-t="import"><i>⇥</i><em>Import</em></button>
  <button id="tabcode" class="act" data-t="code"><i>&lt;/&gt;</i><em>Code</em></button>
  <div style="flex:1"></div>
  <button id="tabsdk" class="act" data-t="sdk"><i>⚙</i><em>Settings</em></button>
</div>

<div id="work">
  <!-- Contextual toolbar. Only what the current activity needs: a map selector belongs
       to Maps and is noise everywhere else. -->
  <header>
    <b id="ctxtitle">Maps</b>
    <span id="ctxbar">
      <select id="mapsel"></select>
      <select id="atlassel" title="tileset this map is drawn with"></select>
      <span id="tool"></span>
    </span>
    <div style="flex:1"></div>
    <span id="dirty"></span>
    <button id="save">Save map</button>
    <button id="build" class="primary">Build</button>
  </header>

  <!-- The budgets, on every page, above whatever is being worked on.
       Four ceilings, spent against by different work and discovered at different times.
       Resources fail at the appstore, the binary fails at the linker with a struct.error
       naming nothing, RAM fails as a malloc returning NULL mid-scene, and persist fails
       on a watch in front of a player. None of those tell you WHICH edit did it, and by
       then the edit is hours old -- so the numbers live here rather than behind a tab. -->
  <!-- One line, only when there is something to say. An update banner that is always
       present is a banner nobody reads. -->
  <div id="updbanner" style="display:none">
    <span id="updbannertext"></span>
    <button id="updbannergo">Update…</button>
    <button id="updbannerhide" title="not now">✕</button>
  </div>

  <div id="budgetbar">
    <div class="bcell" id="bc-res">
      <div class="bhead"><span>Resources</span><b id="bv-res">—</b></div>
      <div class="meter"><i id="bm-res"></i></div>
      <small id="bn-res">—</small>
    </div>
    <div class="bcell" id="bc-app">
      <div class="bhead"><span>App binary</span><b id="bv-app">—</b></div>
      <div class="meter"><i id="bm-app"></i></div>
      <small id="bn-app">—</small>
    </div>
    <div class="bcell" id="bc-ram">
      <div class="bhead"><span>RAM</span><b id="bv-ram">—</b></div>
      <div class="meter"><i id="bm-ram"></i></div>
      <small id="bn-ram">—</small>
    </div>
    <div class="bcell" id="bc-save">
      <div class="bhead"><span>Save</span><b id="bv-save">—</b></div>
      <div class="meter"><i id="bm-save"></i></div>
      <small id="bn-save">—</small>
    </div>
  </div>
<main>
  <aside id="side">
    <section><h2>Paint</h2><div class="tiles" id="legend"></div>
      <small id="painthint">Click to paint. <kbd>W</kbd> sets a warp, <kbd>S</kbd> the
      start.</small>
    </section>
    <section><h2>Map</h2><div id="mapinfo"><small>—</small></div>
      <div class="mini">
        <input id="nmname" placeholder="name" size="8">
        <input id="nmw" type="number" value="24" min="3" max="255" title="width">
        <input id="nmh" type="number" value="16" min="3" max="255" title="height">
        <button id="newmap">＋ Map</button>
      </div>
    </section>
    <!-- What the watch actually shows of this map. Drag the blue grip to move it. -->
    <section><h2>Camera</h2>
      <label class="mini"><input id="camon" type="checkbox" checked> show device frame</label>
      <small id="caminfo">—</small>
    </section>
    <section><h2>Transitions</h2><div id="warps"></div>
      <div class="mini">
        <span id="warpfrom">pick a tile</span>
      </div>
      <small>Press <kbd>W</kbd>, click a tile, then choose where it leads.</small>
    </section>
    <section><h2>Palettes</h2><div id="pals"></div></section>
    <!-- Orientation is a project-wide content decision, not a view setting: it changes
         what the pipeline BAKES. Named for where the button cluster ends up, because
         that is what the choice is really about -- under one thumb it is a menu, along
         the top edge it is shoulder triggers, along the bottom it is flippers. -->
    <section><h2>Orientation</h2>
      <select id="orient">
        <option value="portrait">Portrait — cluster right, one thumb</option>
        <option value="buttons_top">Landscape — cluster top, triggers</option>
        <option value="buttons_bottom">Landscape — cluster bottom, flippers</option>
      </select>
      <small id="orientnote">—</small></section>
    <!-- The budgets used to live here, in the Maps sidebar, where four of the five tabs
         could not see them. They are now the strip above every page. -->
  </aside>
  <div id="stage"><canvas id="cv"></canvas></div>
  <div id="import" style="display:none;flex:1;overflow:auto;padding:1.5rem">
    <div class="imp">
      <div class="fields">
        <label>Sheet<select id="sheet"></select></label>
        <label>Tile px<input id="tile" type="number" value="16" min="4" step="4"></label>
        <label>Region x<input id="rx" type="number" value="0" min="0"></label>
        <label>y<input id="ry" type="number" value="0" min="0"></label>
        <label>w<input id="rw" type="number" value="16" min="1"></label>
        <label>h<input id="rh" type="number" value="16" min="1"></label>
        <label>Max tiles<input id="maxt" type="number" value="64" min="1"></label>
        <label>Name<input id="aname" placeholder="cave_env"></label>
        <!-- The colour key. Picked off the sheet with the eyedropper rather than typed,
             because the value that matters is the one actually in the art -- a
             background that looks like magenta is often not 255,0,255. -->
        <label class="keypick">Transparent
          <button id="keypick" title="click, then click the sheet">⌖ pick</button>
          <i id="keyswatch" class="none"></i>
          <span id="keyval">none</span>
          <button id="keynone" title="no colour key">clear</button>
        </label>
        <button id="addatlas" class="primary">Add atlas</button>
      </div>
      <div id="stats" class="stats"></div>
      <div class="plate"><h3>Slice — click a tile to drop it</h3>
        <div class="row" style="margin-bottom:.5rem">
          <button id="slkeepall">Keep all</button>
          <button id="sldropdup">Drop duplicates</button>
          <button id="sldropempty">Drop empties</button>
          <!-- Tiles are 16 pixels square. Judging whether two of them are really the
               same, or whether a carve cut through something, cannot be done at that
               size on a modern display. -->
          <label class="zoom">zoom
            <input id="slzoom" type="range" min="1" max="8" step="1" value="3">
            <b id="slzoomv">3×</b>
          </label>
          <span id="slnote"></span>
        </div>
        <div id="slice"></div>
      </div>
      <!-- The region, drawn ON the sheet. Four numbers in tile units are hard to hold in
           your head against a picture; a box over the art is not. -->
      <div class="plate"><h3>Sheet — the box is what gets carved</h3>
        <div id="sheetwrap"><img id="sheetimg" alt=""><i id="cropbox"></i></div>
        <small id="cropnote">—</small>
      </div>
      <div class="plate"><h3>Tiles kept</h3><img id="strip" alt=""></div>
    </div>
  </div>

  <!-- Fonts. The layout puts the device-sized canvas next to the controls rather than
       below them, because the whole point is watching the text change as you drag the
       threshold -- a preview you have to scroll to is a preview you stop looking at. -->
  <div id="fonts" style="display:none;flex:1;overflow:auto;padding:1.25rem">
    <div class="fontgrid">
      <div class="fontctl">
        <section><h2>Typeface</h2>
          <div class="fields col">
            <label>Source<select id="fsrc"></select></label>
            <label>Size px<input id="fsize" type="number" value="12" min="4" max="64"></label>
            <label>Depth<select id="fdepth">
              <option value="1">1bpp — crisp</option>
              <option value="2">2bpp — antialiased</option>
            </select></label>
            <label>Threshold <b id="fthreshv">128</b>
              <input id="fthresh" type="range" min="1" max="254" value="128"></label>
            <label>Tracking<input id="ftrack" type="number" value="0" min="-8" max="8"></label>
            <label>Charset<select id="fcharset">
              <option value="auto">auto — from dialog</option>
              <option value="ascii">ascii — all 95</option>
            </select></label>
            <label>Extra<input id="fextra" placeholder="0123456789/%"></label>
          </div>
          <small id="fnote">Threshold decides which greyscale samples become ink.
            At small sizes it makes or breaks legibility — drag it and watch.</small>
        </section>

        <section><h2>Preview over</h2>
          <div class="fields col">
            <label>Background<select id="fbg">
              <option value="solid">flat colour</option>
              <option value="map">a real map</option>
            </select></label>
            <label id="fmapwrap">Map<select id="fmap"></select></label>
            <label id="fscrollwrap">Scroll
              <span class="pair"><input id="fsx" type="number" value="0" min="0" step="8">
              <input id="fsy" type="number" value="0" min="0" step="8"></span></label>
            <label>Clear<span class="swrow"><i id="fbgcsw"></i>
              <select id="fbgc" class="sw"></select></span></label>
            <label class="check"><input id="fbox" type="checkbox" checked> Text box</label>
            <label id="fboxcwrap">Box fill<span class="swrow"><i id="fboxcsw"></i>
              <select id="fboxc" class="sw"></select></span></label>
            <label>Ink<span class="swrow"><i id="finksw"></i>
              <select id="fink" class="sw"></select></span></label>
            <label>Align<select id="falign">
              <option value="left">left</option><option value="center">centre</option>
              <option value="right">right</option>
            </select></label>
            <label>Zoom<select id="fscale">
              <option value="1">1:1 — actual size</option>
              <option value="2" selected>2x</option>
              <option value="3">3x</option>
              <option value="4">4x</option>
            </select></label>
          </div>
        </section>

        <section><h2>Text</h2>
          <select id="fdlg"></select>
          <textarea id="ftext" rows="3">The mines run deep, and the lamps went out weeks ago.</textarea>
        </section>

        <section><h2>Add to manifest</h2>
          <div class="fields col">
            <label>Name<input id="fname" placeholder="hud"></label>
            <label>Licence<input id="flic" placeholder="SIL OFL 1.1"></label>
          </div>
          <small>Rasterised glyphs are redistribution even though the outlines stay
            behind, so the licence is required rather than suggested. A system typeface
            is copied into <code>art/fonts/</code> so the build works elsewhere.</small>
          <button id="faddbtn" class="primary">Add font</button>
        </section>
      </div>

      <div class="fontview">
        <div class="plate wide"><h3 id="fscenetitle">On the watch — 200&times;228</h3>
          <div id="fscenewrap"><img id="fscene" alt=""></div>
          <small id="fscenenote">—</small>
        </div>
        <div id="fstats" class="stats"></div>
        <div class="plate wide"><h3>Glyphs</h3><img id="fsheet" alt="">
          <small id="fchars"></small></div>
        <div class="plate wide"><h3>Declared</h3><div id="fdeclared"></div></div>
      </div>
    </div>
  </div>

  <!-- Sprites: a pixel editor that paints in ARGB2222.
       Not trying to be Aseprite. It exists so that trying an idea does not require
       installing something else first, and so the colours on screen are the device's
       actual 64 rather than an approximation quantised later. -->
  <div id="pixel" style="display:none;flex:1;overflow:auto;padding:1.25rem">
    <div class="fontgrid">
      <div class="fontctl">
        <section><h2>Canvas</h2>
          <div class="fields col">
            <label>Width<input id="pxw" type="number" value="16" min="1" max="128"></label>
            <label>Height<input id="pxh" type="number" value="24" min="1" max="128"></label>
            <label>Frames<input id="pxframes" type="number" value="1" min="1" max="32"
              title="stacked vertically, which is the layout the sprite importer expects"></label>
            <label>Zoom<input id="pxzoom" type="range" min="2" max="24" value="12"></label>
            <label class="check"><input id="pxgrid" type="checkbox" checked> Grid</label>
          </div>
          <small>A sheet is frames stacked vertically — the layout
            <code>[[sprite]] frames</code> already expects.</small>
        </section>

        <section><h2>Tool</h2>
          <div class="row">
            <button id="toolpen" class="primary">Pencil</button>
            <button id="toolfill">Fill</button>
            <button id="toolpick">Pick</button>
            <button id="toolerase">Erase</button>
          </div>
          <div class="row" style="margin-top:.4rem">
            <button id="pxundo">Undo</button>
            <button id="pxclear">Clear</button>
          </div>
        </section>

        <section><h2>Colour</h2>
          <div id="pxpal" class="pal"></div>
          <small id="pxcur">—</small>
        </section>

        <section><h2>File</h2>
          <div class="fields col">
            <label>Open<select id="pxopen"><option value="">—</option></select></label>
            <label>Save as<input id="pxname" placeholder="art/hero.png"></label>
          </div>
          <button id="pxsave" class="primary">Save PNG</button>
          <small id="pxnote">Saved into the project, then importable as a sprite.</small>
        </section>
      </div>

      <div class="fontview">
        <div class="plate wide"><h3>Canvas</h3>
          <div id="pxwrap"><canvas id="pxcv"></canvas></div>
        </div>
        <div class="plate wide"><h3>Actual size</h3>
          <div id="px1wrap"><canvas id="pxcv1"></canvas></div>
          <small>What the watch shows. If it does not read here, it will not read there.</small>
        </div>
      </div>
    </div>
  </div>

  <!-- Code: a stub IDE. A file tree and an editor, sized so the tab exists and has the
       right shape. When M8's Alloy scripting lands, .js joins the same tree through the
       same endpoints -- which is why this is a file tree over the project rather than
       anything C-specific. -->
  <div id="code" style="display:none;flex:1;overflow:hidden;padding:0">
    <div class="codegrid">
      <aside class="codetree"><div id="codelist"></div></aside>
      <div class="codemain">
        <div class="codebar">
          <b id="codepath">no file open</b>
          <span id="codenote"></span>
          <span style="flex:1"></span>
          <span id="codedirty"></span>
          <button id="codesave" class="primary" disabled>Save</button>
        </div>
        <div class="codeedit">
          <!-- The classic overlay: a highlighted <pre> behind a transparent-text
               <textarea>. Keeps real editing behaviour -- caret, selection, undo, IME --
               which a contenteditable reimplementation would have to rebuild badly. -->
          <div id="codescroll">
            <pre id="codehl" aria-hidden="true"><code></code></pre>
            <textarea id="codetext" spellcheck="false" wrap="off"
              placeholder="Pick a file on the left."></textarea>
          </div>
        </div>
        <div id="codediag"></div>
      </div>
    </div>
  </div>

  <!-- Settings: configuration rather than authoring. The toolchain lives here because it
       is a once-ever setup step -- as a peer to Maps and Fonts it carried the same visual
       weight as the things you use every minute. -->
  <div id="sdk" style="display:none;flex:1;overflow:auto;padding:1.5rem">
    <div class="sdkwrap">
      <!-- Updates. The editor is a file someone downloaded once; without this, a fix
           reaches them only if they think to check a releases page they may never have
           seen. Three steps with the user between each, deliberately: this binary carries
           the engine their project compiles against, so an upgrade is a decision. -->
      <div class="plate wide"><h3>Version</h3>
        <div id="updinfo">—</div>
        <div class="row" style="margin-top:.7rem">
          <button id="updcheck">Check for updates</button>
          <button id="upddl" class="primary" style="display:none">Download</button>
          <button id="updapply" class="primary" style="display:none">Install</button>
          <button id="updnotes" style="display:none">Release notes</button>
        </div>
        <div id="updbar" class="meter" style="display:none;margin-top:.6rem">
          <i id="updfill"></i></div>
        <pre id="updbody" style="display:none"></pre>
      </div>
      <div class="plate wide"><h3>Project</h3>
        <div id="projinfo">—</div>
        <div class="row" style="margin-top:.7rem">
          <button id="projopen">Open a folder…</button>
          <button id="projnew">New project…</button>
          <button id="projadopt" style="display:none">Add a .pknproj</button>
        </div>
        <div id="recentwrap" style="margin-top:.7rem">
          <small>Recent</small><div id="recent"></div>
        </div>
      </div>

      <!-- Folder picker. Server-side listing rather than a native dialog, because a
           frozen app cannot count on one being available and a browser tab never has
           one. Shown only while picking. -->
      <div id="picker" class="plate wide" style="display:none">
        <h3 id="pickertitle">Open a project</h3>
        <div class="row" style="margin-bottom:.5rem">
          <input id="pickerpath" style="flex:1;min-width:14rem" placeholder="path">
          <button id="pickerup">Up</button>
          <button id="pickergo">Go</button>
        </div>
        <div id="pickerlist"></div>
        <div id="newfields" style="display:none;margin-top:.7rem">
          <div class="fields col">
            <label>Name<input id="newname" placeholder="My Game"></label>
            <label>Folder<input id="newfolder" placeholder="my-game"></label>
            <label>Author<input id="newauthor" placeholder=""></label>
          </div>
        </div>
        <div class="row" style="margin-top:.7rem">
          <button id="pickerok" class="primary">Open this folder</button>
          <button id="pickercancel">Cancel</button>
        </div>
        <small id="pickernote"></small>
      </div>

      <div class="plate wide"><h3>Engine</h3>
        <div id="engstate"></div>
        <p class="prose" id="engprose"></p>
        <label class="check accept"><input id="engown" type="checkbox">
          I am a developer, I know what I am doing, and I accept responsibility for
          modifying engine code</label>
        <div class="row">
          <button id="engresync" style="display:none">Discard changes and re-track</button>
        </div>
      </div>

      <div id="sdkstate" class="plate wide"><h3>Toolchain status</h3>
        <div id="sdkstatus">—</div></div>

      <div class="plate wide"><h3>The Pebble SDK</h3>
        <p class="prose">Authoring, previewing, validating and budgeting all work with
        nothing installed. Producing a <code>.pbw</code> needs the Pebble SDK — about
        767&nbsp;MB, and it carries its own ARM toolchain, so it is one install rather
        than two.</p>
        <p class="prose"><b>The editor does not ship the SDK and does not download it
        directly.</b> Pebble licenses it to <em>you</em>, and the terms are explicit that
        the licence is non-transferable and that the SDK may not be redistributed. So this
        button drives Pebble&rsquo;s own <code>pebble</code> tool: the files go from
        Pebble to your machine, exactly as if you had run it yourself.</p>

        <div id="sdkterms" class="terms"></div>
        <label class="check accept"><input id="sdkaccept" type="checkbox">
          I have read and accept these terms</label>
        <div class="row">
          <button id="sdkinstall" class="primary" disabled>Install the SDK</button>
          <button id="sdkrefresh">Check for updates</button>
        </div>
      </div>

      <div class="plate wide"><h3>Output</h3><pre id="sdklog">—</pre></div>
    </div>
  </div>
</main>

  <!-- Output. One shared panel rather than a box inside each activity: a build result
       is not a property of the tab you happened to be on when you pressed Build. -->
  <section id="outpanel">
    <div class="outbar">
      <b>Output</b><span id="outhint"></span>
      <div style="flex:1"></div>
      <button id="outtoggle">Hide</button>
    </div>
    <pre id="log">Ready.</pre>
  </section>

  <footer id="statusbar">
    <span id="stproject">—</span>
    <span id="stengine"></span>
    <span id="stbudget"></span>
    <div style="flex:1"></div>
    <span id="stsdk"></span>
  </footer>
</div>
<script>
const S={data:null,map:null,ch:null,mode:'paint',dirty:false,img:{},T:32};
const $=s=>document.querySelector(s);

async function load(){
  S.data=await (await fetch('/api/state')).json();

  // Launched with no project -- from a dock, say. Go straight to Settings, which is
  // where opening and creating live, rather than showing three empty authoring tabs.
  const authoring=['tabmaps','tabimport','tabfonts','tabpixel','tabcode'];
  if(S.data.no_project){
    for(const id of authoring) $('#'+id).disabled=true;
    showTab('sdk');
    return;
  }
  for(const id of authoring) $('#'+id).disabled=false;

  $('#mapsel').innerHTML=S.data.maps.map((m,i)=>`<option value="${i}">${m.name}</option>`).join('');
  $('#atlassel').innerHTML=S.data.atlases.map(a=>`<option value="${a.name}">${a.name}</option>`).join('');
  drawPalettes(); budget(); statusbar(); orientation();
  // Once, a moment after the editor is usable: an update check is never
  // worth delaying the first paint for.
  setTimeout(()=>updCheck(), 1500);
  startHeartbeat();
  selectMap(0);
}

function atlas(){
  return S.data.atlases.find(a=>a.name===S.map.atlas) || S.data.atlases[0];
}

// A legend character names a tile ROLE; the role resolves through whichever atlas this
// map is drawn with. Two tilesets can both define "wall" and mean different tiles.
function resolve(ch){
  const a=atlas(), e=S.data.legend[ch];
  if(!a||!e) return null;
  const idx=a.roles[e.tile.toLowerCase()];
  if(idx===undefined||idx>=a.tiles.length) return null;
  return {uri:a.tiles[idx],index:idx,role:e.tile,flags:e.flags};
}

function drawLegend(){
  const el=$('#legend'); el.innerHTML=''; S.img={};
  const a=atlas();
  if(!a){ el.innerHTML='<small>No atlas built yet — press Build.</small>'; return }

  let usable=0, missing=[];
  for(const ch of Object.keys(S.data.legend)){
    const r=resolve(ch);
    if(!r){ missing.push(`${ch} (${S.data.legend[ch].tile})`); continue }
    usable++;
    const img=new Image(); img.src=r.uri; S.img[ch]=img;
    img.onload=draw;

    const b=document.createElement('button');
    b.className='tile'+(S.ch===ch?' sel':'');
    b.title=`${ch} → ${r.role} (tile ${r.index}) ${r.flags.join(' ')}`;
    b.innerHTML=`<img src="${r.uri}" alt="${ch}"><b>${ch}</b>`;
    b.onclick=()=>{S.ch=ch;S.mode='paint';drawLegend();tool()};
    el.appendChild(b);
  }
  if(!usable||!S.img[S.ch]) S.ch=Object.keys(S.data.legend).find(c=>resolve(c))||null;

  $('#painthint').innerHTML = missing.length
    ? `<span style="color:var(--bad)">${a.name} defines no tile for ${missing.join(', ')}
       — give it an <code>autopick</code> or <code>[atlas.semantic]</code> table.</span>`
    : 'Click to paint. <kbd>W</kbd> sets a warp, <kbd>S</kbd> the start.';
}
function drawPalettes(){
  $('#pals').innerHTML=S.data.palettes.map(p=>'<div class="pal">'+p.map(c=>
    c==='transparent'?'<div class="sw clear"></div>':
    `<div class="sw" style="background:${c}" title="${c}"></div>`).join('')+'</div>').join('');
}
// Budget is recomputed from UNSAVED editor state, not from the last build. Finding out
// a map blew the cap after six hours of work is the failure this exists to prevent, so
// the number has to move while the map is being painted.
let estTimer=null, estLast=null;

// The canvas the author is working on, and the reason it is that shape. Dimensions come
// from the server rather than being derived here, so there is one place that knows the
// display is 200x228 and which way round a landscape project turns it.
function orientation(){
  const o=S.data.orientation==='buttons_right'?'portrait':S.data.orientation;
  $('#orient').value=o;
  const [w,h]=S.data.screen;
  $('#orientnote').textContent=o==='portrait'
    ? `${w}×${h} canvas. Content is baked as drawn.`
    : `${w}×${h} canvas. Art, maps and glyphs are baked turned, so the watch draws them `
      +`the ordinary way round.`;
  const plate=$('#fscenetitle');
  if(plate) plate.innerHTML=`On the watch — ${w}&times;${h}`;
}

function budget(now){
  clearTimeout(estTimer);
  const go=async()=>{
    // Maps are sent as they currently stand in the editor, including edits not yet
    // saved to the manifest.
    const maps={};
    for(const m of (S.data.maps||[])) maps[m.name]=m.rows.join('\n');
    let e;
    try{
      e=await (await fetch('/api/estimate',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({maps})})).json();
    }catch(_){ return }
    if(e.error) return;
    estLast=e;
    paintBudget(e);
  };
  if(now) go(); else estTimer=setTimeout(go,220);
}

// Exact bytes in the headline, rounded KB only in the supporting note. Every one of these
// ceilings is a cliff rather than a slope -- 65,535 is a uint16, not a guideline -- and
// "64 KB / 64 KB" cannot tell you whether you are 2 bytes under it or 40 over.
const B=n=>n.toLocaleString();
const KB=n=>n>=10240?`${(n/1024).toFixed(0)} KB`:`${n.toLocaleString()} B`;

// One cell of the strip. `pct` null means nothing has measured it -- which is drawn as a
// striped bar rather than an empty one, because an empty bar reads as "plenty of room"
// when what it means is "nobody knows".
function paintCell(id, value, pct, note, state){
  const cell=$('#bc-'+id), bar=$('#bm-'+id);
  cell.classList.toggle('over', state==='over');
  cell.classList.toggle('warnb', state==='warn');
  cell.classList.toggle('stale', state==='stale'||pct===null);
  bar.parentElement.classList.toggle('unknown', pct===null);
  bar.style.width=(pct===null?100:Math.min(100,pct))+'%';
  // Cleared rather than set when nothing is known, so the stripe defined in CSS shows
  // through: an inline colour would win, and a full solid bar reads as "100% spent",
  // which is the opposite of what an unmeasured cell means.
  bar.style.background=pct===null?'':
    (state==='over'?'var(--bad)':(state==='warn'?'#d08b2c':'var(--accent)'));
  $('#bv-'+id).textContent=value;
  $('#bn-'+id).innerHTML=note;
}

function paintBudget(e){
  // --- resources. The only one of the four that moves as you paint, which is why the
  //     estimate is recomputed from unsaved rows rather than read off disk.
  paintCell('res', `${B(e.total)} / ${B(e.budget)} B`, e.pct,
    `${e.pct.toFixed(1)}% of the appstore cap`
    + (e.exact?'':' · <b>some assets not built yet</b>'),
    e.over?'over':(e.warn?'warn':''));

  const a=e.app||{};
  if(!a.known){
    paintCell('app', 'not measured', null,
      `${a.why||'no build yet'} · ceiling 65,535 B`, '');
    paintCell('ram', 'not measured', null, 'measured from the linked binary', '');
  }else{
    paintCell('app', `${B(a.used)} / ${B(a.limit)} B`, a.pct,
      a.stale
        ? '<b>stale — sources or assets changed since this build</b>'
        : `${a.pct.toFixed(1)}%`
          + ((a.modules||[])[0]
             ? ` · largest ${a.modules[0].name} ${KB(a.modules[0].bytes)}` : ''),
      a.over?'over':(a.warn?'warn':(a.stale?'stale':'')));

    // RAM is the heap left over, not the static size again: code, rodata, statics and
    // the heap all come out of one slot, so every byte of binary is a byte an arena
    // cannot have. That is the number an author sizes a scene against.
    if(a.slot){
      const usedPct=100*a.used/a.slot;
      paintCell('ram', `${B(a.heap)} B free`, usedPct,
        `heap left of the ${KB(a.slot)} ${a.platform} slot · ${B(a.mutable)} B `
        + `mutable statics`,
        a.heap<16384?'warn':'');
    }else{
      paintCell('ram', KB(a.mutable), null, 'mutable statics; slot size unknown', '');
    }
  }

  const s=e.save||{};
  paintCell('save', s.known?`${B(s.used)} / ${B(s.limit)} B`:'—',
    s.known?s.pct:null, s.known?'persisted per launch':(s.why||'not built yet'), '');

  // The status bar carries whichever ceiling is in the most trouble, since it is the one
  // signal that survives a collapsed strip and there is room for one number.
  const appPct=a.known?a.pct:0;
  const worst=appPct>e.pct
    ? {label:'app', used:a.used, pct:appPct, over:a.over, warn:a.warn}
    : {label:'resources', used:e.total, pct:e.pct, over:e.over, warn:e.warn};

  const st=$('#stbudget');
  if(st){
    st.textContent=`${worst.label} ${KB(worst.used)} (${worst.pct.toFixed(1)}%)`
      + (worst.over?' OVER BUDGET':'');
    st.style.fontWeight=worst.over?'700':'';
  }
  const bar=$('#statusbar');
  if(bar) bar.style.background=worst.over?'var(--bad)':
    (worst.warn?'#a8701f':'var(--accent)');
}
function tool(){
  $('#tool').innerHTML=S.mode==='paint'?`painting <kbd>${S.ch}</kbd>`:
    S.mode==='warp'?'<kbd>click a door to add/remove a warp</kbd>':'<kbd>click to set start</kbd>';
}
function selectMap(i){
  S.map=JSON.parse(JSON.stringify(S.data.maps[i]));
  $('#atlassel').value=S.map.atlas||'';
  // The frame starts where the player does, which is the section an author is most
  // likely to want to look at first.
  if(!S.cam) S.cam={on:$('#camon').checked, x:0, y:0};
  const r=camRect();
  S.cam.x=Math.max(0,(S.map.start[0]+0.5)*S.T-r.w/2);
  S.cam.y=Math.max(0,(S.map.start[1]+0.5)*S.T-r.h/2);
  S.dirty=false; mark(); drawLegend(); renderWarps(); warpForm(null); info(); draw();
  camInfo();
}
$('#camon').onchange=e=>{
  if(!S.cam) S.cam={on:true,x:0,y:0};
  S.cam.on=e.target.checked; draw(); camInfo();
};
$('#atlassel').onchange=e=>{
  S.map.atlas=e.target.value;
  S.dirty=true; mark(); drawLegend(); info(); draw();
};
function mark(){$('#dirty').textContent=S.dirty?'● unsaved':''}
// A warp needs a destination map and tile, so the form appears once a source tile is
// picked rather than asking through a chain of prompts.
function warpForm(at){
  const box=$('#warpfrom');
  if(!at){ box.textContent='pick a tile'; return }
  const others=S.data.maps.map(o=>o.name);
  box.innerHTML=`from (${at[0]},${at[1]}) →
    <select id="wto">${others.map(n=>`<option>${n}</option>`).join('')}</select>
    <input id="wx" type="number" value="1" min="0" title="destination x">
    <input id="wy" type="number" value="1" min="0" title="destination y">
    <button id="wadd">Add</button>`;
  $('#wadd').onclick=()=>{
    S.map.warps.push({at,to:[$('#wto').value,+$('#wx').value,+$('#wy').value]});
    S.dirty=true; S.mode='paint'; mark(); renderWarps(); info(); tool(); draw();
    warpForm(null);
  };
}

function renderWarps(){
  const m=S.map;
  $('#warps').innerHTML = m.warps.length ? m.warps.map((w,i)=>
    `<div class="warp">(${w.at[0]},${w.at[1]}) → <b>${w.to[0]}</b> (${w.to[1]},${w.to[2]})
     <button data-i="${i}" title="remove">✕</button></div>`).join('')
    : '<small>none</small>';
  for(const b of document.querySelectorAll('#warps button'))
    b.onclick=()=>{S.map.warps.splice(+b.dataset.i,1);S.dirty=true;mark();
      renderWarps();info();draw()};
}

function info(){
  const m=S.map;
  $('#mapinfo').innerHTML=`<small>${m.rows[0].length}×${m.rows.length} · `+
    `tileset <b>${m.atlas||'—'}</b> · start (${m.start})<br>`+
    (m.warps.length?m.warps.map(w=>`warp (${w.at}) → ${w.to[0]} (${w.to[1]},${w.to[2]})`).join('<br>'):'no warps')+'</small>';
}

function draw(){
  const m=S.map,T=S.T,cv=$('#cv'),g=cv.getContext('2d');
  cv.width=m.rows[0].length*T; cv.height=m.rows.length*T;
  g.imageSmoothingEnabled=false;
  g.fillStyle='#000'; g.fillRect(0,0,cv.width,cv.height);
  for(let y=0;y<m.rows.length;y++)for(let x=0;x<m.rows[y].length;x++){
    const im=S.img[m.rows[y][x]];
    if(im&&im.complete) g.drawImage(im,x*T,y*T,T,T);
  }
  // start marker and warps, drawn over the map so placement is checkable at a glance
  g.strokeStyle='#55aaff'; g.lineWidth=2;
  g.strokeRect(m.start[0]*T+1,m.start[1]*T+1,T-2,T-2);
  g.strokeStyle='#e0913f';
  for(const w of m.warps) g.strokeRect(w.at[0]*T+1,w.at[1]*T+1,T-2,T-2);
  drawCamera(g,cv);
}

// The device viewport, drawn over the map.
//
// A map is authored at whatever zoom fits the window; the watch shows 200x228 pixels of
// it. Those are not the same picture, and the gap is where "this room feels open" turns
// into a room the player sees a fifth of. Everything outside the frame is dimmed rather
// than hidden, because the point is to judge a section against its surroundings.
function camRect(){
  const a=atlas(), tile=(a&&a.tile)||16, scale=S.T/tile;
  const [sw,sh]=(S.data.screen||[200,228]);
  return {w:sw*scale, h:sh*scale, tile, scale};
}

function drawCamera(g,cv){
  if(!S.cam||!S.cam.on) return;
  const r=camRect();
  const x=Math.max(0,Math.min(S.cam.x, Math.max(0,cv.width-r.w)));
  const y=Math.max(0,Math.min(S.cam.y, Math.max(0,cv.height-r.h)));
  S.cam.x=x; S.cam.y=y;

  g.save();
  g.fillStyle='rgba(0,0,0,.55)';
  g.fillRect(0,0,cv.width,y);
  g.fillRect(0,y+r.h,cv.width,cv.height-(y+r.h));
  g.fillRect(0,y,x,r.h);
  g.fillRect(x+r.w,y,cv.width-(x+r.w),r.h);

  g.strokeStyle='#7fd1ff'; g.lineWidth=2;
  g.strokeRect(x+1,y+1,r.w-2,r.h-2);
  // Grab handle, so dragging the frame and painting inside it stay different gestures.
  g.fillStyle='#7fd1ff';
  g.fillRect(x,y,CAM_GRIP,CAM_GRIP);
  g.restore();
}

const CAM_GRIP=14;

function camHit(px,py){
  if(!S.cam||!S.cam.on) return false;
  return px>=S.cam.x && px<=S.cam.x+CAM_GRIP && py>=S.cam.y && py<=S.cam.y+CAM_GRIP;
}

function camInfo(){
  const r=camRect(), [sw,sh]=(S.data.screen||[200,228]);
  const tx=(S.cam.x/S.T), ty=(S.cam.y/S.T);
  $('#caminfo').innerHTML=S.cam.on
    ? `${sw}×${sh} px — ${(r.w/S.T).toFixed(1)}×${(r.h/S.T).toFixed(1)} tiles at `
      +`${r.tile}px · top-left tile ${tx.toFixed(1)}, ${ty.toFixed(1)}`
    : 'hidden';
}

// The camera grip is checked before painting: a drag that starts on it moves the frame,
// and anything else paints. Two gestures on one canvas, told apart by where the drag
// STARTED rather than by a mode -- switching modes to nudge a viewport is the kind of
// friction that means nobody nudges it.
let camDrag=null;

$('#cv').addEventListener('mousedown',e=>{
  const r=e.target.getBoundingClientRect();
  const px=e.clientX-r.left, py=e.clientY-r.top;
  if(camHit(px,py)){ camDrag={dx:px-S.cam.x, dy:py-S.cam.y}; e.preventDefault(); return }
  paint(e,true);
});
addEventListener('mousemove',e=>{
  if(!camDrag) return;
  const cv=$('#cv'), r=cv.getBoundingClientRect();
  S.cam.x=e.clientX-r.left-camDrag.dx;
  S.cam.y=e.clientY-r.top-camDrag.dy;
  draw(); camInfo();
});
addEventListener('mouseup',()=>{ camDrag=null });

$('#cv').addEventListener('mousemove',e=>{if(e.buttons&&!camDrag)paint(e,false)});
function paint(e,click){
  const r=e.target.getBoundingClientRect();
  const x=Math.floor((e.clientX-r.left)/S.T), y=Math.floor((e.clientY-r.top)/S.T);
  const m=S.map;
  if(x<0||y<0||y>=m.rows.length||x>=m.rows[y].length) return;

  if(S.mode==='start'){ if(!click)return; m.start=[x,y]; S.mode='paint'; }
  else if(S.mode==='warp'){
    if(!click)return;
    const i=m.warps.findIndex(w=>w.at[0]===x&&w.at[1]===y);
    if(i>=0){ m.warps.splice(i,1); S.mode='paint'; warpForm(null); }
    else warpForm([x,y]);
  } else {
    const row=m.rows[y];
    if(row[x]===S.ch) return;
    m.rows[y]=row.slice(0,x)+S.ch+row.slice(x+1);
  }
  S.dirty=true; mark(); info(); tool(); draw(); budget();
}

addEventListener('keydown',e=>{
  if(e.target.tagName==='SELECT')return;
  if(e.key==='w'||e.key==='W'){S.mode='warp';tool()}
  if(e.key==='s'||e.key==='S'){S.mode='start';tool()}
  if(e.key==='Escape'){S.mode='paint';tool()}
});
$('#mapsel').onchange=e=>{
  if(S.dirty&&!confirm('Discard unsaved changes to this map?')){
    e.target.value=S.data.maps.findIndex(m=>m.name===S.map.name); return;
  }
  selectMap(+e.target.value);
};
$('#save').onclick=async()=>{
  const r=await (await fetch('/api/map',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(S.map)})).json();
  const log=$('#log');
  log.className=r.ok?'ok':'bad';
  log.textContent=r.ok?`Saved ${S.map.name} to the manifest.`:r.error;
  if(r.ok){S.dirty=false;mark()}
};
$('#newmap').onclick=async()=>{
  const name=$('#nmname').value.trim();
  if(!name){alert('Name the map first.');return}
  const r=await (await fetch('/api/newmap',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name,w:+$('#nmw').value,h:+$('#nmh').value,
      atlas:$('#atlassel').value||S.data.atlases[0].name})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  if(!r.ok){log.textContent=r.error;return}
  log.textContent=`Created map "${name}" and a scene for it. Press Build.`;
  $('#nmname').value='';
  await load();
  const i=S.data.maps.findIndex(m=>m.name===name);
  $('#mapsel').value=i; selectMap(i);
};
// Changing this changes what the pipeline BAKES -- every atlas, sprite, map and glyph
// comes out turned -- so the resources on disk are stale the moment it is picked. Saying
// so beats letting someone wonder why the preview and the watch disagree.
$('#orient').onchange=async()=>{
  const r=await (await fetch('/api/orientation',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({orientation:$('#orient').value})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  if(!r.ok){log.textContent=r.error;return}
  log.textContent='Orientation set. Press Build — every asset is baked turned, so the '
                 +'resources on disk are now stale.';
  const keep=S.map&&S.map.name; await load();
  const i=Math.max(0,S.data.maps.findIndex(m=>m.name===keep));
  $('#mapsel').value=i; selectMap(i);
};
$('#build').onclick=async()=>{
  const log=$('#log'); log.className=''; log.textContent='Building…';
  const r=await (await fetch('/api/build',{method:'POST'})).json();
  log.className=r.ok?'ok':'bad'; log.textContent=r.output.trim()||'(no output)';
  if(r.ok){ const keep=S.map.name; await load();
    const i=S.data.maps.findIndex(m=>m.name===keep);
    $('#mapsel').value=i; selectMap(i); }
  // A build is when the estimate stops being an estimate: every blob it guessed at now
  // exists on disk. Repaint immediately rather than on the next keystroke.
  budget(true);
};
// ------------------------------------------------------------------ import view
let sheets=[];
function showTab(which){
  const imp=which==='import', fnt=which==='fonts', maps=which==='maps',
        sdk=which==='sdk', pix=which==='pixel', cod=which==='code';
  $('#import').style.display=imp?'block':'none';
  $('#fonts').style.display=fnt?'block':'none';
  $('#sdk').style.display=sdk?'block':'none';
  $('#pixel').style.display=pix?'block':'none';
  $('#code').style.display=cod?'block':'none';
  if(sdk){ sdkStatus(); updCheck() }
  if(pix&&!PX.data){ pxPalette(); pxInit(+$('#pxw').value,+$('#pxh').value,1); pxLoadList() }
  if(cod&&!$('#codelist').children.length) codeTree();
  $('#stage').style.display=maps?'flex':'none';
  // The map sidebar and its toolbar controls belong to Maps. Showing them elsewhere
  // implies they apply there.
  $('#side').style.display=maps?'':'none';
  $('#ctxbar').style.display=maps?'':'none';
  $('#save').style.display=maps?'':'none';
  if(imp&&!sheets.length) loadSheets();
  if(fnt&&!fontSources.length) loadFonts();
  // The strip spans every tab, so it re-reads on arrival: importing an atlas or adding a
  // font spends the same budget a map edit does, and a number that only refreshed while
  // painting would be a number nobody could trust anywhere else.
  budget(true);

  for(const b of document.querySelectorAll('.act'))
    b.classList.toggle('on', b.dataset.t===which);
  $('#ctxtitle').textContent={maps:'Maps',import:'Import',fonts:'Fonts',
    pixel:'Sprites',code:'Code',sdk:'Settings'}[which]||'';
}

function statusbar(){
  const d=S.data||{}, e=d.engine||{}, p=d.paths||{};
  if(d.no_project){ $('#stproject').textContent='no project open'; return }
  $('#stproject').textContent=`${d.name} — ${p.root||''}`;
  $('#stengine').textContent=`engine ${e.editor||'?'}`;
  if(estLast) paintBudget(estLast);
  // Fetched once on load rather than polled: it changes when someone installs an SDK,
  // which is not a per-second event.
  fetch('/api/sdk/status').then(r=>r.json()).then(s=>{
    $('#stsdk').textContent=s.can_build?`SDK ${s.active}`:'no SDK — see Settings';
  }).catch(()=>{});
}

$('#outtoggle').onclick=()=>{
  const hid=$('#outpanel').classList.toggle('hidden');
  $('#outtoggle').textContent=hid?'Show':'Hide';
};
$('#tabmaps').onclick=()=>showTab('maps');
$('#tabimport').onclick=()=>showTab('import');
$('#tabfonts').onclick=()=>showTab('fonts');
$('#tabsdk').onclick=()=>showTab('sdk');
$('#tabpixel').onclick=()=>showTab('pixel');
$('#tabcode').onclick=()=>showTab('code');

async function loadSheets(){
  sheets=await (await fetch('/api/sheets')).json();
  $('#sheet').innerHTML=sheets.map(s=>`<option value="${s.path}">${s.name}</option>`).join('');
  analyse(); drawSlice();
}
// Cells the author has dropped, as indices into the region. Reset when the region
// changes, because the indices mean something different the moment it does.
let IMPEX=new Set(), impRegion='';

function impRegionKey(){
  return [$('#sheet').value,$('#tile').value,$('#rx').value,$('#ry').value,
          $('#rw').value,$('#rh').value].join('/');
}

async function drawSlice(){
  const key=impRegionKey();
  if(key!==impRegion){ IMPEX=new Set(); impRegion=key }
  const body={sheet:$('#sheet').value,tile:+$('#tile').value,
    region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
    exclude:[...IMPEX], colorkey:KEY};
  if(!body.sheet) return;
  const g=await (await fetch('/api/slice',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
  if(g.error){ $('#slnote').textContent=g.error; return }

  const el=$('#slice');
  // Zoom is a tile SIZE, not a CSS transform: the grid stays a grid, the cells stay
  // clickable at their real positions, and the container scrolls. A transform would
  // scale the hit areas out from under the pointer.
  const z=+$('#slzoom').value, cell=16*z;
  el.style.gridTemplateColumns=`repeat(${g.cols}, ${cell}px)`;
  el.style.maxWidth='100%';
  el.innerHTML=g.cells.map(c=>
    `<button data-i="${c.i}" class="${IMPEX.has(c.i)?'off':c.state}"
      title="cell ${c.i} at ${c.x},${c.y} — ${IMPEX.has(c.i)?'dropped':c.state}"
      ><img src="${c.img}" alt=""></button>`).join('');
  for(const b of el.querySelectorAll('button'))
    b.onclick=()=>{
      const i=+b.dataset.i;
      IMPEX.has(i)?IMPEX.delete(i):IMPEX.add(i);
      drawSlice(); analyse();
    };
  const kept=g.cells.filter(c=>c.state==='unique'&&!IMPEX.has(c.i)).length;
  $('#slnote').textContent=`${kept} kept of ${g.cells.length} cells`
    +(IMPEX.size?` · ${IMPEX.size} dropped`:'')
    +(g.capped?` · showing the first ${g.limit}`:'');
}

$('#slkeepall').onclick=()=>{ IMPEX=new Set(); drawSlice(); analyse() };
$('#sldropdup').onclick=async()=>{
  const g=await (await fetch('/api/slice',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      exclude:[],colorkey:KEY})})).json();
  // Duplicates already cost nothing -- dedup collapses them -- so this is about a
  // shorter grid to read, not about bytes.
  for(const c of g.cells) if(c.state==='dup') IMPEX.add(c.i);
  drawSlice(); analyse();
};
$('#sldropempty').onclick=async()=>{
  const g=await (await fetch('/api/slice',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      exclude:[],colorkey:KEY})})).json();
  for(const c of g.cells) if(c.state==='empty') IMPEX.add(c.i);
  drawSlice(); analyse();
};

let pending=null;
function analyse(){
  clearTimeout(pending);
  pending=setTimeout(async()=>{
    const body={sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      max_tiles:+$('#maxt').value,exclude:[...IMPEX],colorkey:KEY};
    if(!body.sheet) return;
    const r=await (await fetch('/api/analyse',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.error){$('#stats').innerHTML=`<div class="warn"><b>!</b><span>${r.error}</span></div>`;return}
    const cell=(v,l,warn)=>`<div class="${warn?'warn':''}"><b>${v}</b><span>${l}</span></div>`;
    // What it costs, and what it costs YOU: the same bytes read very differently
    // against a budget that is already 7% spent or already 97%.
    const B=n=>n.toLocaleString();
    $('#stats').innerHTML=
      cell(r.unique,'unique tiles',r.capped)+
      cell(r.colours,'colours')+
      cell(r.palettes,'palettes')+
      cell(B(r.bytes),'bytes it adds')+
      cell(`${B(r.res_before)} → ${B(r.res_after)}`,
           `resources, ${r.res_pct_after.toFixed(1)}% of the cap`, r.res_over)+
      (r.heap_after!==null&&r.heap_after!==undefined
        ? cell(`${B(r.heap_before)} → ${B(r.heap_after)}`,
               'heap while a scene holds it', r.heap_after<16384)
        : cell('—','heap: build once to measure'))+
      cell(r.repaired,'repaired',r.repaired>0)+
      cell(r.sheet_tiles.join('×'),'sheet tiles');
    $('#sheetimg').src=r.thumb;
    $('#strip').src=r.strip||'';
    drawCrop(r.sheet_tiles);
  },180);
}
for(const id of ['sheet','tile','rx','ry','rw','rh','maxt'])
  $('#'+id).addEventListener('input',()=>{ analyse(); drawSlice() });

$('#slzoom').addEventListener('input',()=>{
  $('#slzoomv').textContent=$('#slzoom').value+'×';
  drawSlice();
});

// ------------------------------------------------------------- the colour key
//
// A great deal of tile art is distributed with no alpha channel at all, drawn on a flat
// background -- magenta by convention, but often not exactly magenta. Without a key those
// pixels are ordinary opaque colour: they eat palette entries, they defeat the blitter's
// early-out for transparent pixels, and they draw a rectangle around every sprite.
//
// Picked off the sheet rather than typed, because the value that matters is the one
// actually in the file. The thumbnail is resized with NEAREST, so its pixels are the
// source's pixels and reading one is exact.
let KEY=null;

function keyLabel(){
  const sw=$('#keyswatch');
  sw.classList.toggle('none', !KEY);
  sw.style.background=KEY?`rgb(${KEY[0]},${KEY[1]},${KEY[2]})`:'';
  $('#keyval').textContent=KEY?`${KEY[0]}, ${KEY[1]}, ${KEY[2]}`:'none';
}

$('#keypick').onclick=()=>{
  $('#sheetwrap').classList.toggle('picking');
  $('#cropnote').textContent=$('#sheetwrap').classList.contains('picking')
    ? 'click a pixel on the sheet — usually the background behind a sprite'
    : '';
};

$('#keynone').onclick=()=>{
  // No key is a real answer, not an empty field: art that already has alpha needs none.
  KEY=null; keyLabel(); analyse(); drawSlice();
};

$('#sheetimg').addEventListener('click',e=>{
  if(!$('#sheetwrap').classList.contains('picking')) return;
  const img=e.target, r=img.getBoundingClientRect();
  const x=Math.floor((e.clientX-r.left)/r.width*img.naturalWidth);
  const y=Math.floor((e.clientY-r.top)/r.height*img.naturalHeight);
  const c=document.createElement('canvas');
  c.width=img.naturalWidth; c.height=img.naturalHeight;
  const g=c.getContext('2d', {willReadFrequently:true});
  g.imageSmoothingEnabled=false;
  g.drawImage(img,0,0);
  const p=g.getImageData(Math.max(0,x),Math.max(0,y),1,1).data;
  KEY=[p[0],p[1],p[2]];
  $('#sheetwrap').classList.remove('picking');
  keyLabel(); analyse(); drawSlice();
});

// The region as a box on the sheet. Percentages of the sheet's size in TILES, which is
// the same fraction as pixels and survives the thumbnail being scaled to fit.
function drawCrop(sheetTiles){
  const box=$('#cropbox');
  if(!sheetTiles || !sheetTiles[0] || !sheetTiles[1]){ box.style.display='none'; return }
  const [sx,sy]=sheetTiles;
  const rx=+$('#rx').value, ry=+$('#ry').value,
        rw=+$('#rw').value, rh=+$('#rh').value;
  // 'block', not '': clearing the inline style hands display back to the stylesheet,
  // which sets none -- so the box positioned itself perfectly and stayed invisible.
  box.style.display='block';
  box.style.left  =(100*Math.min(rx,sx)/sx)+'%';
  box.style.top   =(100*Math.min(ry,sy)/sy)+'%';
  box.style.width =(100*Math.min(rw,sx-rx)/sx)+'%';
  box.style.height=(100*Math.min(rh,sy-ry)/sy)+'%';

  // Running off the sheet is the commonest way a carve goes wrong, and the pipeline
  // rejects it at build time; saying so here saves the round trip.
  const over=(rx+rw>sx)||(ry+rh>sy);
  box.style.borderColor=over?'var(--bad)':'var(--accent)';
  $('#cropnote').innerHTML=over
    ? `<b class="bad">the region runs off the sheet</b> — it is ${sx}×${sy} tiles`
    : `${rw}×${rh} of ${sx}×${sy} tiles — ${(100*rw*rh/(sx*sy)).toFixed(0)}% of the sheet`;
}

$('#addatlas').onclick=async()=>{
  const name=$('#aname').value.trim();
  if(!name){alert('Name the atlas first.');return}
  const r=await (await fetch('/api/atlas',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name,sheet:$('#sheet').value,tile:+$('#tile').value,colorkey:KEY,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      max_tiles:+$('#maxt').value,exclude:[...IMPEX]})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  log.textContent=r.ok?`Added [[atlas]] "${name}" to the manifest. Press Build.`:r.error;
};

// ------------------------------------------------------------------- fonts view
//
// Every pixel shown here is rendered server-side from the packed blob and sent back as
// a PNG. Compositing in the browser would be faster but would mean two rasterisers --
// and the moment they disagree the preview is worse than useless, because it looks
// authoritative while being wrong.
let fontSources=[];

// ARGB2222: alpha in the top two bits, then R, G, B. Only the opaque values are worth
// offering -- the framebuffer is opaque and text is drawn onto it, not composited under.
function argbSwatches(){
  const out=[];
  for(let r=0;r<4;r++)for(let g=0;g<4;g++)for(let b=0;b<4;b++)
    out.push({v:0xC0|(r<<4)|(g<<2)|b,
              css:`rgb(${r*85},${g*85},${b*85})`,
              hex:'0x'+(0xC0|(r<<4)|(g<<2)|b).toString(16).toUpperCase()});
  return out;
}
function fillSwatch(sel,chip,chosen){
  sel.innerHTML=argbSwatches().map(s=>
    `<option value="${s.v}"${s.v===chosen?' selected':''}>${s.hex}</option>`).join('');
  const paint=()=>{
    const s=argbSwatches().find(s=>s.v===+sel.value);
    if(s) $(chip).style.background=s.css;
  };
  sel.addEventListener('input',paint);
  paint();
}

// Controls that only mean something in a particular mode are hidden rather than
// disabled: a scroll offset with no map behind it is noise, not a disabled feature.
function fontModes(){
  const isMap=$('#fbg').value==='map';
  $('#fmapwrap').style.display=isMap?'':'none';
  $('#fscrollwrap').style.display=isMap?'':'none';
  $('#fboxcwrap').style.display=$('#fbox').checked?'':'none';
}

async function loadFonts(){
  fontSources=await (await fetch('/api/fonts')).json();
  // Project fonts first and visually separated: referencing a system font in the
  // manifest would build here and nowhere else, so the distinction matters.
  const mine=fontSources.filter(f=>f.in_project), sys=fontSources.filter(f=>!f.in_project);
  const opt=f=>`<option value="${f.in_project?f.rel:f.path}">${f.name}</option>`;
  $('#fsrc').innerHTML=
    (mine.length?`<optgroup label="in this project">${mine.map(opt).join('')}</optgroup>`:'')+
    (sys.length?`<optgroup label="installed on this machine">${sys.map(opt).join('')}</optgroup>`:'');

  $('#fmap').innerHTML=(S.data.maps||[]).map(m=>`<option>${m.name}</option>`).join('');

  const dlg=S.data.dialog||{};
  const pages=[];
  for(const [k,v] of Object.entries(dlg)) v.forEach((p,i)=>pages.push([`${k} · ${i}`,p]));
  $('#fdlg').innerHTML=`<option value="">— your own text —</option>`+
    pages.map(([l,p])=>`<option value="${encodeURIComponent(p)}">${l}</option>`).join('');

  fillSwatch($('#fbgc'),'#fbgcsw',0xC0);   // black: what a frame is usually cleared to
  fillSwatch($('#fboxc'),'#fboxcsw',0xC0);
  fillSwatch($('#fink'),'#finksw',0xFF);   // white
  declared();
  fontModes();
  refreshFont();
}

function declared(){
  const fonts=S.data.fonts||[];
  $('#fdeclared').innerHTML=fonts.length?fonts.map(f=>
    `<div><b>${f.name}</b><span>${f.size}px · ${f.depth}bpp · ${
      f.bytes!==null?f.bytes.toLocaleString()+' B':'not built'}</span></div>`).join('')
    :'<small>None yet.</small>';
}

function fontSpec(){
  return {source:$('#fsrc').value,size:+$('#fsize').value,depth:+$('#fdepth').value,
          threshold:+$('#fthresh').value,tracking:+$('#ftrack').value,
          charset:$('#fcharset').value,extra:$('#fextra').value};
}

let fpending=null;
function refreshFont(){
  clearTimeout(fpending);
  fpending=setTimeout(async()=>{
    if(!$('#fsrc').value) return;
    const spec=fontSpec();
    const post=(u,b)=>fetch(u,{method:'POST',headers:{'content-type':'application/json'},
                              body:JSON.stringify(b)}).then(r=>r.json());

    const r=await post('/api/font/preview',spec);
    if(r.error){
      $('#fstats').innerHTML=`<div class="warn"><b>!</b><span>${r.error}</span></div>`;
      return;
    }
    const cell=(v,l,warn)=>`<div class="${warn?'warn':''}"><b>${v}</b><span>${l}</span></div>`;
    $('#fstats').innerHTML=
      cell(r.glyph_count,'glyphs')+
      cell(r.line_height+'px','line height')+
      cell(r.baseline+'px','baseline')+
      cell(r.bytes.toLocaleString(),'bytes')+
      cell(r.pct.toFixed(2)+'%','of budget')+
      // A glyph that rasterised to nothing and is not a space means the threshold ate
      // it. That is the commonest way an imported font is quietly broken, so it is
      // called out rather than left to be noticed on the watch.
      cell(r.blank_glyphs,'blank glyphs',r.blank_glyphs>1);
    $('#fsheet').src=r.sheet;
    $('#fchars').textContent=`carries: ${r.chars}`;

    const box=$('#fbox').checked;
    const s=await post('/api/font/scene',{
      spec,background:$('#fbg').value,map:$('#fmap').value,
      scroll_x:+$('#fsx').value,scroll_y:+$('#fsy').value,
      bg_colour:+$('#fbgc').value,ink:+$('#fink').value,
      align:$('#falign').value,scale:+$('#fscale').value,
      text:$('#ftext').value,x:4,y:4,
      box:box?{on:true,x:8,y:140,w:184,h:72,colour:+$('#fboxc').value,
               border:true,border_colour:+$('#fink').value}:{on:false}});
    if(s.error){$('#fscenenote').textContent=s.error;return}
    $('#fscene').src=s.image;
    $('#fscenenote').innerHTML=
      `${s.lines} line${s.lines===1?'':'s'} · ${s.text_height}px of text · shown at ${s.scale}x`+
      (s.overflow?' · <b class="bad">overflows the box</b>':'');
  },140);
}

for(const id of ['fsrc','fsize','fdepth','fthresh','ftrack','fcharset','fextra',
                 'fbg','fmap','fsx','fsy','fbgc','fbox','fboxc','fink','falign',
                 'fscale','ftext'])
  $('#'+id).addEventListener('input',()=>{
    if(id==='fthresh') $('#fthreshv').textContent=$('#fthresh').value;
    if(id==='fdepth'){
      // The threshold means different things at each depth: a hard cutoff at 1bpp, a
      // black point at 2bpp. Carrying 128 across would flatten every antialiased sample
      // and make 2bpp look identical to 1bpp at twice the bytes.
      const two=$('#fdepth').value==='2';
      $('#fthresh').value=two?24:128;
      $('#fthreshv').textContent=$('#fthresh').value;
    }
    if(id==='fbg'||id==='fbox') fontModes();
    refreshFont();
  });

$('#fdlg').addEventListener('change',()=>{
  const v=$('#fdlg').value;
  if(v){$('#ftext').value=decodeURIComponent(v); refreshFont()}
});

$('#faddbtn').onclick=async()=>{
  const name=$('#fname').value.trim(), lic=$('#flic').value.trim();
  if(!name){alert('Name the font first.');return}
  if(!lic){alert('A licence is required — rasterised glyphs are redistribution.');return}
  const r=await (await fetch('/api/font',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({...fontSpec(),name,license:lic})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  log.textContent=r.ok
    ?`Added [[font]] "${name}". Add it to a scene's fonts = [...] and press Build.`
    :r.error;
  if(r.ok){await load(); declared()}
};

// --------------------------------------------------------------- toolchain view
let sdkPoll=null;

// ----------------------------------------------------------------- heartbeat
//
// The server has no other way to know this page is still here. Without it, closing the
// tab left the editor running and holding its port, so the next launch found something
// listening and refused to start -- "closed" and "still running" looked identical.
//
// The goodbye goes through sendBeacon because a normal fetch is cancelled when the page
// unloads; a beacon is queued by the browser and delivered anyway. It only shortens the
// server's grace period, so a second tab's heartbeat can still cancel the shutdown.
function startHeartbeat(){
  setInterval(()=>{ fetch('/api/alive').catch(()=>{}) }, 5000);
  addEventListener('pagehide',()=>{
    try{ navigator.sendBeacon('/api/bye') }catch(_){}
  });
}

// ------------------------------------------------------------------- updates
//
// Check, download, install: three steps, each one the user's. The editor carries the
// engine a project compiles against, so an upgrade that happened by itself would change
// what a build produces without anyone asking for it.

let UPD=null, updPoll=null;

function updRender(){
  const u=UPD||{};
  const info=$('#updinfo');
  if(!u.checked){
    info.innerHTML=`<div><span class="k">running</span> <b>${u.current||'—'}</b></div>`
      +`<small>${u.why||'not checked yet'}</small>`;
  }else if(!u.available){
    info.innerHTML=`<div><span class="k">running</span> <b>${u.current}</b></div>`
      +`<small>up to date</small>`;
  }else{
    info.innerHTML=`<div><span class="k">running</span> <b>${u.current}</b></div>`
      +`<div><span class="k">available</span> <b>${u.version}`
      +`${u.prerelease?' <small>(prerelease)</small>':''}</b></div>`
      +`<small>${u.asset.name} — ${(u.asset.bytes/1048576).toFixed(0)} MB</small>`;
  }
  $('#upddl').style.display=u.available&&!(u.dl&&u.dl.ready)?'':'none';
  $('#updapply').style.display=(u.dl&&u.dl.ready)?'':'none';
  $('#updnotes').style.display=u.available&&u.notes?'':'none';

  // The banner is the only part of this that goes looking for attention, so it appears
  // once per version and stays gone once dismissed.
  const show=u.available&&sessionStorage.getItem('updhide')!==u.version;
  $('#updbanner').style.display=show?'flex':'none';
  if(show){
    $('#updbannertext').innerHTML=
      `<b>${u.version}</b> is available — you are running ${u.current}.`;
  }
}

async function updCheck(force){
  try{
    UPD=await (await fetch('/api/update'+(force?'/check':''),
      {method:force?'POST':'GET'})).json();
  }catch(_){ return }
  UPD.dl=await (await fetch('/api/update/progress')).json();
  updRender();
}

function updWatch(){
  clearInterval(updPoll);
  updPoll=setInterval(async()=>{
    const d=await (await fetch('/api/update/progress')).json();
    UPD.dl=d;
    $('#updbar').style.display=d.busy?'':'none';
    $('#updfill').style.width=(d.pct||0)+'%';
    if(!d.busy){
      clearInterval(updPoll);
      $('#updbar').style.display='none';
      if(d.error){
        $('#updinfo').innerHTML+=`<small class="bad">${d.error}</small>`;
      }
      updRender();
    }
  },400);
}

$('#updcheck').onclick=()=>updCheck(true);
$('#upddl').onclick=async()=>{
  await fetch('/api/update/download',{method:'POST'});
  $('#updbar').style.display=''; updWatch();
};
$('#updapply').onclick=async()=>{
  const r=await (await fetch('/api/update/apply',{method:'POST'})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  log.textContent=r.ok?r.message:r.error;
  // Its own block: appended inline it ran straight on from the asset size above it,
  // which read as one sentence about a file rather than a result.
  $('#updinfo').innerHTML+=`<div style="margin-top:.4rem"><small class="${r.ok?'':'bad'}">`
    +`${r.ok?r.message:r.error}</small></div>`;
};
$('#updnotes').onclick=()=>{
  const b=$('#updbody');
  b.style.display=b.style.display==='none'?'':'none';
  b.textContent=(UPD&&UPD.notes)||'';
};
$('#updbannergo').onclick=()=>{ showTab('sdk'); $('#updbanner').style.display='none' };
$('#updbannerhide').onclick=()=>{
  if(UPD&&UPD.version) sessionStorage.setItem('updhide',UPD.version);
  $('#updbanner').style.display='none';
};

async function sdkStatus(remote){
  const s=await (await fetch('/api/sdk/status'+(remote?'?remote=1':''))).json();
  const row=(k,v,cls)=>`<div><span class="k">${k}</span> <span class="${cls||''}">${v}</span></div>`;

  const d=S.data||{}, p=d.paths||{}, e=d.engine||{};
  const pct=d.budget?(100*d.used/d.budget).toFixed(1):'0';
  $('#projinfo').innerHTML= d.no_project
    ? '<div>No project open. Open a folder, or create one.</div>'
    : row('name', d.name||'—')+
      row('folder', `<span class="p">${p.root||'—'}</span>`)+
      row('manifest', `<span class="p">${p.manifest||'—'}</span>`)+
      row('header', `<span class="p">${p.header||'—'}</span>`)+
      row('resources', `${(d.used||0).toLocaleString()} / ${(d.budget||0).toLocaleString()} B (${pct}%)`)+
      row('app binary', d.app&&d.app.known
        ? `${d.app.used.toLocaleString()} / ${d.app.limit.toLocaleString()} B `
          + `(${d.app.pct.toFixed(1)}%)`
        : `<span class="dim">${(d.app&&d.app.why)||'not measured'}</span>`)+
      // The engine is staged from the editor at each build, so what matters is whether
      // this project last built against a different one.
      row('engine', e.linked
        ? `${e.editor} (live tree, symlinked)`
        : (e.changed
            ? `<span class="no">built against ${e.built_against}, editor has ${e.editor}</span>`
            : `${e.editor}${e.built_against?'':' (not built yet)'}`));

  $('#projadopt').style.display=
    (!d.no_project && d.project_file && !d.project_file.format) ? '' : 'none';

  // Engine ownership. Unlocking is not just write permission: it stops the editor
  // restaging the engine, so the project keeps its changes and stops getting fixes.
  // Both halves of that trade are stated, because only stating the first would be a
  // pleasant surprise followed by an unpleasant one.
  const owned=!!e.owned;
  $('#engstate').innerHTML=
    row('source', e.linked?'symlinked to a live tree'
        :(owned?'owned by this project':'staged from the editor'), owned?'no':'yes')+
    row('version', owned?`forked from ${e.owned_from||'?'}`:(e.editor||'—'))+
    (owned&&e.owned_at?row('since', e.owned_at):'');
  $('#engprose').textContent = e.linked
    ? 'This project points at a live engine tree, so edits are picked up by the next build already.'
    : (owned
      ? 'Restaging is off for this project. Your edits under src/c/pnx are kept and are '
        + 'compiled into every build — and this project no longer receives engine fixes '
        + 'from editor updates. Re-tracking discards them.'
      : 'The engine is restaged from the editor before every build, so edits under '
        + 'src/c/pnx would be silently overwritten. Taking ownership stops the restaging '
        + 'and hands this project its own copy — after which it stops receiving engine '
        + 'fixes when the editor updates.');
  $('#engown').checked=owned;
  $('#engown').disabled=!!e.linked;
  $('#engresync').style.display=owned?'':'none';

  const rec=await (await fetch('/api/project/recent')).json();
  $('#recent').innerHTML=rec.recent.length
    ? rec.recent.map(r=>`<button data-path="${r.path}">${r.name} — ${r.path}</button>`).join('')
    : '<small>—</small>';
  for(const b of $('#recent').querySelectorAll('button'))
    b.onclick=()=>openProject(b.dataset.path);
  $('#sdkstatus').innerHTML=
    row('pebble tool', s.pebble||'not installed', s.pebble?'yes':'no')+
    row('active SDK', s.active||'none', s.active?'yes':'no')+
    row('installed', s.installed.length?s.installed.join(', '):'none')+
    row('can build a .pbw', s.can_build?'yes':'no', s.can_build?'yes':'no')+
    (s.newer?row('newer available', s.newer):'')+
    (!s.pebble&&!s.installer
      ? row('installer', 'none found — install uv, pipx or pip', 'no') : '');

  if(!$('#sdkterms').children.length)
    $('#sdkterms').innerHTML=s.terms.map(([t,u])=>
      `<a href="${u}" target="_blank" rel="noopener">${t} &nearr;</a>`).join('');

  // An acceptance already on record stays ticked and locked: it is a statement the user
  // made, not a form field to toggle.
  if(s.accepted){ $('#sdkaccept').checked=true; $('#sdkaccept').disabled=true; }
  $('#sdkinstall').disabled=s.busy||!$('#sdkaccept').checked;
  $('#sdkinstall').textContent=s.busy?'Installing…'
    :(s.active?'Reinstall / update the SDK':'Install the SDK');
  if(s.log&&s.log.trim()) $('#sdklog').textContent=s.log;

  // Poll only while something is actually running.
  if(s.busy&&!sdkPoll) sdkPoll=setInterval(()=>sdkStatus(),1500);
  if(!s.busy&&sdkPoll){ clearInterval(sdkPoll); sdkPoll=null; }
  return s;
}

$('#sdkaccept').onchange=async()=>{
  if(!$('#sdkaccept').checked){ $('#sdkinstall').disabled=true; return }
  await fetch('/api/sdk/accept',{method:'POST'});
  $('#sdkaccept').disabled=true;
  $('#sdkinstall').disabled=false;
};

$('#sdkinstall').onclick=async()=>{
  $('#sdkinstall').disabled=true;
  $('#sdklog').textContent='Starting…\n';
  const r=await (await fetch('/api/sdk/install',{method:'POST',
    headers:{'content-type':'application/json'},body:'{}'})).json();
  if(r.error){ $('#sdklog').textContent=r.error; $('#sdkinstall').disabled=false; return }
  sdkStatus();
};

$('#sdkrefresh').onclick=()=>sdkStatus(true);

// ------------------------------------------------------------------ sprite editor
//
// Pixels are held as ARGB2222 bytes -- the device's own encoding -- not as CSS colours.
// Painting in the target colour space means the canvas cannot show a colour the watch
// cannot, so nothing collapses on import.
const PX={w:16,h:24,frames:1,zoom:12,data:null,colour:0xFF,tool:'pen',undo:[]};

function pxTotalH(){ return PX.h*PX.frames }

function pxInit(w,h,frames){
  PX.w=w; PX.h=h; PX.frames=frames;
  PX.data=new Uint8Array(w*pxTotalH());   // 0 is transparent, as everywhere else
  PX.undo=[];
  pxDraw();
}

function pxSnapshot(){
  PX.undo.push(PX.data.slice());
  if(PX.undo.length>40) PX.undo.shift();   // bounded: this is a scratch tool
}

function pxDraw(){
  const z=PX.zoom, H=pxTotalH();
  for(const [id,scale] of [['pxcv',z],['pxcv1',1]]){
    const cv=$('#'+id); cv.width=PX.w*scale; cv.height=H*scale;
    const g=cv.getContext('2d'); g.imageSmoothingEnabled=false;
    g.clearRect(0,0,cv.width,cv.height);
    for(let y=0;y<H;y++)for(let x=0;x<PX.w;x++){
      const v=PX.data[y*PX.w+x];
      if(!v) continue;
      g.fillStyle=argbCss(v);
      g.fillRect(x*scale,y*scale,scale,scale);
    }
    if(scale>3&&$('#pxgrid').checked&&id==='pxcv'){
      g.strokeStyle='rgba(128,128,128,.28)'; g.lineWidth=1;
      for(let x=0;x<=PX.w;x++){g.beginPath();g.moveTo(x*scale+.5,0);g.lineTo(x*scale+.5,H*scale);g.stroke()}
      for(let y=0;y<=H;y++){g.beginPath();g.moveTo(0,y*scale+.5);g.lineTo(PX.w*scale,y*scale+.5);g.stroke()}
      // Frame boundaries drawn stronger, since that is the division the importer reads.
      g.strokeStyle='var(--accent)'; g.strokeStyle='rgba(85,170,255,.9)'; g.lineWidth=2;
      for(let f=1;f<PX.frames;f++){
        g.beginPath(); g.moveTo(0,f*PX.h*scale); g.lineTo(PX.w*scale,f*PX.h*scale); g.stroke();
      }
    }
  }
}

function argbCss(v){
  const r=((v>>4)&3)*85,g=((v>>2)&3)*85,b=(v&3)*85;
  return `rgb(${r},${g},${b})`;
}

function pxFill(x,y,target){
  // Iterative flood fill: a recursive one blows the stack on a full 128x128 canvas.
  if(target===PX.colour) return;
  const H=pxTotalH(), stack=[[x,y]];
  while(stack.length){
    const [cx,cy]=stack.pop();
    if(cx<0||cy<0||cx>=PX.w||cy>=H) continue;
    const i=cy*PX.w+cx;
    if(PX.data[i]!==target) continue;
    PX.data[i]=PX.colour;
    stack.push([cx+1,cy],[cx-1,cy],[cx,cy+1],[cx,cy-1]);
  }
}

function pxAt(e){
  const r=$('#pxcv').getBoundingClientRect();
  return [Math.floor((e.clientX-r.left)/PX.zoom), Math.floor((e.clientY-r.top)/PX.zoom)];
}

let pxDown=false;
function pxPaint(e,first){
  const [x,y]=pxAt(e);
  if(x<0||y<0||x>=PX.w||y>=pxTotalH()) return;
  const i=y*PX.w+x;
  if(PX.tool==='pick'){ pxSetColour(PX.data[i]); return }
  if(first) pxSnapshot();
  if(PX.tool==='fill') pxFill(x,y,PX.data[i]);
  else PX.data[i]= PX.tool==='erase' ? 0 : PX.colour;
  pxDraw();
}
$('#pxcv').addEventListener('mousedown',e=>{pxDown=true; pxPaint(e,true)});
$('#pxcv').addEventListener('mousemove',e=>{if(pxDown&&PX.tool!=='fill')pxPaint(e,false)});
addEventListener('mouseup',()=>{pxDown=false});

function pxSetColour(v){
  PX.colour=v;
  for(const el of $('#pxpal').querySelectorAll('i'))
    el.className=(+el.dataset.v===v?'on':'')+(+el.dataset.v===0?' tr':'');
  $('#pxcur').textContent = v ? `0x${v.toString(16).toUpperCase()} ${argbCss(v)}`
                              : 'transparent';
}

function pxPalette(){
  // Transparent first, then the 64 opaque ARGB2222 values.
  const cells=[{v:0,css:''}].concat(argbSwatches().map(s=>({v:s.v,css:s.css})));
  $('#pxpal').innerHTML=cells.map(c=>
    `<i data-v="${c.v}" class="${c.v?'':'tr'}" style="${c.v?`background:${c.css}`:''}"
       title="${c.v?'0x'+c.v.toString(16).toUpperCase():'transparent'}"></i>`).join('');
  for(const el of $('#pxpal').querySelectorAll('i'))
    el.onclick=()=>pxSetColour(+el.dataset.v);
  pxSetColour(0xFF);
}

for(const [id,tool] of [['toolpen','pen'],['toolfill','fill'],['toolpick','pick'],
                        ['toolerase','erase']])
  $('#'+id).onclick=()=>{
    PX.tool=tool;
    for(const t of ['toolpen','toolfill','toolpick','toolerase'])
      $('#'+t).className = t===id ? 'primary' : '';
  };

$('#pxundo').onclick=()=>{ if(PX.undo.length){ PX.data=PX.undo.pop(); pxDraw() } };
$('#pxclear').onclick=()=>{ pxSnapshot(); PX.data.fill(0); pxDraw() };
for(const id of ['pxw','pxh','pxframes'])
  $('#'+id).addEventListener('change',()=>
    pxInit(+$('#pxw').value,+$('#pxh').value,+$('#pxframes').value));
$('#pxzoom').addEventListener('input',()=>{PX.zoom=+$('#pxzoom').value; pxDraw()});
$('#pxgrid').addEventListener('change',pxDraw);

async function pxLoadList(){
  const files=await (await fetch('/api/art')).json();
  $('#pxopen').innerHTML='<option value="">—</option>'+
    files.map(f=>`<option value="${f.path}">${f.path}</option>`).join('');
}

$('#pxopen').addEventListener('change',async()=>{
  const path=$('#pxopen').value; if(!path) return;
  const r=await (await fetch('/api/sprite/read',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({path})})).json();
  if(r.error){ $('#pxnote').textContent=r.error; return }
  // Height is assumed to be whole frames of the current frame height where it divides
  // cleanly -- the importer's own convention -- and one frame otherwise.
  const frames=(PX.h && r.h % PX.h===0) ? r.h/PX.h : 1;
  PX.w=r.w; PX.h=r.h/frames; PX.frames=frames;
  $('#pxw').value=PX.w; $('#pxh').value=PX.h; $('#pxframes').value=frames;
  PX.data=Uint8Array.from(r.pixels); PX.undo=[];
  $('#pxname').value=path;
  pxDraw();
  $('#pxnote').textContent=`Loaded ${r.w}x${r.h}.`;
});

$('#pxsave').onclick=async()=>{
  let path=$('#pxname').value.trim();
  if(!path){ $('#pxnote').textContent='Give it a filename first.'; return }
  if(!path.includes('/')) path='art/'+path;
  const r=await (await fetch('/api/sprite/write',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({path,w:PX.w,h:pxTotalH(),pixels:Array.from(PX.data)})})).json();
  $('#pxnote').textContent=r.error?r.error
    :`Saved ${r.path} (${r.bytes} B). Import it from the Import tab.`;
  if(r.ok) pxLoadList();
};

// -------------------------------------------------------------------- code editor
//
// Highlighting and analysis are deliberately bare: one tokenising pass and three checks.
// Not a parser, and it does not try to be -- the value is in catching the cheap mistakes
// before an ARM compile does, and an ARM compile is the authority on everything else.
const CODE={path:null,clean:'',editable:false,symbols:null};

const C_KEYWORDS=new Set(('if else for while do switch case default break continue return '
 +'goto sizeof typedef struct union enum static const volatile extern inline register '
 +'restrict auto _Static_assert').split(' '));
const C_TYPES=new Set(('void char short int long float double signed unsigned bool '
 +'size_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t '
 +'ptrdiff_t intptr_t uintptr_t').split(' '));

const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// One left-to-right pass. Comments and strings are consumed whole so that a keyword or
// a brace inside them is never seen by anything downstream -- which is also what makes
// the brace check below trustworthy.
function cTokens(src){
  const out=[]; let i=0, n=src.length;
  const push=(cls,text)=>out.push({cls,text});
  while(i<n){
    const c=src[i], two=src.substr(i,2);
    if(two==='/*'){ const e=src.indexOf('*/',i+2); const j=e<0?n:e+2;
      push('tk-c',src.slice(i,j)); i=j; continue }
    if(two==='//'){ let j=src.indexOf('\n',i); if(j<0)j=n;
      push('tk-c',src.slice(i,j)); i=j; continue }
    if(c==='"'||c==="'"){
      let j=i+1;
      while(j<n){ if(src[j]==='\\'){j+=2;continue} if(src[j]===c){j++;break}
                  if(src[j]==='\n')break; j++ }
      push('tk-s',src.slice(i,j)); i=j; continue;
    }
    if(c==='#'&&(i===0||src[i-1]==='\n')){
      let j=src.indexOf('\n',i); if(j<0)j=n;
      push('tk-p',src.slice(i,j)); i=j; continue;
    }
    if(/[A-Za-z_]/.test(c)){
      let j=i; while(j<n&&/[A-Za-z0-9_]/.test(src[j]))j++;
      const w=src.slice(i,j);
      push(C_KEYWORDS.has(w)?'tk-k':C_TYPES.has(w)?'tk-t'
           :/^(pnx|Pnx|PNX)/.test(w)?'tk-x':'', w);
      i=j; continue;
    }
    if(/[0-9]/.test(c)){
      let j=i; while(j<n&&/[0-9a-fA-FxXuUlL.]/.test(src[j]))j++;
      push('tk-n',src.slice(i,j)); i=j; continue;
    }
    let j=i; while(j<n&&!/[A-Za-z0-9_#"'\\/]/.test(src[j]))j++;
    if(j===i)j=i+1;
    push('',src.slice(i,j)); i=j;
  }
  return out;
}

function highlight(){
  const src=$('#codetext').value;
  const bad=new Set(CODE.diagBad||[]);
  const html=cTokens(src).map(t=>{
    const cls=(t.cls==='tk-x'&&bad.has(t.text))?'tk-x tk-bad':t.cls;
    return cls?`<span class="${cls}">${esc(t.text)}</span>`:esc(t.text);
  }).join('');
  // A trailing newline keeps the last line scrollable to, matching the textarea.
  $('#codehl').querySelector('code').innerHTML=html+'\n';
  $('#codehl').dataset.ro=CODE.editable?'0':'1';
  // The textarea grows to its content so the wrapper scrolls both layers as one.
  const ta=$('#codetext');
  ta.style.height='auto';
  ta.style.height=Math.max(ta.scrollHeight,$('#codescroll').clientHeight)+'px';
}

// Three checks. Balance, unterminated strings, and unknown engine symbols -- the last
// being the one that earns its place: it catches `pnx_platform_exit` for
// `pnx_platform_quit` in the editor rather than after a full ARM compile.
function codeAnalyse(){
  const src=$('#codetext').value;
  const toks=cTokens(src);
  const diags=[];
  const lineOf=off=>src.slice(0,off).split('\n').length;

  let off=0, depth={'(':0,'[':0,'{':0}, opens=[];
  const close={')':'(',']':'[','}':'{'};
  for(const t of toks){
    if(t.cls==='tk-c'||t.cls==='tk-s'||t.cls==='tk-p'){
      if(t.cls==='tk-s'&&t.text.length<2)
        diags.push({line:lineOf(off),msg:'unterminated string or character literal'});
      off+=t.text.length; continue;
    }
    for(let k=0;k<t.text.length;k++){
      const ch=t.text[k];
      if(ch in depth){ depth[ch]++; opens.push({ch,off:off+k}) }
      else if(ch in close){
        const want=close[ch];
        if(depth[want]===0)
          diags.push({line:lineOf(off+k),msg:`unmatched '${ch}'`});
        else { depth[want]--;
          for(let z=opens.length-1;z>=0;z--) if(opens[z].ch===want){opens.splice(z,1);break} }
      }
    }
    off+=t.text.length;
  }
  for(const o of opens)
    diags.push({line:lineOf(o.off),msg:`'${o.ch}' is never closed`});

  const bad=[];
  if(CODE.symbols){
    const seen=new Set();
    let p=0;
    for(const t of toks){
      if(t.cls==='tk-x'&&!CODE.symbols.has(t.text)&&!seen.has(t.text)){
        seen.add(t.text); bad.push(t.text);
        diags.push({line:lineOf(p),
                    msg:`unknown engine symbol '${t.text}'`+nearest(t.text)});
      }
      p+=t.text.length;
    }
  }
  CODE.diagBad=bad;

  diags.sort((a,b)=>a.line-b.line);
  CODE.quick=diags;
  paintDiags();
}

// The compiler's diagnostics and the in-page ones share a panel, tagged by where they
// came from. They answer different questions -- the page checks what you are typing right
// now, the compiler checks what the file MEANS -- and merging them without saying which
// is which would make a stale compiler result look live.
function paintDiags(){
  const quick=(CODE.quick||[]).map(d=>({...d, src:'edit'}));
  const cc=(CODE.cc||[]).map(d=>({...d, src:d.level==='warning'?'warn':'cc'}));
  const all=[...cc, ...quick].sort((a,b)=>(a.line||0)-(b.line||0));

  $('#codediag').innerHTML=(CODE.ccnote?`<div class="note"><i></i><span>`
      +`${esc(CODE.ccnote)}</span></div>`:'')
    + all.slice(0,60).map(d=>
      `<div data-line="${d.line||0}"><i>${d.line?'line '+d.line:'—'}</i>`
      +`<b class="${d.src}">${d.src==='cc'?'error':(d.src==='warn'?'warn':'·')}</b>`
      +`<span>${esc(d.msg)}${d.note?' — '+esc(d.note):''}</span></div>`).join('');

  for(const el of $('#codediag').querySelectorAll('div[data-line]'))
    el.onclick=()=>{ const n=+el.dataset.line; if(n) gotoLine(n) };
}

// The compiler runs on the SAVED file, so it runs after a save and on open -- not on
// every keystroke, which would compile a file mid-word and report nonsense.
async function codeLint(){
  if(!CODE.path||!/\.(c|h)$/.test(CODE.path)){ CODE.cc=[]; CODE.ccnote=''; return }
  let r;
  try{
    r=await (await fetch('/api/code/lint',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({path:CODE.path})})).json();
  }catch(_){ return }
  CODE.cc=r.ok?r.diags:[];
  CODE.ccnote=r.ok
    ? (r.clean?`${r.compiler}: no complaints`:'')
    : r.why;
  paintDiags();
}

// Edit distance, not shared prefix. Prefix length looks reasonable and is not: every
// `pnx_platform_*` shares thirteen characters, so `pnx_platform_exit` matched
// `pnx_platform_audio_close` as readily as `pnx_platform_quit`. Distance ranks by how
// wrong the name actually is, which is the question being asked.
function editDistance(a,b,cap){
  if(Math.abs(a.length-b.length)>cap) return cap+1;
  let prev=Array.from({length:b.length+1},(_,i)=>i);
  for(let i=1;i<=a.length;i++){
    const cur=[i]; let best=i;
    for(let j=1;j<=b.length;j++){
      cur[j]=Math.min(prev[j]+1, cur[j-1]+1, prev[j-1]+(a[i-1]===b[j-1]?0:1));
      if(cur[j]<best) best=cur[j];
    }
    if(best>cap) return cap+1;      // whole row already too far; stop early
    prev=cur;
  }
  return prev[b.length];
}

function nearest(name){
  if(!CODE.symbols) return '';
  // A quarter of the name, capped: enough for a wrong suffix or a transposition,
  // not enough to suggest something unrelated with confidence.
  const cap=Math.min(5,Math.max(2,Math.floor(name.length/4)));
  let best=null, bestD=cap+1;
  for(const s of CODE.symbols){
    const d=editDistance(name,s,cap);
    if(d<bestD){ bestD=d; best=s; if(d===1) break }
  }
  return best?` — did you mean '${best}'?`:'';
}

function gotoLine(n){
  const ta=$('#codetext'), lines=ta.value.split('\n');
  let off=0; for(let i=0;i<n-1&&i<lines.length;i++) off+=lines[i].length+1;
  ta.focus(); ta.setSelectionRange(off,off+ (lines[n-1]||'').length);
}

// A real tree, not a flat list under directory headings. `src/c/pnx` alone is seven
// directories deep in places, and a flat list makes a project's own two files look like
// part of the engine. Collapsed state is kept per folder, and the engine subtree starts
// closed: it is read-only, it is the biggest thing here, and it is not what someone
// opened the tab to edit.
const CODEOPEN=new Set(['src','src/c']);

function codeNest(files){
  const root={dirs:new Map(), files:[]};
  for(const f of files){
    let node=root;
    const parts=f.path.replace(/\\/g,'/').split('/');
    const name=parts.pop();
    let sofar='';
    for(const p of parts){
      sofar=sofar?sofar+'/'+p:p;
      if(!node.dirs.has(p)) node.dirs.set(p,{dirs:new Map(),files:[],path:sofar});
      node=node.dirs.get(p);
    }
    node.files.push({...f,name});
  }
  return root;
}

function codeRender(node, depth){
  let html='';
  for(const [name,dir] of [...node.dirs.entries()].sort((a,b)=>a[0].localeCompare(b[0]))){
    const open=CODEOPEN.has(dir.path);
    const engine=dir.path.startsWith('src/c/pnx');
    html+=`<div class="cdir${engine?' ro':''}" data-dir="${dir.path}"
      style="padding-left:${depth*.7}rem">${open?'▾':'▸'} ${name}`
      +`${engine&&depth<3?' <small>engine</small>':''}</div>`;
    if(open) html+=codeRender(dir, depth+1);
  }
  for(const f of node.files.sort((a,b)=>a.name.localeCompare(b.name))){
    html+=`<button data-path="${f.path}" class="${f.editable?'':'ro'}"
      style="padding-left:${depth*.7+.85}rem">${f.name}`
      +`${f.generated?' <small>gen</small>':''}</button>`;
  }
  return html;
}

async function codeTree(){
  if(!CODE.symbols){
    try{ CODE.symbols=new Set(await (await fetch('/api/code/symbols')).json()) }
    catch(_){ CODE.symbols=null }
  }
  if(!CODE.files) CODE.files=await (await fetch('/api/code/tree')).json();

  $('#codelist').innerHTML=codeRender(codeNest(CODE.files), 0);
  for(const b of $('#codelist').querySelectorAll('button'))
    b.onclick=()=>codeOpen(b.dataset.path);
  for(const d of $('#codelist').querySelectorAll('.cdir'))
    d.onclick=()=>{
      const p=d.dataset.dir;
      CODEOPEN.has(p)?CODEOPEN.delete(p):CODEOPEN.add(p);
      codeTree();
      if(CODE.path)
        for(const b of $('#codelist').querySelectorAll('button'))
          b.classList.toggle('on', b.dataset.path===CODE.path);
    };
}

async function codeOpen(path){
  if(CODE.path && $('#codetext').value!==CODE.clean &&
     !confirm('Discard unsaved changes?')) return;
  const r=await (await fetch('/api/code/read',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({path})})).json();
  if(r.error){ $('#codenote').textContent=r.error; return }
  CODE.path=path; CODE.clean=r.text; CODE.editable=r.editable;
  $('#codetext').value=r.text;
  $('#codetext').readOnly=!r.editable;
  $('#codepath').textContent=path;
  $('#codenote').textContent=r.note||'';
  $('#codesave').disabled=true;
  for(const b of $('#codelist').querySelectorAll('button'))
    b.classList.toggle('on', b.dataset.path===path);
  codeDirty();
  // Analyse first: highlighting reads its list of unknown symbols.
  if(/\.(c|h)$/.test(path)){ codeAnalyse(); codeLint(); }
  else { CODE.diagBad=[]; CODE.cc=[]; CODE.quick=[]; CODE.ccnote=''; paintDiags() }
  highlight();
  $('#codescroll').scrollTop=0;
}

function codeDirty(){
  const dirty=CODE.editable && $('#codetext').value!==CODE.clean;
  $('#codedirty').textContent=dirty?'● unsaved':'';
  $('#codesave').disabled=!dirty;
}
let codeTimer=null;
$('#codetext').addEventListener('input',()=>{
  codeDirty();
  highlight();                       // immediate: the overlay must not lag the caret
  clearTimeout(codeTimer);           // analysis can wait for a pause in typing
  codeTimer=setTimeout(()=>{
    if(/\.(c|h)$/.test(CODE.path||'')){ codeAnalyse(); highlight() }
  },300);
});
$('#codetext').addEventListener('scroll',()=>{
  // Only the wrapper scrolls, but a long line can still shift the textarea itself.
  $('#codehl').style.transform=`translateX(${-$('#codetext').scrollLeft}px)`;
});

// Tab inserts a tab rather than leaving the field, which is the single thing that makes
// a textarea usable for code at all.
$('#codetext').addEventListener('keydown',e=>{
  if(e.key==='Tab'){
    e.preventDefault();
    const t=e.target, s=t.selectionStart, en=t.selectionEnd;
    t.value=t.value.slice(0,s)+'  '+t.value.slice(en);
    t.selectionStart=t.selectionEnd=s+2;
    codeDirty();
  }
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){ e.preventDefault(); codeSave() }
});

async function codeSave(){
  if(!CODE.path||!CODE.editable) return;
  const text=$('#codetext').value;
  const r=await (await fetch('/api/code/write',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({path:CODE.path,text})})).json();
  if(r.error){ $('#codenote').textContent=r.error; return }
  CODE.clean=text; codeDirty();
  $('#codenote').textContent=`saved ${r.bytes} B`;
  codeLint();          // the compiler reads the file, so this is when it can say anything
}
$('#codesave').onclick=codeSave;

// ----------------------------------------------------------------- project picker
let pickerMode='open';

async function openProject(path){
  const r=await (await fetch('/api/project/open',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({path})})).json();
  if(!r.ok){ $('#pickernote').textContent=r.error; return }
  // A different project means every cached atlas, map and palette is wrong, so the
  // simplest correct thing is to start over.
  location.reload();
}

async function drawPicker(path){
  const b=await (await fetch('/api/project/browse'+(path?'?path='+encodeURIComponent(path):''))).json();
  $('#pickerpath').value=b.path;
  $('#pickerlist').innerHTML=b.entries.length
    ? b.entries.map(e=>`<button data-path="${e.path}" class="${e.project?'isproj':''}"
        >${e.project?'◆':'▸'} ${e.name}</button>`).join('')
    : '<small>(no subfolders)</small>';
  for(const el of $('#pickerlist').querySelectorAll('button'))
    el.onclick=()=>drawPicker(el.dataset.path);

  if(pickerMode==='open'){
    $('#pickerok').disabled=!b.is_project;
    $('#pickernote').textContent=b.is_project
      ? 'This folder is a project.'
      : 'Not a project — pick a folder containing a .pknproj or an assets.toml. ◆ marks one.';
  }else{
    $('#pickerok').disabled=false;
    $('#pickernote').textContent='A new folder is created inside this one.';
  }
  return b;
}

function showPicker(mode){
  pickerMode=mode;
  $('#picker').style.display='';
  $('#pickertitle').textContent=mode==='open'?'Open a project':'New project';
  $('#newfields').style.display=mode==='new'?'':'none';
  $('#pickerok').textContent=mode==='open'?'Open this folder':'Create it here';
  drawPicker((S.data&&S.data.paths&&S.data.paths.root)||null);
}

$('#projopen').onclick=()=>showPicker('open');
$('#projnew').onclick=()=>showPicker('new');
$('#pickercancel').onclick=()=>{$('#picker').style.display='none'};
$('#pickergo').onclick=()=>drawPicker($('#pickerpath').value);
$('#pickerup').onclick=async()=>{
  const b=await (await fetch('/api/project/browse?path='+
    encodeURIComponent($('#pickerpath').value))).json();
  if(b.parent) drawPicker(b.parent);
};

$('#pickerok').onclick=async()=>{
  if(pickerMode==='open'){ openProject($('#pickerpath').value); return }
  const name=$('#newname').value.trim();
  const folder=$('#newfolder').value.trim()||name.toLowerCase().replace(/[^a-z0-9]+/g,'-');
  if(!name){ $('#pickernote').textContent='Name the project first.'; return }
  const r=await (await fetch('/api/project/create',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({parent:$('#pickerpath').value,folder,name,
                         author:$('#newauthor').value})})).json();
  if(!r.ok){ $('#pickernote').textContent=r.error; return }
  location.reload();
};

$('#projadopt').onclick=async()=>{
  await fetch('/api/project/adopt',{method:'POST'});
  location.reload();
};

async function engineOwn(on){
  await fetch('/api/engine/own',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({on})});
  location.reload();
}
$('#engown').onchange=()=>{
  if($('#engown').checked) return engineOwn(true);
  $('#engown').checked=true;      // re-tracking is destructive; route it through the button
  alert('Use "Discard changes and re-track" — going back replaces your engine copy.');
};
$('#engresync').onclick=()=>{
  if(confirm('Replace src/c/pnx with the editor engine copy? Your modifications there '
             +'are discarded.')) engineOwn(false);
};

load();
</script></body></html>"""


TOOLCHAIN = Toolchain()


class Session:
    """The project currently open, and the ones opened before.

    A mutable holder rather than a captured value, because the editor can now switch
    projects without restarting -- the request handlers all read through this.
    """

    RECENT_MAX = 12

    def __init__(self, proj=None):
        self.proj = proj
        if proj:
            self.remember(proj.root)

    def _recent_path(self):
        return os.path.join(_config_dir(), "recent.json")

    def recent(self):
        try:
            with open(self._recent_path()) as f:
                paths = json.load(f)
        except Exception:                                # noqa: BLE001
            return []
        # Filter as they are read: a project deleted or on an unmounted drive should
        # quietly leave the list rather than sit there failing to open.
        out = []
        for p in paths:
            if os.path.isdir(p) and pp.looks_like_project(p):
                try:
                    name = pp.load(p).get("name") or os.path.basename(p)
                except Exception:                        # noqa: BLE001
                    name = os.path.basename(p)
                out.append({"path": p, "name": name})
        return out

    def remember(self, root):
        root = os.path.abspath(root)
        paths = [r["path"] for r in self.recent() if r["path"] != root]
        paths.insert(0, root)
        with open(self._recent_path(), "w") as f:
            json.dump(paths[:self.RECENT_MAX], f, indent=2)

    def open(self, folder):
        proj = Project(folder)
        self.proj = proj
        self.remember(proj.root)
        return proj


def browse(path=None):
    """Directory listing for the folder picker.

    A browser cannot see the filesystem and a frozen app cannot rely on a native dialog
    being available, so the server does the listing. Only directories: the thing being
    chosen is a project folder.
    """
    path = os.path.abspath(os.path.expanduser(path or os.path.expanduser("~")))
    if not os.path.isdir(path):
        path = os.path.expanduser("~")

    entries = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                entries.append({"name": name, "path": full,
                                "project": pp.looks_like_project(full)})
    except PermissionError:
        return {"path": path, "parent": os.path.dirname(path), "entries": [],
                "error": "permission denied", "is_project": False}

    return {"path": path, "parent": os.path.dirname(path) if os.path.dirname(path) != path
            else None,
            "entries": entries, "is_project": pp.looks_like_project(path),
            "empty": not os.listdir(path)}


# Requests run on threads now (see EditorServer), but they still touch one Project, so
# the routing bodies are serialised exactly as they were when the server was
# single-threaded. The lock is taken AFTER the request has been read, which is the whole
# point: a connection that stalls mid-request blocks nothing but itself.
REQUEST_LOCK = threading.Lock()


class EditorServer(socketserver.ThreadingTCPServer):
    """One thread per connection, and none of them outlive the process.

    A single-threaded server was wedged permanently by one connection that never
    completed a request -- and browsers make those routinely, opening speculative sockets
    they may never send anything on. The accept loop blocked in readline() waiting for a
    request line that never came, so the editor sat there listening, queueing connections
    and serving nobody: alive, holding its port, answering nothing.

    `daemon_threads` matters as much as the threading: a lingering connection thread must
    never keep the process up after the window is closed.
    """

    daemon_threads = True
    allow_reuse_address = True


def make_handler(session):
    class Handler(http.server.BaseHTTPRequestHandler):
        # A stalled peer now costs one thread for this long rather than the whole editor
        # forever. StreamRequestHandler applies it to the connection socket.
        timeout = 30

        def log_message(self, *a):
            pass

        def do_GET(self):
            with REQUEST_LOCK:
                self._route_get()

        def do_POST(self):
            with REQUEST_LOCK:
                self._route_post()

        def _send(self, code, body, ctype="application/json"):
            # Any answered request counts as the UI being present, not just the
            # heartbeat: a long build or a big font preview must not look like silence.
            LIVE.touch()
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route_get(self):
            if self.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                # No project open is an ordinary state, not an error: the app can be
                # launched from a dock with no arguments. The UI shows the picker.
                self._send(200, json.dumps(session.proj.state() if session.proj
                                           else {"no_project": True}))
            elif self.path == "/api/sheets":
                self._send(200, json.dumps(session.proj.sheets()))
            elif self.path == "/api/fonts":
                self._send(200, json.dumps(session.proj.font_sources()))
            elif self.path.startswith("/api/sdk/status"):
                remote = "remote=1" in self.path
                self._send(200, json.dumps(TOOLCHAIN.status(remote=remote)))
            elif self.path.startswith("/api/project/browse"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                self._send(200, json.dumps(browse((q.get("path") or [None])[0])))
            elif self.path == "/api/alive":
                # The heartbeat. Cheap on purpose: it runs every few seconds forever.
                self._send(200, "{}")
            elif self.path == "/api/ping":
                # So a second launch can recognise the first one without guessing from
                # the shape of a state payload.
                self._send(200, json.dumps({"app": "pebblnyx-editor",
                                            "version": pp.EDITOR_VERSION,
                                            "project": session.proj.root
                                            if session.proj else None}))
            elif self.path == "/api/update":
                # Cached, so the start-up check and every visit to Settings do not each
                # spend one of GitHub's 60 unauthenticated requests an hour.
                self._send(200, json.dumps(UPDATER.check()))
            elif self.path == "/api/update/progress":
                self._send(200, json.dumps(UPDATER.state()))
            elif self.path == "/api/code/tree":
                self._send(200, json.dumps(session.proj.code_tree()))
            elif self.path == "/api/code/symbols":
                self._send(200, json.dumps(session.proj.code_symbols()))
            elif self.path == "/api/art":
                self._send(200, json.dumps(session.proj.art_files()))
            elif self.path == "/api/project/recent":
                self._send(200, json.dumps({
                    "recent": session.recent(),
                    "engine": pp.FRAMEWORK_VERSION,
                    "have_engine": pp.framework_available(),
                    "open": session.proj.root if session.proj else None,
                }))
            else:
                self._send(404, "{}")

        def _route_post(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b"{}"
            try:
                # Before the project guard: a goodbye has nothing to do with a project,
                # and an editor opened on nothing still has to be closable.
                if self.path == "/api/bye":
                    LIVE.goodbye()
                    self._send(200, "{}")
                    return
                # Everything but opening and creating needs a project to act on.
                if (not session.proj
                        and not self.path.startswith("/api/project/")
                        and not self.path.startswith("/api/sdk/")):
                    self._send(200, json.dumps(
                        {"ok": False, "error": "no project is open",
                         "output": "no project is open"}))
                    return
                if self.path == "/api/map":
                    m = json.loads(raw)
                    session.proj.save_map(m["name"], m["rows"], m["start"], m["warps"],
                                  m.get("atlas"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/analyse":
                    d = json.loads(raw)
                    key = d.get("colorkey")
                    self._send(200, json.dumps(session.proj.analyse(
                        d["sheet"], int(d["tile"]), d["region"],
                        int(d["max_tiles"]), tuple(key) if key else None,
                        d.get("exclude", []))))
                elif self.path == "/api/slice":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.slice_grid(
                        d["sheet"], int(d["tile"]), d["region"],
                        d.get("exclude", []), d.get("colorkey"))))
                elif self.path == "/api/atlas":
                    d = json.loads(raw)
                    session.proj.add_atlas(d["name"], d["sheet"], int(d["tile"]),
                                           d["region"], int(d["max_tiles"]),
                                           d.get("exclude", []), d.get("colorkey"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/newmap":
                    d = json.loads(raw)
                    session.proj.add_map(d["name"], int(d["w"]), int(d["h"]), d["atlas"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/font/preview":
                    self._send(200, json.dumps(session.proj.font_preview(json.loads(raw))))
                elif self.path == "/api/font/scene":
                    self._send(200, json.dumps(session.proj.font_scene(json.loads(raw))))
                elif self.path == "/api/font":
                    session.proj.add_font(json.loads(raw))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/orientation":
                    session.proj.set_orientation(json.loads(raw)["orientation"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/update/check":
                    self._send(200, json.dumps(UPDATER.check(force=True)))
                elif self.path == "/api/update/download":
                    self._send(200, json.dumps(UPDATER.start_download()))
                elif self.path == "/api/update/apply":
                    self._send(200, json.dumps(UPDATER.apply()))
                elif self.path == "/api/sdk/accept":
                    TOOLCHAIN.accept()
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/sdk/install":
                    d = json.loads(raw) if raw.strip() else {}
                    TOOLCHAIN.install(d.get("version", "latest"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/project/open":
                    d = json.loads(raw)
                    session.open(d["path"])
                    self._send(200, json.dumps({"ok": True,
                                                "root": session.proj.root}))
                elif self.path == "/api/project/create":
                    d = json.loads(raw)
                    folder = os.path.join(os.path.expanduser(d["parent"]), d["folder"])
                    pp.create(folder, d["name"], d.get("author", ""))
                    session.open(folder)
                    self._send(200, json.dumps({"ok": True, "root": folder}))
                elif self.path == "/api/project/adopt":
                    # Write a .pknproj into a folder that predates it, so an existing
                    # project becomes a first-class one without being recreated.
                    root = session.proj.root
                    meta = dict(session.proj.meta)
                    meta.setdefault("name", os.path.basename(root))
                    meta.setdefault("manifest", os.path.basename(session.proj.path))
                    pp.save(root, meta)
                    session.open(root)
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/engine/own":
                    d = json.loads(raw) if raw.strip() else {}
                    on = bool(d.get("on", True))
                    pp.take_engine_ownership(session.proj.root, on)
                    if not on:
                        # Reattaching means the editor's engine is authoritative again,
                        # so restage immediately rather than at the next build -- the
                        # discard should be visible now, not later.
                        pp.sync_framework(session.proj.root)
                    session.open(session.proj.root)
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/estimate":
                    d = json.loads(raw) if raw.strip() else {}
                    self._send(200, json.dumps(
                        session.proj.estimate(d.get("maps"))))
                elif self.path == "/api/code/read":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.code_read(d["path"])))
                elif self.path == "/api/code/lint":
                    self._send(200, json.dumps(
                        session.proj.code_lint(json.loads(raw)["path"])))
                elif self.path == "/api/code/write":
                    d = json.loads(raw)
                    self._send(200, json.dumps(
                        session.proj.code_write(d["path"], d["text"])))
                elif self.path == "/api/sprite/read":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.sprite_read(d["path"])))
                elif self.path == "/api/sprite/write":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.sprite_write(
                        d["path"], int(d["w"]), int(d["h"]), d["pixels"])))
                elif self.path == "/api/build":
                    self._send(200, json.dumps(session.proj.build()))
                else:
                    self._send(404, "{}")
            except Exception as e:                       # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "error": str(e),
                                            "output": str(e)}))
    return Handler


def find_manifest():
    """Locate a manifest when none was given.

    Running from an IDE's default configuration passes no arguments, and demanding one
    turns "press play" into "go and configure a run target". Searching a few obvious
    places costs nothing and makes the common case work.
    """
    here = os.getcwd()
    roots = [here, os.path.join(TOOLS, "..")]
    seen, found = set(), []

    for base in roots:
        base = os.path.abspath(base)
        for candidate in [os.path.join(base, "assets.toml")]:
            if os.path.exists(candidate) and candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
        ex = os.path.join(base, "examples")
        if os.path.isdir(ex):
            for name in sorted(os.listdir(ex)):
                candidate = os.path.join(ex, name, "assets.toml")
                if os.path.exists(candidate) and candidate not in seen:
                    seen.add(candidate)
                    found.append(candidate)
    return found


# Chromium-family browsers, which all accept `--app=URL`: a window with no tab strip, no
# address bar and its own entry in the task switcher. Ordered by how likely someone is to
# have made it their main browser.
APP_BROWSERS = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
                "brave-browser", "brave", "microsoft-edge-stable", "microsoft-edge",
                "vivaldi", "opera")


def open_app_window(url):
    """A chromeless window from a browser that is already installed.

    The alternative for Linux was bundling a webview: PyGObject through PyInstaller is
    distro-specific and fragile, and Qt's WebEngine is Chromium again at ~200MB, against a
    binary that is currently 20. Both to render a page the machine can already render.

    **It uses the user's OWN browser profile, deliberately.** An earlier version passed
    `--user-data-dir` to get a private profile, because a Chromium that is already running
    hands the URL to the existing process and exits, and this wanted a process of its own
    to wait on. That took down a compositor: a fresh profile has no GPU preferences, so
    Chromium re-probes and can settle on a different device than the browser the user
    normally runs -- and on a hybrid Intel + NVIDIA machine the compositor then cannot
    import its buffers. Hyprland aborted inside Mesa, in `dri_create_fence_fd`, right
    after `eglCreateImageKHR ... EGL_BAD_MATCH: createImageFromDmaBufs failed`, and took
    the session with it.

    The abort is a driver-and-compositor bug rather than ours, but the trigger was ours,
    and an editor has no business being able to end someone's session. Reusing the profile
    means the window renders exactly the way that user's browser already renders, which is
    a configuration their machine has been surviving all day.

    Waiting on the process is what that costs, and it costs nothing: the page's heartbeat
    already tells the server when the UI is gone, which is what closes the editor now.

    Returns "closed" once a window we owned has been closed, "handed" when the browser
    passed the URL to an instance already running -- a window is open, but not ours to
    watch -- or None when no window could be opened at all.
    """
    exe = next((p for p in (shutil.which(b) for b in APP_BROWSERS) if p), None)
    if not exe:
        return None

    cmd = [exe, f"--app={url}", "--window-size=1400,900",
           "--no-first-run", "--no-default-browser-check"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"could not open a window with {os.path.basename(exe)} ({e})")
        return None

    name = os.path.basename(exe)
    # An immediate exit means one of two things: the URL went to a browser that was
    # already running, or the flags were rejected. Both leave nothing to wait on, and
    # treating either as "the user closed the window" would quit a second after starting.
    time.sleep(1.5)
    if proc.poll() is not None:
        print(f"window: {name} --app  (close it to quit)")
        return "handed"

    print(f"window: {name} --app  (close it to quit)")
    proc.wait()
    return "closed"


def open_window(url, title):
    """Show the editor in a window of its own, however this machine can manage it.

    Two ways, tried in order. A real webview -- WebKitGTK, WebView2, WKWebView -- when
    pywebview is bundled, which is the Windows and macOS builds. Otherwise a Chromium in
    `--app` mode, which is what Linux gets: still a window with no browser furniture, and
    nothing extra to ship.

    Returns True if a window was shown and has since been closed.
    """
    try:
        import webview
        # Silenced AFTER the import, not before: pywebview sets its own logger to DEBUG
        # as it initialises, which undoes anything set earlier. It logs a full traceback
        # for each backend it cannot load -- two of them on a Linux box with neither GTK
        # nor Qt bindings -- and that is this project's ordinary path, not a fault. A
        # launch that goes on to open a window fine should not look like it crashed twice
        # on the way.
        logging.getLogger("pywebview").setLevel(logging.CRITICAL)
    except ImportError:
        webview = None

    if webview is not None:
        try:
            logging.getLogger("pywebview").setLevel(logging.CRITICAL)
            webview.create_window(title, url, width=1400, height=900,
                                  min_size=(900, 600))
            webview.start()
            return "closed"
        except Exception as e:                           # noqa: BLE001
            # Typically a missing system webview or no display. Worth one line, then try
            # the next thing rather than dropping straight to a tab.
            print(f"native window unavailable ({e})")

    return open_app_window(url)


def _ensure_streams():
    """Give stdout/stderr somewhere to go in a windowed build.

    PyInstaller's --windowed mode detaches the console on Windows and macOS, which can
    leave sys.stdout as None. Every bare print() then raises AttributeError, and because
    there is no console the user sees an app that starts and vanishes. Costs two lines to
    make impossible.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w"))


def already_ours(port):
    """True if the thing on this port is another copy of this editor.

    Asked before doing anything drastic about a busy port, because the answer changes
    what should happen: another editor means the user launched twice and wants the one
    that is already up, while a stranger on the port means move aside quietly.
    """
    for path in ("/api/ping", "/api/state"):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=1.5) as r:
                body = json.load(r)
        except Exception:                                # noqa: BLE001
            continue
        if not isinstance(body, dict):
            continue
        # /api/ping says so outright; older builds have no ping, so a state payload with
        # the keys only this editor emits stands in for one.
        if body.get("app") == "pebblnyx-editor":
            return True
        if "no_project" in body or {"legend", "atlases", "maps"} <= set(body):
            return True
    return False


def claim_port(args, session):
    """Handle a busy port without a traceback. Returns (server, port), or (None, None).

    Two situations, and only one of them is a problem:

    A second launch of an editor that is already running -- a double-click on something
    already open -- should show the window that exists, not die at a socket bind. That is
    what a user means by launching it again.

    Anything else on the port is not ours to argue with, so the editor steps sideways to
    the next free one and says where it went. Failing to start because port 8765 happened
    to be taken would be an odd thing for a local tool to do.
    """
    if already_ours(args.port):
        url = f"http://127.0.0.1:{args.port}/"
        print(f"pebblnyx editor is already running at {url} -- opening that one.\n"
              f"    (to run a second copy anyway: --port {args.port + 1})")
        if not args.no_browser:
            if args.browser or not open_window(url, "pebblnyx"):
                webbrowser.open(url)
        return None, None

    for port in range(args.port + 1, args.port + 21):
        try:
            srv = EditorServer(("127.0.0.1", port), make_handler(session))
        except OSError:
            continue
        print(f"port {args.port} is taken by something else -- using {port} instead")
        return srv, port

    raise SystemExit(f"ports {args.port}-{args.port + 20} are all busy; "
                     f"pass --port with one that is free")


def selftest():
    """Check that everything this editor needs is actually inside it.

    The distributed editor is a PyInstaller bundle: CPython, Pillow, the pipeline and the
    engine sources all travel inside one file, which is why installing it needs no Python
    and no pip. That is a claim about a build, and PyInstaller drops things quietly -- a
    missed hidden import produces a binary that starts, opens a project, and fails the
    first time someone rasterises a font. Then it is a bug report, not a build error.

    So the build and CI run this against the packaged artefact, and install.sh runs it
    against what it just installed. Prints a report and returns non-zero on anything
    missing.
    """
    ok = True
    frozen = getattr(sys, "frozen", False)
    print(f"pebblnyx editor {pp.EDITOR_VERSION}"
          f"  ({'packaged' if frozen else 'source checkout'})")
    print(f"  python           {sys.version.split()[0]}  [{sys.executable}]")

    # Required. Each of these is something the editor cannot work without, so a failure
    # here is a failed build rather than a degraded one.
    for label, mod, probe in (
        ("pillow", "PIL", lambda m: m.__version__),
        ("pillow image", "PIL.Image", lambda m: str(m.new("RGBA", (2, 2)).size)),
        ("pillow draw", "PIL.ImageDraw", lambda m: "ok"),
        ("pillow fonts", "PIL.ImageFont", lambda m: "ok"),
        ("tomllib", "tomllib", lambda m: "ok"),
        ("pipeline", "pnx_assets", lambda m: f"blob v{m.BLOB_VERSION}"),
        ("preview", "pnx_preview", lambda m: "ok"),
        ("project", "pnx_project", lambda m: f"engine {m.FRAMEWORK_VERSION}"),
        ("size report", "size_report", lambda m: f"cap {m.VIRTUAL_SIZE_LIMIT}"),
    ):
        try:
            mod_obj = __import__(mod, fromlist=["*"])
            print(f"  {label:<16} {probe(mod_obj)}")
        except Exception as e:                           # noqa: BLE001
            print(f"  {label:<16} MISSING -- {e}")
            ok = False

    # The engine sources, which are data rather than an import: a project builds against
    # the copy inside the editor, so an editor without them can open a project and never
    # build one.
    try:
        root = pp.framework_dir()
        have = os.path.exists(os.path.join(root, "pnx.h"))
        count = sum(len([f for f in fs if f.endswith((".c", ".h"))])
                    for _d, _s, fs in os.walk(root)) if have else 0
        print(f"  engine sources   {count} files  [{root}]" if have
              else f"  engine sources   MISSING at {root}")
        ok = ok and have
    except Exception as e:                               # noqa: BLE001
        print(f"  engine sources   MISSING -- {e}")
        ok = False

    # Optional. A missing webview is a documented fallback to a browser tab, not a fault,
    # and Linux builds ship without one on purpose.
    try:
        import webview                                   # noqa: F401
        print("  native window    available")
    except ImportError:
        print("  native window    not bundled (the editor opens a browser tab)")

    print("SELFTEST OK" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


def main():
    _ensure_streams()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", nargs="?", metavar="PROJECT",
                    help="a project folder or an assets.toml; "
                         "found automatically if omitted")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--browser", action="store_true",
                    help="open a browser tab instead of a native window")
    ap.add_argument("--no-browser", action="store_true",
                    help="serve only; open nothing")
    ap.add_argument("--selftest", action="store_true",
                    help="check that Python, Pillow, the pipeline and the engine are "
                         "all present in this build, then exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    target = args.manifest
    if not target:
        found = find_manifest()
        if found:
            target = found[0]
            print(f"using {os.path.relpath(target)}"
                  + (f"  ({len(found) - 1} other project(s) found; pass a path to pick)"
                     if len(found) > 1 else ""))

    # Opening with nothing is a normal state now, not an error. A distributed editor is
    # launched from a dock or a Start menu with no arguments and no current directory
    # worth guessing from; it should come up and offer to open or create a project.
    proj = None
    if target:
        try:
            proj = Project(target)
        except (ValueError, OSError) as e:
            print(f"could not open {target}: {e}", file=sys.stderr)

    session = Session(proj)
    if proj and not proj.built:
        print("note: assets are not built yet -- press Build in the editor")
    if not proj:
        print("no project open -- use Settings to open or create one")

    try:
        srv = EditorServer(("127.0.0.1", args.port), make_handler(session))
    except OSError as e:
        if e.errno not in (errno.EADDRINUSE, errno.EACCES):
            raise
        srv, port = claim_port(args, session)
        if srv is None:
            return 0                     # an existing editor was opened instead
        args.port = port

    with srv:
        url = f"http://127.0.0.1:{args.port}/"
        title = "pebblnyx"
        print(f"pebblnyx editor: {url}   (ctrl-c to stop)")

        # The server runs on a thread so the window can own the main one, which is what
        # every native UI toolkit requires.
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        try:
            if args.no_browser:
                # Serve only: there is no UI to lose, so nothing to watch for. Scripts
                # and tests rely on this staying up until they stop it.
                threading.Event().wait()
            else:
                shown = None if args.browser else open_window(url, title)
                if shown is None:
                    webbrowser.open(url)

                # "closed" is the only case already finished: a window we owned, which
                # the user shut. A tab, or a window opened by a browser that was already
                # running, leaves a UI out there that we cannot watch directly -- so the
                # page's heartbeat is what tells us when it has gone.
                if shown != "closed":
                    LIVE.armed = True
                    threading.Thread(target=LIVE.watch, args=(srv,),
                                     daemon=True).start()
                    LIVE.done.wait()
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            # shutdown() may already have run from the watchdog; it is idempotent, and
            # server_close() -- from the `with` -- is what actually frees the port.
            with contextlib.suppress(Exception):
                srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
