"""HUD window routes -- calls into HudWindowMixin."""

import json


def handle_get_api_hud_window(self, session):
    self._send(200, json.dumps(session.proj.hud_windows()))

def handle_post_api_hud_window_save(self, session, raw):
    d = json.loads(raw)
    session.proj.save_hud_window(d["name"], d.get("show_ms", 200), d.get("hide_ms", 200),
                                 d.get("ease", "linear"), d.get("slide", [0, 0]))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_hud_window_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_hud_window(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_hud_window_element_save(self, session, raw):
    d = json.loads(raw)
    session.proj.save_hud_window_element(
        d["window"], d.get("index"), d["kind"], d.get("anchor", "top_left"),
        d.get("offset", [0, 0]), d.get("panel"), d.get("sprite"), d.get("frame", 0),
        d.get("hud_var"), d.get("font"), d.get("w"), d.get("h"), d.get("max"),
        d.get("border"), d.get("track"), d.get("fill"), d.get("colour"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_hud_window_element_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_hud_window_element(d["window"], d["index"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_hud_window_preview(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.hud_window_preview(d["name"])))


GET_EXACT = {
    '/api/hud_window': handle_get_api_hud_window,
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/hud_window/save': handle_post_api_hud_window_save,
    '/api/hud_window/remove': handle_post_api_hud_window_remove,
    '/api/hud_window/element/save': handle_post_api_hud_window_element_save,
    '/api/hud_window/element/remove': handle_post_api_hud_window_element_remove,
    '/api/hud_window/preview': handle_post_api_hud_window_preview,
}
