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
import base64
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
import socket
import socketserver
import ssl
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
import pnx_mapfile as mf                                    # noqa: E402
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


def https_context():
    """An SSL context that can actually verify GitHub from inside a frozen binary.

    The packaged editor carries its own OpenSSL, and its compiled-in CA locations are the
    BUILD machine's -- on a target that keeps its certificates somewhere else, or in a
    container with none, every HTTPS request fails with:

        [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

    which the updater reported, accurately and unhelpfully, as "could not reach GitHub".
    It never showed up in testing because a source checkout uses the system Python, which
    finds the system store.

    So the bundled `certifi` roots come first -- they travel inside the binary and are
    therefore always present -- and the system store is the fallback for anyone running
    from source without it.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                    # noqa: BLE001
        return ssl.create_default_context()


class Updater:
    """Checks for, downloads and applies a new editor build.

    Deliberately three separate steps with the user between each one. A silent background
    update is how a tool changes under someone mid-session, and this one carries the
    engine their project compiles against -- so an upgrade is a decision, not a surprise.
    """

    # How long a caller will wait for GitHub before being told to try again. Short on
    # purpose: the answer is "is there a newer build", nobody is blocked on it, and the
    # cost of waiting longer is a UI that appears to have hung.
    CHECK_DEADLINE = 6.0

    # When a check that never returned is written off, so the network coming back does not
    # leave the editor permanently convinced a check is still running.
    CHECK_ABANDON = 60.0

    def __init__(self):
        self.current = pp.EDITOR_VERSION
        self._cache = None          # (checked_at, payload)
        self._lock = threading.Lock()
        self._checking_since = None  # when the in-flight check started, if any
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

        The request itself runs on a worker with a deadline, and this returns whether or
        not the worker is finished. That is not belt-and-braces on top of the urlopen
        timeout: `urlopen(timeout=)` bounds the socket operations and NOT the name lookup,
        so a machine whose DNS has stopped answering blocks in getaddrinfo for as long as
        the resolver wants -- measured at 25 seconds against a 10 second timeout. A caller
        that cannot bound its own wait is how this froze the editor.
        """
        now = time.time()
        with self._lock:
            if self._cache and not force and now - self._cache[0] < 3600:
                return self._cache[1]
            # One check at a time. Clicking the button twice used to mean two calls to
            # GitHub against a 60-per-hour budget; with a hung resolver it meant a thread
            # each, none of which ever came back.
            running = (self._checking_since is not None
                       and now - self._checking_since < self.CHECK_ABANDON)
            if not running:
                self._checking_since = now

        if running:
            return self._pending("a check is already running")

        done = threading.Event()
        box = {}

        def work():
            try:
                box["out"] = self._fetch()
            except Exception as e:                       # noqa: BLE001
                box["out"] = {"current": self.current, "available": False,
                              "checked": False,
                              "why": f"could not reach GitHub: {e}"}
            with self._lock:
                self._cache = (time.time(), box["out"])
                self._checking_since = None
            done.set()

        threading.Thread(target=work, daemon=True).start()
        if done.wait(self.CHECK_DEADLINE):
            return box["out"]

        # The worker is still out there and may yet succeed -- its result will land in the
        # cache and the next check will find it. This answer is not cached, because "we
        # gave up waiting" is a fact about this moment, not about the release list.
        return self._pending(
            f"GitHub did not answer within {self.CHECK_DEADLINE:.0f}s -- still trying")

    def _pending(self, why):
        """What a caller gets while a check is in flight: the last answer, or nothing."""
        with self._lock:
            last = self._cache[1] if self._cache else None
        if last:
            return {**last, "pending": True, "why": why}
        return {"current": self.current, "available": False, "checked": False,
                "pending": True, "why": why}

    def _fetch(self):
        """The network half. Runs on a worker; never on a request thread."""
        out = {"current": self.current, "checked": True, "available": False}
        try:
            req = urllib.request.Request(
                UPDATE_API + "?per_page=10",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": f"pebblnyx-editor/{self.current}"})
            with urllib.request.urlopen(req, timeout=10,
                                       context=https_context()) as r:
                releases = json.load(r)
        except Exception as e:                           # noqa: BLE001
            # Offline is the common case, not an error worth a dialog: the editor works
            # perfectly well without ever reaching GitHub. Caching is the caller's job now,
            # so this just says what happened.
            out.update({"checked": False, "why": f"could not reach GitHub: {e}"})
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
            with urllib.request.urlopen(req, timeout=30,
                                       context=https_context()) as r, \
                    open(part, "wb") as f:
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
        """Install what was downloaded, and come back running the new version.

        The editor restarts itself, which it deliberately did not before -- the worry was
        that a relaunch would take unsaved work with it. That worry is answered where it
        belongs, in the page: the button refuses while anything is unsaved rather than the
        install being permanently manual because something *might* be.

        Each platform reaches "the new version is running" its own way, so what comes back
        says which happened. Only Linux can be done in one step by us; the other two hand
        off to something that outlives this process, because a running .exe cannot be
        replaced and a mounted .app should not be scripted over.
        """
        if not self.downloaded or not os.path.exists(self.downloaded):
            return {"ok": False, "error": "nothing downloaded yet"}

        system = platform.system()
        try:
            if system == "Windows":
                # Inno closes this app, replaces it, and starts it again -- which it can
                # do and we cannot, being the thing that has the file open. /NORESTART
                # is about the MACHINE: it must never reboot anyone to update an editor.
                subprocess.Popen([self.downloaded, "/SILENT", "/CLOSEAPPLICATIONS",
                                  "/RESTARTAPPLICATIONS", "/NORESTART"])
                restart_later()
                return {"ok": True, "action": "installer", "restarting": True,
                        "message": "Installing. The editor will close and reopen on the "
                                   "new version."}

            if system == "Darwin":
                # Copying a .app over itself while it runs is how an install ends up half
                # done, so the image is opened for the usual drag. Not a restart we can
                # honestly promise, and saying so beats pretending.
                subprocess.run(["open", self.downloaded], check=False)
                return {"ok": True, "action": "dmg", "restarting": False,
                        "message": "The disk image is open. Drag the app into "
                                   "Applications, replacing the old one, then reopen it."}

            out = self._apply_linux()
            if out.get("ok"):
                restart_later()
                out["restarting"] = True
                out["message"] = "Installed. Restarting on the new version…"
            return out
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

# Set by main(), so a restart can free the port before the replacement tries to claim it.
SERVER = None


def restart_later(delay=0.6):
    """Relaunch this editor and end this process, in that order and off this thread.

    Ordering is the whole difficulty. The reply to "install" has to reach the page before
    the process dies, or the page sees a dropped connection and reports a failure for
    something that worked. And the listening socket has to be CLOSED before the successor
    starts, or the new process finds the port taken -- by us -- decides an editor is
    already running, and helpfully opens a window onto the corpse.

    So: answer, close the socket, spawn, exit. On a thread, because the request handler is
    what has to return first.
    """
    def run():
        time.sleep(delay)                    # let the response flush

        global SERVER
        if SERVER is not None:
            with contextlib.suppress(Exception):
                SERVER.shutdown()
            with contextlib.suppress(Exception):
                SERVER.server_close()        # frees the port for the successor

        # argv[0] of a frozen build is the binary itself, which is the thing that was
        # just replaced -- so this launches the NEW one, with the same project and port.
        exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]

        # PyInstaller's onefile bootloader unpacks to a temp directory and passes its
        # location to child processes in the environment, so that a program which
        # re-executes itself does not unpack twice. Inherited by a DIFFERENT frozen
        # binary, that is poison: the successor skips its own unpacking and loads from
        # our directory, which we delete on the way out. It dies with
        #
        #   Failed to load Python shared library '/tmp/_MEIxxxxxx/libpython3.12.so.1.0'
        #
        # after the update has already replaced the binary -- so the editor is upgraded
        # and will not start. Scrubbed by prefix rather than by name because the exact
        # variables differ between PyInstaller versions.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("_MEI", "_PYI"))}
        with contextlib.suppress(Exception):
            subprocess.Popen([exe, *sys.argv[1:]], start_new_session=True, env=env)

        # _exit rather than a clean return: the frame that would return is blocked in a
        # window loop or a wait(), and this process has just been superseded on disk.
        os._exit(0)

    threading.Thread(target=run, daemon=True).start()


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


# ---------------------------------------------------------------------- emulator
#
# E3 in docs/EDITOR.md ("emulator panel") was built as a plan and then marked
# SUPERSEDED: it assumed a QEMU target for the platform this framework built for, and at
# the time nothing shipped one. M9 made every platform in Project.PLATFORMS a real
# `pebble build` target, which reopens the question per platform -- an author's
# installed SDK either ships a QEMU image for it or it does not, and that is a property
# of their disk, not of this codebase (EDITOR.md's own "not currently possible" note was
# checked against one SDK snapshot, and this one's `sdk-core/pebble/*/qemu/` shows all
# seven now). So there is no hardcoded list here: every platform is offered, and
# `pebble install --emulator` either works or reports why not, the same way a build
# failure already does everywhere else in this editor.
#
# Runs entirely through the `pebble` CLI -- not libpebble2 directly, and not a
# hand-rolled VNC/RFB client. The one exception is the live frame, which talks to
# QEMU's own monitor socket with `screendump`: that is the mechanism pebble-tool's OWN
# `screenshot`/GIF commands use for a headless capture, so this is a second caller of an
# already-real mechanism instead of a second protocol implementation.
class Emulator:
    """Builds, installs into and controls a QEMU emulator via `pebble`.

    One instance for the whole process, like TOOLCHAIN -- pebble-tool's own emulator
    state (see INFO_PATH below) is a single machine-wide file, so per-project
    bookkeeping here would just be a second, potentially-stale opinion about whose
    emulator is actually running.
    """

    # pebble_tool.sdk.emulator.get_emulator_info_path(): tempfile.gettempdir()/pb-emulator.json.
    # Read-only here -- `pebble install --emulator` and `pebble kill` own writing it. See
    # docs/EDITOR.md's "recorded in a state file, so several tools can attach to one
    # running emulator" -- this editor is simply one more of those tools.
    INFO_PATH = os.path.join(tempfile.gettempdir(), "pb-emulator.json")

    def __init__(self):
        self.log = []
        self.busy = False
        self._lock = threading.Lock()

    @classmethod
    def _all_info(cls):
        try:
            with open(cls.INFO_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _pid_alive(pid):
        # Mirrors pebble-tool's own check (pebble_tool/sdk/emulator.py's
        # _is_pid_running), which documents itself as unreliable on Windows (PBL-21228)
        # -- there is no more portable answer available without a dependency this editor
        # does not otherwise need. Diverges from upstream in one way: an unexpected
        # errno there is re-raised; here it is just "not confirmed alive", because a
        # wrong "not running" is a stale-looking panel and a crash in a status poll is
        # worse.
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def info(self, platform):
        """pebble-tool's live record for `platform`, if a process from it is still alive."""
        for version_info in self._all_info().get(platform, {}).values():
            if self._pid_alive(version_info.get("qemu", {}).get("pid")):
                return version_info
        return None

    def status(self, platform):
        return {"running": self.info(platform) is not None,
                "busy": self.busy, "log": "".join(self.log[-400:])}

    # ------------------------------------------------------------- lifecycle
    #
    # `pebble sdk install` DOES install QEMU -- it lands at
    # <sdk>/toolchain/bin/qemu-pebble, confirmed by running it directly. It just does not
    # put that directory on PATH, and pebble-tool's own emulator spawn resolves
    # `qemu-pebble` as a bare command via subprocess's normal PATH lookup (`PEBBLE_QEMU_
    # PATH`, pebble_tool/sdk/emulator.py) rather than through the SDK-aware path
    # resolution `pebble build` uses internally -- which is why a build can succeed while
    # every emulator command silently fails to find it. So "installing the SDK doesn't
    # install the emulator" was the right symptom read the wrong way: the binary is
    # there, `pebble` just cannot see it without help.
    #
    # The fix mirrors pebble-tool's own layout (pebble_tool/util/get_persist_dir,
    # pebble_tool/sdk/manager.py's `SDKs/current` symlink) rather than asking the user to
    # edit their PATH: this editor already knows how to find the active SDK, so every
    # `pebble` call below gets a PATH with that toolchain's `bin` prepended.
    @staticmethod
    def _sdk_persist_dir():
        if platform.system() == "Darwin":
            return os.path.expanduser("~/Library/Application Support/Pebble SDK")
        legacy = os.path.expanduser("~/.pebble-sdk")
        if os.path.isdir(legacy):
            return legacy
        data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        return os.path.join(data_home, "pebble-sdk")

    @classmethod
    def _pebble_env(cls):
        """`os.environ`, with the active SDK's toolchain (qemu-pebble, arm-none-eabi-*)
        on PATH -- see the lifecycle comment above for why this is needed at all."""
        env = dict(os.environ)
        toolchain_bin = os.path.join(cls._sdk_persist_dir(), "SDKs", "current",
                                     "toolchain", "bin")
        if os.path.isdir(toolchain_bin):
            env["PATH"] = toolchain_bin + os.pathsep + env.get("PATH", "")
        return env

    def _run(self, cmd, cwd=None):
        self.log.append(f"\n$ {' '.join(cmd)}\n")
        try:
            p = subprocess.Popen(cmd, cwd=cwd, env=self._pebble_env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            self.log.append(f"{cmd[0]} not found -- install the Pebble SDK in Settings first\n")
            return False
        for line in p.stdout:
            self.log.append(line)
            del self.log[:-400]
        return p.wait() == 0

    def start(self, project, platform):
        """Bakes the project's CURRENT orientation, compiles it, and boots `platform`.

        Runs on a worker thread: an ARM compile plus a cold QEMU boot is tens of
        seconds, and the panel polls status() rather than blocking the request on it.

        Baking first is what makes this "respect the orientation when testing" rather
        than install whatever the last build happened to leave behind:
        `project.build()` runs the real pipeline against `project.orientation` every
        time -- the exact call the ordinary Build button makes -- so there is no
        separate "emulator build" path that could fall out of sync with the project's
        actual orientation setting.
        """
        pebble = Toolchain.pebble_path()
        if not pebble:
            return False, "pebble-tool is not installed -- see Settings"
        with self._lock:
            if self.busy:
                return False, "an emulator build is already running"
            self.busy = True

        def work():
            try:
                self.log.append(f"\n=== {platform}: baking resources for "
                                f"{pa.ORIENT_NAMES[project.orientation]} ===\n")
                result = project.build()
                self.log.append(result["output"])
                if not result["ok"]:
                    self.log.append("\nasset build failed -- see above\n")
                    return
                if not self._run([pebble, "build"], cwd=project.root):
                    self.log.append("\n`pebble build` failed -- see above\n")
                    return
                # --vnc, even though nothing here speaks VNC: it is the flag that keeps
                # QEMU headless. Without it, pebble_tool/sdk/emulator.py's own
                # _spawn_qemu adds `-display sdl,show-cursor=on` and a real, visible
                # window opens on whatever machine is running this editor -- which is
                # also, evidently, where "6fps on emery" comes from: that is the SDL
                # window's own software-rendered compositing bottlenecking, not the
                # emulated hardware. screendump reads the framebuffer over the monitor
                # socket either way, so headless costs this panel nothing.
                self._run([pebble, "install", "--emulator", platform, "--vnc"], cwd=project.root)
            finally:
                self.busy = False

        threading.Thread(target=work, daemon=True).start()
        return True, None

    def stop(self):
        """`pebble kill` stops every running emulator, not just one platform's --
        pebble-tool has no narrower command, and the fixed VNC/websockify ports it would
        allocate under `--vnc` (docs/EDITOR.md) mean only one is usefully in front at a
        time anyway.
        """
        pebble = Toolchain.pebble_path()
        if not pebble:
            return False
        try:
            subprocess.run([pebble, "kill"], env=self._pebble_env(),
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    def button(self, platform, buttons, action="push"):
        """`push` sets the FULL set of buttons currently held -- not additive, a real
        Pebble reports one bitmask -- so a caller tracking held state client-side (a
        keydown/keyup set, a mousedown/mouseup pair) always resends the complete set
        rather than one name at a time. `release` takes no buttons; it means "nothing is
        held any more", the same state a real hand lifted off every button reaches.
        """
        pebble = Toolchain.pebble_path()
        if action not in ("push", "release"):
            return False
        if action == "push" and (not buttons
                                 or any(b not in ("up", "select", "down", "back")
                                        for b in buttons)):
            return False
        # Checked here rather than left to `pebble emu-button` itself: with nothing
        # alive for `platform` the command can still exit 0 (it spawns a fresh QEMU
        # rather than refusing), which would silently boot an emulator no button on
        # this panel ever asked for.
        if not pebble or self.info(platform) is None:
            return False
        # --vnc here too, and not just in start(): pebble-tool's own connection logic
        # (ManagedEmulatorTransport._find_ports) treats a VNC-state mismatch against
        # the running QEMU as a reason to KILL IT AND RESPAWN A FRESH ONE -- headless,
        # started with --vnc, vs. this call without it would silently kill the emulator
        # on the first button press and boot a new, visible, un-installed one in its
        # place. That is not hypothetical: it is exactly what "buttons don't work, then
        # a new emulator window opens" turned out to be.
        cmd = [pebble, "emu-button", action] + (list(buttons) if action == "push" else []) \
            + ["--emulator", platform, "--vnc"]
        try:
            r = subprocess.run(cmd, env=self._pebble_env(), capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    # ------------------------------------------------------------ live frame

    def frame(self, platform):
        """One PNG frame off the running emulator's screen, or None.

        `screendump` over the QEMU monitor's plain-text protocol -- not VNC. Polled by
        the panel on an interval rather than pushed, which trades video smoothness for
        needing no persistent connection and no client-side decoder beyond an <img> tag.
        """
        info = self.info(platform)
        port = info and info.get("qemu", {}).get("monitor")
        if not port or pa.Image is None:
            return None
        ppm = os.path.join(tempfile.gettempdir(), f"pnx-emu-{platform}.ppm")
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=1.0) as s:
                s.settimeout(1.0)
                with contextlib.suppress(OSError):
                    s.recv(4096)                    # the monitor's own banner
                s.sendall(f"screendump {ppm}\n".encode())
                with contextlib.suppress(OSError):
                    s.recv(4096)
        except OSError:
            return None

        deadline = time.time() + 1.5
        while time.time() < deadline:
            if os.path.exists(ppm) and os.path.getsize(ppm) > 0:
                break
            time.sleep(0.02)
        else:
            return None

        try:
            with pa.Image.open(ppm) as img:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                return buf.getvalue()
        except OSError:
            return None
        finally:
            with contextlib.suppress(OSError):
                os.remove(ppm)


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
        if self.orientation == pa.ORIENT_BUTTONS_LEFT:
            return img.rotate(180, expand=True)        # a half-turn undoes itself either way
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
                # A metatiled atlas cannot be drawn flipped -- pnx_tilemap_draw skips the
                # flip for composed tiles rather than mirroring the quadrant order. So the
                # picker has to hide the mirrored variants for this atlas rather than
                # offer a choice the build will refuse.
                "metatiled": bool(atlas.get("metatiled")),
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
            return dict(pa.FLAG_NAMES)

    def map_atlases(self, m):
        """The atlases one map draws from, in the order that fixes its tile id space.

        `atlas = "x"` and `atlases = ["x", "y"]` are the same key spelled for one or many,
        and a map naming neither draws with the first atlas declared -- the same three
        rules the pipeline's map_atlas_names applies, so the editor shows what will build.
        """
        specs = self.man.get("atlas", [])
        if "atlases" in m:
            want = m["atlases"]
            return list(want) if not isinstance(want, str) else [want]
        if "atlas" in m:
            return [m["atlas"]]
        return [specs[0]["name"]] if specs else []

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
                              else list(e.get("flip", [])))}
                for ch, e in raw.items()}

    def map_doc(self, m):
        """One map as cells + a tile table, whatever it was authored in.

        The page used to hold a map as an array of STRINGS and index it by legend
        character, which cannot represent a `.pnxmap` at all -- a binary map has no
        characters, and above ~90 tiles there are not enough to invent. So both formats
        are normalised to the same shape here and the canvas only knows one.

        For a `rows` map each table entry carries the character it came from, so saving
        writes back the author's own glyphs rather than reassigning them -- a map whose
        diff churned every time it was opened would not be worth keeping in text.
        """
        names = self.map_atlases(m)
        default = names[0] if names else None

        if "source" in m:
            path = self._safe(m["source"])
            doc = mf.read(path)
            for t in doc["tiles"]:
                t.setdefault("flags", 0)
            return {"format": "source", "source": m["source"],
                    "w": doc["w"], "h": doc["h"], "cells": doc["cells"],
                    "tiles": doc["tiles"], "start": doc["start"],
                    "warps": [{"at": wp["at"], "to": wp["to"], "gated": wp["gated"]}
                              for wp in doc["warps"]]}

        rows = [r for r in m.get("rows", "").strip("\n").split("\n") if r.strip()]
        legend = dict(self.man.get("legend", {}))
        legend.update(m.get("legend", {}))

        tiles, seen, cells = [], {}, []
        h = len(rows)
        w = len(rows[0]) if rows else 0
        for row in rows:
            for ch in row:
                e = legend.get(ch)
                if e is None:
                    # An unknown character is carried as a placeholder rather than
                    # refused: the page has to be able to OPEN a broken map to fix it,
                    # and the build is what says no.
                    key = ("?", ch)
                    if key not in seen:
                        seen[key] = len(tiles)
                        tiles.append({"atlas": default, "index": 0, "flip": "",
                                      "flags": 0, "ch": ch, "missing": True})
                    cells.append(seen[key])
                    continue
                flip = e.get("flip", [])
                flip = [flip] if isinstance(flip, str) else list(flip)
                key = (ch,)
                if key not in seen:
                    seen[key] = len(tiles)
                    tiles.append({"atlas": e.get("atlas") or default,
                                  "index": e["tile"],
                                  "flip": "".join(sorted(flip)),
                                  "flags": self._flag_byte(e.get("flags", [])),
                                  "flag_names": list(e.get("flags", [])),
                                  "ch": ch})
                cells.append(seen[key])

        return {"format": "rows", "w": w, "h": h, "cells": cells, "tiles": tiles,
                "start": list(m.get("start", [1, 1])),
                "warps": [dict(wp) for wp in m.get("warps", [])]}

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
                        # tile table, for a `rows` map and a `.pnxmap` alike.
                        "format": doc["format"],
                        "source": doc.get("source"),
                        "w": doc["w"], "h": doc["h"],
                        "cells": doc["cells"], "tiles": doc["tiles"],
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

    def state(self):
        legend = self._legend_payload(self.man.get("legend", {}))
        return {
            # Every flag name a legend entry may carry, and its bit. Built-ins first so
            # the page can show them as the fixed pair they are, then whatever
            # [tile_flags] invented.
            "flags": self.flag_names(),
            "flag_bits_free": [b for b in pa.FLAG_USER_BITS
                               if b not in self.flag_names().values()],
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

    # The seven shipping targets, read from the SDK's own table
    # (sdk-core/pebble/common/tools/pebble_sdk_platform.py -- see docs/ROADMAP.md, M9), not
    # recalled. One table so the app-RAM slot, the resource cap and the emulator panel
    # (E3) never drift apart from each other or from what ROADMAP.md reports.
    #
    # `ram` is everything a Pebble app owns in one slot -- code, rodata, statics AND the
    # heap -- which is why "how much is left" is the number worth showing, not the static
    # size alone. `resources` is the appstore cap a submitted .pbw's resource pack must
    # fit under, NOT the larger on-device sideload ceiling; `aplite` is the one platform
    # where the two caps differ.
    #
    # No `qemu` flag: whether pebble-tool ships an emulator image for a platform is a
    # property of the SDK installed on THIS disk, not of this codebase -- EDITOR.md's own
    # "not currently possible" note was checked against one SDK snapshot and had gone
    # stale by the time it was re-checked (see Emulator, above). So the emulator panel
    # offers all seven and lets `pebble install --emulator` say so if one is missing,
    # the same way every other build failure here is reported.
    PLATFORMS = {
        "emery":   {"w": 200, "h": 228, "bw": False, "round": False,
                    "ram": 131072, "resources": 262144, "speaker": True},
        "gabbro":  {"w": 260, "h": 260, "bw": False, "round": True,
                    "ram": 131072, "resources": 262144, "speaker": False},
        "flint":   {"w": 144, "h": 168, "bw": True,  "round": False,
                    "ram": 65536,  "resources": 262144, "speaker": True},
        "basalt":  {"w": 144, "h": 168, "bw": False, "round": False,
                    "ram": 65536,  "resources": 262144, "speaker": False},
        "chalk":   {"w": 180, "h": 180, "bw": False, "round": True,
                    "ram": 65536,  "resources": 262144, "speaker": False},
        "diorite": {"w": 144, "h": 168, "bw": True,  "round": False,
                    "ram": 65536,  "resources": 262144, "speaker": False},
        "aplite":  {"w": 144, "h": 168, "bw": True,  "round": False,
                    "ram": 24576,  "resources": 131072, "speaker": False},
    }

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

        cells, bad = [], []
        for i, f in enumerate(spec.get("frames", [])):
            x, y, w, h = (int(v) for v in f)
            # Reported rather than raised: a sprite whose sheet was re-imported smaller is
            # exactly when someone needs to open it and look, and refusing the whole list
            # because frame 5 hangs off the edge would deny them that.
            if x < 0 or y < 0 or x + w > W or y + h > H:
                bad.append(i)
                continue
            buf = [pa.to_gcolor8(px[x + a, y + b], key)
                   for b in range(h) for a in range(w)]
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

        # Every atlas the map draws from, since a legend character now says which one it
        # resolves against. Loading only the first would draw the other tilesets' cells as
        # holes -- which reads as missing art rather than as a preview limitation.
        loaded = {}
        for name in m["atlases"]:
            spec = next((a for a in self.man.get("atlas", []) if a["name"] == name), None)
            if not spec:
                continue
            blob = os.path.join(self.res, spec["out"])
            if os.path.exists(blob):
                loaded[name] = pv.parse_atlas(pv.read(blob))
        if not loaded:
            return None

        roles_by_atlas = self._roles()
        legend = self.man.get("legend", {})
        default_atlas = m["atlases"][0] if m["atlases"] else None
        T = next(iter(loaded.values()))["tile_px"]

        # Resolved once per character rather than once per cell, the way compile_map does.
        resolved = {}
        for ch, e in legend.items():
            which = e.get("atlas") or default_atlas
            atlas = loaded.get(which)
            if not atlas:
                continue
            idx = roles_by_atlas.get(which, {}).get(e.get("tile"))
            if idx is not None and idx < atlas["count"]:
                resolved[ch] = (atlas, idx)

        img = Image.new("RGBA", (self.SCREEN_W, self.SCREEN_H),
                        (pv.gcolor_rgb(clear) or (0, 0, 0)) + (255,))
        first_tx, first_ty = ox // T, oy // T
        for j in range(self.SCREEN_H // T + 2):
            for i in range(self.SCREEN_W // T + 2):
                tx, ty = first_tx + i, first_ty + j
                if not (0 <= ty < len(m["rows"]) and 0 <= tx < len(m["rows"][ty])):
                    continue
                hit = resolved.get(m["rows"][ty][tx])
                if not hit:
                    continue
                tile = self._upright(pv.tile_image(hit[0], palettes, hit[1]))
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

    def font_users(self, name):
        """The scenes that load this face, so removing it can refuse and say which."""
        return [f"scene {s}" for s, spec in self.man.get("scene", {}).items()
                if name in spec.get("fonts", [])]

    def remove_font(self, name):
        """Delete a [[font]], once no scene loads it.

        `add_font` had no counterpart, so a face imported to try it out could only be
        taken out again by hand. The TTF it was rasterised from is left on disk: it was
        copied into the project deliberately, and deleting someone's licensed typeface
        because they dropped one derived asset would be well beyond what was asked.
        """
        lines = open(self.path).read().split("\n")
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[font]]":
                nxt = next((j for j in range(i + 1, len(lines))
                            if lines[j].lstrip().startswith("[")), len(lines))
                if any(re.match(rf'^name\s*=\s*"{re.escape(name)}"', lines[j].strip())
                       for j in range(i + 1, nxt)):
                    start = i
                    break
        if start is None:
            raise ValueError(f"no font named {name!r}")

        users = self.font_users(name)
        if users:
            raise ValueError(f"cannot remove {name!r} — {', '.join(users)} loads it. "
                             f"Drop it there first.")

        end = next((j for j in range(start + 1, len(lines))
                    if lines[j].lstrip().startswith("[")), len(lines))
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1
        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    # --------------------------------------------------------------- project keys
    #
    # [project] was readable everywhere and settable nowhere except `orientation`, so the
    # appstore budget the whole status bar is measured against could not be changed from
    # the thing doing the measuring.

    def set_project(self, key, value):
        """Rewrite one key of [project], creating it if the table lacks it."""
        if key not in ("name", "budget_bytes", "resources", "header"):
            raise ValueError(f"{key!r} is not a [project] key the editor sets")

        if key == "budget_bytes":
            value = int(value)
            # The device ceiling, not the appstore one: someone shipping outside the
            # appstore may legitimately want the larger cap, and the pipeline already
            # reports against both. Below a floor the number stops meaning anything.
            if not 1024 <= value <= 1048576:
                raise ValueError("the budget must be between 1 KB and the device's 1 MB "
                                 "ceiling")
            line = f"budget_bytes = {value}"
        else:
            value = str(value).strip()
            if not value:
                raise ValueError(f"{key} cannot be empty")
            if key == "name" and not re.fullmatch(r"[A-Za-z0-9 _.-]+", value):
                raise ValueError("a project name may hold letters, digits, spaces, "
                                 "underscores, dots and hyphens")
            if key in ("resources", "header") and os.path.isabs(value):
                raise ValueError(f"{key} is a path inside the project, not an absolute "
                                 f"one")
            line = f'{key} = "{value}"'

        lines = open(self.path).read().split("\n")
        start = next((i for i, l in enumerate(lines) if l.strip() == "[project]"), None)
        if start is None:
            raise ValueError("the manifest has no [project] table")
        end = next((j for j in range(start + 1, len(lines))
                    if lines[j].lstrip().startswith("[")), len(lines))

        at = next((j for j in range(start + 1, end)
                   if re.match(rf"\s*{key}\s*=", lines[j])), None)
        if at is not None:
            lines[at] = line
        else:
            # One past the LAST actual `key = value` line, not "walk back from the block's
            # end over blank lines" -- the block's end can include trailing comments that
            # belong to whatever table follows [project], since detection only looks for
            # the next `[`. A key that has never appeared before must not land among them.
            at = start + 1
            for j in range(start + 1, end):
                if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                    at = j + 1
            lines[at:at] = [line]
        with open(self.path, "w") as f:
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

    def analyse(self, rel, tile, region, max_tiles, colorkey, exclude=(),
               ink_threshold=pa.DEFAULT_INK_THRESHOLD):
        """Price a candidate carve before it is committed.

        This is the number that decides a project's content budget, and it is invisible
        until something is built -- so the editor computes it live. Region selection is
        where the budget is won: five complete tilesets are 111% of the appstore limit,
        while 128 tiles from each is 32%.

        `ink_threshold` drives the 1-bit preview strip: docs/PORTING.md's "the pipeline
        proposes a split by luminance [and] the editor lets you flip individual entries
        against a live 1-bit preview" -- the slider half of that sentence. Per-entry
        flipping is not built; every colour in the carve answers to the one threshold.
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
        est = len(unique) * tile_bytes + len(pals) * pa.PALETTE_BYTES + pa.HEADER_BYTES

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

        # Same tiles, rendered as a 1-bit platform would actually draw them: ink (black)
        # where a pixel's luminance falls under the threshold, paper (white) otherwise,
        # transparent left alone. Classifying per PIXEL rather than routing through `pals`
        # is deliberate -- ink/paper is a property of the colour itself
        # (tools/pnx_assets.py's palette_ink_mask), not of which merged palette a tile
        # happened to land in, so this does not need pals to agree with what the real
        # build would assign.
        bw_strip = None
        if unique:
            cols = min(16, len(unique))
            rows = (len(unique) + cols - 1) // cols
            grid = Image.new("RGBA", (cols * tile, rows * tile), (0, 0, 0, 0))
            for i, t in enumerate(unique):
                cell = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
                cp = cell.load()
                for j in range(tile):
                    for k in range(tile):
                        c = t[j * tile + k]
                        if c == pa.TRANSPARENT:  # the GColor8 byte, not a palette index
                            continue
                        ink = pa.gcolor_luminance(c) < ink_threshold
                        cp[k, j] = (0, 0, 0, 255) if ink else (255, 255, 255, 255)
                grid.paste(cell, ((i % cols) * tile, (i // cols) * tile))
            grid = grid.resize((grid.width * 2, grid.height * 2), Image.NEAREST)
            bw_strip = pv.data_uri(grid)

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
            # An atlas is read whole into a pool slot, so its blob size is still its
            # resident cost -- what changed with WorldTiles is WHEN it is resident, not how
            # big it is. A map that streams its atlases pays for the slots it declared
            # rather than for all of them, and only the pipeline knows that number, so the
            # figure here is the honest upper bound: what this atlas costs while loaded.
            "heap_before": heap,
            "heap_after": (heap - est) if heap is not None else None,
            "strip": strip,
            "bw_strip": bw_strip,
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

    # Keys the Import tab owns. Anything else in an [[atlas]] block -- metatiles,
    # semantic, variants, and every comment around them -- belongs to whoever wrote it
    # and is never touched by an edit from here.
    ATLAS_KEYS = ("sheet", "tile", "region", "max_tiles", "colorkey", "exclude")

    def validate_atlas(self, rel, tile, region, max_tiles, exclude=(), colorkey=None,
                       name=None):
        """Run a candidate carve through the REAL pipeline and report what it says.

        Not a re-implementation of the checks: it calls the same `pack_atlas` the build
        calls, so anything the build would reject is rejected here, in front of the person
        who can still change it. The alternative is what happened before -- the block goes
        into the manifest, Build fails, and now there is a broken atlas in the file to
        remove by hand.

        Errors block. Warnings do not: a capped carve or a repaired tile is a legitimate
        choice, and refusing it would be the editor overruling an author about their own
        art. They are said out loud instead.
        """
        spec = {"name": name or "candidate", "sheet": rel, "tile": int(tile),
                "region": list(region), "max_tiles": int(max_tiles),
                "out": f"{name or 'candidate'}.bin",
                "exclude": list(exclude)}
        if colorkey:
            spec["colorkey"] = list(colorkey)

        warnings = []
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                atlas = pa.pack_atlas(self.root, spec, self.orientation)
        except pa.BuildError as e:
            return {"ok": False, "error": str(e), "warnings": []}
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "warnings": []}

        unique = len(atlas["tiles"])
        if unique >= int(max_tiles):
            warnings.append(
                f"the carve hit max_tiles ({max_tiles}) -- tiles past that were dropped, "
                f"so the atlas may be missing art. Raise max_tiles or carve less.")
        if atlas.get("repaired"):
            warnings.append(
                f"{atlas['repaired']} tile(s) had more than {pa.PALETTE_USABLE} colours "
                f"and were reduced. The art is altered; edit it to avoid that.")

        # Autopick is where a small carve fails, and it fails at BUILD time with a message
        # about roles rather than about the region -- so it is checked here too.
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.autopick_tiles(atlas, ["floor", "wall", "accent"])
        except pa.BuildError as e:
            return {"ok": False, "error": str(e), "warnings": warnings}

        est = self.estimate()
        after = est["total"] + len(atlas["blob"]) if atlas.get("blob") else est["total"]
        if after > est["budget"]:
            warnings.append(
                f"this would put resources over the {est['budget']:,} B appstore cap.")

        return {"ok": True, "error": None, "warnings": warnings,
                "unique": unique, "palettes": len(atlas.get("variants", [])) or None}

    def atlas_spec(self, name):
        """An existing atlas's settings, for loading back into the Import tab."""
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == name), None)
        if not spec:
            raise ValueError(f"no atlas named {name!r}")
        return {"name": name, "sheet": spec.get("sheet"), "tile": spec.get("tile", 16),
                "region": spec.get("region", [0, 0, 16, 16]),
                "max_tiles": spec.get("max_tiles", 64),
                "colorkey": spec.get("colorkey"),
                "autopick": list(spec.get("autopick", [])),
                "semantic": {k: int(v) for k, v in spec.get("semantic", {}).items()},
                # Sent as written, so "auto" and a forced true/false stay tellable apart.
                "metatiles": spec.get("metatiles", "auto"),
                "variants": list(spec.get("variants", [])),
                "exclude": [int(e) for e in spec.get("exclude", [])
                            if not isinstance(e, (list, tuple))]}

    # --------------------------------------------------------------------- legend
    #
    # Painting used to be limited to whatever the legend already said, and the legend was
    # only reachable by hand-editing the manifest. That put the three tiles `autopick`
    # names between an artist and a 200-tile sheet. These writers are what let the tile
    # picker mint an entry, so choosing a tile and choosing what it MEANS are one action.

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
        here in front of the map's own legend, i.e. in the wrong table.
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
            if s.startswith("[") and not s.startswith("[map."):
                end = j
                break
        return lines, start, end

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

    def _legend_lines(self, ch, tile, atlas=None, flags=(), flip=(), map_name=None):
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
        return body

    def save_legend(self, ch, tile, atlas=None, flags=(), flip=(), map_name=None):
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
        if flip:
            which = atlas or (names[0] if names else None)
            built = next((a for a in self.atlases() if a["name"] == which), None)
            if built and built["metatiled"]:
                raise ValueError(
                    f"atlas {which!r} is metatiled, and the runtime does not flip a "
                    f"composed tile -- it would draw unmirrored. Set `metatiles = false` "
                    f"on that atlas to paint it flipped.")

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
                self._legend_lines(ch, tile, atlas, flags, flip, map_name) + [""] * gap)
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
        elif map_name:
            # Appended at the END of the [[map]] block, which is the only place it can go:
            # a subtable closes its parent, so anything written above `rows` would put the
            # rest of the map's own keys inside `[map.legend]` and the build would fail
            # complaining that the map has no rows.
            lines, mstart, mend = self._map_block(map_name)
            new = self._legend_lines(ch, tile, atlas, flags, flip, map_name)
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
            new = self._legend_lines(ch, tile, atlas, flags, flip)
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

    # ----------------------------------------------------------------- tile roles
    #
    # A role is what game code names a tile by. `autopick` invents three of them, and
    # everything else needed a hand-written [atlas.semantic] table -- so a tile could be
    # painted but never referred to from C. Naming one is the other half of the picker:
    # the index makes it paintable, the role makes it addressable.

    def save_role(self, atlas, role, index):
        """Name one tile of an atlas, writing [atlas.semantic] under its block.

        The subtable has to sit immediately after its own [[atlas]] block: in TOML it
        binds to the most recent array element, so the same three lines under a different
        atlas silently name a tile in the wrong tileset.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", role):
            raise ValueError("a role name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == atlas), None)
        if not spec:
            raise ValueError(f"no atlas named {atlas!r}")

        index = int(index)
        built = next((a for a in self.atlases() if a["name"] == atlas), None)
        count = built["count"] if built else spec.get("max_tiles")
        if count is not None and not 0 <= index < count:
            raise ValueError(f"atlas {atlas!r} holds {count} tiles (0-{count - 1}), "
                             f"not {index}")

        # `autopick` runs first and `semantic` overrides it -- that is the pipeline's own
        # documented behaviour ("a manifest can start auto and be pinned down later"), so
        # naming a tile the autopick already claimed is a pin, not a clash. This used to
        # be refused, which left an autopicked name unreachable from the editor entirely:
        # the only way out was hand-editing `autopick`, and the Import tab could not even
        # clear it. The caller is told, because the pin does move the tile every map that
        # draws through this role will use.
        pinned = role in spec.get("autopick", [])

        taken = spec.get("semantic", {}).get(role)
        if taken is not None and int(taken) != index:
            raise ValueError(f"{atlas!r} already names tile {taken} {role!r}. "
                             f"Pick another name.")

        lines, start, end = self._atlas_block(atlas)
        entry = f"{role} = {index}"

        # The block ends at the next table header, which IS `[atlas.semantic]` when one is
        # already there.
        if end < len(lines) and lines[end].strip() == "[atlas.semantic]":
            stop = next((j for j in range(end + 1, len(lines))
                         if lines[j].lstrip().startswith("[")), len(lines))
            at = next((j for j in range(end + 1, stop)
                       if re.match(rf"\s*{re.escape(role)}\s*=", lines[j])), None)
            if at is not None:
                lines[at] = entry
            else:
                while stop > end and lines[stop - 1].strip() == "":
                    stop -= 1
                lines[stop:stop] = [entry]
        else:
            while end > start and lines[end - 1].strip() == "":
                end -= 1
            lines[end:end] = ["", "# Tiles this project names. Game code refers to them as"
                                  f" {re.sub(r'[^A-Za-z0-9]', '_', atlas).upper()}_TILE_*.",
                              "[atlas.semantic]", entry, ""]

        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()
        return {"pinned": pinned}

    def remove_role(self, atlas, role):
        """Unname a tile. The tile stays; only the name goes."""
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == atlas), None)
        if not spec or role not in spec.get("semantic", {}):
            raise ValueError(f"atlas {atlas!r} does not name a tile {role!r}")
        users = [ch for ch, e in self.man.get("legend", {}).items()
                 if e.get("tile") == role and (e.get("atlas") or self._default_atlas()) == atlas]
        if users:
            raise ValueError(f"legend {', '.join(repr(c) for c in sorted(users))} "
                             f"resolve through {role!r}. Repoint them first.")
        lines, start, end = self._atlas_block(atlas)
        if not (end < len(lines) and lines[end].strip() == "[atlas.semantic]"):
            raise ValueError(f"no [atlas.semantic] table for {atlas!r}")
        stop = next((j for j in range(end + 1, len(lines))
                     if lines[j].lstrip().startswith("[")), len(lines))
        at = next((j for j in range(end + 1, stop)
                   if re.match(rf"\s*{re.escape(role)}\s*=", lines[j])), None)
        if at is None:
            raise ValueError(f"no role named {role!r}")
        del lines[at]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def _default_atlas(self):
        specs = self.man.get("atlas", [])
        return specs[0]["name"] if specs else None

    def set_autopick(self, atlas, roles):
        """Rewrite one atlas's `autopick` list.

        The importer's only pre-build say over roles: which ones to invent from the art.
        Everything else has to wait for a packed atlas, because a role is an index into
        one and dedup decides what those are.
        """
        for r in roles:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", r):
                raise ValueError(f"role {r!r} must be lowercase letters, digits and "
                                 f"underscores")
        if len(set(roles)) != len(roles):
            raise ValueError("the same role twice")
        lines, start, end = self._atlas_block(atlas)
        entry = "autopick = [" + ", ".join(f'"{r}"' for r in roles) + "]"
        at = next((j for j in range(start, end)
                   if re.match(r"\s*autopick\s*=", lines[j])), None)
        if at is not None and roles:
            lines[at] = entry
        elif at is not None:
            del lines[at]
        elif roles:
            lines[end:end] = [entry]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def _atlas_block(self, name):
        """(lines, start, end) for one [[atlas]] block, end exclusive.

        Line surgery rather than re-emitting from the parsed manifest, because re-emitting
        would discard the comments -- and in this project a manifest's comments are half
        its content.
        """
        lines = open(self.path).read().split("\n")

        # The [[atlas]] whose name matches, up to the next table header at column 0.
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "[[atlas]]":
                for j in range(i + 1, len(lines)):
                    if lines[j].lstrip().startswith("["):
                        break
                    m = re.match(r'\s*name\s*=\s*"([^"]+)"', lines[j])
                    if m:
                        if m.group(1) == name:
                            start = i
                        break
            if start is not None:
                break
        if start is None:
            raise ValueError(f"no [[atlas]] block named {name!r} in the manifest")

        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))
        return lines, start, end

    def atlas_users(self, name):
        """Everything that would break if this atlas went away, in words.

        Returned rather than raised, so the page can warn BEFORE the button is pressed
        instead of only explaining after it is refused.
        """
        specs = self.man.get("atlas", [])
        first = specs[0]["name"] if specs else None
        reasons = []

        for m in self.man.get("map", []):
            drawn = m.get("atlases") or ([m["atlas"]] if "atlas" in m else None)
            if drawn is None:
                # A map with no atlas key draws with the first one declared. Removing that
                # one silently re-points every such map at a different tileset, which
                # rebuilds clean and draws the wrong world.
                if name == first:
                    reasons.append(f"map {m['name']!r} has no `atlas` key, so it draws "
                                   f"with the first atlas declared -- which is this one")
            elif name in drawn:
                reasons.append(f"map {m['name']!r} draws with it")

        for sname, spec in self.man.get("scene", {}).items():
            if name in spec.get("atlases", []):
                reasons.append(f"scene {sname!r} lists it in `atlases`")

        # A legend entry naming the atlas only matters if a map actually PAINTS with that
        # character. The pipeline takes the same view -- it reports the cell, not the
        # legend -- so a character nobody used is a dangling reference, not a dependency,
        # and refusing over it would make removal impossible for no gain.
        for ch, e in self.man.get("legend", {}).items():
            if e.get("atlas") != name:
                continue
            painted = [m["name"] for m in self.man.get("map", [])
                       if ch in m.get("rows", "")]
            if painted:
                reasons.append(f"legend {ch!r} resolves against it and is painted in "
                               + ", ".join(f"map {p!r}" for p in painted))
        return reasons

    def remove_atlas(self, name):
        """Delete an [[atlas]] block, once nothing depends on it.

        The check is the point. Deleting an atlas a map draws with produces a manifest that
        fails to build -- recoverable, but only by hand-editing, which is the thing this
        exists to avoid. So the editor says what still uses it and changes nothing.
        """
        if not any(a.get("name") == name for a in self.man.get("atlas", [])):
            raise ValueError(f"no atlas named {name!r}")

        users = self.atlas_users(name)
        if users:
            raise ValueError(f"{name!r} is still in use: " + "; ".join(users)
                             + ". Point those at another atlas first.")

        lines, start, end = self._atlas_block(name)

        # Take the blank lines that separated this block from the next, so removing an
        # atlas and adding one back does not leave a widening gap. Comments ABOVE the
        # block are left alone: an editor-written atlas keeps its comments inside the
        # block, and a hand-written one may sit under a section heading that belongs to
        # the file rather than to it.
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while start > 0 and lines[start - 1].strip() == "":
            start -= 1

        lines[start:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def update_atlas(self, name, rel, tile, region, max_tiles, exclude=(),
                     colorkey=None):
        """Rewrite one atlas's settings in place, keeping everything else in its block.

        Only the keys the Import tab owns are replaced; a `metatiles` line, a
        `[atlas.semantic]` table or a paragraph explaining the carve survives untouched.
        """
        lines, start, end = self._atlas_block(name)

        want = {
            "sheet": f'sheet = "{rel}"',
            "tile": f"tile = {int(tile)}",
            "region": f"region = [{region[0]}, {region[1]}, {region[2]}, {region[3]}]",
            "max_tiles": f"max_tiles = {int(max_tiles)}",
        }
        if colorkey:
            r, g, b = (int(c) for c in colorkey)
            want["colorkey"] = f"colorkey = [{r}, {g}, {b}]"

        block = lines[start:end]
        seen = set()
        out = []
        skipping = False
        for line in block:
            key = (re.match(r"\s*([a-z_]+)\s*=", line) or [None, None])[1]
            # `exclude` spans several lines; drop the old one wholesale and re-emit it.
            if skipping:
                if "]" in line:
                    skipping = False
                continue
            if key == "exclude":
                skipping = "]" not in line
                continue
            if key in want:
                out.append(want[key])
                seen.add(key)
                continue
            # A key we manage that is now unset -- a cleared colour key -- goes away.
            if key == "colorkey":
                continue
            out.append(line)

        for key, text in want.items():
            if key not in seen:
                out.append(text)
        if exclude:
            out.append(self._exclude_block(exclude).rstrip("\n"))

        lines[start:end] = out
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def set_atlas_extras(self, name, metatiles=None, variants=None):
        """The two atlas keys the Import form never owned.

        `metatiles` composes each tile from four deduplicated quadrants -- a real saving
        on a full sheet and a loss on a small carve, which is why the pipeline's default
        is "auto" and why forcing it either way has to be possible. `variants` are palette
        swaps: the same art recoloured costs a 16-byte palette instead of another copy of
        every tile, and the pipeline verifies they really are recolours.

        Validated by building the candidate, because "is this actually a recolour" is not
        a question that can be answered from the manifest.
        """
        spec = next((a for a in self.man.get("atlas", []) if a.get("name") == name), None)
        if not spec:
            raise ValueError(f"no atlas named {name!r}")

        want = []
        if metatiles is not None:
            if metatiles in ("", "auto"):
                want.append(("metatiles", 'metatiles = "auto"' if metatiles == "auto"
                             else None))
            elif metatiles in (True, False, "true", "false"):
                on = metatiles in (True, "true")
                want.append(("metatiles", f"metatiles = {'true' if on else 'false'}"))
            else:
                frac = float(metatiles)
                if not 0.0 < frac < 1.0:
                    raise ValueError("a metatiles threshold is a fraction between 0 and "
                                     "1 -- the saving quadrants must reach to be worth "
                                     "taking")
                want.append(("metatiles", f"metatiles = {frac}"))

        if variants is not None:
            for v in variants:
                if not os.path.exists(self._safe(v)):
                    raise ValueError(f"no such file: {v}")
            probe = dict(spec)
            probe["variants"] = list(variants)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    pa.pack_atlas(self.root, probe, self.orientation)
            except pa.BuildError as e:
                raise ValueError(str(e)) from None
            want.append(("variants", "variants = " + json.dumps(list(variants))
                         if variants else None))

        if not want:
            return

        lines, start, end = self._atlas_block(name)
        for key, value in want:
            at = next((j for j in range(start, end)
                       if re.match(rf"\s*{key}\s*=", lines[j])), None)
            if at is not None and value:
                lines[at] = value
            elif at is not None:
                del lines[at]
                end -= 1
            elif value:
                # One past the LAST actual key line -- see save_project's own comment for
                # why "walk back over blanks from the block's end" can land a new key
                # among comments belonging to whatever table follows this atlas.
                at = start
                for j in range(start, end):
                    if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                        at = j + 1
                lines[at:at] = [value]
                end += 1
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def add_atlas(self, name, rel, tile, region, max_tiles, exclude=(),
                  colorkey=None):
        """Append an [[atlas]] block. Appending keeps every existing comment intact."""
        if any(a["name"] == name for a in self.man.get("atlas", [])):
            raise ValueError(f"an atlas named {name!r} already exists")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")

        # The manifest is the build's input, so nothing that would fail the build goes
        # into it from here. Checked server-side as well as in the page: the page can be
        # bypassed, the manifest cannot be un-broken by anything but hand-editing.
        check = self.validate_atlas(rel, tile, region, max_tiles, exclude, colorkey, name)
        if not check["ok"]:
            raise ValueError(check["error"])

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

    def legend_chars(self, atlas=None):
        """Pick sensible default characters for a blank map's floor and walls.

        Derived from the legend's flags rather than assuming '.' and '#', so a project
        with its own legend gets a blank map it can actually paint on.
        """
        specs = self.man.get("atlas", [])
        default = specs[0]["name"] if specs else None
        floor = wall = None
        for ch, e in self.man.get("legend", {}).items():
            # A character resolving against ANOTHER tileset cannot go in this map's blank
            # room: the build would refuse it, and the new map would open unpaintable.
            if atlas and (e.get("atlas") or default) != atlas:
                continue
            flags = e.get("flags", [])
            if "solid" in flags and wall is None:
                wall = ch
            elif not flags and floor is None:
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
        # where a number would silently start pointing at different art.
        tiles = [{"atlas": atlas, "index": "floor", "flip": "", "flags": 0},
                 {"atlas": atlas, "index": "wall", "flip": "", "flags": pa.FLAG_SOLID}]
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

    # ---------------------------------------------------------------------- dialog
    #
    # Text is content, so it lives in the manifest rather than as string literals in C --
    # and until now that meant a text editor, because the tab that reads dialog (Fonts,
    # for `charset = "auto"`) could only read it.

    # ----------------------------------------------------------------------- music
    #
    # A song is patterns of rows, an order list, and a table of instruments -- the shape
    # the sequencer reads and the shape a tracker shows. It had no editor at all, which
    # left the one asset class that is genuinely hard to write by hand as the only one
    # still requiring a text editor.
    #
    # A row cell is `NOTE:INSTRUMENT`, or '.' to hold and '-' to release. That spelling is
    # the manifest's, and the editor keeps it rather than inventing a second one: a song
    # half-edited by hand and half in the tool has to stay one song.

    def songs(self):
        """Every [music.*], in the shape a tracker draws."""
        out = []
        for name, spec in sorted(self.man.get("music", {}).items()):
            patterns = [list(p.get("rows", [])) for p in spec.get("pattern", [])]
            rows_per = len(patterns[0]) if patterns else 0
            instruments = []
            synth = spec.get("synth", [])
            for i, ins in enumerate(spec.get("instrument", [])):
                entry = {"name": ins.get("name", ""),
                         "wave": ins.get("wave", "square"),
                         "attack": ins.get("attack", 5),
                         "decay": ins.get("decay", 50),
                         "sustain": ins.get("sustain", 180),
                         "release": ins.get("release", 100),
                         # The synth record for this index, if the song carries one. The
                         # pipeline requires the two tables be the same length -- a row
                         # names ONE instrument index -- so they are shown as one thing.
                         "synth": dict(synth[i]) if i < len(synth) else None}
                instruments.append(entry)
            out.append({
                "name": name,
                "tempo": spec.get("tempo", 120),
                "channels": spec.get("channels", 4),
                "rows_per": rows_per,
                "patterns": patterns,
                "order": list(spec.get("order", list(range(len(patterns))))),
                "instruments": instruments,
                "has_synth": bool(synth),
                # What the blob will cost: two bytes a cell, plus the tables.
                "bytes": (len(patterns) * rows_per * spec.get("channels", 4) * 2
                          + len(instruments) * 8
                          + (2 + len(synth) * 48 if synth else 0)),
            })
        return out

    def add_song(self, name, tempo=120, rows=16, synth=True):
        """Create a [music.*] with one instrument and one empty pattern.

        Seeded rather than left blank: the pipeline refuses a song with no instruments and
        no patterns, so an empty one could not be saved at all -- the same reason a new map
        arrives with a room already in it.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("a song name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        if name in self.man.get("music", {}):
            raise ValueError(f"a song named {name!r} already exists")
        rows = int(rows)
        if not 1 <= rows <= 64:
            raise ValueError("a pattern holds between 1 and 64 rows")

        blank = "     ".join(["."] * 4)
        body = [f"[music.{name}]", f"tempo = {int(tempo)}", "channels = 4", "",
                f"[[music.{name}.instrument]]",
                'wave = "square"', "attack = 5", "decay = 80", "sustain = 180",
                "release = 120", ""]
        if synth:
            body += [f"[[music.{name}.synth]]",
                     'filter = "lowpass"', "cutoff_base = 128", "resonance = 0",
                     "cutoff_env = 0", 'lfo_target = "off"', "lfo_rate = 0",
                     "lfo_depth = 0", "pitch_env = 0", "pitch_env_decay = 0",
                     "reverb = 0", "chorus = 0",
                     "amp = { attack = 5, decay = 80, sustain = 180, release = 120 }",
                     "cutoff = { attack = 5, decay = 80, sustain = 128, release = 120 }",
                     "osc = [",
                     '  { wave = "square", volume = 200, detune = 0, octave = 0, '
                     'duty = 128 },',
                     "]", ""]
        body += [f"[[music.{name}.pattern]]", "rows = ["]
        body += [f'  "{blank}",' for _ in range(rows)]
        body += ["]"]

        with open(self.path, "a") as f:
            f.write("\n\n" + "\n".join(body) + "\n")
        self.reload()

    def remove_song(self, name):
        """Delete a song and every table under it."""
        if name not in self.man.get("music", {}):
            raise ValueError(f"no song named {name!r}")
        lines = open(self.path).read().split("\n")
        keep, drop = [], False
        for line in lines:
            s = line.lstrip()
            if s.startswith("["):
                inner = s.lstrip("[")
                drop = (inner.startswith(f"music.{name}]")
                        or inner.startswith(f"music.{name}."))
            if not drop:
                keep.append(line)
        with open(self.path, "w") as f:
            f.write("\n".join(keep))
        self.reload()

    def add_instrument(self, name):
        """Append an instrument, and its synth record when the song has a synth table.

        Both or neither. The pipeline refuses tables of different lengths because a pattern
        row names one index, so adding to one alone would break the build in a way that
        points at the song rather than at this button.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        count = len(spec.get("instrument", []))
        if count >= 255:
            raise ValueError("a song holds at most 255 instruments")

        body = [f"[[music.{name}.instrument]]", 'wave = "square"', "attack = 5",
                "decay = 80", "sustain = 180", "release = 120"]
        found = self._nth_table(f"[[music.{name}.instrument]]", count - 1)
        if not found:
            raise ValueError(f"song {name!r} has no instruments to append after")
        lines, head, end = found
        while end > head and lines[end - 1].strip() == "":
            end -= 1
        lines[end:end] = [""] + body
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

        if spec.get("synth"):
            self.add_synth_record(name)

    def add_synth_record(self, name):
        body = [f"[[music.{name}.synth]]", 'filter = "off"', "cutoff_base = 128",
                "resonance = 0", "cutoff_env = 0", 'lfo_target = "off"', "lfo_rate = 0",
                "lfo_depth = 0", "pitch_env = 0", "pitch_env_decay = 0", "reverb = 0",
                "chorus = 0",
                "amp = { attack = 5, decay = 80, sustain = 180, release = 120 }",
                "cutoff = { attack = 5, decay = 80, sustain = 128, release = 120 }",
                "osc = [",
                '  { wave = "square", volume = 200, detune = 0, octave = 0, duty = 128 },',
                "]"]
        count = len(self.man.get("music", {}).get(name, {}).get("synth", []))
        found = self._nth_table(f"[[music.{name}.synth]]", count - 1)
        if not found:
            with open(self.path, "a") as f:
                f.write("\n\n" + "\n".join(body) + "\n")
            self.reload()
            return
        lines, head, end = found
        while end > head and lines[end - 1].strip() == "":
            end -= 1
        lines[end:end] = [""] + body
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def instrument_users(self, name, index):
        """Which patterns play this instrument, so removing it can refuse and say where."""
        spec = self.man.get("music", {}).get(name, {})
        hits = []
        for pi, pat in enumerate(spec.get("pattern", [])):
            for ri, row in enumerate(pat.get("rows", [])):
                for cell in row.split():
                    if ":" in cell and cell.split(":", 1)[1] == str(index):
                        hits.append(f"pattern {pi} row {ri}")
                        break
        return hits

    def remove_instrument(self, name, index):
        """Delete an instrument, once no row plays it.

        Refused while in use rather than renumbering, because a row names an instrument by
        INDEX -- removing one silently repoints every note above it at a different sound.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        count = len(spec.get("instrument", []))
        if count <= 1:
            raise ValueError("a song needs at least one instrument")
        if not 0 <= index < count:
            raise ValueError(f"song {name!r} has {count} instruments, not {index + 1}")
        users = self.instrument_users(name, index)
        if users:
            raise ValueError(
                f"instrument {index} is played in {', '.join(users[:3])}"
                + (f" and {len(users) - 3} more" if len(users) > 3 else "")
                + ". Repoint those notes first -- removing it would renumber every "
                  "instrument above it and silently change what they play.")
        if index != count - 1:
            raise ValueError(
                f"only the last instrument can be removed ({count - 1}), because a row "
                f"names an instrument by index and removing one from the middle repoints "
                f"every note above it.")

        for header in (f"[[music.{name}.instrument]]", f"[[music.{name}.synth]]"):
            found = self._nth_table(header, index)
            if not found:
                continue
            lines, head, end = found
            while end < len(lines) and lines[end].strip() == "":
                end += 1
            while head > 0 and lines[head - 1].strip() == "":
                head -= 1
            lines[head:end] = [""]
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()

    def _music_block(self, name):
        """(lines, start, end) for a [music.x] table and every subtable under it.

        `[[music.x.pattern]]` and `[[music.x.instrument]]` bind to the song, so the block
        runs past them -- stopping at the first bracket would cut a song in half and leave
        its patterns orphaned under whatever came next.
        """
        lines = open(self.path).read().split("\n")
        want = f"[music.{name}]"
        start = next((i for i, l in enumerate(lines) if l.strip() == want), None)
        if start is None:
            return None
        end = len(lines)
        for j in range(start + 1, len(lines)):
            s = lines[j].lstrip()
            if not s.startswith("["):
                continue
            # Both spellings belong to the song: `[music.x.foo]` for a subtable and
            # `[[music.x.foo]]` for an array of them. Matching only the first stopped the
            # block at the song's own first instrument, which made every pattern in it
            # invisible to the editor.
            inner = s.lstrip("[")
            if not inner.startswith(f"music.{name}."):
                end = j
                break
        return lines, start, end

    # ---------------------------------------------------------------------- samples
    #
    # Short effects only. The pipeline caps a sample at 1.5 s and says why: one second of
    # PCM is 16,000 bytes against ~160 for a whole song, so anything sustained belongs in
    # [music.*] where it costs notes rather than kilobytes.

    def samples(self):
        out = []
        for name, spec in sorted(self.man.get("sample", {}).items()):
            rel = spec.get("file", "")
            full = os.path.join(self.root, rel)
            entry = {"name": name, "file": rel, "source_bytes": None, "bytes": None}
            if os.path.exists(full):
                entry["source_bytes"] = os.path.getsize(full)
            blob = os.path.join(self.res, f"sfx_{name}.bin")
            if os.path.exists(blob):
                entry["bytes"] = os.path.getsize(blob)
            out.append(entry)
        return out

    def wav_files(self):
        """WAVs inside the project, so adding one needs no file dialog."""
        out = []
        for dirpath, dirnames, files in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("build", "resources", "__pycache__", ".git")]
            for fn in sorted(files):
                if fn.lower().endswith(".wav"):
                    full = os.path.join(dirpath, fn)
                    out.append({"path": os.path.relpath(full, self.root),
                                "bytes": os.path.getsize(full)})
        return out

    def save_sample(self, name, rel):
        """Declare a [sample.*], validated by packing it the way the build will."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("a sample name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")
        if not os.path.exists(self._safe(rel)):
            raise ValueError(f"no such file: {rel}")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pa.pack_samples(self.root, {name: {"file": rel}}, self.orientation)
        except pa.BuildError as e:
            raise ValueError(str(e)) from None

        body = [f"[sample.{name}]", f'file = "{rel}"']
        found = self._nth_table(f"[sample.{name}]", 0)
        if found:
            self._replace_table(f"[sample.{name}]", 0, body)
            return
        with open(self.path, "a") as f:
            f.write("\n\n" + "\n".join(body) + "\n")
        self.reload()

    def remove_sample(self, name):
        if name not in self.man.get("sample", {}):
            raise ValueError(f"no sample named {name!r}")
        found = self._nth_table(f"[sample.{name}]", 0)
        if not found:
            raise ValueError(f"no [sample.{name}] block in the manifest")
        lines, head, end = found
        while end < len(lines) and lines[end].strip() == "":
            end += 1
        while head > 0 and lines[head - 1].strip() == "":
            head -= 1
        lines[head:end] = [""]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def _nth_table(self, header, index):
        """(lines, start, end) for the index-th occurrence of a table header, file-wide.

        Searched across the WHOLE file rather than inside the song's leading block,
        because TOML lets a table's subtables appear anywhere -- audiotest's synth records
        sit after its samples, which are not part of the song at all. Assuming contiguity
        found the patterns and silently missed the synth table.
        """
        lines = open(self.path).read().split("\n")
        heads = [j for j, l in enumerate(lines) if l.strip() == header]
        if index >= len(heads):
            return None
        head = heads[index]
        end = next((j for j in range(head + 1, len(lines))
                    if lines[j].lstrip().startswith("[")), len(lines))
        return lines, head, end

    def _replace_table(self, header, index, body):
        """Rewrite one table in place, keeping the blank lines that separate it."""
        found = self._nth_table(header, index)
        if not found:
            raise ValueError(f"{header} #{index} is not in the manifest")
        lines, head, end = found
        gap = 0
        while end - gap > head and lines[end - gap - 1].strip() == "":
            gap += 1
        lines[head:end] = body + [""] * gap
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def save_song_meta(self, name, tempo=None, order=None):
        """Tempo and the order list -- the two song-level things a tracker changes often.

        Patterns and instruments are edited through their own writers, because rewriting a
        whole song to change one cell would discard every comment in it.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")

        want = []
        if tempo is not None:
            t = int(tempo)
            if not 20 <= t <= 400:
                raise ValueError("tempo must be between 20 and 400 bpm")
            want.append(("tempo", f"tempo = {t}"))
        if order is not None:
            count = len(spec.get("pattern", []))
            for p in order:
                if not 0 <= int(p) < count:
                    raise ValueError(f"order names pattern {p}, but the song has {count}")
            if not order:
                raise ValueError("an order list with no entries plays nothing")
            want.append(("order", "order = " + json.dumps([int(p) for p in order])))

        lines, start, end = self._music_block(name)
        for key, value in want:
            at = next((j for j in range(start + 1, end)
                       if re.match(rf"\s*{key}\s*=", lines[j])), None)
            if at is not None:
                # The key may span several lines -- `order` is routinely written one
                # pattern per line -- so the whole array is consumed. Replacing only the
                # line the key sits on leaves the continuation lines behind as bare TOML,
                # which is the same way migrating a map broke on a multi-line `warps`.
                stop = at + 1
                depth = lines[at].count("[") - lines[at].count("]")
                while depth > 0 and stop < end:
                    depth += lines[stop].count("[") - lines[stop].count("]")
                    stop += 1
                lines[at:stop] = [value]
                end -= (stop - at) - 1
            else:
                # Before the first subtable, or the key would bind to a pattern.
                limit = next((j for j in range(start + 1, end)
                             if lines[j].lstrip().startswith("[")), end)
                # One past the last actual key line before that subtable -- not "walk
                # back over blanks from the subtable", which can land a new key between
                # a subtable's own explanatory comment and the subtable it describes.
                at = start + 1
                for j in range(start + 1, limit):
                    if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                        at = j + 1
                lines[at:at] = [value]
                end += 1
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

    def save_pattern(self, name, index, rows, append=False):
        """Rewrite one pattern's rows, or append a new one. The unit a tracker edits.

        Appending only. Removing a pattern is deliberately not offered: the order list
        names patterns by INDEX, so deleting one silently renumbers every entry after it
        and the song plays something different with nothing to show why.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        patterns = spec.get("pattern", [])
        if append:
            if len(patterns) >= 255:
                raise ValueError("a song holds at most 255 patterns")
            index = len(patterns)
        elif not 0 <= index < len(patterns):
            raise ValueError(f"song {name!r} has {len(patterns)} patterns, not {index + 1}")

        channels = int(spec.get("channels", 4))
        expect = len(patterns[0].get("rows", [])) if patterns else len(rows)
        if len(rows) != expect:
            raise ValueError(f"pattern 0 has {expect} rows, so this one must too -- the "
                             f"pipeline requires every pattern in a song to match")
        for ri, row in enumerate(rows):
            cells = row.split()
            if len(cells) != channels:
                raise ValueError(f"row {ri} has {len(cells)} cells for {channels} channels")

        body = [f"[[music.{name}.pattern]]", "rows = ["]
        body += [f'  {json.dumps(r)},' for r in rows]
        body += ["]"]

        if append:
            # After the LAST existing pattern, so the file order matches the index order --
            # a pattern appended at the end of the file but numbered from the middle would
            # make the manifest unreadable next to the tracker.
            found = self._nth_table(f"[[music.{name}.pattern]]", len(patterns) - 1)
            if not found:
                raise ValueError(f"song {name!r} has no patterns to append after")
            lines, head, end = found
            while end > head and lines[end - 1].strip() == "":
                end -= 1
            lines[end:end] = [""] + body
            with open(self.path, "w") as f:
                f.write("\n".join(lines))
            self.reload()
            return

        self._replace_table(f"[[music.{name}.pattern]]", index, body)

    def save_instrument(self, name, index, plain, synth=None):
        """Rewrite one instrument of a song, both halves.

        The plain envelope and the synth record are edited as ONE thing because a pattern
        row names one instrument index: the pipeline refuses tables of different lengths
        precisely so a note cannot play a different sound depending on which table it
        resolved through. Splitting them in the UI would invite exactly that.

        Validated by packing the candidate through the real pipeline, the same way an
        atlas carve and a sprite are, so anything the build would reject is rejected here.
        """
        spec = self.man.get("music", {}).get(name)
        if spec is None:
            raise ValueError(f"no song named {name!r}")
        count = len(spec.get("instrument", []))
        if not 0 <= index < count:
            raise ValueError(f"song {name!r} has {count} instruments, not {index + 1}")

        if plain.get("wave") not in pa.WAVEFORMS:
            raise ValueError(f"unknown waveform {plain.get('wave')!r} "
                             f"(known: {', '.join(pa.WAVEFORMS)})")
        if synth is not None:
            try:
                pa.pack_synth_instrument(synth, f"instrument {index}")
            except pa.BuildError as e:
                raise ValueError(str(e)) from None
            if not spec.get("synth"):
                raise ValueError(
                    f"song {name!r} has no synth table. Adding one means adding a record "
                    f"for every instrument, because a row names one index and the two "
                    f"tables have to line up.")

        label = str(plain.get("name", "")).strip()
        if label and not re.fullmatch(r"[a-z][a-z0-9_]*", label):
            raise ValueError("an instrument name must be lowercase letters, digits and "
                             "underscores -- it becomes a C identifier")

        body = [f"[[music.{name}.instrument]]"]
        # Optional, and written first when present so the block reads as a named thing.
        # A song's instruments are referred to by INDEX in a pattern row, which is fine for
        # the four bytes it costs and useless for reading -- `3` says nothing, `bass` does.
        if label:
            body.append(f'name = "{label}"')
        body += [f'wave = "{plain["wave"]}"',
                f'attack = {int(plain.get("attack", 5))}',
                f'decay = {int(plain.get("decay", 50))}',
                f'sustain = {int(plain.get("sustain", 180))}',
                f'release = {int(plain.get("release", 100))}']
        self._replace_table(f"[[music.{name}.instrument]]", index, body)

        if synth is not None:
            self._save_synth_record(name, index, synth)

    def _save_synth_record(self, name, index, synth):
        """Rewrite one [[music.x.synth]] entry, keeping the rest of the table."""
        def env(e, d_attack, d_decay, d_sustain, d_release):
            return ("{ attack = %d, decay = %d, sustain = %d, release = %d }"
                    % (int(e.get("attack", d_attack)), int(e.get("decay", d_decay)),
                       int(e.get("sustain", d_sustain)), int(e.get("release", d_release))))

        body = [f"[[music.{name}.synth]]",
                f'filter = "{synth.get("filter", "off")}"',
                f'cutoff_base = {int(synth.get("cutoff_base", 128))}',
                f'resonance = {int(synth.get("resonance", 0))}',
                f'cutoff_env = {int(synth.get("cutoff_env", 0))}',
                f'lfo_target = "{synth.get("lfo_target", "off")}"',
                f'lfo_rate = {int(synth.get("lfo_rate", 0))}',
                f'lfo_depth = {int(synth.get("lfo_depth", 0))}',
                f'pitch_env = {int(synth.get("pitch_env", 0))}',
                f'pitch_env_decay = {int(synth.get("pitch_env_decay", 0))}',
                f'reverb = {int(synth.get("reverb", 0))}',
                f'chorus = {int(synth.get("chorus", 0))}',
                "amp = " + env(synth.get("amp", {}), 5, 80, 180, 120),
                "cutoff = " + env(synth.get("cutoff", {}), 5, 80, 128, 120),
                "osc = ["]
        for o in synth.get("osc", []):
            body.append('  { wave = "%s", volume = %d, detune = %d, octave = %d, '
                        'duty = %d },'
                        % (o.get("wave", "square"), int(o.get("volume", 200)),
                           int(o.get("detune", 0)), int(o.get("octave", 0)),
                           int(o.get("duty", 128))))
        body.append("]")
        self._replace_table(f"[[music.{name}.synth]]", index, body)

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

    # ------------------------------------------------------------------ sprite specs
    #
    # The Sprites tab painted PNGs and stopped there, so a sprite drawn in the editor
    # still had to be declared by hand before anything could load it -- the same dead end
    # a map without a scene has. These are the declaration half: frames, the anim names
    # game code uses, and palette variants.

    def sprites(self):
        """Every [[sprite]], with its built size where a build exists."""
        out = []
        for spec in self.man.get("sprite", []):
            frames = [list(f) for f in spec.get("frames", [])]
            entry = {"name": spec.get("name"), "sheet": spec.get("sheet"),
                     "frames": frames,
                     "anim": dict(spec.get("anim", {})),
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
                        colorkey=None, bw_variant=None):
        """Run a candidate sprite through the REAL pack_sprite and report what it says.

        Same bargain as validate_atlas: not a second implementation of the frame checks,
        the actual one -- so a frame running off the sheet, a set of frames that disagree
        on size, an odd pixel count that cannot pack at 4bpp, or an anim naming a frame
        that does not exist all fail here rather than after the block is in the manifest.
        `bw_variant` gets the same treatment: pack_sprite is what actually knows the
        variant names (derived from each path's basename), so this reports the real
        rejection rather than reimplementing the check.
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
                "w": built["w"], "h": built["h"],
                "variants": len(spec.get("variants", [])),
                "variant_names": [v["name"] for v in built["variants"]]}

    def _sprite_block(self, name):
        """(lines, start, end) for one [[sprite]], stepping over its [sprite.anim]."""
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
            if s.startswith("[") and not s.startswith("[sprite."):
                end = j
                break
        return lines, start, end

    def sprite_users(self, name):
        """The scenes that load this sprite, so removing it can refuse and say which."""
        return [f"scene {s}" for s, spec in self.man.get("scene", {}).items()
                if name in spec.get("sprites", [])]

    def save_sprite(self, name, sheet, frames, anim=None, variants=(), colorkey=None,
                    bw_variant=None):
        """Create or rewrite one [[sprite]] block, validated the way the build will."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("name must be lowercase letters, digits and underscores")

        anim = dict(anim or {})
        for a in anim:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", a):
                raise ValueError(f"anim name {a!r} must be lowercase letters, digits and "
                                 f"underscores -- it becomes a C identifier")

        check = self.validate_sprite(name, sheet, frames, anim, variants, colorkey,
                                     bw_variant)
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
            if anim:
                body += ["", "[sprite.anim]"] + [f"{k} = {int(v)}" for k, v in anim.items()]
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
        simple_end = anim_at if anim_at is not None else end

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
        # a bare `name = index` pair with nothing to explain, and the set of them changes
        # as a set when frames are added or removed.
        if anim_at is not None:
            stop = next((j for j in range(anim_at + 1, end)
                         if lines[j].lstrip().startswith("[")), end)
            lines[anim_at:stop] = (["[sprite.anim]"]
                                   + [f"{k} = {int(v)}" for k, v in anim.items()]
                                   if anim else [])
        elif anim:
            # Same reasoning as the simple-key case above: after the last actual key
            # line, not "walk back from the block's end over blanks", which can land the
            # new subtable after comments that belong to whatever section follows.
            at = start + 1
            for j in range(start + 1, end):
                if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                    at = j + 1
            lines[at:at] = ["", "[sprite.anim]"] + [f"{k} = {int(v)}"
                                                    for k, v in anim.items()]

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

    # ------------------------------------------------------------------ map lifecycle
    #
    # Neither of these existed, so iterating on a test map -- the ordinary business of
    # trying a layout and throwing it away -- meant hand-editing the manifest. Both refuse
    # while something still points at the map, because a dangling warp destination and a
    # scene naming a map that is gone both fail the build a long way from the cause.

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

    # ---------------------------------------------------------------------- scenes
    #
    # A scene is the framework's ONLY load point, and it was the one thing in the manifest
    # with no editor at all -- so a new map could be drawn, painted and built, and still
    # not be reachable from the game without hand-editing TOML. It is also the unit the
    # arena is sized from, which is why the cost of each one is shown while it is edited
    # rather than after a build.

    def scenes(self):
        """Every [scene.*], with what it loads and what that costs resident."""
        out = []
        for name, spec in self.man.get("scene", {}).items():
            out.append({
                "name": name,
                "map": spec.get("map"),
                "sprites": list(spec.get("sprites", [])),
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
                   atlases=()):
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

        if not (map_name or sprites or fonts or atlases or dialog):
            raise ValueError("a scene that loads nothing cannot be built")

        # Key at a time, not block at a time. Replacing the whole table would be shorter
        # and would silently eat the comments inside it -- and in this project a
        # manifest's comments are half its content: the example's cave scene explains in
        # two lines why it does NOT load the dialogue face, which is exactly the kind of
        # reasoning that never gets written down twice.
        want = [("map", f'map = "{map_name}"' if map_name else None),
                ("atlases", "atlases = " + json.dumps(list(atlases)) if atlases else None),
                ("sprites", "sprites = " + json.dumps(list(sprites)) if sprites else None),
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

    # ------------------------------------------------------------------- saving

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

/* Sits directly above the budget strip it re-prices, so the two never read as unrelated. */
#platformrow{display:flex;align-items:center;flex-wrap:wrap;gap:.6rem;padding:.4rem .9rem;
  border-bottom:1px solid var(--line);font-size:.82rem}
#platformrow .mini{margin:0}
#platformrow small{color:var(--dim)}

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
  background:none;line-height:0;position:relative}
.tile img{width:32px;height:32px;image-rendering:pixelated;display:block}
.tile.sel{border-color:var(--accent)}
.tile b{display:block;font:9px ui-monospace,monospace;color:var(--dim);text-align:center}
/* The flags a character carries, on the swatch itself. Behaviour you have to hover to
   see is behaviour nobody checks, and "which of these is solid" is the question a
   tile palette is asked most often. */
.tile .fmark{position:absolute;top:0;right:0;font-style:normal;font-size:9px;
  line-height:1;padding:1px 2px;border-radius:0 3px 0 3px;
  background:var(--accent);color:#fff}
/* Which tileset a run of characters came from. Only shown when a map draws from more
   than one, because otherwise it is a heading over the whole world. */
.palgroup{width:100%;font:600 .6rem/1.6 ui-monospace,Menlo,monospace;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim);margin-top:.35rem}
.dim{color:var(--dim)}

/* Overlays. The tile picker shows hundreds of tiles at once, which is more than any
   sidebar can hold and the whole reason it is not one. */
.overlay{position:fixed;inset:0;z-index:50;background:#0009;
  display:flex;align-items:center;justify-content:center;padding:2rem}
.overlay .sheet{background:var(--surface);border:1px solid var(--line);border-radius:8px;
  width:min(920px,100%);max-height:100%;display:flex;flex-direction:column;
  box-shadow:0 12px 40px #0006}
.overlay .sheet.narrow{width:min(460px,100%)}
.overlay .sheet>header{display:flex;align-items:center;gap:.6rem;padding:.6rem .9rem;
  border-bottom:1px solid var(--line)}
.overlay .sheet>footer{padding:.5rem .9rem;border-top:1px solid var(--line);
  font-size:.78rem}
#pickbody,#setbody{overflow:auto;padding:.9rem;min-height:0}
/* A picked tile is bigger than a palette swatch: at 32px you cannot tell two variants of
   the same wall apart, which is exactly the choice being made here. */
#pickbody .tile img{width:40px;height:40px}
#pickbody .used{outline:2px solid var(--accent);outline-offset:1px;border-radius:4px}
.setrow{display:flex;align-items:center;gap:.5rem;padding:.35rem 0;
  border-bottom:1px solid var(--line)}
.setrow b{font-weight:600}
.setrow .grow{flex:1}
.setrow button{padding:.1rem .45rem;line-height:1.4}
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
/* One scene per card, because a scene is a set of independent choices and a flat list of
   checkboxes gives no sign where one ends and the next begins. */
.scenecard{border:1px solid var(--line);border-radius:6px;padding:.6rem .8rem;
  margin:.6rem 0;max-width:60ch}
/* ------------------------------------------------------------------ the synth panel
   Modelled on a hardware synth, because that is what it is: a signal path you read left
   to right -- oscillators, filter, amplifier, modulation, effects -- with each stage a
   module you can find without reading. A wall of numbered boxes hides the one thing the
   instrument is actually about, which is what feeds what.

   Silkscreen typography does the labelling: 10px, uppercase, widely tracked, dim. That is
   the panel vernacular and it costs nothing but discipline.

   ONE accent, not five. Each module carries a thin coloured rule to mark its stage the way
   a Roland front panel does, but every interactive element uses the shell's own accent --
   a different hue per control would be a rainbow, and the colour would stop meaning
   "you can turn this". */
.synth{display:flex;gap:.55rem;flex-wrap:wrap;align-items:stretch;margin:.5rem 0}
#music>section{margin-bottom:1.4rem}
#music .musicgrid{margin-bottom:.6rem}
/* flex:0 0 auto, NOT the flex default. A module sizes to the controls it holds and wraps
   to the next line when the rack runs out of room; letting it shrink instead squeezed
   whichever module happened to be last until its text ran out through the border. A knob
   is a fixed-size object, so the module around it is too. */
.mod{background:var(--surface);border:1px solid var(--line);border-radius:7px;
  padding:.5rem .6rem .6rem;flex:0 0 auto;position:relative;overflow:hidden}
.mod::before{content:"";position:absolute;inset:0 0 auto 0;height:2px;
  background:var(--mod-hue,var(--dim));opacity:.75}
.mod>h4{margin:.15rem 0 .5rem;font-size:10px;font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim)}
.mod .row{display:flex;gap:.5rem;align-items:flex-start}

/* The knob. Drag vertically, wheel, or focus and use the arrow keys; double-click to type
   an exact value. A knob alone would be imprecise and a number box alone would not feel
   like an instrument, so it is both -- the arc for the gesture, the readout for the value.
   This is the one place the design spends its boldness. */
.knob{width:52px;text-align:center;user-select:none}
.knob .dial{width:38px;height:38px;margin:0 auto;border-radius:50%;position:relative;
  cursor:ns-resize;background:
    conic-gradient(from 215deg,var(--accent) calc(var(--p,0) * 290deg),#00000000 0),
    radial-gradient(circle at 50% 38%,#39424f,#20262f 70%);
  box-shadow:inset 0 0 0 1px var(--line),0 1px 2px rgba(0,0,0,.4)}
.knob .dial::after{content:"";position:absolute;left:50%;top:5px;width:2px;height:11px;
  margin-left:-1px;background:var(--fg);border-radius:1px;transform-origin:50% 14px;
  transform:rotate(calc(-145deg + var(--p,0) * 290deg))}
.knob .dial:focus{outline:none;box-shadow:inset 0 0 0 1px var(--accent),0 0 0 2px var(--soft)}
.knob b{display:block;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);font-weight:600;margin-top:.2rem}
.knob i{display:block;font-style:normal;font-size:11px;
  font-family:ui-monospace,Menlo,Consolas,monospace}
.knob input{width:46px;font-size:11px;text-align:center;
  font-family:ui-monospace,Menlo,Consolas,monospace}

/* A switch that reads as a switch: the options are all visible and the chosen one is lit,
   the way a waveform selector is on a panel. A dropdown hides the alternatives, which is
   the wrong shape for four choices you pick between constantly. */
.pick{display:flex;gap:2px;background:#00000033;border:1px solid var(--line);
  border-radius:5px;padding:2px}
.pick button{border:0;background:transparent;color:var(--dim);border-radius:3px;
  padding:.2rem .35rem;font-size:11px;line-height:1;cursor:pointer}
.pick button.on{background:var(--accent);color:#0b1016}
.pick.wide button{padding:.25rem .5rem}
.wglyph{display:block;width:22px;height:12px}

/* The envelope, drawn from the values themselves. Not decoration: attack, decay, sustain
   and release are a SHAPE, and four numbers do not read as one. */
.envbox{background:#00000033;border:1px solid var(--line);border-radius:5px;padding:2px}

/* The tracker and the instrument panel side by side: a pattern is read while an
   instrument is tweaked, and putting them on separate screens would mean editing a sound
   you cannot hear in context. */
.musicgrid{display:flex;gap:1.2rem;flex-wrap:wrap;align-items:flex-start}

/* The tracker. Conventions borrowed from the form because they are load-bearing, not
   nostalgic: a fixed-width grid so columns line up at a glance, every fourth row marked
   because music is read in beats, and NOTE and INSTRUMENT as separate fields because they
   are separate decisions. `---` is an empty step and `===` is a release, which is what a
   tracker has always drawn and what the manifest's '.' and '-' mean. */
.tracker{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
  border:1px solid var(--line);border-radius:6px;background:var(--surface);
  padding:.35rem;display:inline-block}
.thead,.trow{display:flex;gap:.3rem;align-items:center}
.thead{border-bottom:1px solid var(--line);padding-bottom:.25rem;margin-bottom:.2rem}
.thead>span{width:5.4rem;font-size:10px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--dim);text-align:center}
.thead>b,.trow>b{width:1.9rem;text-align:right;color:var(--dim);font-weight:400;
  font-size:11px}
.trow.beat{background:rgba(255,255,255,.045)}
.trow.here{background:var(--soft)}
.tstep{width:5.4rem;display:flex;gap:2px;justify-content:center}
.tnote,.tinst{font-family:inherit;font-size:inherit;padding:1px 2px;background:transparent;
  border:1px solid transparent;color:var(--dim);text-align:center}
.tnote{width:3.1rem;letter-spacing:.04em}
.tinst{width:1.7rem}
.tnote.on,.tinst.on{color:var(--fg)}
.tnote.off{color:var(--bad)}
.tnote:focus,.tinst:focus{border-color:var(--accent);background:rgba(255,255,255,.08);
  outline:none;color:var(--fg)}

/* The sample list, with each one measured against the 1.5 s cap the pipeline enforces.
   The bar is the point: the limit is the reason samples are short, and a number alone does
   not say how close you are to it. */
.smprow{display:flex;gap:.6rem;align-items:center;margin:.3rem 0;max-width:44rem}
.smprow>b{width:7rem;flex:0 0 auto}
.smpbar{flex:0 0 120px;height:6px;background:#00000044;border-radius:3px;overflow:hidden}
.smpbar>i{display:block;height:100%;background:var(--accent)}
.smpbar>i.over{background:var(--bad)}
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

/* The import target. A dashed well rather than a bare button because it is also a drop
   zone, and a button does not look like somewhere you can let go of a file. */
.drop{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;
  border:1px dashed var(--line);border-radius:6px;padding:.5rem .6rem;margin-bottom:.5rem;
  transition:border-color .12s,background .12s}
.drop.over{border-color:var(--accent);background:rgba(255,255,255,.05)}
.drop.over *{pointer-events:none}

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
/* Scaled up from the watch's real pixel count -- 1x would be a postage stamp on any
   monitor -- and pixelated per .plate img above so the scaling stays crisp, not blurred. */
#emuscreen{width:288px;background:var(--ink);border:1px solid var(--line);
  border-radius:3px;min-height:120px}
/* BACK alone on the left, UP/SELECT/DOWN stacked on the right -- see the HTML comment
   above this block for why that layout is fixed rather than orientation-aware.
   tabindex on the row, not a button, so a click anywhere in the bezel (the screen
   image included) focuses it for the arrow-key/Enter/Backspace listener without
   fighting a button's own focus ring. */
.emubezel{display:flex;align-items:center;gap:.9rem;margin-top:.5rem;outline:none}
.emubezel:focus-visible{outline:2px solid var(--accent);outline-offset:4px;border-radius:6px}
.emucluster{display:flex;flex-direction:column;gap:.5rem}
.emubtn{width:2.5rem;height:2.5rem;border-radius:50%;border:1px solid var(--line);
  background:var(--surface);color:var(--fg);cursor:pointer;
  font:600 .6rem/1 ui-monospace,Menlo,monospace;user-select:none;padding:0}
.emubtn:active,.emubtn.held{background:var(--accent);color:#fff;border-color:var(--accent)}
</style></head><body>
<!-- Activity rail, editor area, status bar: the shape VS Code and Rider settled on, and
     for the reason they settled on it. Six top tabs was already crowded and every new
     capability added a seventh; a rail scales down the side and leaves the whole width
     to the thing being worked on. -->
<div id="rail">
  <button id="tabmaps" class="act on" data-t="maps"><i>▦</i><em>Maps</em></button>
  <!-- Scenes sits directly under Maps because it is the other half of the same job: a map
       that no scene loads is content the game cannot reach, which is the dead end the
       pipeline's own checks exist to catch everywhere else. -->
  <button id="tabscenes" class="act" data-t="scenes"><i>◳</i><em>Scenes</em></button>
  <button id="tabpixel" class="act" data-t="pixel"><i>✎</i><em>Sprites</em></button>
  <!-- Dialog sits with the content tabs rather than under Fonts, even though `charset =
       "auto"` derives a font's glyph set from it: what a character says is content, and
       which typeface says it is not. -->
  <button id="tabdialog" class="act" data-t="dialog"><i>❝</i><em>Dialog</em></button>
  <!-- Music. A tracker over the sequencer's own model: patterns of rows, an order list,
       and a table of instruments. Last of the content types to get an editor, and the one
       that needed it most -- a song is the only asset here nobody can write by hand. -->
  <button id="tabmusic" class="act" data-t="music"><i>♪</i><em>Music</em></button>
  <button id="tabfonts" class="act" data-t="fonts"><i>A</i><em>Fonts</em></button>
  <button id="tabimport" class="act" data-t="import"><i>⇥</i><em>Import</em></button>
  <button id="tabcode" class="act" data-t="code"><i>&lt;/&gt;</i><em>Code</em></button>
  <!-- E3 in docs/EDITOR.md, rebuilt now that pebble-tool ships a QEMU image for every
       platform this framework builds for rather than none of them. -->
  <button id="tabdevice" class="act" data-t="device"><i>▶</i><em>Device</em></button>
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
      <!-- A map draws from a LIST of tilesets, not one. The button opens the picker
           rather than a select carrying the choice, because choosing tilesets and
           choosing tiles out of them is one job done in one place. -->
      <button id="tilesets" title="tilesets this map is drawn with">Tilesets…</button>
      <button id="pick" title="every tile of every tileset this map uses">Tiles…</button>
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

  <!-- Which of the seven the numbers below describe. "This project's build" is the old,
       only behaviour: whatever's on disk, unlabelled. Naming an actual platform re-prices
       resources against ITS cap and, once it has been built, reads RAM/app size off ITS
       ELF -- a `~bw` project can look smaller on `aplite` than on `emery` without ever
       running `pebble build` again for the resource half of that. -->
  <div id="platformrow">
    <label class="mini">Check overhead for
      <select id="checkplatform"><option value="">this project's build</option></select>
    </label>
    <small id="platformnote">—</small>
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
    <!-- What the selected character IS: which tile of which tileset, mirrored how, and
         what the game will read off it. Flags used to be reachable only by hand-editing
         the manifest, which meant the one property of a tile that changes how the game
         plays was the one property the editor could not set. -->
    <section><h2>Tile</h2><div id="tileinfo"><small class="dim">—</small></div></section>
    <section><h2>Map</h2><div id="mapinfo"><small>—</small></div>
      <div class="mini">
        <input id="nmname" placeholder="name" size="8">
        <input id="nmw" type="number" value="24" min="3" max="255" title="width">
        <input id="nmh" type="number" value="16" min="3" max="255" title="height">
        <button id="newmap">＋ Map</button>
      </div>
      <!-- Renaming and deleting the map being looked at. Both refuse while a warp or a
           scene still points at it, so the pair below is safe to press. -->
      <div class="mini">
        <button id="renmap" title="rename this map, carrying warps and scenes with it"
                >Rename…</button>
        <button id="delmap" title="delete this map">Delete…</button>
        <button id="cvtmap" title="move this map into its own .pnxmap file"
                >Convert…</button>
      </div>
      <!-- The M4b palette variant and the M4d streaming controls. Reachable only by hand
           until now, so the streaming work M4d measured could not be tuned from the tool
           that reports its cost. "auto" is the pipeline's own choice and the default. -->
      <div class="fields col" style="margin-top:.5rem">
        <label>Palette<select id="mppal"></select></label>
        <label>WorldTile<select id="mpwt">
          <option value="auto">auto</option>
          <option value="4">4</option><option value="8">8</option>
          <option value="16">16</option><option value="32">32</option>
        </select></label>
        <label>Atlas slots<input id="mpslots" type="number" min="1" max="255"
          placeholder="auto" title="how many of the map's tilesets stay resident"></label>
        <label>Bank bytes<input id="mpbank" type="number" min="512" step="512"
          placeholder="default" title="how large a WorldTile bank resource may get"></label>
        <label class="check"><input id="mpres" type="checkbox"> hold whole
          <span class="dim" title="a slot per WorldTile: nothing is ever evicted, which is
            what a map cost before WorldTiles existed">(resident)</span></label>
      </div>
      <small id="mplog" class="dim">—</small>
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
         the top edge it is shoulder triggers, along the bottom it is flippers, and along
         the left edge it is the same menu grip mirrored for the other hand. -->
    <section><h2>Orientation</h2>
      <select id="orient">
        <option value="portrait">Portrait — cluster right, one thumb</option>
        <option value="buttons_top">Landscape — cluster top, triggers</option>
        <option value="buttons_bottom">Landscape — cluster bottom, flippers</option>
        <option value="buttons_left">Portrait, upside down — cluster left</option>
      </select>
      <small id="orientnote">—</small></section>
    <!-- The budgets used to live here, in the Maps sidebar, where four of the five tabs
         could not see them. They are now the strip above every page. -->
  </aside>
  <div id="stage"><canvas id="cv"></canvas></div>
  <div id="import" style="display:none;flex:1;overflow:auto;padding:1.5rem">
    <div class="imp">
      <div class="fields">
        <!-- The same import as the Sprites tab, because this tab is *called* Import and
             could only ever offer files that were already in the project. -->
        <div id="atdrop" class="drop" style="flex-basis:100%">
          <input id="atfile" type="file" accept="image/png" multiple hidden>
          <button id="atpick">Import PNG…</button>
          <span class="dim">or drop one here</span>
          <small id="atimplog" class="dim"></small>
        </div>
        <label>Sheet<select id="sheet"></select></label>
        <label>Tile px<input id="tile" type="number" value="16" min="4" step="4"></label>
        <label>Region x<input id="rx" type="number" value="0" min="0"></label>
        <label>y<input id="ry" type="number" value="0" min="0"></label>
        <label>w<input id="rw" type="number" value="16" min="1"></label>
        <label>h<input id="rh" type="number" value="16" min="1"></label>
        <label>Max tiles<input id="maxt" type="number" value="64" min="1"></label>
        <!-- The roles the pipeline invents from the art. It was hardcoded to
             floor/wall/accent, which is why a freshly imported atlas offered three
             paintable tiles out of however many it packed. Everything past these is
             named per tile in the tile picker, which needs a packed atlas to point at. -->
        <label title="roles picked from the art at build time, comma separated"
               >Autopick<input id="apick" placeholder="floor, wall, accent"
                               autocomplete="off"></label>
        <!-- Metatiles compose each tile from four deduplicated quadrants: a real saving on
             a full sheet, a loss on a small carve, which is why "auto" weighs it per atlas
             rather than picking once for the project. A metatiled atlas cannot be drawn
             mirrored, so forcing it off is what makes flipped tiles available. -->
        <label title="compose tiles from four deduplicated 8x8 quadrants"
               >Metatiles<select id="ameta">
          <option value="auto">auto</option>
          <option value="true">always</option>
          <option value="false">never</option>
        </select></label>
        <!-- Palette swaps: the same art recoloured costs a 16-byte palette instead of
             another copy of every tile. The pipeline checks they really are recolours. -->
        <label title="comma-separated PNGs, same layout as the base sheet"
               >Variants<input id="avars" placeholder="art/dungeon_ice.png"
                               autocomplete="off"></label>
        <!-- Typing the name of an atlas that already exists turns this into an edit.
             Before, Add was the only door: a carve you got wrong was in the manifest for
             good, and fixing it meant hand-editing TOML. -->
        <label>Name<input id="aname" placeholder="cave_env" list="atlasnames"
                          autocomplete="off"></label>
        <datalist id="atlasnames"></datalist>
        <button id="aload" style="display:none" title="load this atlas's settings">
          ↺ load</button>
        <!-- Removal lives next to load, because both only apply to an atlas that already
             exists. Until this, the only way to undo an import was to hand-edit the TOML
             the editor exists to keep you out of. -->
        <button id="adel" style="display:none" title="delete this atlas">
          🗑 remove</button>
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
      <!-- What pack_unit_2bpp (tools/pnx_assets.py) actually bakes into the ~bw resource
           flint/diorite/aplite build from -- same source art, no second import, no
           re-authoring, just a different pixel format chosen at build time. The threshold
           is docs/PORTING.md's "the pipeline proposes a split by luminance"; flipping
           individual pixels against this preview is not built, so every colour in the
           carve answers to the one slider. -->
      <div class="plate"><h3>Tiles kept, 1-bit preview
          <label class="mini" style="font-weight:normal">ink threshold
            <input id="bwthresh" type="range" min="1" max="29" step="1" value="15">
            <b id="bwthreshv">15</b> / 30
          </label></h3>
        <img id="bwstrip" alt="" style="background:#fff">
      </div>
    </div>
  </div>

  <!-- Fonts. The layout puts the device-sized canvas next to the controls rather than
       below them, because the whole point is watching the text change as you drag the
       threshold -- a preview you have to scroll to is a preview you stop looking at. -->
  <!-- Music. The pattern grid is the sequencer's data model shown directly: a cell is
       NOTE:INSTRUMENT, '.' holds and '-' releases, which is the manifest's own spelling.
       Keeping it means a song half-edited by hand and half in the tool is still one song. -->
  <div id="music" style="display:none;flex:1;overflow:auto;padding:1.25rem">
    <section>
      <div class="mini">
        <b>Song</b><select id="msong"></select>
        <label class="mini">tempo <input id="mtempo" type="number" min="20" max="400"
          style="width:5rem"></label>
        <button id="msongnew">New song…</button>
        <button id="msongdel">Remove song</button>
        <span id="mcost" class="dim"></span>
      </div>
      <div class="mini">
        <b>Order</b><input id="morder" size="40" placeholder="0, 1, 2, 1">
        <span class="dim">which patterns play, in sequence</span>
      </div>
      <!-- No save button anywhere in this tab. Everything else in the editor writes the
           manifest as you change it -- the legend, scenes, dialog, map properties -- and
           one tab with its own save model is a thing to remember rather than a thing to
           learn. -->
    </section>

    <div class="musicgrid">
      <section>
        <h2>Pattern <select id="mpat"></select></h2>
        <div class="mini">
          <label class="mini">octave <select id="moct">
            <option>1</option><option>2</option><option>3</option>
            <option selected>4</option><option>5</option><option>6</option>
          </select></label>
          <button id="mpatadd" title="append an empty pattern">+ Pattern</button>
          <button id="mpatclone" title="copy this pattern to a new one">Clone</button>
        </div>
        <div id="mrows" class="tracker"></div>
        <div class="mini"><span id="mpatlog" class="dim"></span></div>
        <small class="dim" style="display:block;max-width:34ch;margin:.5rem 0 1.2rem">
          Type on the piano row to enter notes — <kbd>z</kbd>…<kbd>m</kbd> naturals,
          <kbd>s</kbd><kbd>d</kbd><kbd>g</kbd> sharps, <kbd>q</kbd> up an octave.
          <kbd>-</kbd> releases, <kbd>del</kbd> clears.
        </small>
      </section>

      <section style="flex:1;min-width:24rem">
        <h2>Instrument <select id="minst"></select>
          <button id="minstadd" title="append an instrument">+</button>
          <button id="minstdel" title="remove the last instrument">−</button>
        </h2>
        <div id="minstbody"></div>
        <div class="mini"><span id="minstlog" class="dim"></span></div>
      </section>
    </div>

    <section>
      <h2>Samples</h2>
      <p class="dim" style="max-width:60ch">Short effects only. One second of PCM is
        16,000 bytes against a few hundred for a whole song, so the pipeline caps a sample
        at 1.5&nbsp;s — anything sustained belongs in a pattern.</p>
      <div id="msamples"></div>
      <div class="mini">
        <input id="msname" placeholder="name" size="10">
        <select id="mswav"></select>
        <button id="msadd">Add sample</button>
      </div>
      <div id="mslog" class="mini"></div>
    </section>
  </div>

  <!-- Dialog. One blob for every conversation, because a resource read costs ~29us per
       CALL regardless of size -- so the cost shown per entry is text, not overhead. -->
  <div id="dialog" style="display:none;flex:1;overflow:auto;padding:1.25rem">
    <section>
      <h2>Dialog</h2>
      <p class="dim" style="max-width:60ch">
        One page per line. A page is one screenful — the engine does not scroll them.
        Fonts with <code>charset = "auto"</code> rasterise exactly the characters used
        here, so adding a word can add a glyph.
      </p>
      <div id="dialoglist"></div>
      <div class="mini" style="margin-top:.8rem">
        <button id="dlgnew">New conversation…</button>
      </div>
      <div id="dialoglog" class="mini"></div>
    </section>
  </div>

  <!-- Scenes. The framework's only load point, and the unit the scene arena is sized
       from -- so each row carries its own resident cost rather than leaving that to a
       build. Everything listed here is held for the whole scene. -->
  <div id="scenes" style="display:none;flex:1;overflow:auto;padding:1.25rem">
    <section>
      <h2>Scenes</h2>
      <p class="dim" style="max-width:60ch">
        A scene is the only thing the game can load. It names one map and whatever has to
        be resident while that map is on screen. A map with no scene cannot be reached.
      </p>
      <div id="scenelist"></div>
      <div class="mini" style="margin-top:.8rem">
        <button id="scnew">New scene…</button>
      </div>
      <div id="scenelog" class="mini"></div>
    </section>
  </div>

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

        <!-- `add_font` had no counterpart, so a face imported to try it out could only be
             taken out by hand. The TTF it was rasterised from is deliberately left on
             disk. -->
        <section><h2>Declared fonts</h2>
          <div class="fields col">
            <label>Font<select id="fdelsel"></select></label>
          </div>
          <button id="fdelbtn">Remove font</button>
          <small id="fdellog">The TTF stays in <code>art/fonts/</code>.</small>
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
        <!-- Frames picked off a sheet. The declare form below can only describe a
             vertical stack, which is the layout this engine's own examples happen to use
             and not the one most sheets ship in — a run of poses across a row, several
             rows to a character. Anything else meant writing the rectangles by hand. -->
        <section><h2>From sheet</h2>
          <!-- Bringing the file in. Every sheet list here is filled by walking the
               project's art/ folder, and until this there was no way to put anything in
               it from the editor -- importing a sprite sheet meant finding the project
               directory in a file manager, which for the packaged editor is a folder the
               user has never seen. -->
          <div id="shdrop" class="drop">
            <input id="shfile" type="file" accept="image/png" multiple hidden>
            <button id="shpick">Import PNG…</button>
            <span class="dim">or drop one here</span>
          </div>
          <small id="shimplog" class="dim"></small>
          <div class="fields col">
            <label>Sheet<select id="shsheet"></select></label>
            <label>Frame w<input id="shfw" type="number" value="16" min="1" max="255"></label>
            <label>Frame h<input id="shfh" type="number" value="24" min="1" max="255"></label>
            <label>Origin x<input id="shox" type="number" value="0" min="0"
              title="where the grid starts, for sheets with a border"></label>
            <label>Origin y<input id="shoy" type="number" value="0" min="0"></label>
            <label>Gap x<input id="shgx" type="number" value="0" min="0"
              title="spacing between frames, for sheets drawn on a grid with gutters"></label>
            <label>Gap y<input id="shgy" type="number" value="0" min="0"></label>
          </div>
          <div class="row" style="margin-top:.4rem">
            <button id="shslice" class="primary">Slice</button>
            <button id="shclear">Clear picks</button>
          </div>
          <small id="shlog">Click frames in the order the animation plays.</small>
        </section>

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


        <!-- The declaration half. Painting a PNG left the sprite undeclared, so nothing
             could load it — the same dead end a map without a scene has. Frames come from
             the picks above when there are any, and from the canvas size otherwise. -->
        <section><h2>Declare</h2>
          <div class="fields col">
            <label>Sprite<select id="spsel"><option value="">— new —</option></select></label>
            <label>Name<input id="spname" placeholder="hero" size="10"></label>
            <label>Sheet<select id="spsheet"></select></label>
            <label>Frame w<input id="spfw" type="number" value="16" min="1" max="255"></label>
            <label>Frame h<input id="spfh" type="number" value="24" min="1" max="255"></label>
            <label>Frames<input id="spn" type="number" value="1" min="1" max="64"
              title="stacked vertically down the sheet"></label>
            <label>Anim<input id="spanim" placeholder="stand,step_a,step_b" size="12"
              title="one name per frame, in order — these become C identifiers"></label>
            <!-- A 1-bit screen has no colour to tell a recolour by, so a sprite with
                 variants does not preserve variant SELECTION on one at all -- there is
                 exactly one 1-bit rendering, baked from whichever source this names (the
                 base, if left on "— base —"). tools/pnx_assets.py's pack_sprite. -->
            <label>BW variant<select id="spbw"
              title="which source becomes this sprite's one 1-bit rendering"
              ><option value="">— base —</option></select></label>
          </div>
          <div class="row" style="margin-top:.4rem">
            <button id="spsave" class="primary">Save sprite</button>
            <button id="spdel">Remove</button>
          </div>
          <small id="splog">—</small>

          <!-- The declared sprite's OWN frames. Opening art used to mean going to the
               sheet slicer and re-deriving, by hand, the frame geometry the manifest
               already states -- and for a sprite whose frames are scattered rather than
               stacked there was no slicer setting that could reproduce them at all.
               These rects come from the declaration, so every layout opens alike. -->
          <div id="spframes" class="cells" style="margin-top:.5rem"></div>
          <small id="spframelog" class="dim">Pick a sprite to open its frames.</small>
        </section>
      </div>

      <div class="fontview">
        <!-- The sliced sheet. Clicking a cell picks it as the next frame; clicking a
             picked one drops it and renumbers the rest, because the order IS the anim
             index and a gap in it would be a frame nobody can name. -->
        <div class="plate wide" id="shplate" style="display:none">
          <h3>Sheet frames <small id="shcount" class="dim"></small></h3>
          <div id="shgrid" class="tiles"></div>
          <small class="dim">Click to pick in order · double-click a picked frame to
            edit it on the canvas</small>
        </div>
        <div class="plate wide"><h3 id="pxtitle">Canvas</h3>
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
      <!-- [project]. Readable everywhere and settable nowhere, which meant the appstore
           budget the whole status bar measures against could not be changed from the
           thing doing the measuring. -->
      <div class="plate wide"><h3>Project</h3>
        <div class="fields col">
          <label>Name<input id="prname" size="18"></label>
          <label>Budget bytes<input id="prbudget" type="number" min="1024"
            max="1048576" step="1024"
            title="the appstore cap is 262144; the device ceiling is 1048576"></label>
          <label>Resources<input id="prres" size="18"></label>
          <label>Header<input id="prhdr" size="24"></label>
        </div>
        <button id="prsave" class="primary">Save project</button>
        <small id="prlog">The appstore rejects bundles over 262,144 B. The device's own
          ceiling is 1,048,576 B — shipping is the constraint that binds. Changing
          Resources or Header relocates build output; the blobs already written stay
          where they are.</small>
      </div>
      <!-- Updates. The editor is a file someone downloaded once; without this, a fix
           reaches them only if they think to check a releases page they may never have
           seen. Three steps with the user between each, deliberately: this binary carries
           the engine their project compiles against, so an upgrade is a decision. -->
      <div class="plate wide"><h3>Version</h3>
        <div id="updinfo">—</div>
        <div class="row" style="margin-top:.7rem">
          <button id="updcheck">Check for updates</button>
          <button id="upddl" class="primary" style="display:none">Download</button>
          <button id="updapply" class="primary" style="display:none">Install &amp; Restart</button>
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

  <!-- The emulator panel E3 planned and marked SUPERSEDED (docs/EDITOR.md): it assumed
       a QEMU target for the platform this framework built for, and none existed. M9
       made every platform a real `pebble build` target, and the SDK installed here
       ships a QEMU image for all seven of them, not the four EDITOR.md found when it
       was written -- so this is offered for any platform and `pebble` says so if one
       is actually missing, the same way any other build failure is reported. The
       screen is QEMU's own monitor `screendump`, polled -- not VNC, not noVNC. -->
  <div id="device" style="display:none;flex:1;overflow:auto;padding:1.5rem">
    <div class="sdkwrap">
      <div class="plate wide"><h3>Emulator</h3>
        <p class="prose">Builds this project for one platform and installs it into
          <code>pebble</code>'s own emulator, baked for whichever orientation this
          project is currently set to (see Settings) -- the same asset build the Build
          button runs, so what you see here is what the resource budget is measured
          against, not a separate copy of it.</p>
        <div class="row">
          <select id="emuplatform"></select>
          <button id="emustart" class="primary">Build &amp; run</button>
          <button id="emustop">Stop</button>
        </div>
        <small id="emunote">—</small>
      </div>

      <!-- BACK alone on the left, UP/SELECT/DOWN stacked on the right -- the physical
           layout every Pebble has, CloudPebble's emulator included. Fixed regardless of
           this project's orientation: the screendump is the raw device framebuffer, and
           the buttons are hardware that never moves -- pnx_input remaps what pressing
           one MEANS to the game, not where it sits on the case. -->
      <div class="plate wide" id="emuscreenwrap" style="display:none">
        <h3>Screen</h3>
        <div class="emubezel" tabindex="0">
          <button id="emuback" class="emubtn emuback" data-btn="back" title="Back -- Backspace">BACK</button>
          <img id="emuscreen" alt="emulator screen">
          <div class="emucluster">
            <button id="emuup" class="emubtn" data-btn="up" title="Up -- ↑">UP</button>
            <button id="emuselect" class="emubtn" data-btn="select" title="Select -- Enter or →">SEL</button>
            <button id="emudown" class="emubtn" data-btn="down" title="Down -- ↓">DOWN</button>
          </div>
        </div>
        <small>Press and hold, on screen or with ↑/↓/Enter/Backspace while this
          panel has focus -- a real hold, not a canned click, so <code>pnx_input_held_ms</code>
          sees the same thing a real hand would. No touchscreen input: <code>pebble-tool</code>'s
          QEMU bridge has no touch message in its wire protocol (checked against
          <code>libpebble2</code>'s own protocol union) -- only buttons, so <code>emery</code>
          and <code>gabbro</code> can be run here but not tapped.</small>
      </div>

      <div class="plate wide"><h3>Output</h3><pre id="emulog">—</pre></div>
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

<!-- The tile picker.
     Painting used to be limited to the legend, and the legend to whatever `autopick`
     named -- three tiles of an atlas that may hold two hundred. This is where the other
     hundred and ninety-seven become reachable: every tile of every tileset the map uses,
     and clicking one binds it to a legend character on the spot. Bigger than a sidebar
     can hold, hence the overlay. -->
<div id="pickwrap" class="overlay" style="display:none">
  <div class="sheet">
    <header>
      <b>Tiles</b>
      <span id="pickhint" class="dim"></span>
      <div style="flex:1"></div>
      <label class="mini"><input id="pickflipx" type="checkbox"> flip X</label>
      <label class="mini"><input id="pickflipy" type="checkbox"> flip Y</label>
      <button id="pickclose">Close</button>
    </header>
    <div id="pickbody"></div>
  </div>
</div>

<!-- Which tilesets this map draws from, and in what order -- the order is not cosmetic,
     it fixes the map's tile id space. -->
<div id="setwrap" class="overlay" style="display:none">
  <div class="sheet narrow">
    <header>
      <b>Tilesets</b>
      <div style="flex:1"></div>
      <button id="setclose">Close</button>
    </header>
    <div id="setbody"></div>
    <footer class="dim" id="setnote"></footer>
  </div>
</div>
<script>
const S={data:null,map:null,ch:null,mode:'paint',dirty:false,img:{},T:32};
const $=s=>document.querySelector(s);

// One JSON POST and one line of output. Both existed inline in a dozen handlers; the
// legend and flag editors add enough more of them that the repetition stopped paying.
const post=async(url,body)=>(await fetch(url,{method:'POST',
  headers:{'content-type':'application/json'},
  body:JSON.stringify(body||{})})).json();

function say(text,bad){
  const log=$('#log');
  log.className=bad===false?'ok':(bad===undefined?'':'bad');
  log.textContent=text;
}

// Re-read the manifest after the server has written to it, keeping the map being edited
// selected. The legend is project-wide state, so a character minted from the picker has
// to come back through /api/state rather than being invented in the page -- otherwise
// the palette shows a tile the manifest does not have.
async function reload(){
  const keep=S.map&&S.map.name, dirty=S.dirty;
  // Cells, not rows: painting writes the cell grid now, so preserving `rows` across a
  // reload would restore the state the map had before the last brush stroke.
  const cells=S.map&&S.map.cells, tiles=S.map&&S.map.tiles;
  const start=S.map&&S.map.start, warps=S.map&&S.map.warps;
  const sets=S.map&&S.map.atlases;
  S.data=await (await fetch('/api/state')).json();
  const i=Math.max(0,S.data.maps.findIndex(m=>m.name===keep));
  $('#mapsel').value=i;
  selectMap(i);
  // Unsaved painting survives the round trip. Reloading state to pick up one new legend
  // character would otherwise throw away every edit made since the last save, which is
  // the kind of loss that teaches people not to touch a feature.
  if(dirty&&cells&&haveMap()){
    // The tile table is merged rather than replaced: a character just minted through the
    // picker arrives from the server as a NEW entry, and dropping the server's copy would
    // throw the new tile away the moment it was added.
    if(tiles&&S.map.tiles&&S.map.tiles.length>=tiles.length) {
      tiles.forEach((old,i)=>{ if(S.map.tiles[i]) Object.assign(S.map.tiles[i],old) });
    }
    S.map.cells=cells; S.map.start=start; S.map.warps=warps;
    if(sets){ S.map.atlases=sets; S.map.atlas=sets[0] }
    S.dirty=true; mark(); drawLegend(); draw(); info();
  }
}

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
  drawPalettes(); platformSelector(); budget(); statusbar(); orientation(); atlasMode();
  // Once, a moment after the editor is usable: an update check is never
  // worth delaying the first paint for.
  setTimeout(()=>updCheck(), 1500);
  startHeartbeat();
  selectMap(0);
}

// The atlases this map draws from, in the order that fixes its tile id space. A map is
// not "drawn with an atlas" -- it is drawn with a LIST, and the first one is only the
// default for characters that do not name their own.
function mapAtlases(){
  const want=(S.map&&S.map.atlases)||[];
  const found=want.map(n=>S.data.atlases.find(a=>a.name===n)).filter(Boolean);
  return found.length?found:(S.data.atlases[0]?[S.data.atlases[0]]:[]);
}
function atlas(name){
  const list=mapAtlases();
  if(!name) return list[0];
  return list.find(a=>a.name===name)||null;
}

// A legend character names a tile in ONE of the map's atlases -- the one it pins, or the
// map's first if it pins none. It used to resolve against the map's first atlas always,
// which is why a character belonging to a second tileset showed as missing art even
// though the same manifest built and previewed correctly.
//
// The tile is either a role the atlas defines or a raw index into it. Roles are the
// better thing to write, but an atlas has hundreds of tiles and only a handful are worth
// naming, so an index is how the rest get painted at all.
// The legend this map actually paints with: the project table overlaid with the map's
// own, which is exactly what the pipeline builds. Merged in the page rather than on the
// server because WHICH table an entry lives in decides what editing it changes -- a
// project character changes every map, a map's own changes this one -- so the page needs
// both halves, not the answer.
//
// The overlay is what makes a tileset paintable at all. One character per cell caps a map
// at the printable set, about ninety; project-wide, that ninety was shared by every map
// and every atlas in the game, so most of a carved tileset could never be placed.
function LEG(){
  return Object.assign({}, S.data.legend, (S.map&&S.map.legend)||{});
}
function scopeOf(ch){
  return (S.map&&S.map.legend&&S.map.legend[ch])?'map':'project';
}

function resolve(ch){
  const e=LEG()[ch];
  if(!e) return null;
  const a=atlas(e.atlas);
  if(!a) return null;
  const byIndex=typeof e.tile==='number';
  const idx=byIndex?e.tile:a.roles[String(e.tile).toLowerCase()];
  if(idx===undefined||idx<0||idx>=a.tiles.length) return null;
  return {uri:a.tiles[idx], index:idx, atlas:a.name, role:byIndex?null:e.tile,
          flags:e.flags||[], flip:e.flip||[]};
}

// Why a character does not resolve, in the words that say what to do about it. "missing"
// with no reason is the failure this replaced: the answer was always in the manifest and
// never on the screen.
function whyMissing(ch){
  const e=LEG()[ch], want=e.atlas;
  if(want && !mapAtlases().some(a=>a.name===want))
    return `${ch} draws from ${want}, which this map does not use`;
  const a=atlas(want);
  if(!a) return `${ch} has no atlas to draw from`;
  if(typeof e.tile==='number')
    return `${ch} names tile ${e.tile} of ${a.name}, which packed ${a.tiles.length}`;
  return `${ch} names role "${e.tile}", which ${a.name} does not define`;
}

// CSS transforms, so a mirrored tile costs no second image: the same data URI is drawn
// turned. The pipeline stores the flip as two bits in the cell for exactly the same
// reason, which is what makes this an honest preview rather than a lookalike.
function flipCss(flip){
  if(!flip||!flip.length) return '';
  const sx=flip.includes('x')?-1:1, sy=flip.includes('y')?-1:1;
  return `transform:scale(${sx},${sy})`;
}

function drawLegend(){
  const el=$('#legend'); el.innerHTML=''; S.img={};
  const list=mapAtlases();
  if(!list.length){ el.innerHTML='<small>No atlas built yet — press Build.</small>'; return }

  // The palette is the map's TILE TABLE, not the project legend -- which is what lets one
  // canvas draw both authoring formats. A `rows` map's entries carry the character they
  // came from and show it; a `.pnxmap`'s show their index, because there is no character
  // and above ~90 tiles there could not be one.
  //
  // Grouped by atlas, because with several tilesets in one map an ungrouped strip is just
  // a pile: which tileset a tile came from is the thing you are choosing between.
  const tiles=(S.map&&S.map.tiles)||[];
  const usable=[], missing=[];
  tiles.forEach((t,i)=>{ (resolveTile(t)?usable:missing).push(i) });
  const groups=new Map(list.map(a=>[a.name,[]]));
  for(const i of usable){
    const r=resolveTile(tiles[i]);
    if(groups.has(r.atlas)) groups.get(r.atlas).push(i);
  }

  for(const [name,members] of groups){
    if(list.length>1){
      const h=document.createElement('div');
      h.className='palgroup'; h.textContent=name;
      el.appendChild(h);
    }
    if(!members.length){
      const p=document.createElement('small');
      p.className='dim'; p.textContent='no tiles yet';
      el.appendChild(p);
    }
    for(const i of members){
      const t=tiles[i], r=resolveTile(t);
      const img=new Image(); img.src=r.uri; S.img[i]=img;
      img.onload=draw;

      const label=t.ch!==undefined?t.ch:String(i);
      const b=document.createElement('button');
      b.className='tile'+(S.ti===i?' sel':'');
      b.title=`${label} → ${r.role?r.role:'tile '+r.index} of ${r.atlas}`
        +(r.flip.length?` flipped ${r.flip.join('')}`:'')
        +(r.flags.length?` [${r.flags.join(' ')}]`:'')
        +(t.ch!==undefined?(scopeOf(t.ch)==='map'?' — this map only':' — project-wide'):'');
      b.innerHTML=`<img src="${r.uri}" alt="${label}" style="${flipCss(r.flip)}">`
        +`<b>${label}</b>`
        +(r.flags.length?`<i class="fmark">${flagMark(r.flags)}</i>`:'');
      b.onclick=()=>{ selectTile(i) };
      el.appendChild(b);
    }
  }
  // Re-derived every time, not only when the index has gone stale. `ti` can stay valid
  // across a change that makes `ch` wrong -- converting a map to a file keeps index 1 and
  // takes its character away -- and a leftover `ch` sends the tile panel down the legend
  // path for a map that has no legend.
  if(!usable.length||S.img[S.ti]===undefined) selectTile(usable.length?usable[0]:null,true);
  else selectTile(S.ti,true);

  $('#painthint').innerHTML = missing.length
    ? `<span style="color:var(--bad)">${missing.map(i=>whyTileMissing(tiles[i],i))
        .join('; ')}.</span>`
    : 'Click to paint. <kbd>W</kbd> sets a warp, <kbd>S</kbd> the start.';
  tileInfo();
}

// One selection, two names. `ti` is what paints; `ch` is the legend character behind it,
// which only a `rows` map has and which the legend sidebar still edits. Keeping both in
// step here is what stops the two halves of the Maps tab disagreeing about what is
// selected.
function selectTile(i,quiet){
  S.ti=i;
  const t=(S.map&&S.map.tiles&&i!=null)?S.map.tiles[i]:null;
  S.ch=(t&&t.ch!==undefined)?t.ch:null;
  if(quiet) return;
  S.mode='paint'; drawLegend(); tool(); tileInfo();
}

// A tile table entry resolved against the built atlases: the same job resolve(ch) does
// for a legend character, minus the character.
function resolveTile(t){
  if(!t) return null;
  const a=atlas(t.atlas);
  if(!a) return null;
  const byIndex=typeof t.index==='number';
  const idx=byIndex?t.index:a.roles[String(t.index).toLowerCase()];
  if(idx===undefined||idx<0||idx>=a.tiles.length) return null;
  const flip=[...((t.flip)||'')];
  return {uri:a.tiles[idx], index:idx, atlas:a.name, role:byIndex?null:t.index,
          flags:t.flag_names||[], flip};
}

function whyTileMissing(t,i){
  const label=t.ch!==undefined?`'${t.ch}'`:`tile ${i}`;
  if(t.missing) return `${label} has no legend entry`;
  if(t.atlas && !mapAtlases().some(a=>a.name===t.atlas))
    return `${label} draws from ${t.atlas}, which this map does not use`;
  const a=atlas(t.atlas);
  if(!a) return `${label} has no atlas to draw from`;
  if(typeof t.index==='number')
    return `${label} names tile ${t.index} of ${a.name}, which packed ${a.tiles.length}`;
  return `${label} names role "${t.index}", which ${a.name} does not define`;
}

// One glyph per flag, so the palette shows behaviour without a tooltip. Solid and warp
// get the two shapes everyone already reads; a project's own flags get their initial.
function flagMark(flags){
  return flags.map(f=>f==='solid'?'▪':f==='warp'?'⇢':f[0].toUpperCase()).join('');
}

// ------------------------------------------------------------------ the tile panel
//
// The selected character, and every property of it that the manifest carries. Editing
// here writes the manifest immediately rather than waiting for Save map: the legend is
// project-wide, and a flag change means something to every map that paints the character,
// not just the one on screen. Save map saves the ROWS; this is not part of them.

// The tile panel for a map whose cells live in a file. Everything here edits the map's
// own tile table and is saved with the map -- no legend, no project-wide effect, and no
// character to run out of.
function tileInfoSource(){
  const box=$('#tileinfo');
  const t=S.map.tiles[S.ti], r=resolveTile(t);
  if(!r){ box.innerHTML='<small class="dim">no tile selected</small>'; return }
  const known=S.data.flags||{solid:1,warp:2};
  box.innerHTML='';

  const head=document.createElement('div');
  head.className='mini';
  head.innerHTML=`<img src="${r.uri}" style="width:32px;height:32px;`
    +`image-rendering:pixelated;${flipCss(r.flip)}">`
    +`<small><b>#${S.ti}</b> → ${r.role?'role "'+r.role+'"':'tile '+r.index}`
    +`<br><span class="dim">${r.atlas}</span></small>`;
  box.appendChild(head);

  const flags=document.createElement('div');
  flags.style.margin='.4rem 0';
  for(const name of Object.keys(known)){
    const on=(t.flag_names||[]).includes(name);
    const l=document.createElement('label');
    l.className='mini';
    l.innerHTML=`<input type="checkbox" ${on?'checked':''}> ${name}`
      +` <span class="dim">0x${known[name].toString(16).padStart(2,'0')}</span>`;
    l.querySelector('input').onchange=ev=>{
      const next=(t.flag_names||[]).filter(f=>f!==name);
      if(ev.target.checked) next.push(name);
      t.flag_names=next;
      // The byte is what the file stores; the names are what the page shows. Both are
      // kept so a reload does not have to re-derive one from the other.
      t.flags=next.reduce((b,n)=>b|(known[n]||0),0);
      S.dirty=true; mark(); drawLegend(); draw(); budget();
    };
    flags.appendChild(l);
  }
  box.appendChild(flags);

  const a=atlas(r.atlas);
  if(!(a&&a.metatiled)){
    const flip=document.createElement('div');
    for(const axis of ['x','y']){
      const on=[...(t.flip||'')].includes(axis);
      const l=document.createElement('label');
      l.className='mini';
      l.innerHTML=`<input type="checkbox" ${on?'checked':''}> flip ${axis.toUpperCase()}`;
      l.querySelector('input').onchange=ev=>{
        const set=new Set([...(t.flip||'')]);
        ev.target.checked?set.add(axis):set.delete(axis);
        t.flip=[...set].sort().join('');
        S.dirty=true; mark(); drawLegend(); draw();
      };
      flip.appendChild(l);
    }
    box.appendChild(flip);
  }

  const foot=document.createElement('small');
  foot.className='dim';
  foot.textContent='this map only — saved into '+(S.map.source||'its map file');
  box.appendChild(foot);
}

function tileInfo(){
  const box=$('#tileinfo'), ch=S.ch;
  // A `.pnxmap` has no legend characters, so its tiles are edited on the table entry
  // itself and saved with the map. A `rows` map keeps going through the legend, because
  // there the character IS the thing and it is shared with other maps.
  if(S.ti!=null && !ch && S.map && S.map.format==='source'){ tileInfoSource(); return }

  const r=ch?resolve(ch):null;
  if(!r){ box.innerHTML='<small class="dim">no tile selected</small>'; return }

  const e=LEG()[ch];
  const known=S.data.flags||{solid:1,warp:2};
  box.innerHTML='';

  const head=document.createElement('div');
  head.className='mini';
  head.innerHTML=`<img src="${r.uri}" style="width:32px;height:32px;`
    +`image-rendering:pixelated;${flipCss(r.flip)}">`
    +`<small><b>${ch}</b> → ${r.role?'role "'+r.role+'"':'tile '+r.index}`
    +`<br><span class="dim">${r.atlas}</span></small>`;
  box.appendChild(head);

  // Flags. A checkbox each, including the project's own, because the difference between
  // solid and walkable is the difference between a wall and a floor and it should not
  // take a text editor to say which one this is.
  const flags=document.createElement('div');
  flags.style.margin='.4rem 0';
  for(const name of Object.keys(known)){
    const on=(e.flags||[]).includes(name);
    const l=document.createElement('label');
    l.className='mini';
    l.innerHTML=`<input type="checkbox" ${on?'checked':''}> ${name}`
      +` <span class="dim">0x${known[name].toString(16).padStart(2,'0')}</span>`;
    l.querySelector('input').onchange=ev=>{
      const next=(e.flags||[]).filter(f=>f!==name);
      if(ev.target.checked) next.push(name);
      writeLegend(ch,{flags:next});
    };
    flags.appendChild(l);
  }
  box.appendChild(flags);

  // Mirroring, hidden for a metatiled atlas rather than offered and then refused: the
  // runtime does not flip composed tiles, so the choice does not exist there.
  const a=atlas(r.atlas);
  if(a&&!a.metatiled){
    const flip=document.createElement('div');
    for(const axis of ['x','y']){
      const on=r.flip.includes(axis);
      const l=document.createElement('label');
      l.className='mini';
      l.innerHTML=`<input type="checkbox" ${on?'checked':''}> flip ${axis.toUpperCase()}`;
      l.querySelector('input').onchange=ev=>{
        const next=r.flip.filter(f=>f!==axis);
        if(ev.target.checked) next.push(axis);
        writeLegend(ch,{flip:next});
      };
      flip.appendChild(l);
    }
    box.appendChild(flip);
  }

  const foot=document.createElement('div');
  foot.className='mini';
  const del=document.createElement('button');
  del.textContent='Remove';
  del.title='delete this legend character';
  const own=scopeOf(ch)==='map';
  del.onclick=async()=>{
    // Removed from the table it lives in. A project character deleted while another map
    // still paints it is refused by the server, which is why the scope is sent rather
    // than guessed from the map currently open.
    const r2=await post('/api/legend/remove',{char:ch, map:own?S.map.name:null});
    if(!r2.ok){ say(r2.error); return }
    S.ti=null; S.ch=null; await reload();
    say(`Removed ${ch} from ${own?`map "${S.map.name}"`:'the project legend'}.`,false);
  };
  // Which table this character lives in, said plainly next to the button that deletes it.
  // The two look identical on the canvas and behave completely differently: editing a
  // project character changes every map that paints it.
  const scope=document.createElement('small');
  scope.className='dim';
  scope.style.marginLeft='.4rem';
  scope.textContent=own?`this map only`:`project-wide — every map`;
  foot.appendChild(del);
  foot.appendChild(scope);
  box.appendChild(foot);

  // Defining a flag is rare enough to sit behind a click, and common enough that it
  // cannot only live in the manifest.
  const add=document.createElement('div');
  add.className='mini';
  const nf=document.createElement('button');
  nf.textContent='＋ flag';
  nf.title='name a new tile flag the game can test';
  nf.onclick=async()=>{
    const name=prompt('Name the flag (lowercase; it becomes TILE_FLAG_… in the header):');
    if(!name) return;
    const r2=await post('/api/flag',{name:name.trim()});
    if(!r2.ok){ say(r2.error); return }
    await reload();
    say(`${r2.name} is bit 0x${r2.bit.toString(16)} — test it with `
        +`TILE_FLAG_${r2.name.toUpperCase()}.`,false);
  };
  add.appendChild(nf);
  box.appendChild(add);
}

// One legend character, rewritten with some fields changed and the rest kept. Everything
// the manifest holds for it has to be resent, because the endpoint replaces the entry
// rather than patching it -- a partial write would silently drop the flags when you
// changed the flip.
async function writeLegend(ch,changes){
  const e=LEG()[ch], r=resolve(ch);
  // Rewritten in the table it already lives in. Sending no scope would move a map's own
  // character into the project table, which silently changes every other map.
  const body={char:ch, tile:e.tile, atlas:e.atlas||(r?r.atlas:null),
              flags:e.flags||[], flip:e.flip||[],
              map:scopeOf(ch)==='map'?S.map.name:null, ...changes};
  const res=await post('/api/legend',body);
  if(!res.ok){ say(res.error); tileInfo(); return }
  await reload();
  const at=(S.map.tiles||[]).findIndex(t=>t.ch===ch);
  selectTile(at>=0?at:S.ti, true);
  drawLegend(); draw();
}

// ------------------------------------------------------------------- the tile picker
//
// Every tile of every tileset the map draws from. Clicking one paints with it -- binding
// a legend character to it first if it does not have one yet, because "which tile" and
// "what does this tile mean" are the same decision and splitting them across two screens
// is what made the other 197 tiles of an atlas unreachable.

// Characters worth spending on a tile, in the order they get spent. Punctuation and
// digits first because they read as terrain in a rows block; letters after, where the
// case still distinguishes them. Space is excluded -- a rows block would not survive it.
const PICK_CHARS =
  ".,:;'\"!?*+-=/\\|<>()[]{}~^&%$@#0123456789"
  +"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";

function freeChar(){
  const taken=new Set(Object.keys(LEG()));
  return [...PICK_CHARS].find(c=>!taken.has(c))||null;
}

// The legend character already bound to this exact tile, flips included. Two characters
// for one tile is legal and sometimes wanted -- the same slab as scenery and as a door --
// but the picker should reuse rather than mint a duplicate nobody asked for.
function charFor(name,index,flip){
  const key=[...flip].sort().join('');
  return Object.keys(LEG()).find(ch=>{
    const r=resolve(ch);
    return r&&r.atlas===name&&r.index===index
      &&[...r.flip].sort().join('')===key;
  })||null;
}

function pickFlip(){
  return ['x','y'].filter(a=>$('#pickflip'+a).checked);
}

function drawTilePicker(){
  const body=$('#pickbody'); body.innerHTML='';
  const list=mapAtlases();
  const flip=pickFlip();
  if(!list.length){ body.innerHTML='<small>No atlas built yet — press Build.</small>'; return }

  for(const a of list){
    const h=document.createElement('div');
    h.className='palgroup';
    h.textContent=`${a.name} — ${a.tiles.length} tiles`
      +(a.metatiled?' (metatiled: cannot be flipped)':'');
    body.appendChild(h);

    const strip=document.createElement('div');
    strip.className='tiles';
    // A metatiled atlas cannot be drawn mirrored -- the runtime skips the flip for
    // composed tiles rather than mirroring the quadrant order -- so its tiles are shown
    // upright whatever the checkboxes say, instead of previewing a build that fails.
    const use=a.metatiled?[]:flip;
    a.tiles.forEach((uri,i)=>{
      const bound=charFor(a.name,i,use);
      const b=document.createElement('button');
      b.className='tile'+(bound&&bound===S.ch?' sel':'')+(bound?' used':'');
      const role=Object.keys(a.roles||{}).find(r=>a.roles[r]===i);
      b.title=`tile ${i} of ${a.name}`+(role?` — role "${role}"`:'')
        +(bound?` — painted as ${bound}`:' — click to give it a character');
      b.innerHTML=`<img src="${uri}" style="${flipCss(use)}">`
        +`<b>${bound||(role?role.slice(0,4):i)}</b>`;
      b.onclick=()=>bindTile(a.name,i,use,bound);
      // Right-click names the tile. A role is what game code calls it -- painting only
      // needs the index, but a door the game has to FIND needs a name, and that used to
      // mean hand-writing an [atlas.semantic] table.
      b.oncontextmenu=ev=>{ ev.preventDefault(); nameTile(a.name,i,role) };
      strip.appendChild(b);
    });
    body.appendChild(strip);
  }

  const free=freeChar();
  $('#pickhint').innerHTML=free
    ? `click a tile to paint with it${flip.length?' (mirrored '+flip.join('')+')':''}`
      +` · <b>right-click</b> to name it for game code`
    : 'this map has used all 92 legend characters — free one up in the sidebar, or move '
      +'a character that only this map paints out of the project legend';
}

// A role, written into [atlas.semantic]. Named tiles are how C refers to a tile at all:
// TILE_DOOR rather than the number 47, which changes the next time the sheet is recarved.
async function nameTile(atlasName,index,current){
  const role=prompt(
    `Name tile ${index} of ${atlasName}.\n\n`
    +`Game code will call it ${atlasName.replace(/[^A-Za-z0-9]/g,'_').toUpperCase()}`
    +`_TILE_<NAME>. Lowercase letters, digits and underscores.`,
    current||'');
  if(role===null) return;
  const want=role.trim();
  if(!want){
    if(!current) return;
    const r=await post('/api/role/remove',{atlas:atlasName,role:current});
    if(!r.ok){ say(r.error); return }
    await reload(); drawTilePicker();
    say(`${current} is no longer a name in ${atlasName}.`,false);
    return;
  }
  const r=await post('/api/role',{atlas:atlasName,role:want,index});
  if(!r.ok){ say(r.error); return }
  await reload(); drawTilePicker();
  // Pinning an autopicked name is allowed -- it is how a prototype becomes chosen art --
  // but it moves the tile every map drawing through that role uses, so it is said out
  // loud rather than left to be noticed after the next build.
  say(r.pinned
    ? `"${want}" was autopicked and is now pinned to tile ${index} of ${atlasName}.`
      +` Every map drawing through it moves. Build to update the header.`
    : `Tile ${index} of ${atlasName} is now "${want}". Build to update the header.`,
    false);
}

// Clicking a tile either selects the character that already draws it, or mints one. The
// minting is the point: it is what makes a tile with no role paintable, and it writes the
// manifest rather than holding the binding in the page, so what you painted is what
// builds.
async function bindTile(name,index,flip,bound){
  if(bound){
    // Already in this map's table: select it rather than adding a second entry for the
    // same tile, which would paint identically and read as a duplicate in the palette.
    const at=(S.map.tiles||[]).findIndex(t=>t.ch===bound);
    selectTile(at>=0?at:S.ti); drawTilePicker(); return;
  }

  const ch=freeChar();
  if(!ch){ say('every legend character is taken.'); return }

  // Minted into THIS MAP's legend, not the project one. A character costs a slot out of
  // the printable set, and painting a decorative tile is a decision about one map -- so
  // spending a project-wide slot on it used up the same ninety for every other map in the
  // game. The project table keeps the characters that mean the same thing everywhere.
  //
  // The atlas is still named explicitly even when it is the map's first, because a
  // character that resolves by default would resolve against a different tileset if the
  // map's atlas list is ever reordered, and mean a different tile.
  const r=await post('/api/legend',
    {char:ch, tile:index, atlas:name, flags:[], flip:flip, map:S.map.name});
  if(!r.ok){ say(r.error); return }
  await reload();
  // The new character arrives in the map's tile table via reload(); select it there.
  const at=(S.map.tiles||[]).findIndex(t=>t.ch===ch);
  selectTile(at>=0?at:null);
  drawTilePicker();
  say(`${ch} now paints tile ${index} of ${name}.`);
}

// ---------------------------------------------------------------- the tileset list
//
// A map's `atlases` list, which the editor could never edit: the toolbar had one select,
// so a map drawing from three tilesets could only ever be told about the first.

function drawSets(){
  const body=$('#setbody'); body.innerHTML='';
  const chosen=(S.map.atlases||[]).slice();

  chosen.forEach((name,i)=>{
    const row=document.createElement('div');
    row.className='setrow';
    row.innerHTML=`<b>${name}</b><span class="grow dim">${i===0?'default':''}</span>`;
    const up=document.createElement('button');
    up.textContent='↑'; up.title='earlier in the id space'; up.disabled=i===0;
    up.onclick=()=>{ chosen.splice(i-1,0,chosen.splice(i,1)[0]); setAtlases(chosen) };
    const rm=document.createElement('button');
    rm.textContent='✕'; rm.title='stop drawing from this tileset';
    rm.disabled=chosen.length<2;
    rm.onclick=()=>{ chosen.splice(i,1); setAtlases(chosen) };
    row.append(up,rm);
    body.appendChild(row);
  });

  const rest=S.data.atlases.filter(a=>!chosen.includes(a.name));
  if(rest.length){
    const add=document.createElement('div');
    add.className='setrow';
    add.innerHTML='<span class="grow dim">add</span>';
    const sel=document.createElement('select');
    sel.innerHTML=rest.map(a=>`<option>${a.name}</option>`).join('');
    const go=document.createElement('button');
    go.textContent='＋';
    go.onclick=()=>setAtlases(chosen.concat([sel.value]));
    add.append(sel,go);
    body.appendChild(add);
  }

  // The order is not cosmetic and the note says so, because reordering silently
  // renumbers every cell in the map when it is rebuilt.
  $('#setnote').innerHTML=
    'Order fixes this map\'s tile id space, and the first tileset is what a legend '
    +'character with no <code>atlas</code> of its own resolves against. '
    +'Each one is a pool slot on the watch.';
}

function setAtlases(list){
  if(!list.length) return;
  S.map.atlases=list; S.map.atlas=list[0];
  S.dirty=true; mark();
  drawSets(); drawLegend(); info(); draw(); budget();
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

// Options come from the server (state().platforms), not a list typed into the page, so
// the seven never drift from PLATFORMS in pnx_editor.py. Built once: state() sends the
// same catalog on every reload, and rebuilding the dropdown's options would drop
// whatever an author had picked mid-session.
function platformSelector(){
  const sel=$('#checkplatform');
  if(sel.options.length<=1){
    for(const name of Object.keys(S.data.platforms)){
      const p=S.data.platforms[name], o=document.createElement('option');
      o.value=name;
      o.textContent=`${name} — ${p.w}×${p.h}${p.round?' round':''}, `
                   +(p.bw?'1-bit':'colour');
      sel.appendChild(o);
    }
  }
  sel.value=S.checkPlatform||'';
  platformNote();
}

// This is a VIEW into what is already built, not the project's own screen -- so it reads
// S.data.platforms rather than S.data.screen/orientation, which describe the project's
// actual default build and do not change when this selector does.
function platformNote(){
  const name=S.checkPlatform, note=$('#platformnote');
  if(!name){
    note.textContent='Prices what is on disk for the project’s own default build.';
    return;
  }
  const p=S.data.platforms[name];
  note.textContent=`${p.w}×${p.h}${p.round?' round':''} · `
    +`${p.bw?'1-bit (ships its ‘~bw’ blobs where built)':'colour'} · `
    +`${KB(p.resources)} resources · ${KB(p.ram)} RAM`;
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
        body:JSON.stringify({maps,platform:S.checkPlatform||undefined})})).json();
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
    `${e.pct.toFixed(1)}% of ${e.platform?e.platform+"'s":'the'} appstore cap`
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
  const lbl=(S.map&&S.map.tiles&&S.ti!=null&&S.map.tiles[S.ti])
    ?(S.map.tiles[S.ti].ch!==undefined?S.map.tiles[S.ti].ch:'#'+S.ti):'—';
  $('#tool').innerHTML=S.mode==='paint'?`painting <kbd>${lbl}</kbd>`:
    S.mode==='warp'?'<kbd>click a door to add/remove a warp</kbd>':'<kbd>click to set start</kbd>';
}
function selectMap(i){
  // A project with no maps is an ordinary state -- a fresh one, or an example that is
  // only about audio -- and this used to throw on it: JSON.parse(JSON.stringify(undefined))
  // raises, at the last line of load(), leaving S.map null with every Maps control still
  // bound to it. The window came up and nothing in it did anything, which reads as a dead
  // editor rather than as an empty project.
  if(!S.data.maps.length || !S.data.maps[i]){ noMaps(); return }
  S.map=JSON.parse(JSON.stringify(S.data.maps[i]));
  // Normalised once, here, so nothing downstream has to know that `atlas` and `atlases`
  // are the same key spelled for one tileset or many.
  if(!S.map.atlases||!S.map.atlases.length)
    S.map.atlases=S.map.atlas?[S.map.atlas]:[];
  // The frame starts where the player does, which is the section an author is most
  // likely to want to look at first.
  if(!S.cam) S.cam={on:$('#camon').checked, x:0, y:0};
  const r=camRect();
  S.cam.x=Math.max(0,(S.map.start[0]+0.5)*S.T-r.w/2);
  S.cam.y=Math.max(0,(S.map.start[1]+0.5)*S.T-r.h/2);
  for(const id of ['#tilesets','#pick','#save']) $(id).disabled=false;
  S.dirty=false; mark(); drawLegend(); renderWarps(); warpForm(null); info(); draw();
  camInfo(); drawMapProps();
}
// What the Maps view shows when there is nothing to show. Says which of the two things is
// missing, because "add a map" is useless advice to a project with no tileset to draw one
// with -- the server refuses that, and refusing without saying so is how the old dead
// window felt.
function noMaps(){
  S.map=null; S.ch=null; S.ti=null; S.dirty=false; mark();
  const cv=$('#cv'); cv.width=cv.height=0;
  $('#legend').innerHTML='';
  $('#warps').innerHTML='<small>—</small>';
  $('#tileinfo').innerHTML='<small class="dim">—</small>';
  $('#mapinfo').innerHTML='<small class="dim">no maps yet</small>';
  $('#caminfo').textContent='—';
  $('#painthint').innerHTML = S.data.atlases.length
    ? 'This project has no maps. Name one below and press <b>＋ Map</b>.'
    : 'This project has no tilesets yet. Import a sheet on the <b>Import</b> tab, press '
      +'<b>Build</b>, then come back and add a map.';
  for(const id of ['#tilesets','#pick','#save']) $(id).disabled=true;
  $('#tool').textContent='';
}

// Every Maps control needs a map. They are re-enabled here rather than at each call site
// so a control added later cannot forget.
// Every Maps control needs a map, and a map is now cells rather than rows -- a
// `.pnxmap` has no rows at all, so testing for them would have declared every source map
// missing and greyed out the whole tab.
function haveMap(){ return !!(S.map && S.map.cells && S.map.cells.length) }

$('#camon').onchange=e=>{
  if(!S.cam) S.cam={on:true,x:0,y:0};
  S.cam.on=e.target.checked; if(haveMap()){ draw(); camInfo() }
};
$('#tilesets').onclick=()=>{ if(!haveMap())return; drawSets();
  $('#setwrap').style.display='flex' };
$('#setclose').onclick=()=>{ $('#setwrap').style.display='none' };
$('#pick').onclick=()=>{ if(!haveMap())return; drawTilePicker();
  $('#pickwrap').style.display='flex' };
$('#pickclose').onclick=()=>{ $('#pickwrap').style.display='none' };
$('#pickflipx').onchange=drawTilePicker;
$('#pickflipy').onchange=drawTilePicker;
// Clicking the backdrop closes; clicking the sheet must not. Overlays that swallow a
// misplaced click are the ones people stop trusting.
for(const id of ['#pickwrap','#setwrap'])
  $(id).onclick=e=>{ if(e.target===$(id)) $(id).style.display='none' };
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
  if(!haveMap()) return;
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
  if(!haveMap()) return;
  const m=S.map;
  const sets=(m.atlases&&m.atlases.length?m.atlases:[m.atlas]).filter(Boolean);
  $('#mapinfo').innerHTML=`<small>${m.w}×${m.h} · `+
    `${sets.length>1?'tilesets':'tileset'} <b>${sets.join(', ')||'—'}</b> · `+
    `start (${m.start})<br>`+
    (m.warps.length?m.warps.map(w=>`warp (${w.at}) → ${w.to[0]} (${w.to[1]},${w.to[2]})`).join('<br>'):'no warps')+'</small>';
}

function draw(){
  // Tile images call this from onload, which can land after the view has moved to a
  // project or a state with no map.
  if(!haveMap()) return;
  const m=S.map,T=S.T,cv=$('#cv'),g=cv.getContext('2d');
  cv.width=m.w*T; cv.height=m.h*T;
  g.imageSmoothingEnabled=false;
  g.fillStyle='#000'; g.fillRect(0,0,cv.width,cv.height);
  for(let y=0;y<m.h;y++)for(let x=0;x<m.w;x++){
    const ti=m.cells[y*m.w+x], im=S.img[ti];
    if(!im||!im.complete) continue;
    // A flipped tile has to be drawn flipped HERE too. The watch mirrors it from two bits
    // in the cell, so an editor that drew it upright would be showing a map that does not
    // exist -- and mirrored tiles are placed precisely because the mirroring is what you
    // are looking at.
    const flip=[...(((m.tiles[ti]||{}).flip)||'')];
    if(!flip.length){ g.drawImage(im,x*T,y*T,T,T); continue }
    const sx=flip.includes('x')?-1:1, sy=flip.includes('y')?-1:1;
    g.save();
    g.translate(x*T+(sx<0?T:0), y*T+(sy<0?T:0));
    g.scale(sx,sy);
    g.drawImage(im,0,0,T,T);
    g.restore();
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
  if(!haveMap()||S.ti==null) return;
  const r=e.target.getBoundingClientRect();
  const x=Math.floor((e.clientX-r.left)/S.T), y=Math.floor((e.clientY-r.top)/S.T);
  const m=S.map;
  if(x<0||y<0||y>=m.h||x>=m.w) return;

  if(S.mode==='start'){ if(!click)return; m.start=[x,y]; S.mode='paint'; }
  else if(S.mode==='warp'){
    if(!click)return;
    const i=m.warps.findIndex(w=>w.at[0]===x&&w.at[1]===y);
    if(i>=0){ m.warps.splice(i,1); S.mode='paint'; warpForm(null); }
    else warpForm([x,y]);
  } else {
    // A cell is an index into the map's tile table, which is what both authoring formats
    // reduce to -- so painting is the same operation whether the map is text or a file.
    const at=y*m.w+x;
    if(m.cells[at]===S.ti) return;
    m.cells[at]=S.ti;
  }
  S.dirty=true; mark(); info(); tool(); draw(); budget();
}

addEventListener('keydown',e=>{
  if(e.target.tagName==='SELECT')return;
  // Escape closes whichever overlay is open before it touches the paint mode, so the
  // key that means "get me out of this" does.
  if(e.key==='Escape'){
    for(const id of ['#pickwrap','#setwrap']){
      if($(id).style.display!=='none'){ $(id).style.display='none'; return }
    }
  }
  if(!haveMap()) return;
  if(e.key==='w'||e.key==='W'){S.mode='warp';tool()}
  if(e.key==='s'||e.key==='S'){S.mode='start';tool()}
  if(e.key==='Escape'){S.mode='paint';tool()}
});
$('#mapsel').onchange=e=>{
  if(S.dirty&&!confirm('Discard unsaved changes to this map?')){
    e.target.value=S.data.maps.findIndex(m=>m.name===(S.map&&S.map.name)); return;
  }
  selectMap(+e.target.value);
};
$('#save').onclick=async()=>{
  if(!haveMap()) return;
  const body=JSON.parse(JSON.stringify(S.map));
  if(body.format!=='source'){
    // A text map is stored as characters, so the cell grid is rendered back through each
    // tile's own character -- which is why the table carries it. Reassigning characters
    // here instead would churn the whole map's diff every time it was opened.
    const missing=body.tiles.findIndex(t=>t.ch===undefined);
    if(missing>=0){
      say(`tile #${missing} has no legend character, so this map cannot be saved as text.`);
      return;
    }
    body.rows=[];
    for(let y=0;y<body.h;y++){
      let row='';
      for(let x=0;x<body.w;x++) row+=body.tiles[body.cells[y*body.w+x]].ch;
      body.rows.push(row);
    }
  }
  const r=await (await fetch('/api/map',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
  const log=$('#log');
  log.className=r.ok?'ok':'bad';
  log.textContent=r.ok
    ?`Saved ${S.map.name} to ${S.map.format==='source'?S.map.source:'the manifest'}.`
    :r.error;
  if(r.ok){S.dirty=false;mark()}
};
$('#newmap').onclick=async()=>{
  const name=$('#nmname').value.trim();
  if(!name){alert('Name the map first.');return}
  const r=await (await fetch('/api/newmap',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name,w:+$('#nmw').value,h:+$('#nmh').value,
      // The tileset the map being looked at uses, which is nearly always the one a new
      // map next to it wants -- and the only one whose legend characters are certain to
      // resolve, so the blank room it comes with is paintable.
      atlas:(S.map&&S.map.atlases&&S.map.atlases[0])
            ||(S.data.atlases[0]&&S.data.atlases[0].name)})})).json();
  const log=$('#log'); log.className=r.ok?'ok':'bad';
  if(!r.ok){log.textContent=r.error;return}
  log.textContent=`Created map "${name}" and a scene for it. Press Build.`;
  $('#nmname').value='';
  await load();
  const i=S.data.maps.findIndex(m=>m.name===name);
  $('#mapsel').value=i; selectMap(i);
};
// Map properties. Written on change rather than behind a Save, matching the legend and
// the scene panel: every one of these goes straight into the manifest.
function drawMapProps(){
  if(!S.map||!$('#mppal')) return;
  const m=S.map;
  $('#mppal').innerHTML='<option value="">— none —</option>'
    +(m.palettes||[]).map(p=>`<option${p===m.palette?' selected':''}>${p}</option>`)
      .join('');
  $('#mppal').value=m.palette||'';
  $('#mpwt').value=String(m.worldtile==null?'auto':m.worldtile);
  $('#mpslots').value=m.atlas_slots==null?'':m.atlas_slots;
  $('#mpbank').value=m.bank_bytes==null?'':m.bank_bytes;
  $('#mpres').checked=!!m.resident;
  $('#mplog').className='dim';
  $('#mplog').textContent=(m.palettes&&m.palettes.length)?'':
    'no palette variants on this map’s tilesets';
}

async function writeMapProps(changes){
  if(!S.map) return;
  const r=await post('/api/map/props',{name:S.map.name,...changes});
  const log=$('#mplog');
  if(!r.ok){ log.className='bad'; log.textContent=r.error; drawMapProps(); return }
  await load();
  const i=S.data.maps.findIndex(x=>x.name===S.map.name);
  if(i>=0) selectMap(i);
  log.className='ok'; log.textContent='Saved. Press Build.';
  budget(true);
}

// An empty box means "unset", which is how a map goes back to the pipeline's own choice
// -- distinct from a number, and the reason these send "" rather than omitting the key.
$('#mppal').onchange =()=>writeMapProps({palette:$('#mppal').value});
$('#mpwt').onchange  =()=>writeMapProps({worldtile:$('#mpwt').value});
$('#mpslots').onchange=()=>writeMapProps({atlas_slots:$('#mpslots').value});
$('#mpbank').onchange =()=>writeMapProps({bank_bytes:$('#mpbank').value});
$('#mpres').onchange  =()=>writeMapProps({resident:$('#mpres').checked});

// Moving a map out of the manifest and into its own file. Offered rather than done for
// you, and never in reverse automatically: the trade is real in both directions -- a file
// gets you the tile ceiling and the manifest back, and costs you a readable git diff.
$('#cvtmap').onclick=async()=>{
  if(!S.map){ say('No map selected.'); return }
  if(S.map.format==='source'){
    say(`"${S.map.name}" already lives in ${S.map.source}.`); return;
  }
  if(S.dirty){ say('Save this map first — converting reads what is on disk.'); return }
  if(!confirm(`Move "${S.map.name}" into its own .pnxmap file?\n\n`
      +`Its grid, start and warps leave the manifest. Tilesets, palette and streaming `
      +`settings stay.\n\nYou gain the ~90-tile ceiling and a smaller manifest. You `
      +`lose a readable diff on map changes.`)) return;
  const r=await post('/api/map/migrate',{name:S.map.name});
  if(!r.ok){ say(r.error); return }
  await load();
  const i=S.data.maps.findIndex(m=>m.name===S.map.name);
  if(i>=0){ $('#mapsel').value=i; selectMap(i) }
  say(`Moved to ${r.source} — ${r.tiles} tile entries, ${r.bytes} B.`,false);
};

$('#renmap').onclick=async()=>{
  if(!S.map){ say('No map selected.'); return }
  const to=prompt(`Rename map "${S.map.name}" to:\n\n`
    +`Warps aimed at it and scenes that load it are updated too. `
    +`Lowercase letters, digits and underscores.`, S.map.name);
  if(!to||to.trim()===S.map.name) return;
  const r=await post('/api/map/rename',{name:S.map.name, to:to.trim()});
  if(!r.ok){ say(r.error); return }
  await load();
  const i=S.data.maps.findIndex(m=>m.name===to.trim());
  $('#mapsel').value=i; selectMap(i);
  say(`Renamed to "${to.trim()}". Press Build.`,false);
};

// Asks what still points at the map BEFORE confirming, so the dialog says "the cave warps
// to it" rather than offering a delete that is then refused -- the same shape as removing
// an atlas.
$('#delmap').onclick=async()=>{
  if(!S.map){ say('No map selected.'); return }
  const name=S.map.name;
  const u=await post('/api/map/users',{name});
  if(u.users&&u.users.length){
    say(`Cannot delete "${name}" — ${u.users.join('; ')}.`);
    return;
  }
  if(!confirm(`Delete map "${name}"?\n\n`
              +`Nothing points at it. Its rows and its own legend go with it.`)) return;
  const r=await post('/api/map/remove',{name});
  if(!r.ok){ say(r.error); return }
  await load();
  $('#mapsel').value=0; selectMap(0);
  say(`Deleted "${name}". Press Build.`,false);
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

// A VIEW setting, unlike orientation: picking a platform here re-prices what is already
// on disk against a different target, it never touches the manifest or the pipeline.
$('#checkplatform').onchange=()=>{
  S.checkPlatform=$('#checkplatform').value;
  platformNote();
  budget(true);
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
        sdk=which==='sdk', pix=which==='pixel', cod=which==='code',
        scn=which==='scenes', dlg=which==='dialog', mus=which==='music',
        dev=which==='device';
  $('#import').style.display=imp?'block':'none';
  $('#fonts').style.display=fnt?'block':'none';
  $('#sdk').style.display=sdk?'block':'none';
  $('#pixel').style.display=pix?'block':'none';
  $('#code').style.display=cod?'block':'none';
  $('#scenes').style.display=scn?'block':'none';
  $('#dialog').style.display=dlg?'block':'none';
  $('#music').style.display=mus?'block':'none';
  $('#device').style.display=dev?'block':'none';
  if(mus) drawMusic();
  if(scn) drawScenes();
  if(dlg) drawDialog();
  if(fnt) drawFontList();
  if(sdk){ sdkStatus(); updCheck(); drawProject() }
  if(pix&&!PX.data){ pxPalette(); pxInit(+$('#pxw').value,+$('#pxh').value,1); pxLoadList() }
  if(cod&&!$('#codelist').children.length) codeTree();
  if(dev) emuEnter(); else emuLeave();
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
    pixel:'Sprites',code:'Code',sdk:'Settings',scenes:'Scenes',
    dialog:'Dialog',music:'Music',device:'Device'}[which]||'';
}

// ------------------------------------------------------- declaring a sprite
//
// The Sprites tab could paint a PNG and not declare it, so the art existed and nothing
// could load it. Frames are derived from a width, a height and a count rather than typed
// as rectangles: a sheet is frames stacked vertically, which is the layout both the
// painter above and `[[sprite]] frames` already assume.

// Frames picked off a sheet, in click order. Null means "none picked", which is different
// from an empty list and is why this is not just an array: with no picks the declare form
// falls back to deriving a vertical stack from the canvas size, which is what it did
// before and is still right for art painted here.
const SH={cells:null, picks:[], sheet:null};

function spFrames(){
  if(SH.picks.length) return SH.picks.map(i=>{
    const c=SH.cells[i]; return [c.x,c.y,c.w,c.h];
  });
  const w=+$('#spfw').value, h=+$('#spfh').value, n=+$('#spn').value;
  return Array.from({length:n},(_,i)=>[0,i*h,w,h]);
}

function shLog(msg,bad){
  const el=$('#shlog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg;
}

function drawSheetGrid(){
  const grid=$('#shgrid'); grid.innerHTML='';
  if(!SH.cells){ $('#shplate').style.display='none'; return }
  $('#shplate').style.display='';
  $('#shcount').textContent=`${SH.cells.length} cells · ${SH.picks.length} picked`;
  SH.cells.forEach((c,i)=>{
    const at=SH.picks.indexOf(i);
    const b=document.createElement('button');
    b.className='tile'+(at>=0?' sel':'')+(c.blank?' used':'');
    b.title=`${c.x},${c.y} ${c.w}x${c.h}`+(c.blank?' — blank':'')
      +(at>=0?` — frame ${at}`:'');
    b.innerHTML=`<img src="${c.img}" alt="">`
      +`<b>${at>=0?at:(c.blank?'·':'')}</b>`;
    b.onclick=()=>{
      const k=SH.picks.indexOf(i);
      if(k>=0) SH.picks.splice(k,1); else SH.picks.push(i);
      drawSheetGrid();
      // The declare form's size boxes follow the picks, so what is about to be written
      // and what is on screen cannot disagree.
      if(SH.picks.length){
        $('#spfw').value=c.w; $('#spfh').value=c.h; $('#spn').value=SH.picks.length;
      }
      shLog(SH.picks.length?`${SH.picks.length} frame(s) picked — they become anim `
            +`indices 0..${SH.picks.length-1}.`
            :'Click frames in the order the animation plays.');
    };
    // Editing one pose out of a sheet, which the canvas could not do: it opened whole
    // files, so touching one frame of an eight-pose sheet meant loading all eight.
    b.ondblclick=async ev=>{
      ev.preventDefault();
      const r=await post('/api/frame/read',
        {sheet:SH.sheet, x:c.x, y:c.y, w:c.w, h:c.h});
      if(r.error){ shLog(r.error,true); return }
      PX.w=r.w; PX.h=r.h; PX.frames=1;
      PX.data=Uint8Array.from(r.pixels); PX.undo=[];
      PX.origin={sheet:SH.sheet, x:c.x, y:c.y};
      $('#pxw').value=r.w; $('#pxh').value=r.h; $('#pxframes').value=1;
      $('#pxtitle').textContent=`Canvas — ${SH.sheet} @ ${c.x},${c.y}`;
      $('#pxnote').textContent=`Editing one frame. Save writes it back into the sheet.`;
      pxDraw();
      shLog(`Editing frame at ${c.x},${c.y}.`,false);
    };
    grid.appendChild(b);
  });
}

$('#shslice').onclick=async()=>{
  const sheet=$('#shsheet').value;
  if(!sheet){ shLog('No PNG in the project to slice.',true); return }
  const r=await post('/api/sheet/frames',{sheet, fw:+$('#shfw').value, fh:+$('#shfh').value,
    ox:+$('#shox').value, oy:+$('#shoy').value,
    gx:+$('#shgx').value, gy:+$('#shgy').value});
  if(r.error){ shLog(r.error,true); return }
  SH.cells=r.cells; SH.picks=[]; SH.sheet=sheet;
  $('#spsheet').value=sheet;
  drawSheetGrid();
  shLog(`${r.cols}x${r.rows} of ${$('#shfw').value}x${$('#shfh').value}`
    +(r.capped?` — showing the first ${r.limit}`:'')
    +'. Click frames in play order.');
};

$('#shclear').onclick=()=>{
  SH.picks=[]; drawSheetGrid();
  shLog('Picks cleared — the declare form is back to a vertical stack.');
};

function spLog(msg,bad){
  const el=$('#splog');
  el.className=bad===false?'ok':(bad?'bad':'');
  el.textContent=msg||'—';
}

function drawSpriteForm(){
  const sel=$('#spsel'), cur=sel.value;
  const list=(S.data&&S.data.sprites)||[];
  sel.innerHTML='<option value="">— new —</option>'
    +list.map(s=>`<option${s.name===cur?' selected':''}>${s.name}</option>`).join('');
  const sheets=$('#spsheet');
  const arts=(S.art||[]).map(a=>a.path);
  sheets.innerHTML=arts.map(p=>`<option>${p}</option>`).join('');
  // The strip follows the selection through a reload, so saving a declaration does not
  // silently leave the frames of the sprite that was selected before it.
  if(typeof spShowFrames==='function') spShowFrames(sel.value);
}

// Loading an existing sprite back into the form. Frame rectangles collapse to w/h/count
// only when they really are a vertical stack; anything else is left to the manifest
// rather than silently rewritten into a shape it never had.
// tools/pnx_assets.py's pack_sprite derives a variant's NAME from its path the same way:
// basename, extension stripped. Kept in step so the dropdown offers exactly the names the
// pipeline would actually accept for bw_variant.
function spVariantName(path){
  return path.split('/').pop().replace(/\.[^.]*$/,'');
}

$('#spsel').onchange=()=>{
  const name=$('#spsel').value;
  if(!name){ spLog(''); return }
  const s=(S.data.sprites||[]).find(x=>x.name===name);
  if(!s) return;
  $('#spname').value=s.name;
  $('#spsheet').value=s.sheet;
  const f=s.frames||[];
  $('#spfw').value=s.w||16; $('#spfh').value=s.h||24; $('#spn').value=f.length||1;
  const stacked=f.every((r,i)=>r[0]===0&&r[1]===i*(s.h||0)&&r[2]===s.w&&r[3]===s.h);
  // The anim map is name -> frame index; the box takes them in frame order.
  const byIndex=[];
  for(const [k,v] of Object.entries(s.anim||{})) byIndex[v]=k;
  $('#spanim').value=byIndex.filter(Boolean).join(',');
  spLog(stacked?'':'frames are not a vertical stack — saving will rewrite them as one',
        stacked?null:true);
  const bw=$('#spbw'), names=(s.variants||[]).map(spVariantName);
  bw.innerHTML='<option value="">— base —</option>'
    +names.map(n=>`<option${n===s.bw_variant?' selected':''}>${n}</option>`).join('');
  bw.disabled=!names.length;
  spShowFrames(name);
};

// The declared sprite's frames, opened straight into the canvas.
//
// Rects come from the manifest rather than from a slicer, which is the whole point: the
// canvas used to guess the frame split from file height against whatever height it
// happened to be showing, so a 24x144 sheet opened as six frames of 24x24 while the
// declaration next to it said four of 24x36.
//
// Clicking one sets PX.origin, so Save composites that pose back into the sheet at its own
// rect and leaves the others alone -- the path the sheet slicer already proved.
async function spShowFrames(name){
  const box=$('#spframes'), log=$('#spframelog');
  box.innerHTML='';
  if(!name){ log.className='dim'; log.textContent='Pick a sprite to open its frames.'; return }

  const r=await post('/api/sprite/frames',{name});
  if(r.error){ log.className='bad'; log.textContent=r.error; return }

  r.cells.forEach(c=>{
    const b=document.createElement('button');
    b.className='cell'+(c.blank?' blank':'');
    b.title=`frame ${c.i} — ${c.w}x${c.h} at ${c.x},${c.y}\nclick to edit it`;
    b.innerHTML=`<img src="${c.img}" alt=""><span>${c.i}</span>`;
    b.onclick=async()=>{
      const f=await post('/api/frame/read',{sheet:r.sheet,x:c.x,y:c.y,w:c.w,h:c.h});
      if(f.error){ log.className='bad'; log.textContent=f.error; return }
      PX.w=f.w; PX.h=f.h; PX.frames=1;
      PX.data=Uint8Array.from(f.pixels); PX.undo=[];
      PX.origin={sheet:r.sheet, x:c.x, y:c.y};
      $('#pxw').value=f.w; $('#pxh').value=f.h; $('#pxframes').value=1;
      $('#pxtitle').textContent=`Canvas — ${name} frame ${c.i}`;
      $('#pxnote').textContent='Editing one frame. Save writes it back into the sheet.';
      pxDraw();
      log.className='dim';
      log.textContent=`Editing ${name} frame ${c.i} (${c.w}x${c.h} at ${c.x},${c.y}).`;
      // The canvas is above the fold on a watch-sized window; scrolling to it is the
      // difference between "nothing happened" and "it opened".
      $('#pxcv').scrollIntoView({block:'nearest'});
    };
    box.appendChild(b);
  });

  // A frame hanging off its sheet is reported rather than hidden: it is exactly what a
  // re-imported, smaller sheet looks like, and it will fail the build later with less
  // context than this.
  if(r.out_of_bounds && r.out_of_bounds.length){
    log.className='bad';
    log.textContent=`frame(s) ${r.out_of_bounds.join(', ')} run past `
      +`${r.sheet} (${r.sheet_size[0]}x${r.sheet_size[1]}) and cannot be opened.`;
  }else{
    log.className='dim';
    log.textContent=`${r.cells.length} frame(s) of ${r.sheet}. Click one to edit it.`;
  }
}

$('#spsave').onclick=async()=>{
  const name=$('#spname').value.trim();
  if(!name){ spLog('Name the sprite first.',true); return }
  const sheet=$('#spsheet').value;
  if(!sheet){ spLog('No PNG in the project to point at. Paint and save one first.',true);
              return }
  const names=$('#spanim').value.split(',').map(s=>s.trim()).filter(Boolean);
  const anim={};
  names.forEach((n,i)=>{ anim[n]=i });

  const frames=spFrames();
  // Frames picked off the sheet win over the vertical stack, and they carry their own
  // sheet with them: picking poses out of one file and declaring them against another
  // would validate and then draw the wrong art.
  const useSheet=SH.picks.length?SH.sheet:sheet;
  // variants/colorkey have no edit control of their own in this panel -- carried through
  // from what is already declared so saving a name/frame change does not silently drop
  // them. bw_variant DOES have a control (#spbw), and depends on variants surviving this.
  const existing=(S.data.sprites||[]).find(x=>x.name===$('#spsel').value)||{};
  const variants=existing.variants||[];
  const colorkey=existing.colorkey||null;
  const bwVariant=$('#spbw').value||null;
  // Validated through the real pack_sprite before anything is written, the same way an
  // atlas carve is: a frame running off the sheet used to go into the manifest and only
  // fail when Build was pressed, leaving a broken block to remove by hand.
  const v=await post('/api/sprite/validate',
    {name,sheet:useSheet,frames,anim,variants,colorkey,bw_variant:bwVariant});
  if(!v.ok){ spLog(v.error,true); return }

  const r=await post('/api/sprite/save',
    {name,sheet:useSheet,frames,anim,variants,colorkey,bw_variant:bwVariant});
  if(!r.ok){ spLog(r.error,true); return }
  await load(); drawSpriteForm(); $('#spsel').value=name;
  spLog(`Saved "${name}" — ${v.frames} frame(s) of ${v.w}x${v.h}`
    +`${SH.picks.length?' picked from '+useSheet:''}. Press Build.`,false);
  budget(true);
};

$('#spdel').onclick=async()=>{
  const name=$('#spsel').value||$('#spname').value.trim();
  if(!name){ spLog('Pick a sprite to remove.',true); return }
  const u=await post('/api/sprite/users',{name});
  if(u.users&&u.users.length){
    spLog(`Cannot remove "${name}" — ${u.users.join(', ')} loads it.`,true); return;
  }
  if(!confirm(`Remove sprite "${name}" from the manifest?\n\n`
              +`Nothing loads it. The PNG on disk is left alone.`)) return;
  const r=await post('/api/sprite/remove',{name});
  if(!r.ok){ spLog(r.error,true); return }
  await load(); drawSpriteForm(); $('#spsel').value='';
  spLog(`Removed "${name}". Press Build.`,false);
  budget(true);
};

// --------------------------------------------------------- removing a font
//
// The list is rebuilt from state rather than held, so a font added in the panel above
// appears here without a reload.

function drawFontList(){
  const sel=$('#fdelsel');
  if(!sel) return;
  const cur=sel.value;
  const list=(S.data&&S.data.fonts)||[];
  sel.innerHTML=list.map(f=>`<option${f.name===cur?' selected':''}>${f.name}</option>`)
    .join('')||'<option value="">— none —</option>';
}

$('#fdelbtn').onclick=async()=>{
  const name=$('#fdelsel').value;
  const log=$('#fdellog');
  if(!name){ log.className='bad'; log.textContent='No font to remove.'; return }
  // Asked before confirming, so the dialog names the scene rather than offering a delete
  // that is then refused.
  const u=await post('/api/font/users',{name});
  if(u.users&&u.users.length){
    log.className='bad';
    log.textContent=`Cannot remove "${name}" — ${u.users.join(', ')} loads it.`;
    return;
  }
  if(!confirm(`Remove font "${name}" from the manifest?\n\n`
              +`Nothing loads it. The TTF in art/fonts/ is left alone.`)) return;
  const r=await post('/api/font/remove',{name});
  if(!r.ok){ log.className='bad'; log.textContent=r.error; return }
  await load(); drawFontList();
  log.className='ok';
  log.textContent=`Removed "${name}". Press Build.`;
  budget(true);
};

// ------------------------------------------------------------------ project keys

function drawProject(){
  const d=S.data||{}, p=d.paths||{};
  if(!$('#prname')||d.no_project) return;
  $('#prname').value=d.name||'';
  $('#prbudget').value=d.budget||262144;
  // Shown relative to the project root, which is how the manifest states them -- an
  // absolute path here would be written back as one and stop building elsewhere.
  $('#prres').value=d.project_resources||'';
  $('#prhdr').value=d.project_header||'';
}

$('#prsave').onclick=async()=>{
  const log=$('#prlog');
  const want=[['name',$('#prname').value],['budget_bytes',$('#prbudget').value],
              ['resources',$('#prres').value],['header',$('#prhdr').value]];
  for(const [key,value] of want){
    const r=await post('/api/project/set',{key,value});
    if(!r.ok){ log.className='bad'; log.textContent=`${key}: ${r.error}`; return }
  }
  await load(); drawProject(); statusbar(); budget(true);
  log.className='ok';
  log.textContent='Saved. The budget strip and status bar now measure against it.';
};

// -------------------------------------------------------------------------- music
//
// A tracker over the sequencer's own model. The cell spelling is the MANIFEST's --
// `NOTE:INSTRUMENT`, '.' to hold, '-' to release -- rather than a prettier one invented
// here, because a song half-edited by hand and half in this tool has to stay one song.

const MU = { song: 0, pattern: 0, inst: 0, rows: null, octave: 4, row: 0 };

function muSong(){ return ((S.data && S.data.songs) || [])[MU.song] || null; }

function muSay(id, msg, bad){
  const el = $(id);
  if(!el) return;
  el.className = (id === '#mslog' ? 'mini ' : '') + (bad === false ? 'ok' : (bad ? 'bad' : 'dim'));
  el.textContent = msg || '';
}

// A row is fixed-width cells separated by spaces. Split on whitespace to read; pad to a
// column on write, so the manifest stays readable as a grid rather than becoming ragged
// the first time it is saved.
function muCells(row, channels){
  const c = row.trim().split(/\s+/).filter(s => s.length);
  while(c.length < channels) c.push('.');
  return c.slice(0, channels);
}
function muRow(cells){
  return cells.map(c => (c || '.').padEnd(5, ' ')).join(' ');
}

function drawMusic(){
  const songs = (S.data && S.data.songs) || [];
  const sel = $('#msong');
  sel.innerHTML = songs.map((s, i) => `<option value="${i}">${s.name}</option>`).join('');
  if(!songs.length){
    $('#mrows').innerHTML = '<small class="dim">No [music.*] in this manifest.</small>';
    $('#minstbody').innerHTML = '';
    drawSamples();
    return;
  }
  if(MU.song >= songs.length) MU.song = 0;
  sel.value = String(MU.song);
  const s = songs[MU.song];

  $('#mtempo').value = s.tempo;
  $('#morder').value = s.order.join(', ');
  $('#mcost').textContent = `${s.patterns.length} patterns x ${s.rows_per} rows x `
    + `${s.channels} ch - ${s.bytes} B` + (s.has_synth ? ' - synth' : ' - envelopes');

  const pat = $('#mpat');
  pat.innerHTML = s.patterns.map((_, i) => `<option value="${i}">${i}</option>`).join('');
  if(MU.pattern >= s.patterns.length) MU.pattern = 0;
  pat.value = String(MU.pattern);

  const inst = $('#minst');
  inst.innerHTML = s.instruments.map((x, i) =>
    `<option value="${i}">${i} - ${x.name || x.wave}</option>`).join('');
  if(MU.inst >= s.instruments.length) MU.inst = 0;
  inst.value = String(MU.inst);

  drawTracker();
  drawInstrument();
  drawSamples();
}

// Note names both ways. A tracker shows `C-4`; the manifest writes `C4`. The dash is the
// tracker's own device for keeping a sharp and a natural the same width so columns line up,
// and it is worth keeping on screen and dropping on save.
const NOTE_NAMES = ['C-','C#','D-','D#','E-','F-','F#','G-','G#','A-','A#','B-'];

// The inverse of parse_note in pnx_assets.py, which maps C4 to MIDI 60 -- so the octave
// is floor(n/12) MINUS ONE. Getting that wrong wrote every keyboard-entered note an octave
// high, which builds cleanly and plays wrong, and is invisible unless you can hear it.
function midiToTracker(n){
  return NOTE_NAMES[n % 12] + (Math.floor(n / 12) - 1);
}

// `C-4` on screen becomes `C4` in the manifest. The dash exists so a natural and a sharp
// occupy the same width and the columns line up; the manifest has no columns and does not
// want it.
function toManifestNote(s){
  return s.trim().replace(/^([A-G])-(-?\d)$/, '$1$2');
}

// A cell is `NOTE:INSTRUMENT`, '.' to hold, '-' to release. Split for display so note and
// instrument are separate fields -- they are separate decisions, and one text box for both
// means retyping the instrument to change a note.
function splitCell(cell){
  const c = (cell || '.').trim();
  if(c === '.') return { note: '', inst: '' };
  if(c === '-') return { note: 'off', inst: '' };
  const i = c.indexOf(':');
  const note = i < 0 ? c : c.slice(0, i);
  // Shown in tracker form so the grid stays aligned; stored without the dash.
  const shown = note.replace(/^([A-G])(-?\d)$/, '$1-$2');
  return { note: shown, inst: i < 0 ? '' : c.slice(i + 1) };
}
function joinCell(note, inst){
  const n = toManifestNote(note || '');
  if(!n) return '.';
  if(n === 'off' || n === '-' || n === '===') return '-';
  const i = (inst || '').trim();
  return i ? `${n}:${i}` : n;
}

// The piano row, as every tracker has mapped it since Fasttracker: the home row is the
// naturals and the row above is the sharps, so a keyboard is a keyboard.
const PIANO = { z:0, s:1, x:2, d:3, c:4, v:5, g:6, b:7, h:8, n:9, j:10, m:11,
                q:12, '2':13, w:14, '3':15, e:16, r:17, '5':18, t:19, '6':20,
                y:21, '7':22, u:23 };

function drawTracker(){
  const s = muSong();
  const box = $('#mrows');
  box.innerHTML = '';
  if(!s) return;
  MU.rows = s.patterns[MU.pattern].map(r => muCells(r, s.channels));

  const head = document.createElement('div');
  head.className = 'thead';
  const hn = document.createElement('b');
  hn.textContent = '';
  head.appendChild(hn);
  for(let c = 0; c < s.channels; c++){
    const sp = document.createElement('span');
    sp.textContent = `ch ${c + 1}`;
    head.appendChild(sp);
  }
  box.appendChild(head);

  MU.rows.forEach((cells, ri) => {
    const row = document.createElement('div');
    row.className = 'trow' + (ri % 4 === 0 ? ' beat' : '');
    const n = document.createElement('b');
    n.textContent = String(ri).padStart(2, '0');
    row.appendChild(n);

    cells.forEach((cell, ci) => {
      const step = document.createElement('div');
      step.className = 'tstep';
      const parts = splitCell(cell);

      const note = document.createElement('input');
      note.className = 'tnote' + (parts.note ? (parts.note === 'off' ? ' off' : ' on') : '');
      note.value = parts.note === 'off' ? '===' : (parts.note || '---');
      note.spellcheck = false;
      note.title = `row ${ri}, channel ${ci + 1} - type a note, or . to clear`;

      const inst = document.createElement('input');
      inst.className = 'tinst' + (parts.inst ? ' on' : '');
      inst.value = parts.inst || '--';
      inst.spellcheck = false;
      inst.title = 'instrument';

      const commit = () => {
        let nv = note.value.trim();
        if(nv === '---' || nv === '.' || nv === '') nv = '';
        if(nv === '===' || nv === '-') nv = 'off';
        let iv = inst.value.trim();
        if(iv === '--' || iv === '.') iv = '';
        MU.rows[ri][ci] = joinCell(nv, iv);
        // Write through to the cached song BEFORE redrawing. drawTracker rebuilds MU.rows
        // from `s.patterns`, and muSavePattern is async, so without this the redraw reads
        // the pre-edit pattern back and the note vanishes from the grid a frame after it
        // was typed -- while the correct value is sitting on disk. That desync is worse
        // than an outright failure, because reloading the page "fixes" it.
        s.patterns[MU.pattern] = MU.rows.map(muRow);
        muSavePattern();
        drawTracker();
        // Keeping the caret where it was: a grid that jumps to the top on every keystroke
        // cannot be played into.
        const sel = box.querySelectorAll('.tnote')[ri * s.channels + ci];
        if(sel) sel.focus();
      };

      // Typing a letter plays the note it sits under, the way a tracker keyboard does.
      // Anything else falls through to ordinary text editing, so `C#4` can still be typed
      // out in full.
      note.onkeydown = e => {
        if(e.ctrlKey || e.metaKey || e.altKey) return;
        const k = e.key.toLowerCase();
        if(k === 'delete' || k === 'backspace'){
          e.preventDefault(); note.value = '---'; commit(); return;
        }
        if(k === '-' || k === '='){
          e.preventDefault(); note.value = '==='; commit(); return;
        }
        if(k in PIANO){
          e.preventDefault();
          const midi = (MU.octave + 1) * 12 + PIANO[k];
          note.value = midiToTracker(Math.max(0, Math.min(119, midi)));
          if(inst.value === '--') inst.value = String(MU.inst);
          commit();
          return;
        }
        if(k === 'arrowdown' || k === 'enter'){
          e.preventDefault();
          const all = box.querySelectorAll('.tnote');
          const nx = all[(ri + 1) * s.channels + ci];
          if(nx){ nx.focus(); nx.select() }
        }
        if(k === 'arrowup'){
          e.preventDefault();
          const all = box.querySelectorAll('.tnote');
          const pv = all[(ri - 1) * s.channels + ci];
          if(pv){ pv.focus(); pv.select() }
        }
      };
      note.onchange = commit;
      inst.onchange = commit;
      note.onfocus = () => { note.select(); MU.row = ri };

      step.append(note, inst);
      row.appendChild(step);
    });
    box.appendChild(row);
  });
}


// ---------------------------------------------------------------- panel controls
//
// Three primitives, because a synth panel is three kinds of control and nothing else: a
// continuous value, a choice between a few options, and a shape you read rather than
// count.

// A knob. Drag vertically, wheel, or focus and arrow; double-click to type an exact value.
// Both halves are needed -- the arc gives the gesture, the readout gives the precision a
// developer tool owes you -- and a knob that could only be dragged would be a worse number
// box wearing a costume.
function knob(label, value, lo, hi, on){
  const wrap = document.createElement('div');
  wrap.className = 'knob';
  const dial = document.createElement('div');
  dial.className = 'dial';
  dial.tabIndex = 0;
  dial.setAttribute('role', 'slider');
  dial.setAttribute('aria-label', label);
  dial.setAttribute('aria-valuemin', lo);
  dial.setAttribute('aria-valuemax', hi);
  const name = document.createElement('b');
  name.textContent = label;
  const read = document.createElement('i');

  let v = value;
  const span = (hi - lo) || 1;
  const paint = () => {
    dial.style.setProperty('--p', String((v - lo) / span));
    dial.setAttribute('aria-valuenow', v);
    read.textContent = v;
  };
  const set = nv => {
    nv = Math.max(lo, Math.min(hi, Math.round(nv)));
    if(nv === v) return;
    v = nv; paint(); on(v);
  };
  paint();

  // Coarse by default, fine with shift -- a 5000 ms envelope and a 0..3 octave want very
  // different sensitivities from the same gesture.
  let dragging = false, lastY = 0;
  dial.addEventListener('pointerdown', e => {
    dragging = true; lastY = e.clientY; dial.setPointerCapture(e.pointerId); dial.focus();
  });
  dial.addEventListener('pointermove', e => {
    if(!dragging) return;
    const step = (e.shiftKey ? 1 : Math.max(1, Math.round(span / 60)));
    set(v + (lastY - e.clientY) * step);
    lastY = e.clientY;
  });
  const stop = () => { dragging = false };
  dial.addEventListener('pointerup', stop);
  dial.addEventListener('pointercancel', stop);
  dial.addEventListener('wheel', e => {
    e.preventDefault();
    set(v + (e.deltaY < 0 ? 1 : -1) * (e.shiftKey ? 1 : Math.max(1, Math.round(span / 60))));
  }, { passive: false });
  dial.addEventListener('keydown', e => {
    const step = e.shiftKey ? 1 : Math.max(1, Math.round(span / 60));
    if(e.key === 'ArrowUp' || e.key === 'ArrowRight'){ set(v + step); e.preventDefault() }
    if(e.key === 'ArrowDown' || e.key === 'ArrowLeft'){ set(v - step); e.preventDefault() }
  });
  // The exact value, for when a knob is the wrong tool -- which it is whenever you already
  // know the number you want.
  read.ondblclick = () => {
    const box = document.createElement('input');
    box.value = v;
    box.onblur = box.onchange = () => {
      set(parseInt(box.value, 10) || lo);
      box.replaceWith(read);
    };
    read.replaceWith(box);
    box.focus(); box.select();
  };

  wrap.append(dial, name, read);
  return wrap;
}

// A switch with every option visible and the chosen one lit. A dropdown would hide the
// alternatives, which is wrong for a choice you make constantly between four things.
function switcher(options, value, on, glyphs){
  const box = document.createElement('div');
  box.className = 'pick' + (glyphs ? '' : ' wide');
  const btns = [];
  options.forEach(o => {
    const b = document.createElement('button');
    b.className = o === value ? 'on' : '';
    b.title = o;
    b.innerHTML = glyphs ? waveGlyph(o) : o;
    // The switch moves its own light. It used to depend on the panel being rebuilt after
    // every save, so once saving stopped redrawing, a click would change the sound without
    // changing anything on screen.
    b.onclick = () => { btns.forEach(x => x.classList.toggle('on', x === b)); on(o) };
    btns.push(b);
    box.appendChild(b);
  });
  return box;
}

// Waveform marks. Iconic, not previews -- the real thing is band-limited per octave and
// drawing an idealised curve as if it were the output would be a preview that lies. The
// harmonic count beside the oscillator is the honest version of that information.
function waveGlyph(w){
  const p = { square:   'M1 9 L1 3 L7 3 L7 9 L13 9 L13 3 L19 3 L19 9',
              saw:      'M1 9 L7 3 L7 9 L13 3 L13 9 L19 3',
              triangle: 'M1 9 L5 3 L9 9 L13 3 L17 9 L19 6',
              noise:    'M1 6 L3 3 L4 8 L6 4 L8 9 L10 3 L12 7 L14 4 L16 8 L18 5 L19 7' }[w]
          || 'M1 6 L19 6';
  return `<svg class="wglyph" viewBox="0 0 20 12" fill="none" stroke="currentColor"
    stroke-width="1.4" stroke-linejoin="round"><path d="${p}"/></svg>`;
}

// The envelope as a shape. Four numbers do not read as one thing; a curve does, and the
// difference between a pluck and a pad is visible in it before you play a note.
function envCurve(e){
  const A = Math.max(0, e.attack || 0), D = Math.max(0, e.decay || 0),
        S = Math.max(0, Math.min(255, e.sustain ?? 0)), R = Math.max(0, e.release || 0);
  const W = 150, H = 44, pad = 3, top = pad, bot = H - pad;
  // Time is scaled to the longest stage so the shape stays legible whether the envelope is
  // 5 ms or 5 s -- an absolute axis would flatten every fast envelope into a vertical line.
  const total = Math.max(1, A + D + R) * 1.25;
  const x = ms => pad + (ms / total) * (W - pad * 2);
  const sy = bot - (S / 255) * (bot - top);
  const hold = total * 0.18;
  const d = `M${x(0)} ${bot} L${x(A)} ${top} L${x(A + D)} ${sy} `
          + `L${x(A + D + hold)} ${sy} L${x(A + D + hold + R)} ${bot}`;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" fill="none">
    <path d="${d}" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="${d} L${x(0)} ${bot} Z" fill="var(--accent)" opacity=".12"/>
  </svg>`;
}

// How many harmonics survive band-limiting at this pitch. Mirrors MIP_HARMONICS in
// pnx_synth.c -- shown because it is the constraint that decides whether a bright waveform
// stays bright up the keyboard, and it is invisible everywhere else.
function harmonicsAt(midi){
  const H = [31, 31, 31, 31, 31, 16, 8, 4];
  return H[Math.max(0, Math.min(7, Math.floor(midi / 12)))];
}

function drawInstrument(){
  const s = muSong();
  const box = $('#minstbody');
  box.innerHTML = '';
  if(!s) return;
  const ins = s.instruments[MU.inst];
  const waves = (S.data.waveforms) || ['square','saw','triangle','noise'];

  const plain = JSON.parse(JSON.stringify(ins));
  delete plain.synth;
  const sy = ins.synth ? JSON.parse(JSON.stringify(ins.synth)) : null;

  // Readouts derived from the model -- envelope curves, the harmonic count, the header
  // name -- repaint themselves after every change. The panel used to get this for free
  // from a full rebuild on save, which is exactly what made a knob undraggable.
  const live = [];
  const push = () => { for(const f of live) f(); muWrite(plain, sy) };

  const mod = (title, hue) => {
    const m = document.createElement('div');
    m.className = 'mod';
    if(hue) m.style.setProperty('--mod-hue', hue);
    const h = document.createElement('h4');
    h.textContent = title;
    m.appendChild(h);
    return m;
  };
  // The name, first. An instrument is referred to by index everywhere it is USED -- a
  // pattern row, the generated header -- so the one place it can be called something is
  // here, and naming it is what makes the index legible everywhere else.
  const idrow = document.createElement('div');
  idrow.className = 'mini';
  idrow.innerHTML = '<span class="dim" style="font-size:10px;letter-spacing:.09em;'
    + 'text-transform:uppercase">name</span> ';
  const nameBox = document.createElement('input');
  nameBox.value = plain.name || '';
  nameBox.placeholder = `instrument ${MU.inst}`;
  nameBox.size = 14;
  nameBox.title = 'lowercase letters, digits and underscores - becomes '
    + `MUSIC_${(s.name || '').toUpperCase()}_INST_<NAME> in the generated header`;
  nameBox.onchange = () => { plain.name = nameBox.value.trim(); push() };
  idrow.appendChild(nameBox);
  const hint = document.createElement('small');
  hint.className = 'dim';
  hint.style.fontSize = '10px';
  const paintHint = () => {
    hint.textContent = plain.name
      ? `MUSIC_${(s.name || '').toUpperCase()}_INST_${plain.name.toUpperCase()}`
      : 'unnamed - patterns still reach it by index';
  };
  paintHint();
  live.push(paintHint);
  idrow.appendChild(hint);
  box.appendChild(idrow);

  const chain = document.createElement('div');
  chain.className = 'synth';
  box.appendChild(chain);

  if(!sy){
    // No synth table. The plain envelope is the whole instrument, so it gets the panel
    // rather than being demoted to a footnote under one that does not exist.
    const m = mod('Envelope', 'var(--accent)');
    const r = document.createElement('div');
    r.className = 'row';
    r.appendChild(switcher(waves, plain.wave, v => { plain.wave = v; push() }, true));
    for(const [k, hi] of [['attack',5000],['decay',5000],['sustain',255],['release',5000]])
      r.appendChild(knob(k, plain[k], 0, hi, v => { plain[k] = v; push() }));
    m.appendChild(r);
    chain.appendChild(m);
    const note = document.createElement('small');
    note.className = 'dim';
    note.textContent = 'This song has no synth table. Adding one means a record for every '
      + 'instrument, because a pattern row names one index and the tables have to line up.';
    box.appendChild(note);
    return;
  }

  // --- oscillators. The signal starts here, so they are leftmost.
  (sy.osc || []).forEach((o, oi) => {
    const m = mod(`Osc ${oi + 1}`, 'var(--accent)');
    const r = document.createElement('div');
    r.className = 'row';
    const col = document.createElement('div');
    col.appendChild(switcher(waves, o.wave || 'square', v => { o.wave = v; push() }, true));
    const harm = document.createElement('small');
    harm.className = 'dim';
    harm.style.cssText = 'display:block;font-size:10px;margin-top:.35rem;letter-spacing:.06em';
    // Real information, not decoration: a saw an octave up carries a quarter the harmonics,
    // and that is why it sounds duller. Nothing else in the tool says so.
    const paintHarm = () => {
      harm.textContent = `${harmonicsAt(60 + (o.octave || 0) * 12)} harm at C4`;
    };
    paintHarm();
    live.push(paintHarm);
    col.appendChild(harm);
    r.appendChild(col);
    r.appendChild(knob('level', o.volume ?? 200, 0, 255, v => { o.volume = v; push() }));
    r.appendChild(knob('detune', o.detune ?? 0, -100, 100, v => { o.detune = v; push() }));
    r.appendChild(knob('oct', o.octave ?? 0, -4, 4, v => { o.octave = v; push() }));
    // Pulse width is square-only, so it is built once and shown conditionally rather than
    // appended conditionally: switching waveform no longer redraws the module, and a knob
    // that can only appear on a redraw would never appear.
    const width = knob('width', o.duty ?? 128, 16, 240, v => { o.duty = v; push() });
    const paintWidth = () => {
      width.style.display = (o.wave || 'square') === 'square' ? '' : 'none';
    };
    paintWidth();
    live.push(paintWidth);
    r.appendChild(width);
    m.appendChild(r);
    chain.appendChild(m);
  });

  // --- filter, with its own envelope drawn beside it.
  const fm = mod('Filter', '#e0a33c');
  const fr = document.createElement('div');
  fr.className = 'row';
  fr.appendChild(switcher(S.data.filter_modes || ['off','lowpass','highpass','bandpass'],
    sy.filter || 'off', v => { sy.filter = v; push() }));
  fr.appendChild(knob('cutoff', sy.cutoff_base ?? 128, 0, 255, v => { sy.cutoff_base = v; push() }));
  fr.appendChild(knob('reson', sy.resonance ?? 0, 0, 255, v => { sy.resonance = v; push() }));
  fr.appendChild(knob('env amt', sy.cutoff_env ?? 0, 0, 255, v => { sy.cutoff_env = v; push() }));
  fm.appendChild(fr);
  const fe = sy.cutoff || {};
  const fenv = document.createElement('div');
  fenv.className = 'row';
  fenv.style.marginTop = '.45rem';
  const fbox = document.createElement('div');
  fbox.className = 'envbox';
  fbox.innerHTML = envCurve(fe);
  live.push(() => { fbox.innerHTML = envCurve(sy.cutoff || {}) });
  fenv.appendChild(fbox);
  for(const [k, hi] of [['a',5000],['d',5000],['s',255],['r',5000]]){
    const key = { a:'attack', d:'decay', s:'sustain', r:'release' }[k];
    fenv.appendChild(knob(k, fe[key] ?? 0, 0, hi,
      v => { sy.cutoff = sy.cutoff || {}; sy.cutoff[key] = v; push() }));
  }
  fm.appendChild(fenv);
  chain.appendChild(fm);

  // --- amplifier.
  const am = mod('Amp', '#5fd28d');
  const ae = sy.amp || {};
  const ar = document.createElement('div');
  ar.className = 'row';
  const abox = document.createElement('div');
  abox.className = 'envbox';
  abox.innerHTML = envCurve(ae);
  live.push(() => { abox.innerHTML = envCurve(sy.amp || {}) });
  ar.appendChild(abox);
  for(const [k, hi] of [['a',5000],['d',5000],['s',255],['r',5000]]){
    const key = { a:'attack', d:'decay', s:'sustain', r:'release' }[k];
    ar.appendChild(knob(k, ae[key] ?? 0, 0, hi,
      v => { sy.amp = sy.amp || {}; sy.amp[key] = v; push() }));
  }
  am.appendChild(ar);
  chain.appendChild(am);

  // --- modulation. The LFO routes to one destination, which is why it is a switch and not
  // four separate depth controls.
  const lm = mod('Mod', '#a98cf0');
  // The destination switch spans the module and the four knobs sit under it in one row.
  // Putting the switch inline with the knobs left a wrapped row and a module two thirds
  // empty -- the LFO destination is a routing choice, not a fifth knob, and reads better
  // as the heading of the controls it governs.
  const lr = document.createElement('div');
  lr.className = 'row';
  lr.style.marginBottom = '.45rem';
  lr.appendChild(switcher(S.data.lfo_targets || ['off','pitch','volume','duty','cutoff'],
    sy.lfo_target || 'off', v => { sy.lfo_target = v; push() }));
  lm.appendChild(lr);
  const pr = document.createElement('div');
  pr.className = 'row';
  pr.appendChild(knob('rate', sy.lfo_rate ?? 0, 0, 255, v => { sy.lfo_rate = v; push() }));
  pr.appendChild(knob('depth', sy.lfo_depth ?? 0, 0, 255, v => { sy.lfo_depth = v; push() }));
  pr.appendChild(knob('pitch env', sy.pitch_env ?? 0, -1200, 1200,
    v => { sy.pitch_env = v; push() }));
  pr.appendChild(knob('fall', sy.pitch_env_decay ?? 0, 0, 255,
    v => { sy.pitch_env_decay = v; push() }));
  lm.appendChild(pr);
  chain.appendChild(lm);

  // --- sends. Global instances, so these are levels into a shared effect rather than
  // effects of their own -- worth saying, because it is why they are cheap.
  const xm = mod('Sends', '#7b8798');
  const xr = document.createElement('div');
  xr.className = 'row';
  xr.appendChild(knob('reverb', sy.reverb ?? 0, 0, 255, v => { sy.reverb = v; push() }));
  xr.appendChild(knob('chorus', sy.chorus ?? 0, 0, 255, v => { sy.chorus = v; push() }));
  xm.appendChild(xr);
  const xn = document.createElement('small');
  xn.className = 'dim';
  xn.style.cssText = 'display:block;font-size:10px;margin-top:.3rem;max-width:9rem';
  xn.textContent = 'one shared reverb and chorus for all four voices';
  xm.appendChild(xn);
  chain.appendChild(xm);

  // --- the fallback envelope, last because it is what plays only when the synth is
  // compiled out. Still shown: a build with PNX_USE_SYNTH=0 makes this the whole sound.
  const pm = mod('If synth is off', '#3a4451');
  pm.style.opacity = '.72';
  const prow = document.createElement('div');
  prow.className = 'row';
  prow.appendChild(switcher(waves, plain.wave, v => { plain.wave = v; push() }, true));
  for(const [k, hi] of [['attack',5000],['decay',5000],['sustain',255],['release',5000]])
    prow.appendChild(knob(k, plain[k], 0, hi, v => { plain[k] = v; push() }));
  pm.appendChild(prow);
  const pn = document.createElement('small');
  pn.className = 'dim';
  pn.style.cssText = 'display:block;font-size:10px;margin-top:.3rem;max-width:11rem';
  pn.textContent = 'the plain envelope, used only in a PNX_USE_SYNTH=0 build';
  pm.appendChild(pn);
  chain.appendChild(pm);
}


// Both halves in one request. The pipeline refuses tables of different lengths precisely
// so a note cannot play a different sound depending on which one it resolved through, and
// saving them separately would be the same mistake one step earlier.
//
// Write-BEHIND, and deliberately so. A knob calls this on every pointermove, so writing
// synchronously would be a request per pixel; and the obvious "reload and redraw" ending
// would replace the very dial the pointer has captured, killing the drag after its first
// step. So: coalesce, and never rebuild the panel from a write. The panel owns the live
// model and repaints its own readouts.
let muWriteTimer = null, muWritePend = null;
function muWrite(plain, synth){
  const s = muSong();
  if(!s) return;
  // The index is captured NOW, not at flush time: switching instruments inside the
  // coalescing window would otherwise land these values on the wrong one.
  muWritePend = { song: s, index: MU.inst, plain, synth };
  muSay('#minstlog', 'editing…');
  if(muWriteTimer) return;
  muWriteTimer = setTimeout(muFlushInstrument, 260);
}

async function muFlushInstrument(){
  muWriteTimer = null;
  const p = muWritePend;
  muWritePend = null;
  if(!p) return;
  const r = await post('/api/song/instrument',
    { name: p.song.name, index: p.index, plain: p.plain, synth: p.synth });
  if(!r.ok){ muSay('#minstlog', r.error, true); return }
  // Write through to the cached song. Without this, switching instruments and back would
  // redraw the panel from the pre-edit model -- the same desync the tracker grid had.
  const merged = JSON.parse(JSON.stringify(p.plain));
  if(p.synth) merged.synth = JSON.parse(JSON.stringify(p.synth));
  p.song.instruments[p.index] = merged;
  muRelabelInstruments();
  muSay('#minstlog', 'saved', false);
  budget(true);
}

// A rename has to reach the picker, which is the only place an instrument is named once
// the panel is drawn. Relabelling in place rather than redrawing keeps the caret in the
// name field the user is still typing in.
function muRelabelInstruments(){
  const s = muSong();
  const sel = $('#minst');
  if(!s || !sel) return;
  [...sel.options].forEach((o, i) => {
    const x = s.instruments[i];
    if(x) o.textContent = `${i} - ${x.name || x.wave}`;
  });
}

function drawSamples(){
  const box = $('#msamples');
  if(!box) return;
  box.innerHTML = '';
  const list = (S.data && S.data.samples) || [];
  if(!list.length) box.innerHTML = '<small class="dim">No samples.</small>';
  for(const sm of list){
    const row = document.createElement('div');
    row.className = 'smprow';
    // Seconds, from the packed size: 16 kHz 8-bit is one byte a sample, so the blob IS the
    // duration. Measured against the 1.5 s the pipeline enforces, because that limit is
    // the reason samples are short and a byte count does not say how close you are.
    const secs = sm.bytes ? (sm.bytes - 8) / 16000 : null;
    const pct = secs === null ? 0 : Math.min(100, (secs / 1.5) * 100);
    row.innerHTML = `<b>${sm.name}</b>`
      + `<span class="dim" style="flex:1">${sm.file}</span>`
      + `<span class="smpbar"><i style="width:${pct}%" class="${pct >= 100 ? 'over' : ''}">`
      + `</i></span>`
      + `<span class="dim" style="width:6.5rem;text-align:right">`
      + (secs === null ? 'not built' : `${secs.toFixed(2)} s of 1.5`) + '</span> ';
    const del = document.createElement('button');
    del.textContent = 'Remove';
    del.onclick = async () => {
      if(!confirm(`Remove sample "${sm.name}"? The WAV on disk is left alone.`)) return;
      const r = await post('/api/sample/remove', { name: sm.name });
      if(!r.ok){ muSay('#mslog', r.error, true); return }
      await reload(); drawMusic(); budget(true);
    };
    row.appendChild(del);
    box.appendChild(row);
  }
  const wav = $('#mswav');
  wav.innerHTML = ((S.data && S.data.wavs) || [])
    .map(w => `<option>${w.path}</option>`).join('');
}

// Leaving an instrument settles its pending write first. The write already carries the
// index it was made against, so it would land correctly either way -- but a save that
// completes after you have moved on reports "saved" under a different instrument, and the
// 260 ms is only there to coalesce a drag, not to survive one.
function muSettle(){
  if(!muWriteTimer) return;
  clearTimeout(muWriteTimer);
  muWriteTimer = null;
  muFlushInstrument();
}

$('#msong').onchange = () => { muSettle(); MU.song = +$('#msong').value; MU.pattern = 0; MU.inst = 0; drawMusic() };
$('#mpat').onchange  = () => { MU.pattern = +$('#mpat').value; drawTracker(); muSay('#mpatlog','') };
$('#minst').onchange = () => { muSettle(); MU.inst = +$('#minst').value; drawInstrument() };
$('#moct').onchange  = () => { MU.octave = +$('#moct').value };

// A new pattern is empty; a clone is this one. Both are additive -- nothing here removes a
// pattern, because the order list names patterns by index and deleting one silently
// renumbers every entry after it. That is a change worth making deliberately in the
// manifest rather than accidentally with a button.
async function muAddPattern(copy){
  const s = muSong(); if(!s) return;
  const blank = Array.from({ length: s.rows_per },
    () => Array.from({ length: s.channels }, () => '.').join('     '));
  const rows = copy ? s.patterns[MU.pattern].slice() : blank;
  const r = await post('/api/song/pattern',
    { name: s.name, index: s.patterns.length, rows, append: true });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload();
  MU.pattern = (S.data.songs[MU.song].patterns.length - 1);
  drawMusic(); budget(true);
  muSay('#mpatlog', copy ? 'cloned' : 'added', false);
}
$('#mpatadd').onclick   = () => muAddPattern(false);
$('#mpatclone').onclick = () => muAddPattern(true);

$('#mtempo').onchange = async () => {
  const s = muSong(); if(!s) return;
  const r = await post('/api/song/meta', { name: s.name, tempo: +$('#mtempo').value });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload(); drawMusic();
};

// Written as it changes, like everything else in the editor. The row is redrawn from the
// local copy immediately so typing stays responsive, and the manifest catches up.
async function muSavePattern(){
  const s = muSong(); if(!s || !MU.rows) return;
  const r = await post('/api/song/pattern',
    { name: s.name, index: MU.pattern, rows: MU.rows.map(muRow) });
  muSay('#mpatlog', r.ok ? 'saved' : r.error, r.ok ? false : true);
  if(r.ok) budget(true);
}

$('#morder').onchange = async () => {
  const s = muSong(); if(!s) return;
  const order = $('#morder').value.split(/[,\s]+/).filter(Boolean).map(Number);
  const r = await post('/api/song/meta', { name: s.name, order });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload(); drawMusic(); budget(true);
  muSay('#mpatlog', 'order saved', false);
};

$('#msongnew').onclick = async () => {
  const name = prompt('Name the song.\n\n'
    + 'Game code loads it as PNX_ASSET_MUSIC_<NAME>. Lowercase letters, digits and '
    + 'underscores.');
  if(!name) return;
  const r = await post('/api/song', { name: name.trim() });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload();
  MU.song = (S.data.songs || []).findIndex(x => x.name === name.trim());
  MU.pattern = 0; MU.inst = 0;
  drawMusic(); budget(true);
};

$('#msongdel').onclick = async () => {
  const s = muSong(); if(!s) return;
  if(!confirm(`Remove song "${s.name}"?\n\n`
    + `Game code loading it by name will stop compiling.`)) return;
  const r = await post('/api/song/remove', { name: s.name });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  MU.song = 0; MU.pattern = 0; MU.inst = 0;
  await reload(); drawMusic(); budget(true);
};

$('#minstadd').onclick = async () => {
  const s = muSong(); if(!s) return;
  const r = await post('/api/song/instrument/add', { name: s.name });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  await reload();
  MU.inst = S.data.songs[MU.song].instruments.length - 1;
  drawMusic(); budget(true);
};

$('#minstdel').onclick = async () => {
  const s = muSong(); if(!s) return;
  const r = await post('/api/song/instrument/remove',
    { name: s.name, index: s.instruments.length - 1 });
  if(!r.ok){ muSay('#mpatlog', r.error, true); return }
  MU.inst = 0;
  await reload(); drawMusic(); budget(true);
};

$('#msadd').onclick = async () => {
  const name = $('#msname').value.trim();
  const file = $('#mswav').value;
  if(!name){ muSay('#mslog', 'Name it first.', true); return }
  if(!file){ muSay('#mslog', 'No WAV in the project to add.', true); return }
  const r = await post('/api/sample', { name, file });
  if(!r.ok){ muSay('#mslog', r.error, true); return }
  $('#msname').value = '';
  await reload(); drawMusic(); budget(true);
  muSay('#mslog', `Added "${name}". Press Build.`, false);
};

// ------------------------------------------------------------------------- dialog
//
// A textarea per conversation, one page per line. Saved on blur rather than per
// keystroke: every write goes through the manifest and re-derives the glyph set of every
// `charset = "auto"` font, which is not work to do between two letters of a word.

function dlgSay(msg,bad){
  const el=$('#dialoglog');
  el.className='mini '+(bad===false?'ok':'bad');
  el.textContent=msg||'';
}

function drawDialog(){
  const box=$('#dialoglist'); box.innerHTML='';
  const list=(S.data&&S.data.dialogs)||[];
  if(!list.length){
    box.innerHTML='<small class="dim">No conversations yet.</small>';
    return;
  }
  for(const d of list){
    const card=document.createElement('section');
    card.className='scenecard';
    const head=document.createElement('div');
    head.className='mini';
    head.innerHTML=`<b>${d.name}</b> <span class="dim">${d.pages.length} page`
      +`${d.pages.length===1?'':'s'} · ${d.bytes} B</span>`;
    card.appendChild(head);

    const ta=document.createElement('textarea');
    ta.rows=Math.max(3,d.pages.length+1);
    ta.style.width='100%';
    ta.value=d.pages.join('\n');
    ta.onblur=async()=>{
      const pages=ta.value.split('\n').map(s=>s.trim()).filter(Boolean);
      if(!pages.length){ dlgSay('A conversation needs at least one page.'); return }
      if(pages.join('\n')===d.pages.join('\n')) return;
      const r=await post('/api/dialog',{name:d.name,pages});
      if(!r.ok){ dlgSay(r.error); return }
      await reload(); drawDialog(); dlgSay('');
      budget(true);
    };
    card.appendChild(ta);

    const foot=document.createElement('div');
    foot.className='mini';
    const del=document.createElement('button');
    del.textContent='Remove';
    del.onclick=async()=>{
      if(!confirm(`Remove conversation "${d.name}"?`)) return;
      const r=await post('/api/dialog/remove',{name:d.name});
      if(!r.ok){ dlgSay(r.error); return }
      await reload(); drawDialog(); dlgSay(`Removed "${d.name}".`,false);
      budget(true);
    };
    foot.appendChild(del);
    card.appendChild(foot);
    box.appendChild(card);
  }
}

$('#dlgnew').onclick=async()=>{
  const name=prompt('Name the conversation.\n\n'
    +'Game code reaches it as PNX_DIALOG_<NAME>. Lowercase letters, digits and '
    +'underscores.');
  if(!name) return;
  const r=await post('/api/dialog',{name:name.trim(),pages:['...']});
  if(!r.ok){ dlgSay(r.error); return }
  await reload(); drawDialog(); dlgSay(`Added "${name.trim()}".`,false);
};

// ------------------------------------------------------------------------- scenes
//
// A scene is the framework's only load point, and it was the one part of the manifest
// with no editor at all: a map could be drawn, painted and built and still be unreachable
// from the game. Every control here writes the manifest immediately, the way the legend
// does, so there is no separate save to forget.

function sceneSay(msg, bad){
  const el=$('#scenelog');
  el.className='mini '+(bad===false?'ok':'bad');
  el.textContent=msg||'';
}

function drawScenes(){
  const box=$('#scenelist'); box.innerHTML='';
  const d=S.data||{};
  const scenes=d.scenes||[], maps=(d.maps||[]).map(m=>m.name);
  const sprites=d.sprite_names||[], fonts=(d.fonts||[]).map(f=>f.name);

  // A map nothing loads is the dead end worth naming: it builds, it costs bytes, and the
  // game has no way to reach it.
  const loaded=new Set(scenes.map(s=>s.map).filter(Boolean));
  const orphans=maps.filter(m=>!loaded.has(m));

  if(!scenes.length){
    box.innerHTML='<small class="dim">No scenes yet. Nothing can be loaded until '
      +'there is one.</small>';
  }

  for(const sc of scenes){
    const card=document.createElement('section');
    card.className='scenecard';
    const head=document.createElement('div');
    head.className='mini';
    head.innerHTML=`<b>${sc.name}</b>`;
    card.appendChild(head);

    // Map.
    const mrow=document.createElement('label');
    mrow.className='mini';
    mrow.innerHTML='map ';
    const msel=document.createElement('select');
    msel.innerHTML='<option value="">(none)</option>'
      +maps.map(m=>`<option${m===sc.map?' selected':''}>${m}</option>`).join('');
    msel.onchange=()=>writeScene(sc,{map:msel.value||null});
    mrow.appendChild(msel);
    card.appendChild(mrow);

    // Sprites and fonts, a checkbox each. Listed rather than typed because every name
    // here has to resolve at build time, and a select cannot be misspelled.
    for(const [label,all,cur,key] of [['sprites',sprites,sc.sprites,'sprites'],
                                      ['fonts',fonts,sc.fonts,'fonts']]){
      const row=document.createElement('div');
      row.className='mini';
      row.innerHTML=`<span class="dim">${label}</span> `;
      if(!all.length) row.innerHTML+='<small class="dim">none defined</small>';
      for(const n of all){
        const on=cur.includes(n);
        const l=document.createElement('label');
        l.className='mini';
        l.innerHTML=`<input type="checkbox" ${on?'checked':''}> ${n}`;
        l.querySelector('input').onchange=ev=>{
          const next=cur.filter(x=>x!==n);
          if(ev.target.checked) next.push(n);
          writeScene(sc,{[key]:next});
        };
        row.appendChild(l);
      }
      card.appendChild(row);
    }

    // Dialog is a flag, not a list: the pipeline packs every [dialog.*] into one blob.
    const drow=document.createElement('label');
    drow.className='mini';
    drow.innerHTML=`<input type="checkbox" ${sc.dialog?'checked':''}> dialog`;
    drow.querySelector('input').onchange=ev=>writeScene(sc,{dialog:ev.target.checked});
    card.appendChild(drow);

    if(sc.atlases.length){
      const a=document.createElement('small');
      a.className='dim';
      a.textContent=`also loads atlases: ${sc.atlases.join(', ')}`;
      card.appendChild(a);
    }

    const foot=document.createElement('div');
    foot.className='mini';
    const del=document.createElement('button');
    del.textContent='Remove';
    del.onclick=async()=>{
      if(!confirm(`Remove scene "${sc.name}"?\n\n`
                  +`Game code loading it by name will stop compiling.`)) return;
      const r=await post('/api/scene/remove',{name:sc.name});
      if(!r.ok){ sceneSay(r.error); return }
      await reload(); drawScenes(); sceneSay(`Removed "${sc.name}".`,false);
    };
    foot.appendChild(del);
    card.appendChild(foot);
    box.appendChild(card);
  }

  if(orphans.length){
    const warn=document.createElement('p');
    warn.className='mini bad';
    warn.textContent=`No scene loads: ${orphans.join(', ')}. `
      +`Those maps cost bytes and cannot be reached.`;
    box.appendChild(warn);
  }
}

// Every field resent, because the endpoint replaces the table rather than patching it --
// the same reason writeLegend resends. A partial write would drop the fonts when you
// ticked a sprite.
async function writeScene(sc,changes){
  const body={name:sc.name, map:sc.map, sprites:sc.sprites, fonts:sc.fonts,
              dialog:sc.dialog, atlases:sc.atlases, ...changes};
  const r=await post('/api/scene',body);
  if(!r.ok){ sceneSay(r.error); drawScenes(); return }
  await reload(); drawScenes(); sceneSay('');
  budget(true);
}

$('#scnew').onclick=async()=>{
  const name=prompt('Name the scene.\n\n'
    +'Game code loads it as PNX_SCENE_<NAME>. Lowercase letters, digits and underscores.');
  if(!name) return;
  const maps=(S.data.maps||[]).map(m=>m.name);
  // Seeded with a map rather than created empty: a scene that loads nothing is refused by
  // the pipeline, so an empty one could not be saved at all.
  const first=maps.find(m=>!(S.data.scenes||[]).some(s=>s.map===m))||maps[0];
  if(!first){ sceneSay('Add a map first — a scene that loads nothing cannot be built.');
              return }
  const r=await post('/api/scene',{name:name.trim(), map:first, sprites:[],
                                   fonts:(S.data.fonts||[]).map(f=>f.name)});
  if(!r.ok){ sceneSay(r.error); return }
  await reload(); drawScenes(); sceneSay(`Added "${name.trim()}".`,false);
};

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
$('#tabscenes').onclick=()=>showTab('scenes');
$('#tabdialog').onclick=()=>showTab('dialog');
$('#tabmusic').onclick=()=>showTab('music');
$('#tabimport').onclick=()=>showTab('import');
$('#tabfonts').onclick=()=>showTab('fonts');
$('#tabsdk').onclick=()=>showTab('sdk');
$('#tabpixel').onclick=()=>showTab('pixel');
$('#tabcode').onclick=()=>showTab('code');
$('#tabdevice').onclick=()=>showTab('device');

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

// Each analyse is tagged, and a reply that is not the newest is dropped.
//
// The responses used only to paint, so a stale one was a flicker. Now one of them CLAMPS
// THE REGION, and a reply describing the sheet you just switched away from will resize
// the carve to fit a sheet you are no longer looking at -- which is exactly what it did:
// a 16x32 region became 6x1, the shape of the previously selected sheet.
let pending=null, analyseSeq=0;
function analyse(){
  clearTimeout(pending);
  const seq=++analyseSeq;
  pending=setTimeout(async()=>{
    const body={sheet:$('#sheet').value,tile:+$('#tile').value,
      region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value],
      max_tiles:+$('#maxt').value,exclude:[...IMPEX],colorkey:KEY,
      ink_threshold:+$('#bwthresh').value};
    if(!body.sheet) return;
    const r=await (await fetch('/api/analyse',{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
    if(seq!==analyseSeq) return;          // a newer request is already in flight
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
    $('#bwstrip').src=r.bw_strip||'';
    drawCrop(r.sheet_tiles);
  },180);
}
for(const id of ['sheet','tile','rx','ry','rw','rh','maxt'])
  $('#'+id).addEventListener('input',()=>{ analyse(); drawSlice() });
$('#bwthresh').addEventListener('input',()=>{
  $('#bwthreshv').textContent=$('#bwthresh').value;
  analyse();
});

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
// The region cannot be larger than the sheet holds AT THIS TILE SIZE, and the tile size
// changes what the sheet holds: 480x256 is 30x16 tiles at 16px and 15x8 at 32px. Typing a
// region that fits at one size and then changing the size is how a carve ends up running
// off the sheet -- which the pipeline rejects, but only at build time, and only if the
// block already went into the manifest.
//
// Clamped rather than merely flagged, and the inputs' own max is set too, so the spinners
// stop where the art does.
function clampRegion(sx, sy){
  let changed=false;
  const fit=(id, hi)=>{
    const el=$('#'+id);
    el.max=hi;
    if(+el.value > hi){ el.value=hi; changed=true }
    if(+el.value < +el.min){ el.value=el.min; changed=true }
  };
  fit('rx', Math.max(0, sx-1));
  fit('ry', Math.max(0, sy-1));
  fit('rw', Math.max(1, sx - +$('#rx').value));
  fit('rh', Math.max(1, sy - +$('#ry').value));
  return changed;
}

function drawCrop(sheetTiles){
  const box=$('#cropbox');
  if(!sheetTiles || !sheetTiles[0] || !sheetTiles[1]){ box.style.display='none'; return }
  const [sx,sy]=sheetTiles;
  if(clampRegion(sx, sy)){
    // Re-price against what the region actually became, rather than what was typed.
    analyse(); drawSlice();
  }
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

// An atlas that already exists is edited, not duplicated. The button says which, because
// a button that silently does one of two things is worse than either.
function atlasNames(){ return S.data.atlas_names || [] }

function importBody(){
  return {name:$('#aname').value.trim(), sheet:$('#sheet').value, tile:+$('#tile').value,
          colorkey:KEY, max_tiles:+$('#maxt').value, exclude:[...IMPEX],
          region:[+$('#rx').value,+$('#ry').value,+$('#rw').value,+$('#rh').value]};
}

function atlasMode(){
  const name=$('#aname').value.trim();
  const exists=atlasNames().includes(name);
  $('#addatlas').textContent=exists?'Update atlas':'Add atlas';
  $('#aload').style.display=exists?'':'none';
  $('#adel').style.display=exists?'':'none';
  $('#atlasnames').innerHTML=atlasNames().map(n=>`<option value="${n}">`).join('');
  return exists;
}
$('#aname').addEventListener('input',atlasMode);

$('#aload').onclick=async()=>{
  const name=$('#aname').value.trim();
  const s=await (await fetch('/api/atlas/spec',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({name})})).json();
  if(s.error){ $('#log').className='bad'; $('#log').textContent=s.error; return }
  const set=(id,v)=>{ $('#'+id).value=v };
  set('sheet',s.sheet); set('tile',s.tile); set('maxt',s.max_tiles);
  set('rx',s.region[0]); set('ry',s.region[1]); set('rw',s.region[2]); set('rh',s.region[3]);
  set('apick',(s.autopick||[]).join(', '));
  // `metatiles` arrives as written: "auto", a bool, or a fraction that is this atlas's
  // own threshold. The select carries the three named choices; a fraction is left as
  // "auto" in the box and preserved in the file unless it is deliberately changed.
  const mt=s.metatiles;
  set('ameta', (mt===true||mt==='true')?'true'
             : (mt===false||mt==='false')?'false':'auto');
  set('avars',(s.variants||[]).join(', '));
  KEY=s.colorkey||null; keyLabel();
  IMPEX=new Set(s.exclude||[]); impRegion=impRegionKey();
  $('#log').className=''; $('#log').textContent=
    `Loaded "${name}" — change anything and press Update atlas.`;
  analyse(); drawSlice();
};

// Removal asks the server what still uses the atlas BEFORE confirming, so the dialog says
// "the ship map draws with it" rather than offering a delete that will simply be refused.
$('#adel').onclick=async()=>{
  const name=$('#aname').value.trim();
  const log=$('#log');
  const post=(url)=>fetch(url,{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({name})});

  const u=await (await post('/api/atlas/users')).json();
  if(u.users&&u.users.length){
    log.className='bad';
    log.textContent=`Cannot remove "${name}" — ${u.users.join('; ')}.`;
    return;
  }
  if(!confirm(`Remove atlas "${name}" from the manifest?\n\n`
              +`Nothing references it. The art on disk is left alone.`)) return;

  const r=await (await post('/api/atlas/remove')).json();
  if(r.error){ log.className='bad'; log.textContent=r.error; return }
  log.className='ok'; log.textContent=`Removed "${name}". Press Build.`;
  $('#aname').value='';
  await load();
  atlasMode();
};

$('#addatlas').onclick=async()=>{
  const body=importBody();
  if(!body.name){alert('Name the atlas first.');return}
  const log=$('#log');

  // Validated through the real pipeline BEFORE anything is written. A carve that fails
  // the build used to go into the manifest anyway, and the only sign was Build failing
  // afterwards -- leaving a broken block to remove by hand.
  const v=await (await fetch('/api/atlas/validate',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
  if(!v.ok){
    log.className='bad';
    log.textContent=`Not added — ${v.error}`;
    return;
  }

  const editing=atlasNames().includes(body.name);
  const r=await (await fetch(editing?'/api/atlas/update':'/api/atlas',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(body)})).json();
  log.className=r.ok?'ok':'bad';
  if(!r.ok){ log.textContent=r.error; return }

  // Warnings are said, not enforced: a capped carve or a reduced tile is a legitimate
  // choice about someone's own art.
  // The autopick list is written after the block exists, so adding and editing take the
  // same path.
  //
  // An empty box means different things in the two: on a fresh import the scaffold has
  // already written the default three, so empty means "leave the default alone". When
  // EDITING it means "name nothing" -- and it has to be obeyed, because clearing the list
  // is the only way to hand a name like `floor` over to an explicit [atlas.semantic]
  // entry. Skipping it here is what made an autopicked role impossible to override.
  // Metatiles and variants are written after the block exists, for the same reason
  // autopick is: both are decisions about a packed atlas, and the block has to be there
  // to hold them.
  const vars=$('#avars').value.split(',').map(s=>s.trim()).filter(Boolean);
  const x=await post('/api/atlas/extras',
    {name:body.name, metatiles:$('#ameta').value, variants:vars});
  if(!x.ok){ log.className='bad'; log.textContent=x.error; return }

  const picks=$('#apick').value.split(',').map(s=>s.trim()).filter(Boolean);
  if(picks.length||editing){
    const p=await post('/api/autopick',{atlas:body.name,roles:picks});
    if(!p.ok){ log.className='bad'; log.textContent=p.error; return }
  }

  log.textContent=(editing?`Updated [[atlas]] "${body.name}".`
                          :`Added [[atlas]] "${body.name}" to the manifest.`)
    + (v.warnings.length?`\n\n${v.warnings.map(w=>'! '+w).join('\n')}`:'')
    + '\n\nPress Build.';
  await load();
  atlasMode();
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
  if(u.pending){
    // Distinct from "could not reach GitHub": nothing has failed, the answer is simply
    // not back. Saying it failed would be a lie the user acts on by retrying.
    info.innerHTML=`<div><span class="k">running</span> <b>${u.current||'—'}</b></div>`
      +`<small>checking… <span class="dim">${u.why||''}</span></small>`;
  }else if(!u.checked){
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

// The check no longer blocks the editor while GitHub thinks about it, which means it can
// come back "still trying" -- so the page has to come back for the answer rather than
// treating the first reply as final. Bounded, because a resolver that never answers must
// not leave a poll running for the life of the session.
let updRetry=null;
async function updCheck(force){
  clearTimeout(updRetry);
  $('#updcheck').disabled=true;
  try{
    UPD=await (await fetch('/api/update'+(force?'/check':''),
      {method:force?'POST':'GET'})).json();
  }catch(_){ $('#updcheck').disabled=false; return }
  UPD.dl=await (await fetch('/api/update/progress')).json();
  updRender();
  if(UPD.pending && (updCheck.tries=(updCheck.tries||0)+1) <= 10){
    updRetry=setTimeout(()=>updCheck(false), 2000);
  }else{
    updCheck.tries=0;
    $('#updcheck').disabled=false;
  }
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
// What is on the table if this restarts: a painted map that was never saved, or an edited
// source file. The page is the only thing that knows either, which is why the warning
// lives here rather than in the updater.
function unsavedWork(){
  const lost=[];
  if(typeof S!=='undefined' && S.dirty && S.map) lost.push(`the map "${S.map.name}"`);
  if(typeof CODE!=='undefined' && CODE.path && CODE.editable && $('#codetext')
     && $('#codetext').value!==CODE.clean) lost.push(CODE.path);
  return lost;
}

$('#updapply').onclick=async()=>{
  const v=(UPD&&UPD.version)||'the new version';
  const lost=unsavedWork();
  // Always asked, never assumed: this closes the application someone is working in.
  const warning=lost.length
    ? `Install ${v} and restart?\n\nUNSAVED CHANGES WILL BE LOST:\n`
      + lost.map(w=>'  - '+w).join('\n')
      + `\n\nCancel, save them, then install.`
    : `Install ${v} and restart?\n\nThe editor will close and reopen on the new version.`;
  if(!confirm(warning)) return;

  const log=$('#log'); log.className=''; log.textContent='Installing';
  let r;
  try{
    r=await (await fetch('/api/update/apply',{method:'POST'})).json();
  }catch(_){
    // The process can die before its reply lands. That is a restart, not a failure --
    // the poll below establishes which.
    r={ok:true, restarting:true, message:'Installing'};
  }
  log.className=r.ok?'ok':'bad';
  log.textContent=r.ok?r.message:r.error;
  // Its own block: appended inline it ran straight on from the asset size above it,
  // which read as one sentence about a file rather than a result.
  $('#updinfo').innerHTML+=`<div style="margin-top:.4rem"><small class="${r.ok?'':'bad'}">`
    +`${r.ok?r.message:r.error}</small></div>`;
  if(r.ok && r.restarting) waitForRestart();
};

// The old process exits, the new one binds the same port, and the page reloads onto it --
// so a restart is something the user watches happen rather than something they are told
// to go and do.
async function waitForRestart(){
  const was=(UPD&&UPD.current)||'';
  const deadline=Date.now()+60000;
  const dots=setInterval(()=>{ $('#log').textContent+='.' }, 1000);
  while(Date.now()<deadline){
    await new Promise(r=>setTimeout(r,1000));
    try{
      const p=await (await fetch('/api/ping',{cache:'no-store'})).json();
      // Only a DIFFERENT version means the successor is up; the old process answers
      // right until it exits.
      if(p.app==='pebblnyx-editor' && p.version!==was){
        clearInterval(dots);
        location.reload();
        return;
      }
    }catch(_){ /* down: the middle of a restart, not a failure */ }
  }
  clearInterval(dots);
  $('#log').className='bad';
  $('#log').textContent='The editor did not come back on its own -- start it again.';
}
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

// --------------------------------------------------------------------- emulator
//
// Mirrors the SDK panel's own shape (status object with `busy`/`log`, poll only while
// something is running) rather than inventing a second one -- see sdkStatus above.
// Two intervals, not one: emuPoll asks "is anything running yet" every 1.5s the whole
// time this tab is open, and emuFramePoll -- only alive once emuPoll's answer is yes --
// pulls the screen faster, since that is the one a person is actually watching.
let emuPoll=null, emuFramePoll=null;

function emuPlatform(){
  const sel=$('#emuplatform');
  if(sel.options.length===0){
    for(const name of Object.keys(S.data.platforms||{})){
      const o=document.createElement('option'); o.value=name; o.textContent=name;
      sel.appendChild(o);
    }
    sel.value=S.emuPlatform||sel.options[0].value;
  }
  return sel.value;
}

let emuTabActive=false;

function emuEnter(){
  emuTabActive=true;
  emuPlatform();
  emuStatus();
  if(!emuPoll) emuPoll=setInterval(emuStatus,1500);
}

// Leaving the tab, not stopping the emulator -- pebble-tool keeps it running (that is
// the whole point of the state file), this just stops paying for screenshots and status
// polls of a screen nobody is looking at. Also releases anything still held: a button
// pressed and then abandoned by switching tabs should not stay stuck down on a watch
// nobody is looking at either.
function emuLeave(){
  emuTabActive=false;
  if(emuPoll){ clearInterval(emuPoll); emuPoll=null }
  if(emuFramePoll){ clearInterval(emuFramePoll); emuFramePoll=null }
  if(emuHeld.size) emuRelease();
}

async function emuStatus(){
  const platform=emuPlatform();
  let s;
  try{ s=await (await fetch('/api/emulator/status?platform='+platform)).json() }
  catch(_){ return }
  if(s.error) return;

  $('#emustart').disabled=s.busy;
  $('#emustart').textContent=s.busy?'Building…':(s.running?'Rebuild & reinstall':'Build & run');
  $('#emustop').disabled=!s.running&&!s.busy;
  $('#emuscreenwrap').style.display=s.running?'':'none';
  $('#emunote').textContent=s.busy
    ?`Building and installing for ${platform}… an ARM compile plus a cold boot, so `
     +'this can take a while the first time.'
    :(s.running?`${platform} is running.`
      :`${platform} is not running. Build & run compiles this project for it and `
       +'installs into pebble-tool’s own emulator.');
  if(s.log&&s.log.trim()) $('#emulog').textContent=s.log;

  if(s.running&&!emuFramePoll){
    emuFrame();
    emuFramePoll=setInterval(emuFrame,800);
  }else if(!s.running&&emuFramePoll){
    clearInterval(emuFramePoll); emuFramePoll=null;
  }
}

// Cache-busted by the timestamp, not by a fetch+blob-URL dance: the browser already
// knows how to load an <img> and quietly keep the last one on a failed request, which is
// exactly what a screendump that has not landed yet (see Emulator.frame's 1.5s wait,
// pnx_editor.py) should look like -- not a flash to a broken-image icon.
function emuFrame(){
  $('#emuscreen').src='/api/emulator/frame?platform='+emuPlatform()+'&t='+Date.now();
}

$('#emuplatform').onchange=()=>{ S.emuPlatform=$('#emuplatform').value; emuStatus() };

$('#emustart').onclick=async()=>{
  $('#emustart').disabled=true;
  const r=await (await fetch('/api/emulator/start',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({platform:emuPlatform()})})).json();
  if(!r.ok) $('#emunote').textContent=r.error||'could not start';
  emuStatus();
};

$('#emustop').onclick=async()=>{
  await fetch('/api/emulator/stop',{method:'POST'});
  emuStatus();
};

// Held state, not one-shot clicks: a real QemuButton packet is a BITMASK of everything
// currently down (pebble_tool/sdk/... via libpebble2's protocol), so every change here
// resends the complete set rather than one name at a time -- see Emulator.button's own
// docstring in pnx_editor.py. One source for both the on-screen buttons and the
// keyboard below, so "click and hold" and "press and hold a key" are the same code path
// doing the same thing to the same watch.
let emuHeld=new Set();

function emuPush(){
  document.querySelectorAll('.emubtn').forEach(b=>b.classList.toggle('held',emuHeld.has(b.dataset.btn)));
  return fetch('/api/emulator/button',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({platform:emuPlatform(),action:'push',buttons:[...emuHeld]})});
}
function emuRelease(){
  emuHeld.clear();
  document.querySelectorAll('.emubtn').forEach(b=>b.classList.remove('held'));
  return fetch('/api/emulator/button',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({platform:emuPlatform(),action:'release'})});
}
function emuDown(name){
  if(emuHeld.has(name)) return;
  emuHeld.add(name);
  emuPush();
}
function emuUp(name){
  if(!emuHeld.has(name)) return;
  emuHeld.delete(name);
  emuHeld.size?emuPush():emuRelease();
}

// mousedown/mouseup on each button, PLUS a document-level mouseup/mouseleave-of-window
// safety net -- a mouse released after dragging off the button (or off the whole page)
// never fires that button's own mouseup, and without this its button would stay "held"
// on a watch nobody is touching until the next click anywhere lifts it.
for(const b of document.querySelectorAll('.emubtn')){
  b.onmousedown=e=>{ e.preventDefault(); emuDown(b.dataset.btn) };
  b.onmouseup=()=>emuUp(b.dataset.btn);
  b.onmouseleave=()=>emuUp(b.dataset.btn);
}
document.addEventListener('mouseup',()=>{ if(emuHeld.size) emuRelease() });

// Arrow keys / Enter / Backspace, while the BEZEL ITSELF has focus -- not just the
// Device tab, or an arrow key pressed to change the platform dropdown next to it would
// get hijacked into a button press instead of moving the selection. Click the screen or
// tab to it (tabindex on .emubezel) to focus it; the same layout CloudPebble's own
// emulator used keys for. e.repeat is skipped on the way down rather than re-sent: the
// OS's own key-repeat would otherwise resend an identical push every ~30ms, which the
// watch cannot tell apart from a very fast double-press.
const EMU_KEYS={ArrowUp:'up',ArrowDown:'down',ArrowRight:'select',Enter:'select',
                ArrowLeft:'back',Backspace:'back'};
function emuBezelFocused(){
  const a=document.activeElement;
  return !!(emuTabActive&&a&&a.closest&&a.closest('.emubezel'));
}
$('#emuscreen').onclick=()=>document.querySelector('.emubezel').focus();
window.addEventListener('keydown',e=>{
  const btn=EMU_KEYS[e.key]; if(!btn||e.repeat) return;
  if(!emuBezelFocused()) return;
  e.preventDefault();
  emuDown(btn);
});
// NOT gated on focus: a hold started while focused must still release if focus moved
// away before the key came back up, or the button would look stuck down forever on a
// watch nobody is touching any more. Gated on emuHeld instead -- only acts on a key
// this panel itself put a button down for, so an unrelated Enter/arrow elsewhere on the
// page is never swallowed.
window.addEventListener('keyup',e=>{
  const btn=EMU_KEYS[e.key];
  if(!btn||!emuHeld.has(btn)) return;
  e.preventDefault();
  emuUp(btn);
});

// ------------------------------------------------------------------ sprite editor
//
// Pixels are held as ARGB2222 bytes -- the device's own encoding -- not as CSS colours.
// Painting in the target colour space means the canvas cannot show a colour the watch
// cannot, so nothing collapses on import.
// `origin` is set when the canvas holds ONE FRAME cut out of a sheet rather than a whole
// file. Saving then composites it back at that rect instead of replacing the file, because
// the other poses on that sheet are someone else's work and editing one should not be able
// to lose the row it sits in.
const PX={w:16,h:24,frames:1,zoom:12,data:null,colour:0xFF,tool:'pen',undo:[],
          origin:null};

function pxTotalH(){ return PX.h*PX.frames }

function pxInit(w,h,frames){
  // A fresh canvas is not a frame of anything, so the sheet it came from stops applying.
  // Leaving it set is how Save would composite an unrelated drawing into someone's sheet.
  PX.origin=null;
  if($('#pxtitle')) $('#pxtitle').textContent='Canvas';
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

async function pxLoadList(select){
  const files=await (await fetch('/api/art')).json();
  $('#pxopen').innerHTML='<option value="">—</option>'+
    files.map(f=>`<option value="${f.path}">${f.path}</option>`).join('');
  // Kept on S so the Declare panel can offer the same PNGs without a second request:
  // the sheet a sprite points at is nearly always the one just painted.
  S.art=files;
  const sh=$('#shsheet');
  if(sh){
    // `select` wins over the current value: after an import the sheet you just brought in
    // is the one you meant to slice, and leaving the old selection makes the import look
    // like it did nothing.
    const cur=select||sh.value;
    sh.innerHTML=files.map(f=>`<option${f.path===cur?' selected':''}>${f.path}</option>`)
      .join('');
  }
  drawSpriteForm();
}

// Importing art. Files arrive either from the picker or from a drop, and both end here:
// read as base64, posted, then the sheet lists reload with the new file selected.
//
// One at a time rather than in parallel. Two imports landing together can collide on a
// name, and the answer to "that already exists" is a question for the user -- which has
// to be asked about one file, in order, not about whichever of four requests replied
// first.
//
// `logId` is which tab is watching. The endpoint and the destination folder are the same
// wherever the file was dropped -- only the line that reports it differs.
async function importArt(files,logId){
  const log=$(logId||'#shimplog');
  const say=(msg,bad)=>{ log.className=bad?'bad':'dim'; log.textContent=msg };
  let last=null, done=0;
  for(const file of files){
    say(`importing ${file.name}…`);
    let data;
    try{
      data=await new Promise((ok,fail)=>{
        const fr=new FileReader();
        fr.onerror=()=>fail(new Error('could not be read'));
        // The result is a data: URL; everything after the comma is the base64 payload.
        fr.onload=()=>ok(String(fr.result).split(',')[1]||'');
        fr.readAsDataURL(file);
      });
    }catch(e){ say(`${file.name}: ${e.message}`,true); continue }

    let r=await post('/api/art/import',{name:file.name,data});
    if(!r.ok && /already exists/.test(r.error||'')){
      if(!confirm(`art/${file.name} already exists.\n\nReplace it?`)){
        say(`${file.name} skipped`); continue;
      }
      r=await post('/api/art/import',{name:file.name,data,replace:true});
    }
    if(!r.ok){ say(`${file.name}: ${r.error}`,true); continue }
    last=r.path; done++;
  }
  if(!last) return;
  // Both lists, whichever tab the file came in through: the two tabs read the same folder,
  // and a sheet imported on one being absent from the other is the same dead end again.
  await pxLoadList(last);
  if(typeof loadSheets==='function'){
    await loadSheets();
    const sel=$('#sheet');
    if(sel && [...sel.options].some(o=>o.value===last)){
      sel.value=last;
      // 'input', which is what the atlas fields are actually bound to. Assigning .value
      // fires nothing, so the region analysis would still be describing the old sheet.
      sel.dispatchEvent(new Event('input',{bubbles:true}));
    }
  }
  const f=S.art.find(a=>a.path===last)||{};
  // Bytes under a kilobyte. A 168-byte sheet rounding to "0 KB" reads as an import that
  // brought in nothing, which is the one thing the message exists to disprove.
  const size=!f.bytes ? ''
    : f.bytes<1024 ? ` — ${f.bytes} B` : ` — ${Math.round(f.bytes/1024)} KB`;
  say(done===1 ? `${last}${size}` : `${done} files imported, ${last} selected`);
}

// Wires one import well: a button that opens the picker, and the same box as a drop
// target. Called for both tabs, because "bring a file in" should not be two behaviours.
function wireImport(zoneId,fileId,pickId,logId){
  const zone=$(zoneId), input=$(fileId), pick=$(pickId);
  if(!zone||!input||!pick) return;
  pick.onclick=()=>input.click();
  input.onchange=async()=>{
    await importArt([...input.files],logId);
    // Cleared so picking the same file twice fires change again, which is what someone
    // does after replacing the file on disk.
    input.value='';
  };
  // dragover must be cancelled or the browser navigates to the dropped file instead,
  // which throws away the whole editor and any unsaved canvas with it.
  for(const ev of ['dragenter','dragover'])
    zone.addEventListener(ev,e=>{ e.preventDefault(); zone.classList.add('over') });
  for(const ev of ['dragleave','drop'])
    zone.addEventListener(ev,e=>{ e.preventDefault(); zone.classList.remove('over') });
  zone.addEventListener('drop',e=>importArt([...(e.dataTransfer.files||[])],logId));
}
wireImport('#shdrop','#shfile','#shpick','#shimplog');
wireImport('#atdrop','#atfile','#atpick','#atimplog');

$('#pxopen').addEventListener('change',async()=>{
  const path=$('#pxopen').value; if(!path) return;
  const r=await (await fetch('/api/sprite/read',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify({path})})).json();
  if(r.error){ $('#pxnote').textContent=r.error; return }
  // Height is assumed to be whole frames of the current frame height where it divides
  // cleanly -- the importer's own convention -- and one frame otherwise.
  const frames=(PX.h && r.h % PX.h===0) ? r.h/PX.h : 1;
  PX.origin=null;
  if($('#pxtitle')) $('#pxtitle').textContent='Canvas';
  PX.w=r.w; PX.h=r.h/frames; PX.frames=frames;
  $('#pxw').value=PX.w; $('#pxh').value=PX.h; $('#pxframes').value=frames;
  PX.data=Uint8Array.from(r.pixels); PX.undo=[];
  $('#pxname').value=path;
  pxDraw();
  $('#pxnote').textContent=`Loaded ${r.w}x${r.h}.`;
});

$('#pxsave').onclick=async()=>{
  // A frame cut out of a sheet goes back where it came from. Falling through to the
  // whole-file write would replace an eight-pose sheet with one 16x24 pose, which is a
  // loss no undo in this editor reaches.
  if(PX.origin){
    const o=PX.origin;
    const r=await post('/api/frame/write',{sheet:o.sheet, x:o.x, y:o.y,
      w:PX.w, h:PX.h, pixels:Array.from(PX.data)});
    if(r.error){ $('#pxnote').textContent=r.error; return }
    $('#pxnote').textContent=`Wrote the frame back into ${o.sheet} at ${o.x},${o.y}.`;
    // Re-slice so the grid shows what was just painted rather than the stale thumbnail.
    if(SH.sheet===o.sheet){ const keep=SH.picks.slice(); await $('#shslice').onclick();
                            SH.picks=keep; drawSheetGrid() }
    // And the declared-frame strip, for the same reason: it is the other view of the same
    // pixels, and a thumbnail that still shows the pose you just repainted reads as a save
    // that did not happen.
    if($('#spsel') && $('#spsel').value) await spShowFrames($('#spsel').value);
    return;
  }

  let path=$('#pxname').value.trim();
  if(!path){ $('#pxnote').textContent='Give it a filename first.'; return }
  if(!path.includes('/')) path='art/'+path;
  const r=await (await fetch('/api/sprite/write',{method:'POST',
    headers:{'content-type':'application/json'},
    body:JSON.stringify({path,w:PX.w,h:pxTotalH(),pixels:Array.from(PX.data)})})).json();
  $('#pxnote').textContent=r.error?r.error
    :`Saved ${r.path} (${r.bytes} B). Declare it below, or import it as a tileset.`;
  if(r.ok){
    await pxLoadList();
    // The sheet just saved, at the size just painted: the Declare panel underneath is
    // almost always about this PNG, and retyping what the canvas already knows is the
    // step that used to send people to the manifest.
    $('#spsheet').value=r.path;
    $('#spfw').value=PX.w; $('#spfh').value=PX.h; $('#spn').value=PX.frames;
  }
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
EMULATOR = Emulator()


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

# Routes that must NOT take it.
#
# The lock exists because the routing bodies share one Project. These do not touch it --
# they talk to the updater, the toolchain probe or the liveness clock, each of which
# guards its own state -- and taking the lock anyway is what let one slow network call
# freeze the whole editor.
#
# It was "Check for updates" that showed it. The check ran inside the handler, holding
# this lock, so every other request queued behind a call to GitHub: the heartbeat, the
# map, the build button. Worse, `urlopen(timeout=)` bounds the socket and NOT the name
# lookup, so with dead DNS -- a dropped VPN, a captive portal -- the freeze had no upper
# limit at all. Past 25 seconds of blocked heartbeat the liveness watchdog concluded the
# UI was gone and shut the editor down, which is how "check for updates" could close the
# window it was checking from.
LOCK_FREE_PATHS = frozenset({
    "/api/alive", "/api/ping",
    "/api/update", "/api/update/progress", "/api/update/check",
    "/api/update/download", "/api/update/apply",
})


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

        def _serialised(self):
            """The lock, unless this route has no business holding it."""
            path = self.path.split("?", 1)[0]
            return contextlib.nullcontext() if path in LOCK_FREE_PATHS else REQUEST_LOCK

        def do_GET(self):
            with self._serialised():
                self._route_get()

        def do_POST(self):
            with self._serialised():
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
            elif self.path.startswith("/api/emulator/status"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                platform = (q.get("platform") or [None])[0]
                if platform not in Project.PLATFORMS:
                    self._send(400, json.dumps({"error": "unknown platform"}))
                else:
                    self._send(200, json.dumps(EMULATOR.status(platform)))
            elif self.path.startswith("/api/emulator/frame"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                platform = (q.get("platform") or [None])[0]
                png = platform in Project.PLATFORMS and EMULATOR.frame(platform)
                if png:
                    self._send(200, png, "image/png")
                else:
                    # 204, not 404: the route exists, there is simply no frame yet --
                    # the panel polls this constantly while an emulator boots, and a 404
                    # would read as "this feature does not exist" in the browser console.
                    self._send(204, b"", "image/png")
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
                    # Two authoring formats, one endpoint: the page holds every map as
                    # cells plus a tile table, and which file that lands in is the map's
                    # business rather than the canvas's.
                    if m.get("format") == "source":
                        session.proj.save_source_map(
                            m["name"], m["w"], m["h"], m["cells"], m["tiles"],
                            m["start"], m["warps"])
                    else:
                        session.proj.save_map(m["name"], m["rows"], m["start"],
                                              m["warps"], m.get("atlas"),
                                              m.get("atlases"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/map/migrate":
                    d = json.loads(raw)
                    self._send(200, json.dumps(
                        {"ok": True, **session.proj.migrate_map(d["name"],
                                                                d.get("source"))}))
                elif self.path == "/api/legend":
                    d = json.loads(raw)
                    session.proj.save_legend(d["char"], d["tile"], d.get("atlas"),
                                             d.get("flags", []), d.get("flip", []),
                                             d.get("map"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/legend/remove":
                    d = json.loads(raw)
                    session.proj.remove_legend(d["char"], d.get("map"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/legend/users":
                    d = json.loads(raw)
                    self._send(200, json.dumps(
                        {"users": session.proj.legend_users(d["char"], d.get("map"))}))
                elif self.path == "/api/flag":
                    d = json.loads(raw)
                    self._send(200, json.dumps(
                        {"ok": True, **session.proj.save_flag(d["name"], d.get("bit"))}))
                elif self.path == "/api/flag/remove":
                    d = json.loads(raw)
                    session.proj.remove_flag(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/role":
                    d = json.loads(raw)
                    info = session.proj.save_role(d["atlas"], d["role"], d["index"])
                    self._send(200, json.dumps({"ok": True, **info}))
                elif self.path == "/api/role/remove":
                    d = json.loads(raw)
                    session.proj.remove_role(d["atlas"], d["role"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/autopick":
                    d = json.loads(raw)
                    session.proj.set_autopick(d["atlas"], d["roles"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/analyse":
                    d = json.loads(raw)
                    key = d.get("colorkey")
                    self._send(200, json.dumps(session.proj.analyse(
                        d["sheet"], int(d["tile"]), d["region"],
                        int(d["max_tiles"]), tuple(key) if key else None,
                        d.get("exclude", []),
                        int(d.get("ink_threshold", pa.DEFAULT_INK_THRESHOLD)))))
                elif self.path == "/api/slice":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.slice_grid(
                        d["sheet"], int(d["tile"]), d["region"],
                        d.get("exclude", []), d.get("colorkey"))))
                elif self.path == "/api/atlas/validate":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.validate_atlas(
                        d["sheet"], int(d["tile"]), d["region"], int(d["max_tiles"]),
                        d.get("exclude", []), d.get("colorkey"), d.get("name"))))
                elif self.path == "/api/atlas/spec":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.atlas_spec(d["name"])))
                elif self.path == "/api/atlas/update":
                    d = json.loads(raw)
                    session.proj.update_atlas(d["name"], d["sheet"], int(d["tile"]),
                                              d["region"], int(d["max_tiles"]),
                                              d.get("exclude", []), d.get("colorkey"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/atlas/extras":
                    d = json.loads(raw)
                    session.proj.set_atlas_extras(d["name"], d.get("metatiles"),
                                                  d.get("variants"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/atlas/users":
                    d = json.loads(raw)
                    self._send(200, json.dumps(
                        {"users": session.proj.atlas_users(d["name"])}))
                elif self.path == "/api/atlas/remove":
                    d = json.loads(raw)
                    session.proj.remove_atlas(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/atlas":
                    d = json.loads(raw)
                    session.proj.add_atlas(d["name"], d["sheet"], int(d["tile"]),
                                           d["region"], int(d["max_tiles"]),
                                           d.get("exclude", []), d.get("colorkey"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/font/remove":
                    d = json.loads(raw)
                    session.proj.remove_font(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/font/users":
                    d = json.loads(raw)
                    self._send(200, json.dumps(
                        {"users": session.proj.font_users(d["name"])}))
                elif self.path == "/api/project/set":
                    d = json.loads(raw)
                    session.proj.set_project(d["key"], d["value"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/sample":
                    d = json.loads(raw)
                    session.proj.save_sample(d["name"], d["file"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/sample/remove":
                    d = json.loads(raw)
                    session.proj.remove_sample(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/song":
                    d = json.loads(raw)
                    session.proj.add_song(d["name"], int(d.get("tempo", 120)),
                                          int(d.get("rows", 16)),
                                          bool(d.get("synth", True)))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/song/remove":
                    d = json.loads(raw)
                    session.proj.remove_song(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/song/instrument/add":
                    d = json.loads(raw)
                    session.proj.add_instrument(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/song/instrument/remove":
                    d = json.loads(raw)
                    session.proj.remove_instrument(d["name"], int(d["index"]))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/song/meta":
                    d = json.loads(raw)
                    session.proj.save_song_meta(d["name"], d.get("tempo"),
                                                d.get("order"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/song/pattern":
                    d = json.loads(raw)
                    session.proj.save_pattern(d["name"], int(d["index"]), d["rows"],
                                              bool(d.get("append")))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/song/instrument":
                    d = json.loads(raw)
                    session.proj.save_instrument(d["name"], int(d["index"]),
                                                 d["plain"], d.get("synth"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/dialog":
                    d = json.loads(raw)
                    session.proj.save_dialog(d["name"], d["pages"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/dialog/remove":
                    d = json.loads(raw)
                    session.proj.remove_dialog(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/sheet/frames":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.sheet_frames(
                        d["sheet"], d["fw"], d["fh"], d.get("ox", 0), d.get("oy", 0),
                        d.get("gx", 0), d.get("gy", 0), d.get("colorkey"))))
                elif self.path == "/api/frame/read":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.frame_read(
                        d["sheet"], d["x"], d["y"], d["w"], d["h"])))
                elif self.path == "/api/frame/write":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.frame_write(
                        d["sheet"], d["x"], d["y"], d["w"], d["h"], d["pixels"])))
                elif self.path == "/api/sprite/validate":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.validate_sprite(
                        d.get("name"), d["sheet"], d["frames"], d.get("anim"),
                        d.get("variants", []), d.get("colorkey"), d.get("bw_variant"))))
                elif self.path == "/api/sprite/save":
                    d = json.loads(raw)
                    session.proj.save_sprite(d["name"], d["sheet"], d["frames"],
                                             d.get("anim"), d.get("variants", []),
                                             d.get("colorkey"), d.get("bw_variant"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/sprite/remove":
                    d = json.loads(raw)
                    session.proj.remove_sprite(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/sprite/users":
                    d = json.loads(raw)
                    self._send(200, json.dumps(
                        {"users": session.proj.sprite_users(d["name"])}))
                elif self.path == "/api/art/import":
                    d = json.loads(raw)
                    # base64 rather than multipart: every other route here is a JSON POST,
                    # and one endpoint with its own body format would be the only place
                    # this server has to parse an envelope.
                    try:
                        blob = base64.b64decode(d.get("data", ""), validate=True)
                    except Exception:                    # noqa: BLE001
                        raise ValueError("the upload did not arrive intact") from None
                    self._send(200, json.dumps(
                        {"ok": True,
                         **session.proj.art_import(d.get("name", ""), blob,
                                                   bool(d.get("replace")))}))
                elif self.path == "/api/map/props":
                    d = json.loads(raw)
                    session.proj.set_map_props(
                        d["name"], d.get("palette"), d.get("worldtile"),
                        d.get("atlas_slots"), d.get("bank_bytes"), d.get("resident"))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/map/remove":
                    d = json.loads(raw)
                    session.proj.remove_map(d["name"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/map/rename":
                    d = json.loads(raw)
                    session.proj.rename_map(d["name"], d["to"])
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/map/users":
                    d = json.loads(raw)
                    self._send(200, json.dumps(
                        {"users": session.proj.map_users(d["name"])}))
                elif self.path == "/api/scene":
                    d = json.loads(raw)
                    session.proj.save_scene(d["name"], d.get("map"),
                                            d.get("sprites", []), d.get("fonts", []),
                                            bool(d.get("dialog")), d.get("atlases", []))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/api/scene/remove":
                    d = json.loads(raw)
                    session.proj.remove_scene(d["name"])
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
                elif self.path == "/api/emulator/start":
                    d = json.loads(raw)
                    platform = d.get("platform")
                    if not session.proj:
                        self._send(400, json.dumps({"ok": False, "error": "no project open"}))
                    elif platform not in Project.PLATFORMS:
                        self._send(400, json.dumps({"ok": False, "error": "unknown platform"}))
                    else:
                        ok, error = EMULATOR.start(session.proj, platform)
                        self._send(200, json.dumps({"ok": ok, "error": error}))
                elif self.path == "/api/emulator/stop":
                    self._send(200, json.dumps({"ok": EMULATOR.stop()}))
                elif self.path == "/api/emulator/button":
                    d = json.loads(raw)
                    platform = d.get("platform")
                    if platform not in Project.PLATFORMS:
                        self._send(400, json.dumps({"ok": False, "error": "unknown platform"}))
                    else:
                        ok = EMULATOR.button(platform, d.get("buttons", []),
                                             d.get("action", "push"))
                        self._send(200, json.dumps({"ok": ok}))
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
                    # An unrecognised platform is a client bug, not a project problem --
                    # fall back to the default (no-platform) estimate rather than 500ing
                    # the whole budget strip over it.
                    platform = d.get("platform")
                    if platform not in session.proj.PLATFORMS:
                        platform = None
                    self._send(200, json.dumps(
                        session.proj.estimate(d.get("maps"), platform)))
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
                elif self.path == "/api/sprite/frames":
                    d = json.loads(raw)
                    self._send(200, json.dumps(session.proj.sprite_frames(d["name"])))
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
        # The updater's whole job is one HTTPS request, and a frozen binary carries its
        # own OpenSSL with the build machine's CA paths compiled in. Without these roots
        # every check fails as "could not reach GitHub" -- which is what shipped.
        ("certifi", "certifi", lambda m: m.where()),
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

    # Can this binary actually verify a certificate? Importing certifi proves the module
    # is here; loading its roots into a context proves the DATA FILE came along, which is
    # the half PyInstaller drops. No network needed, so this works on a build machine
    # behind any firewall.
    try:
        ctx = https_context()
        stats = ctx.cert_store_stats()
        ok_certs = stats.get("x509_ca", 0) > 0
        print(f"  https roots      {stats.get('x509_ca', 0)} CA certificates loaded"
              if ok_certs else "  https roots      NONE LOADED -- update checks will fail")
        ok = ok and ok_certs
    except Exception as e:                               # noqa: BLE001
        print(f"  https roots      MISSING -- {e}")
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
        # The project you were last in, before anything found by searching. `find_manifest`
        # returns the examples in alphabetical order, so a no-argument launch always landed
        # on `audiotest` -- which has no maps, no atlases and nothing to click. Guessing is
        # the fallback, not the first answer.
        recent = Session().recent()
        if recent:
            target = recent[0]["path"]
            print(f"reopening {recent[0]['name']}  ({target})")
        else:
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

    # The updater needs this to free the port before it launches the replacement.
    global SERVER
    SERVER = srv

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
