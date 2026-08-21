"""The HTTP server: `Session`, `EditorServer`, `make_handler`, and everything around
launching one -- window/browser opening, port-claiming, `--selftest`, and `main()`.

`make_handler` used to hold two ~500-line `if/elif` chains dispatching every route by
hand (`_route_get`/`_route_post` in the original tools/pnx_editor.py). Both are now a
table lookup into `editor.routes`, built from the per-domain modules there; see that
package's docstring for the handler calling convention.
"""

import argparse
import contextlib
import errno
import http.server
import json
import logging
import mimetypes
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import pnx_project as pp                                    # noqa: E402

import editor.updater as updater_mod
from editor import routes
from editor.liveness import LIVE
from editor.project import Project
from editor.session import Session
from editor.updater import UPDATER, https_context, _config_dir

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# The hosted (GitHub Pages) editor talks to this companion cross-origin, so browsers
# preflight it. This is deliberately NOT "Access-Control-Allow-Origin: *" -- a route
# table that can write files, launch external tools, and control a device or an
# emulator must not be reachable from an arbitrary website just because it guessed
# this is running on 127.0.0.1. Only the hosted editor's own origin, plus
# localhost/127.0.0.1 at any port (a locally-served copy of the static site, or a
# future local dev server for it), are ever allowed.
CORS_ALLOWED_ORIGINS = frozenset({
    "https://x64tyko.github.io",
})
CORS_ALLOWED_PREFIXES = ("http://127.0.0.1:", "http://localhost:")


def _origin_allowed(origin):
    return bool(origin) and (origin in CORS_ALLOWED_ORIGINS
                             or origin.startswith(CORS_ALLOWED_PREFIXES))


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

        def do_OPTIONS(self):
            # The CORS preflight a browser sends before a cross-origin request from
            # the hosted editor. Nothing here touches session.proj, so it doesn't take
            # REQUEST_LOCK -- same reasoning as LOCK_FREE_PATHS above.
            origin = self.headers.get("Origin", "")
            if not _origin_allowed(origin):
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def _send(self, code, body, ctype="application/json"):
            # Any answered request counts as the UI being present, not just the
            # heartbeat: a long build or a big font preview must not look like silence.
            LIVE.touch()
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            origin = self.headers.get("Origin", "")
            if _origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(data)

        def _serve_static(self, path):
            """`/static/...` -> a file under editor/static/. The one route that isn't
            in `editor.routes`'s table: it serves a whole directory of files -- the
            frontend's HTML/CSS/JS -- rather than one JSON endpoint. Everything the
            original inline `PAGE` string used to carry as one Python r-string.
            """
            rel = path.split("?", 1)[0][len("/static/"):]
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
                self._send(404, "{}")
                return
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

        def _route_get(self):
            if self.path.startswith("/static/"):
                self._serve_static(self.path)
                return
            fn = routes.GET_EXACT.get(self.path)
            if fn is None:
                for prefix, candidate in routes.GET_PREFIX:
                    if self.path.startswith(prefix):
                        fn = candidate
                        break
            if fn is None:
                self._send(404, "{}")
                return
            fn(self, session)

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
                fn = routes.POST_EXACT.get(self.path)
                if fn is None:
                    self._send(404, "{}")
                    return
                fn(self, session, raw)
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
    updater_mod.SERVER = srv

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
