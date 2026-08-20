"""Sprite sheet and pixel-editor routes -- calls into SpritesMixin."""

import base64
import json


def handle_get_api_art(self, session):
    self._send(200, json.dumps(session.proj.art_files()))

def handle_post_api_sheet_frames(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.sheet_frames(
        d["sheet"], d["fw"], d["fh"], d.get("ox", 0), d.get("oy", 0),
        d.get("gx", 0), d.get("gy", 0), d.get("colorkey"))))

def handle_post_api_sheet_image(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.sheet_image(d["sheet"])))

def handle_post_api_frame_read(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.frame_read(
        d["sheet"], d["x"], d["y"], d["w"], d["h"])))

def handle_post_api_frame_write(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.frame_write(
        d["sheet"], d["x"], d["y"], d["w"], d["h"], d["pixels"])))

def handle_post_api_sprite_validate(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.validate_sprite(
        d.get("name"), d["sheet"], d["frames"], d.get("anim"),
        d.get("variants", []), d.get("colorkey"), d.get("bw_variant"))))

def handle_post_api_sprite_save(self, session, raw):
    d = json.loads(raw)
    session.proj.save_sprite(d["name"], d["sheet"], d["frames"],
                             d.get("anim"), d.get("variants", []),
                             d.get("colorkey"), d.get("bw_variant"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_sprite_collision(self, session, raw):
    d = json.loads(raw)
    session.proj.save_sprite_collision(
        d["name"], int(d["frame"]), int(d["mode"]), kind=int(d.get("kind", 0)),
        rect=d.get("rect"), mask_rows=d.get("mask"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_sprite_collision_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_sprite_collision(d["name"], int(d["frame"]))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_sprite_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_sprite(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_sprite_users(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        {"users": session.proj.sprite_users(d["name"])}))

def handle_post_api_art_import(self, session, raw):
    d = json.loads(raw)
    # base64 rather than multipart: every other route here is a JSON POST,
    # and one endpoint with its own body format would be the only place
    # this server has to parse an envelope.
    try:
        blob = base64.b64decode(d.get("data", ""), validate=True)
    except Exception:                    # noqa: BLE001
        raise ValueError("the upload did not arrive intact") from None
    self._send(200, json.dumps(
        {"ok": True,
         **session.proj.art_import(d.get("name", ""), blob,
                                   bool(d.get("replace")))}))

def handle_post_api_sprite_read(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.sprite_read(d["path"])))

def handle_post_api_sprite_frames(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.sprite_frames(d["name"])))

def handle_post_api_sprite_write(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.sprite_write(
        d["path"], int(d["w"]), int(d["h"]), d["pixels"])))


GET_EXACT = {
    '/api/art': handle_get_api_art,
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/sheet/frames': handle_post_api_sheet_frames,
    '/api/sheet/image': handle_post_api_sheet_image,
    '/api/frame/read': handle_post_api_frame_read,
    '/api/frame/write': handle_post_api_frame_write,
    '/api/sprite/validate': handle_post_api_sprite_validate,
    '/api/sprite/save': handle_post_api_sprite_save,
    '/api/sprite/collision': handle_post_api_sprite_collision,
    '/api/sprite/collision/remove': handle_post_api_sprite_collision_remove,
    '/api/sprite/remove': handle_post_api_sprite_remove,
    '/api/sprite/users': handle_post_api_sprite_users,
    '/api/art/import': handle_post_api_art_import,
    '/api/sprite/read': handle_post_api_sprite_read,
    '/api/sprite/frames': handle_post_api_sprite_frames,
    '/api/sprite/write': handle_post_api_sprite_write,
}
