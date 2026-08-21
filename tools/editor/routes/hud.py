"""HUD variable routes -- calls into HudMixin."""

import json


def handle_get_api_hud_var(self, session):
    self._send(200, json.dumps(session.proj.hud_vars()))

def handle_post_api_hud_var_save(self, session, raw):
    d = json.loads(raw)
    session.proj.save_hud_var(d["name"], d["type"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_hud_var_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_hud_var(d["name"])
    self._send(200, json.dumps({"ok": True}))


GET_EXACT = {
    '/api/hud_var': handle_get_api_hud_var,
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/hud_var/save': handle_post_api_hud_var_save,
    '/api/hud_var/remove': handle_post_api_hud_var_remove,
}
