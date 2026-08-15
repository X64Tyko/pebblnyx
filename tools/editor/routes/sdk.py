"""Pebble SDK + editor self-update routes -- talk to TOOLCHAIN and UPDATER, not to a Project."""

import json

from editor.toolchain import TOOLCHAIN
from editor.updater import UPDATER


def handle_get_api_update(self, session):
    self._send(200, json.dumps(UPDATER.check()))

def handle_get_api_update_progress(self, session):
    self._send(200, json.dumps(UPDATER.state()))

def handle_get_prefix_api_sdk_status(self, session):
    remote = "remote=1" in self.path
    self._send(200, json.dumps(TOOLCHAIN.status(remote=remote)))

def handle_post_api_update_check(self, session, raw):
    self._send(200, json.dumps(UPDATER.check(force=True)))

def handle_post_api_update_download(self, session, raw):
    self._send(200, json.dumps(UPDATER.start_download()))

def handle_post_api_update_apply(self, session, raw):
    self._send(200, json.dumps(UPDATER.apply()))

def handle_post_api_sdk_accept(self, session, raw):
    TOOLCHAIN.accept()
    self._send(200, json.dumps({"ok": True}))

def handle_post_api_sdk_install(self, session, raw):
    d = json.loads(raw) if raw.strip() else {}
    TOOLCHAIN.install(d.get("version", "latest"))
    self._send(200, json.dumps({"ok": True}))


GET_EXACT = {
    '/api/update': handle_get_api_update,
    '/api/update/progress': handle_get_api_update_progress,
}
GET_PREFIX = [
    ('/api/sdk/status', handle_get_prefix_api_sdk_status),
]
POST_EXACT = {
    '/api/update/check': handle_post_api_update_check,
    '/api/update/download': handle_post_api_update_download,
    '/api/update/apply': handle_post_api_update_apply,
    '/api/sdk/accept': handle_post_api_sdk_accept,
    '/api/sdk/install': handle_post_api_sdk_install,
}
