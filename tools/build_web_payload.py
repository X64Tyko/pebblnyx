"""Builds `web-payload.zip`: the slice of the editor that runs inside Pyodide.

Mirrors real relative paths (tools/..., src/pnx/...) rather than flattening anything,
because `pnx_project.framework_dir()` finds the engine sources by walking up from its
own file's location -- the payload has to reproduce that layout for the unmodified
Python to find it the same way it does on a real checkout.

Deliberately excludes tools/editor/server.py, toolchain.py, emulator.py and updater.py:
none of them are imported by anything this payload ships (see editor/routes/__init__.py's
per-domain try/except and editor/webruntime.py, neither of which reaches those modules),
so leaving them out is inert, not load-bearing -- if that ever stops being true, building
and smoke-testing this payload (tests/test_web_payload.py) will fail loudly, not silently
in someone's browser.

Usage: python3 tools/build_web_payload.py [output_path]  (default: web-payload.zip)
"""

import os
import sys
import zipfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)

FLAT_FILES = [
    "tools/pnx_project.py",
    "tools/pnx_assets.py",
    "tools/pnx_huffman_codec.py",  # pnx_assets.py's own compress="huffman" dependency
    "tools/pnx_mapfile.py",
    "tools/pnx_preview.py",
    # project/build.py imports this at module scope for app_size() -- it's pure stdlib
    # (no C extension, confirmed in the design research), and app_size() already
    # degrades gracefully when arm-none-eabi-{nm,readelf} aren't on PATH (they never
    # are, online), so shipping it costs nothing and isn't a toolchain dependency by
    # itself -- only the ARM binaries it would shell out to are actually absent.
    "tools/size_report.py",
    "tools/editor/__init__.py",
    "tools/editor/webruntime.py",
    "tools/editor/session.py",
    "tools/editor/liveness.py",
    "tools/editor/static/index.html",
]

TREE_DIRS = [
    "tools/editor/routes",
    "tools/editor/project",
    "src/pnx",
]


def iter_payload_paths():
    for rel in FLAT_FILES:
        yield rel
    for tree in TREE_DIRS:
        base = os.path.join(ROOT, tree)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, name)
                yield os.path.relpath(full, ROOT).replace(os.sep, "/")


def build(output_path):
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in iter_payload_paths():
            zf.write(os.path.join(ROOT, rel), rel)
    print(f"wrote {output_path}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "web-payload.zip")
