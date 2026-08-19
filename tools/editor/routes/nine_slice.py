"""9-slice panel routes -- calls into NineSliceMixin."""

import json


def handle_get_api_nine_slice(self, session):
    self._send(200, json.dumps(session.proj.nine_slices()))

def handle_post_api_nine_slice_validate(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.validate_nine_slice(
        d.get("name"), d["sheet"], d["border"], d.get("rect"), d.get("colorkey"))))

def handle_post_api_nine_slice_preview(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.nine_slice_preview(
        d["sheet"], d["border"], d.get("rect"), d.get("colorkey"),
        d.get("test_w", 48), d.get("test_h", 32))))

def handle_post_api_nine_slice_save(self, session, raw):
    d = json.loads(raw)
    session.proj.save_nine_slice(d["name"], d["sheet"], d["border"],
                                 d.get("rect"), d.get("colorkey"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_nine_slice_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_nine_slice(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_nine_slice_users(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        {"users": session.proj.nine_slice_users(d["name"])}))


GET_EXACT = {
    '/api/nine_slice': handle_get_api_nine_slice,
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/nine_slice/validate': handle_post_api_nine_slice_validate,
    '/api/nine_slice/preview': handle_post_api_nine_slice_preview,
    '/api/nine_slice/save': handle_post_api_nine_slice_save,
    '/api/nine_slice/remove': handle_post_api_nine_slice_remove,
    '/api/nine_slice/users': handle_post_api_nine_slice_users,
}
