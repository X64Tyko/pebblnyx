"""Update checking and self-replacement: `Updater`, plus the version-compare helpers it
uses (`parse_version`, `newer`) and the relaunch dance (`restart_later`) it triggers once
a new build has been staged.

Split out of the original pnx_editor.py monolith; see tools/editor/__init__.py for why
sibling `pnx_*` modules resolve the same way from here as they used to from tools/.
"""

import contextlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request

import pnx_project as pp                                    # noqa: E402

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


