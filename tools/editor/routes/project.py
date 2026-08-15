"""Project lifecycle (open/create/adopt/browse), the page itself, and the handful of
routes not tied to one editor domain (state, orientation, estimate, build).
"""

import json
import os

import pnx_project as pp                                    # noqa: E402


_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "static")
with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as _f:
    INDEX_HTML = _f.read()


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

def handle_get_root(self, session):
    self._send(200, INDEX_HTML, "text/html; charset=utf-8")

def handle_get_api_state(self, session):
    self._send(200, json.dumps(session.proj.state() if session.proj
                               else {"no_project": True}))

def handle_get_api_alive(self, session):
    self._send(200, "{}")

def handle_get_api_ping(self, session):
    self._send(200, json.dumps({"app": "pebblnyx-editor",
                                "version": pp.EDITOR_VERSION,
                                "project": session.proj.root
                                if session.proj else None}))

def handle_get_api_project_recent(self, session):
    self._send(200, json.dumps({
        "recent": session.recent(),
        "engine": pp.FRAMEWORK_VERSION,
        "have_engine": pp.framework_available(),
        "open": session.proj.root if session.proj else None,
    }))

def handle_get_prefix_api_project_browse(self, session):
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(self.path).query)
    self._send(200, json.dumps(browse((q.get("path") or [None])[0])))

def handle_post_api_project_set(self, session, raw):
    d = json.loads(raw)
    session.proj.set_project(d["key"], d["value"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_orientation(self, session, raw):
    session.proj.set_orientation(json.loads(raw)["orientation"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_project_open(self, session, raw):
    d = json.loads(raw)
    session.open(d["path"])
    self._send(200, json.dumps({"ok": True,
                                "root": session.proj.root}))

def handle_post_api_project_create(self, session, raw):
    d = json.loads(raw)
    folder = os.path.join(os.path.expanduser(d["parent"]), d["folder"])
    pp.create(folder, d["name"], d.get("author", ""))
    session.open(folder)
    self._send(200, json.dumps({"ok": True, "root": folder}))

def handle_post_api_project_adopt(self, session, raw):
    root = session.proj.root
    meta = dict(session.proj.meta)
    meta.setdefault("name", os.path.basename(root))
    meta.setdefault("manifest", os.path.basename(session.proj.path))
    pp.save(root, meta)
    session.open(root)
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_engine_own(self, session, raw):
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

def handle_post_api_estimate(self, session, raw):
    d = json.loads(raw) if raw.strip() else {}
    # An unrecognised platform is a client bug, not a project problem --
    # fall back to the default (no-platform) estimate rather than 500ing
    # the whole budget strip over it.
    platform = d.get("platform")
    if platform not in session.proj.PLATFORMS:
        platform = None
    self._send(200, json.dumps(
        session.proj.estimate(d.get("maps"), platform)))

def handle_post_api_build(self, session, raw):
    self._send(200, json.dumps(session.proj.build()))


GET_EXACT = {
    '/': handle_get_root,
    '/api/state': handle_get_api_state,
    '/api/alive': handle_get_api_alive,
    '/api/ping': handle_get_api_ping,
    '/api/project/recent': handle_get_api_project_recent,
}
GET_PREFIX = [
    ('/api/project/browse', handle_get_prefix_api_project_browse),
]
POST_EXACT = {
    '/api/project/set': handle_post_api_project_set,
    '/api/orientation': handle_post_api_orientation,
    '/api/project/open': handle_post_api_project_open,
    '/api/project/create': handle_post_api_project_create,
    '/api/project/adopt': handle_post_api_project_adopt,
    '/api/engine/own': handle_post_api_engine_own,
    '/api/estimate': handle_post_api_estimate,
    '/api/build': handle_post_api_build,
}
