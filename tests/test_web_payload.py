#!/usr/bin/env python3
"""Tests that `web-payload.zip` (tools/build_web_payload.py) is actually self-contained.

This is the one property that matters for the hosted editor and that nothing else
catches: the payload is a *subset* of tools/, built by listing files rather than by
tracing imports, so it's entirely possible to add a route or a helper that pulls in a
module the list doesn't include -- and have every other test pass, because they all run
with the *full* tools/ tree on sys.path, same as this repo's own editor always has.
Pyodide won't: the browser only ever has what's inside this zip.

So this test extracts the real built zip into an empty temp directory and imports
`editor.webruntime` in a FRESH subprocess with sys.path pointing at nothing but that
directory -- not this process's sys.path, which already has the rest of tools/ on it and
would hide a missing module completely. Two real bugs were caught exactly this way while
building the web runtime: `editor.webruntime` importing `editor.server` (which pulls in
socketserver/webbrowser/argparse, not part of the payload) instead of the extracted
`editor.session`, and `editor.project.build` needing `size_report` at import time even
though the ARM-toolchain calls inside it already degrade gracefully on their own.

Run:  python3 tests/test_web_payload.py
"""

import json
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

failures = 0
checks = 0


def check(label, cond):
    global checks, failures
    checks += 1
    if not cond:
        print(f"  FAIL {label}")
        failures += 1


def _run_in_payload(payload_tools_dir, script):
    """Runs `script` in a fresh interpreter that can see ONLY payload_tools_dir --
    not this process's own sys.path -- and returns (returncode, stdout, stderr).
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = payload_tools_dir
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, env=env, timeout=60)


def check_web_payload_self_contained():
    import build_web_payload

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "web-payload.zip")
        build_web_payload.build(zip_path)
        check("payload zip was written", os.path.isfile(zip_path))

        extracted = os.path.join(tmp, "extracted")
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            zf.extractall(extracted)

        # The whole point: server.py/toolchain.py/emulator.py/updater.py are excluded
        # by design (real-process concerns), but nothing shipped may actually need them.
        for excluded in ("editor/server.py", "editor/toolchain.py",
                         "editor/emulator.py", "editor/updater.py"):
            check(f"{excluded} is not in the payload",
                 not any(n.endswith(excluded) for n in names))
        for required in ("editor/session.py", "editor/webruntime.py",
                         "editor/routes/project.py", "src/pnx/pnx.h"):
            check(f"{required} is in the payload",
                 any(n.endswith(required) for n in names))

        payload_tools = os.path.join(extracted, "tools")

        r = _run_in_payload(payload_tools, "import editor.webruntime")
        check("editor.webruntime imports cleanly with ONLY the payload on sys.path",
             r.returncode == 0)
        if r.returncode != 0:
            print("    " + r.stderr.strip().replace("\n", "\n    "))

        # A full round trip: open a real project, edit through it, confirm the
        # excluded (companion-only) routes are simply absent rather than half-wired.
        example = os.path.join(ROOT, "examples", "quickstart")
        script = f"""
import json
import editor.webruntime as wr
from editor import routes

assert not any('/api/sdk' in k for k in routes.GET_EXACT), 'sdk routes leaked into the online table'
assert not any(p.startswith('/api/device') or p.startswith('/api/emulator')
               for p in routes.POST_EXACT), 'device/emulator routes leaked into the online table'

r = wr.dispatch('GET', '/api/state')
assert r['status'] == 200 and json.loads(r['body']) == {{'no_project': True}}, r

r = wr.dispatch('POST', '/api/project/open', json.dumps({{'path': {example!r}}}).encode())
d = json.loads(r['body'])
assert d['ok'] is True, d

r = wr.dispatch('GET', '/api/state')
d = json.loads(r['body'])
assert d['name'] == 'quickstart', d
# Not asserting d['built'] here: a fresh checkout hasn't been built yet, so this would
# only pass by accident of whatever local state examples/quickstart happens to carry
# (it's a real bug this test shipped with once already -- CI caught it, a dirty local
# checkout didn't). The build itself is exercised properly just below.

r = wr.dispatch('GET', '/api/sheets')
assert r['status'] == 200, r

r = wr.dispatch('POST', '/api/build', b'{{}}')
assert json.loads(r['body'])['ok'] is True, r['body']

r = wr.dispatch('GET', '/api/state')
d = json.loads(r['body'])
assert d['built'] is True, d  # now genuinely true, having just built it above

r = wr.dispatch('GET', '/api/device/status?platform=aplite')
assert r['status'] == 404, r  # not in the online table at all -- graceful, not a crash

print('ROUND_TRIP_OK')
"""
        r = _run_in_payload(payload_tools, script)
        check("full dispatch round trip against a real project succeeds",
             r.returncode == 0 and "ROUND_TRIP_OK" in r.stdout)
        if r.returncode != 0:
            print("    " + r.stderr.strip().replace("\n", "\n    "))


def main():
    check_web_payload_self_contained()
    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
