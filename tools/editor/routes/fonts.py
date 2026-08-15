"""Font routes -- calls into FontsMixin."""

import json


def handle_get_api_fonts(self, session):
    self._send(200, json.dumps(session.proj.font_sources()))

def handle_post_api_font_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_font(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_font_users(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        {"users": session.proj.font_users(d["name"])}))

def handle_post_api_font_preview(self, session, raw):
    self._send(200, json.dumps(session.proj.font_preview(json.loads(raw))))

def handle_post_api_font_scene(self, session, raw):
    self._send(200, json.dumps(session.proj.font_scene(json.loads(raw))))

def handle_post_api_font(self, session, raw):
    session.proj.add_font(json.loads(raw))
    self._send(200, json.dumps({"ok": True}))


GET_EXACT = {
    '/api/fonts': handle_get_api_fonts,
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/font/remove': handle_post_api_font_remove,
    '/api/font/users': handle_post_api_font_users,
    '/api/font/preview': handle_post_api_font_preview,
    '/api/font/scene': handle_post_api_font_scene,
    '/api/font': handle_post_api_font,
}
