"""`Session`: the project currently open, and the ones opened before.

Split out of `server.py` so `editor.webruntime` (the Pyodide bridge) can use the exact
same class without importing `server.py` itself, which pulls in `socketserver`,
`webbrowser` and `argparse` -- real-process concerns with no meaning inside a WASM
sandbox, and no reason to ship into the browser payload.
"""

import json
import os

import pnx_project as pp                                    # noqa: E402

from editor.project import Project


class _FileRecentStorage:
    """Today's persistence for the recent-projects list: one JSON file under
    `_config_dir()`. The only backend the native companion ever uses -- the web
    runtime's own storage (`editor.webruntime._SeededRecentStorage`) never touches this
    class, which is why the `_config_dir` import below is deferred into the method
    rather than sitting at module scope: `editor.updater` (where `_config_dir` lives)
    isn't shipped into the browser payload, so importing it eagerly here would break
    `import editor.session` -- and with it `editor.webruntime` -- online even though
    nothing online ever calls this method.
    """

    def _path(self):
        from editor.updater import _config_dir
        return os.path.join(_config_dir(), "recent.json")

    def load(self):
        try:
            with open(self._path()) as f:
                return json.load(f)
        except Exception:                                # noqa: BLE001
            return []

    def save(self, paths):
        with open(self._path(), "w") as f:
            json.dump(paths, f, indent=2)


class Session:
    """The project currently open, and the ones opened before.

    A mutable holder rather than a captured value, because the editor can now switch
    projects without restarting -- the request handlers all read through this.

    `storage` is where the recent-projects path list lives. It defaults to a JSON file
    on disk (`_FileRecentStorage`), which is all the native companion ever needs. The
    Pyodide web runtime (`editor.webruntime`) passes its own storage instead, because
    there is no real config directory inside a WASM sandbox and the actual persistence
    happens in IndexedDB on the JS side -- see that module for why.
    """

    RECENT_MAX = 12

    def __init__(self, proj=None, storage=None):
        self.storage = storage or _FileRecentStorage()
        self.proj = proj
        if proj:
            self.remember(proj.root)

    def recent(self):
        paths = self.storage.load()
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
        self.storage.save(paths[:self.RECENT_MAX])

    def open(self, folder):
        proj = Project(folder)
        self.proj = proj
        self.remember(proj.root)
        return proj
