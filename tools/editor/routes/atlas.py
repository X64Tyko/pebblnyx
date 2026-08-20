"""Atlas import/carve/roles/collision routes -- calls into AtlasMixin."""

import json

import pnx_assets as pa                                     # noqa: E402


def handle_get_api_sheets(self, session):
    self._send(200, json.dumps(session.proj.sheets()))

def handle_post_api_role(self, session, raw):
    d = json.loads(raw)
    info = session.proj.save_role(d["atlas"], d["role"], d["index"])
    self._send(200, json.dumps({"ok": True, **info}))

def handle_post_api_role_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_role(d["atlas"], d["role"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_autopick(self, session, raw):
    d = json.loads(raw)
    session.proj.set_autopick(d["atlas"], d["roles"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_analyse(self, session, raw):
    d = json.loads(raw)
    key = d.get("colorkey")
    self._send(200, json.dumps(session.proj.analyse(
        d["sheet"], int(d["tile"]), d["region"],
        int(d["max_tiles"]), tuple(key) if key else None,
        d.get("exclude", []),
        int(d.get("ink_threshold", pa.DEFAULT_INK_THRESHOLD)),
        d.get("offset", (0, 0)))))

def handle_post_api_slice(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.slice_grid(
        d["sheet"], int(d["tile"]), d["region"],
        d.get("exclude", []), d.get("colorkey"), d.get("offset", (0, 0)),
        int(d.get("max_tiles", 255)))))

def handle_post_api_atlas_tiles(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.carve_tiles(
        d["sheet"], int(d["tile"]), d["region"], int(d["max_tiles"]),
        d.get("exclude", []), d.get("colorkey"), d.get("offset", (0, 0)),
        d.get("name"))))

def handle_post_api_atlas_origin(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.origin_map(d["name"])))

def handle_post_api_atlas_validate(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.validate_atlas(
        d["sheet"], int(d["tile"]), d["region"], int(d["max_tiles"]),
        d.get("exclude", []), d.get("colorkey"), d.get("name"),
        d.get("offset", (0, 0)))))

def handle_post_api_atlas_spec(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.atlas_spec(d["name"])))

def handle_post_api_atlas_update(self, session, raw):
    d = json.loads(raw)
    session.proj.update_atlas(d["name"], d["sheet"], int(d["tile"]),
                              d["region"], int(d["max_tiles"]),
                              d.get("exclude", []), d.get("colorkey"),
                              d.get("offset", (0, 0)))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_atlas_extras(self, session, raw):
    d = json.loads(raw)
    session.proj.set_atlas_extras(d["name"], d.get("metatiles"),
                                  d.get("variants"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_atlas_users(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        {"users": session.proj.atlas_users(d["name"])}))

def handle_post_api_atlas_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_atlas(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_atlas(self, session, raw):
    d = json.loads(raw)
    session.proj.add_atlas(d["name"], d["sheet"], int(d["tile"]),
                           d["region"], int(d["max_tiles"]),
                           d.get("exclude", []), d.get("colorkey"),
                           d.get("offset", (0, 0)))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_atlas_collision(self, session, raw):
    d = json.loads(raw)
    session.proj.save_atlas_collision(
        d["atlas"], d["tile"], int(d["mode"]), kind=int(d.get("kind", 0)),
        rect=d.get("rect"), mask_rows=d.get("mask"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_atlas_collision_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_atlas_collision(d["atlas"], d["tile"])
    self._send(200, json.dumps({"ok": True}))


GET_EXACT = {
    '/api/sheets': handle_get_api_sheets,
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/role': handle_post_api_role,
    '/api/role/remove': handle_post_api_role_remove,
    '/api/autopick': handle_post_api_autopick,
    '/api/analyse': handle_post_api_analyse,
    '/api/slice': handle_post_api_slice,
    '/api/atlas/tiles': handle_post_api_atlas_tiles,
    '/api/atlas/validate': handle_post_api_atlas_validate,
    '/api/atlas/spec': handle_post_api_atlas_spec,
    '/api/atlas/update': handle_post_api_atlas_update,
    '/api/atlas/extras': handle_post_api_atlas_extras,
    '/api/atlas/users': handle_post_api_atlas_users,
    '/api/atlas/remove': handle_post_api_atlas_remove,
    '/api/atlas': handle_post_api_atlas,
    '/api/atlas/collision': handle_post_api_atlas_collision,
    '/api/atlas/collision/remove': handle_post_api_atlas_collision_remove,
}
