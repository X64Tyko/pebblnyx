"""The HTTP route table, assembled from the per-domain modules in this package.

Each domain module (atlas, maps, sprites, ...) exports up to three names:
    GET_EXACT   -- {path: handler(self, session)}
    GET_PREFIX  -- [(prefix, handler(self, session)), ...], checked in listed order
    POST_EXACT  -- {path: handler(self, session, raw)}

`handler`'s first parameter is the live BaseHTTPRequestHandler instance (named `self` in
every handler body, unchanged from when these bodies were `elif` branches of a method
literally named `self`) -- handlers call `self._send(...)` and, where they need it,
read `self.path` directly, exactly as before. Splitting the file changed nothing about
how a request is served; `tools/pnx_editor.py`'s original `_route_get`/`_route_post`
were two ~500-line `if/elif` chains doing this same dispatch by hand.

Each domain is imported by name inside a try/except rather than with a static `from ...
import` list. `sdk` and `device` reach `pebble`/QEMU through `editor.toolchain` and
`editor.emulator`, which are unavailable or non-functional in the Pyodide (in-browser)
build of this editor -- a domain that fails to import there is simply absent from the
tables instead of taking `import editor.routes` down with it. This keeps ONE route table
for every environment: `editor.webruntime` (the Pyodide bridge) reads `GET_EXACT` etc.
straight from here, the same way `server.py` does, rather than keeping a second,
hand-maintained list of "online-safe" domains that could silently drift out of sync with
this one as domains are added.
"""

import importlib

_DOMAIN_NAMES = ("atlas", "maps", "sprites", "nine_slice", "fonts", "code", "music",
                 "dialog", "scenes", "sdk", "device", "project", "hud", "hud_window")

GET_EXACT = {}
GET_PREFIX = []
POST_EXACT = {}

for _name in _DOMAIN_NAMES:
    try:
        _mod = importlib.import_module(f"editor.routes.{_name}")
    except ImportError:
        continue
    GET_EXACT.update(getattr(_mod, "GET_EXACT", {}))
    GET_PREFIX.extend(getattr(_mod, "GET_PREFIX", []))
    POST_EXACT.update(getattr(_mod, "POST_EXACT", {}))
