"""Dialog routes -- calls into DialogMixin."""

import json


def handle_post_api_dialog(self, session, raw):
    d = json.loads(raw)
    session.proj.save_dialog(d["name"], d["pages"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_dialog_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_dialog(d["name"])
    self._send(200, json.dumps({"ok": True}))


GET_EXACT = {
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/dialog': handle_post_api_dialog,
    '/api/dialog/remove': handle_post_api_dialog_remove,
}
