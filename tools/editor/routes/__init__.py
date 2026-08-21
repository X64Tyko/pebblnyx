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
"""

from editor.routes import atlas, maps, sprites, nine_slice, fonts, code, music, dialog, scenes
from editor.routes import sdk, device, project, hud, hud_window

_DOMAINS = (atlas, maps, sprites, nine_slice, fonts, code, music, dialog, scenes, sdk, device,
           project, hud, hud_window)

GET_EXACT = {}
GET_PREFIX = []
POST_EXACT = {}

for _mod in _DOMAINS:
    GET_EXACT.update(getattr(_mod, "GET_EXACT", {}))
    GET_PREFIX.extend(getattr(_mod, "GET_PREFIX", []))
    POST_EXACT.update(getattr(_mod, "POST_EXACT", {}))
