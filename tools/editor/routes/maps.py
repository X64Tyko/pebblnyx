"""Map, legend and flag routes -- calls into MapsMixin."""

import json


def handle_post_api_map(self, session, raw):
    m = json.loads(raw)
    # Two authoring formats, one endpoint: the page holds every map as
    # cells plus a tile table, and which file that lands in is the map's
    # business rather than the canvas's.
    if m.get("format") == "source":
        session.proj.save_source_map(
            m["name"], m["w"], m["h"], m["cells"], m["tiles"],
            m["start"], m["warps"])
    else:
        session.proj.save_map(m["name"], m["rows"], m["start"],
                              m["warps"], m.get("atlas"),
                              m.get("atlases"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_map_migrate(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        {"ok": True, **session.proj.migrate_map(d["name"],
                                                d.get("source"))}))

def handle_post_api_legend(self, session, raw):
    d = json.loads(raw)
    session.proj.save_legend(d["char"], d["tile"], d.get("atlas"),
                             d.get("flags", []), d.get("flip", []),
                             d.get("map"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_legend_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_legend(d["char"], d.get("map"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_legend_users(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        {"users": session.proj.legend_users(d["char"], d.get("map"))}))

def handle_post_api_flag(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        {"ok": True, **session.proj.save_flag(d["name"], d.get("bit"))}))

def handle_post_api_flag_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_flag(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_map_props(self, session, raw):
    d = json.loads(raw)
    session.proj.set_map_props(
        d["name"], d.get("palette"), d.get("worldtile"),
        d.get("atlas_slots"), d.get("bank_bytes"), d.get("resident"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_map_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_map(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_map_rename(self, session, raw):
    d = json.loads(raw)
    session.proj.rename_map(d["name"], d["to"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_map_users(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        {"users": session.proj.map_users(d["name"])}))

def handle_post_api_newmap(self, session, raw):
    d = json.loads(raw)
    session.proj.add_map(d["name"], int(d["w"]), int(d["h"]), d["atlas"])
    self._send(200, json.dumps({"ok": True}))


GET_EXACT = {
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/map': handle_post_api_map,
    '/api/map/migrate': handle_post_api_map_migrate,
    '/api/legend': handle_post_api_legend,
    '/api/legend/remove': handle_post_api_legend_remove,
    '/api/legend/users': handle_post_api_legend_users,
    '/api/flag': handle_post_api_flag,
    '/api/flag/remove': handle_post_api_flag_remove,
    '/api/map/props': handle_post_api_map_props,
    '/api/map/remove': handle_post_api_map_remove,
    '/api/map/rename': handle_post_api_map_rename,
    '/api/map/users': handle_post_api_map_users,
    '/api/newmap': handle_post_api_newmap,
}
