"""The code tab's routes -- calls into CodeMixin."""

import json


def handle_get_api_code_tree(self, session):
    self._send(200, json.dumps(session.proj.code_tree()))

def handle_get_api_code_symbols(self, session):
    self._send(200, json.dumps(session.proj.code_symbols()))

def handle_post_api_code_read(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(session.proj.code_read(d["path"])))

def handle_post_api_code_lint(self, session, raw):
    self._send(200, json.dumps(
        session.proj.code_lint(json.loads(raw)["path"])))

def handle_post_api_code_write(self, session, raw):
    d = json.loads(raw)
    self._send(200, json.dumps(
        session.proj.code_write(d["path"], d["text"])))


GET_EXACT = {
    '/api/code/tree': handle_get_api_code_tree,
    '/api/code/symbols': handle_get_api_code_symbols,
}
GET_PREFIX = [
]
POST_EXACT = {
    '/api/code/read': handle_post_api_code_read,
    '/api/code/lint': handle_post_api_code_lint,
    '/api/code/write': handle_post_api_code_write,
}
