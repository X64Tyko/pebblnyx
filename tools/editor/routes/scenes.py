"""Scene routes -- calls into ScenesMixin."""

import json


def handle_post_api_scene(self, session, raw):
    d = json.loads(raw)
    session.proj.save_scene(d["name"], d.get("map"),
                            d.get("sprites", []), d.get("fonts", []),
                            bool(d.get("dialog")), d.get("atlases", []),
                            d.get("nine_slices", []))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_scene_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_scene(d["name"])
    self._send(200, json.dumps({"ok": True}))


GET_EXACT = {
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/scene': handle_post_api_scene,
    '/api/scene/remove': handle_post_api_scene_remove,
}
