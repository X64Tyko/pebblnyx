"""Song/pattern/instrument/sample routes -- calls into MusicMixin."""

import json


# The raw WAV behind a declared sample, for an <audio> preview -- there was previously
# no way to hear a sample without a build and a device/emulator round trip.
def handle_get_prefix_api_sample_wav(self, session):
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(self.path).query)
    name = (q.get("name") or [None])[0]
    try:
        data = session.proj.sample_wav_bytes(name)
    except (ValueError, OSError):
        self._send(404, b"", "audio/wav")
        return
    self._send(200, data, "audio/wav")

def handle_post_api_sample(self, session, raw):
    d = json.loads(raw)
    session.proj.save_sample(d["name"], d["file"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_sample_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_sample(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_song(self, session, raw):
    d = json.loads(raw)
    session.proj.add_song(d["name"], int(d.get("tempo", 120)),
                          int(d.get("rows", 16)),
                          bool(d.get("synth", True)))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_song_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_song(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_song_instrument_add(self, session, raw):
    d = json.loads(raw)
    session.proj.add_instrument(d["name"])
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_song_instrument_remove(self, session, raw):
    d = json.loads(raw)
    session.proj.remove_instrument(d["name"], int(d["index"]))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_song_meta(self, session, raw):
    d = json.loads(raw)
    session.proj.save_song_meta(d["name"], d.get("tempo"),
                                d.get("order"))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_song_pattern(self, session, raw):
    d = json.loads(raw)
    session.proj.save_pattern(d["name"], int(d["index"]), d["rows"],
                              bool(d.get("append")))
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_song_instrument(self, session, raw):
    d = json.loads(raw)
    session.proj.save_instrument(d["name"], int(d["index"]),
                                 d["plain"], d.get("synth"))
    self._send(200, json.dumps({"ok": True}))


GET_EXACT = {
}
GET_PREFIX = [
    ('/api/sample/wav', handle_get_prefix_api_sample_wav),
]
POST_EXACT = {
    '/api/sample': handle_post_api_sample,
    '/api/sample/remove': handle_post_api_sample_remove,
    '/api/song': handle_post_api_song,
    '/api/song/remove': handle_post_api_song_remove,
    '/api/song/instrument/add': handle_post_api_song_instrument_add,
    '/api/song/instrument/remove': handle_post_api_song_instrument_remove,
    '/api/song/meta': handle_post_api_song_meta,
    '/api/song/pattern': handle_post_api_song_pattern,
    '/api/song/instrument': handle_post_api_song_instrument,
}
