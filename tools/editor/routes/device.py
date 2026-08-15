"""Emulator panel routes -- talk to EMULATOR, not to a Project."""

import json

from editor.emulator import EMULATOR
from editor.project import Project


def handle_get_prefix_api_emulator_status(self, session):
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(self.path).query)
    platform = (q.get("platform") or [None])[0]
    if platform not in Project.PLATFORMS:
        self._send(400, json.dumps({"error": "unknown platform"}))
    else:
        self._send(200, json.dumps(EMULATOR.status(platform)))

def handle_get_prefix_api_emulator_frame(self, session):
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

def handle_post_api_emulator_start(self, session, raw):
    d = json.loads(raw)
    platform = d.get("platform")
    if not session.proj:
        self._send(400, json.dumps({"ok": False, "error": "no project open"}))
    elif platform not in Project.PLATFORMS:
        self._send(400, json.dumps({"ok": False, "error": "unknown platform"}))
    else:
        ok, error = EMULATOR.start(session.proj, platform)
        self._send(200, json.dumps({"ok": ok, "error": error}))

def handle_post_api_emulator_stop(self, session, raw):
    self._send(200, json.dumps({"ok": EMULATOR.stop()}))

def handle_post_api_emulator_button(self, session, raw):
    d = json.loads(raw)
    platform = d.get("platform")
    if platform not in Project.PLATFORMS:
        self._send(400, json.dumps({"ok": False, "error": "unknown platform"}))
    else:
        ok = EMULATOR.button(platform, d.get("buttons", []),
                             d.get("action", "push"))
        self._send(200, json.dumps({"ok": ok}))


GET_EXACT = {
}
GET_PREFIX = [
    ('/api/emulator/status', handle_get_prefix_api_emulator_status),
    ('/api/emulator/frame', handle_get_prefix_api_emulator_frame),
]
POST_EXACT = {
    '/api/emulator/start': handle_post_api_emulator_start,
    '/api/emulator/stop': handle_post_api_emulator_stop,
    '/api/emulator/button': handle_post_api_emulator_button,
}
