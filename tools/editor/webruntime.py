"""Pyodide dispatch bridge: lets the browser drive the exact same route table
(`editor.routes`) the native companion serves over HTTP, in-process, with no sockets.

This exists so the hosted (GitHub Pages) editor can run real project edits -- fonts,
sprites, atlases, maps, nine-slice, HUD, music, scenes, code -- entirely inside Pyodide
(CPython compiled to WASM), against a folder the user picked on their own machine via
the File System Access API and mounted with `pyodide.mountNativeFS()`. Every route body
under `tools/editor/routes/` runs completely unmodified; only the transport around it is
different.

Deliberately does not import `server.py`: that module also pulls in
`socketserver`/`webbrowser`/`argparse`, real-process concerns with no meaning inside a
single-threaded WASM runtime, and its own dispatch logic (`_route_get`/`_route_post`) is
re-implemented here against a fake handler rather than reused, to avoid dragging that
baggage in. `REQUEST_LOCK`/`LOCK_FREE_PATHS` are likewise not ported: that lock exists
because real OS threads share one `session.proj`; Pyodide runs on the browser's one JS
thread, so there is nothing to serialise against.
"""

import json

from editor import routes
from editor.session import Session


class _FakeHandler:
    """Stands in for `http.server.BaseHTTPRequestHandler`. Every route body only ever
    touches `.path` and `._send(code, body, ctype)` -- confirmed across every handler
    under `tools/editor/routes/` -- so those are the only two things faked here.
    """

    def __init__(self, path):
        self.path = path
        self.status = None
        self.body = b""
        self.ctype = "application/json"

    def _send(self, code, body, ctype="application/json"):
        self.status = code
        self.ctype = ctype
        self.body = body if isinstance(body, (bytes, bytearray)) else body.encode()


class _SeededRecentStorage:
    """Recent-projects storage for the web runtime.

    IndexedDB is the real store, but it's async, and `Session.recent()`/`remember()`
    are called synchronously from inside route handlers (`routes/project.py`). Rather
    than make those routes async -- which would mean editing them, undoing the point of
    this bridge -- the JS side reads IndexedDB once before the page's first `dispatch()`
    call and seeds this with the result via `seed_recent()`; `remember()` just updates
    the in-memory copy and fires a fire-and-forget callback (`on_recent_change`) so JS
    can persist the change back to IndexedDB. Nothing here is read again until the next
    full page load, which re-seeds from IndexedDB anyway, so a write lost to a crash
    between `remember()` and the JS callback finishing costs at most one missed
    "recently opened" entry.
    """

    def __init__(self):
        self._paths = []
        self._on_change = None

    def seed(self, paths):
        self._paths = list(paths)

    def set_on_change(self, fn):
        self._on_change = fn

    def load(self):
        return self._paths

    def save(self, paths):
        self._paths = paths
        if self._on_change is not None:
            self._on_change(paths)


_storage = _SeededRecentStorage()
_session = Session(storage=_storage)


def seed_recent(paths):
    """Called once from JS, after IndexedDB is read and before the first `dispatch()`."""
    _storage.seed(paths)


def on_recent_change(fn):
    """Registers the JS callback `remember()` should fire into, so the page can persist
    the updated recent-projects list to IndexedDB."""
    _storage.set_on_change(fn)


def dispatch(method, path, raw=None):
    """Replays `server.py`'s `_route_get`/`_route_post` dispatch, in-process.

    Returns `{"status": int, "ctype": str, "body": bytes}` -- exactly what
    `self._send()` would have written to a socket, minus the socket.
    """
    h = _FakeHandler(path)
    bare = path.split("?", 1)[0]

    if method == "GET":
        fn = routes.GET_EXACT.get(bare)
        if fn is None:
            fn = next((c for p, c in routes.GET_PREFIX if path.startswith(p)), None)
        if fn is None:
            return {"status": 404, "ctype": "application/json", "body": b"{}"}
        fn(h, _session)
        return {"status": h.status, "ctype": h.ctype, "body": h.body}

    # POST. Same "no project open" gate as server.py's _route_post, ahead of the route
    # lookup, so the online tier's error text matches the companion's exactly.
    if (_session.proj is None
            and not path.startswith("/api/project/")
            and not path.startswith("/api/sdk/")):
        body = json.dumps({"ok": False, "error": "no project is open",
                           "output": "no project is open"}).encode()
        return {"status": 200, "ctype": "application/json", "body": body}

    fn = routes.POST_EXACT.get(bare)
    if fn is None:
        return {"status": 404, "ctype": "application/json", "body": b"{}"}

    body = bytes(raw) if raw is not None else b"{}"
    try:
        fn(h, _session, body)
    except Exception as e:                                 # noqa: BLE001
        h._send(200, json.dumps({"ok": False, "error": str(e), "output": str(e)}))
    return {"status": h.status, "ctype": h.ctype, "body": h.body}
